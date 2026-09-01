from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from money_machine.domain.candidates import MAX_QUOTE_AGE_SECONDS
from money_machine.domain.enums import Side
from money_machine.domain.schemas import OptionQuote
from money_machine.execution import ManagedStructure, close_request

DAILY_LOSS_ENVELOPE_TOLERANCE_PCT = Decimal("0.25")
DAILY_LOSS_ENVELOPE_MIN_TOLERANCE = Decimal("250")


@dataclass(frozen=True, slots=True)
class DailyLossMarkQuality:
    passed: bool
    reason: str


def loss_is_plausible(loss: Decimal, defined_loss_envelope: Decimal) -> bool:
    tolerance = max(
        DAILY_LOSS_ENVELOPE_MIN_TOLERANCE,
        defined_loss_envelope * DAILY_LOSS_ENVELOPE_TOLERANCE_PCT,
    )
    return loss <= defined_loss_envelope + tolerance


def validate_managed_book_marks(
    *,
    managed_structures: tuple[ManagedStructure, ...],
    positions: list[dict[str, object]],
    chains: dict[str, list[OptionQuote]],
    now: datetime,
) -> DailyLossMarkQuality:
    if not managed_structures:
        return DailyLossMarkQuality(
            not positions, "flat_book" if not positions else "unmanaged_positions"
        )

    expected: dict[str, Decimal] = {}
    for managed in managed_structures:
        for leg in managed.structure.legs:
            direction = Decimal("1") if leg.side is Side.BUY else Decimal("-1")
            expected[leg.symbol] = expected.get(leg.symbol, Decimal("0")) + (
                direction * managed.quantity * leg.ratio_qty
            )
    actual = {
        str(position.get("symbol")): Decimal(str(position.get("qty") or 0))
        for position in positions
        if position.get("symbol")
    }
    if actual != expected:
        return DailyLossMarkQuality(False, "position_inventory_mismatch")

    quote_map = {quote.symbol: quote for chain in chains.values() for quote in chain}
    observed_at = now.astimezone(UTC)
    for symbol in expected:
        quote = quote_map.get(symbol)
        if quote is None:
            return DailyLossMarkQuality(False, f"missing_quote:{symbol}")
        age = (observed_at - quote.observed_at.astimezone(UTC)).total_seconds()
        if age < 0 or age > MAX_QUOTE_AGE_SECONDS:
            return DailyLossMarkQuality(False, f"stale_quote:{symbol}")
        if quote.bid <= 0 or quote.ask <= quote.bid:
            return DailyLossMarkQuality(False, f"invalid_bbo:{symbol}")
        if quote.bid_size is not None and quote.bid_size <= 0:
            return DailyLossMarkQuality(False, f"invalid_bid_depth:{symbol}")
        if quote.ask_size is not None and quote.ask_size <= 0:
            return DailyLossMarkQuality(False, f"invalid_ask_depth:{symbol}")

    for managed in managed_structures:
        try:
            request = close_request(
                managed,
                quotes=quote_map,
                client_order_id="daily-loss-mark-check",
                quantity=managed.quantity,
                environment_role="audit",
            )
        except ValueError:
            return DailyLossMarkQuality(False, "incomplete_executable_structure_quote")
        width = (managed.structure.maximum_loss + managed.structure.maximum_profit) / Decimal("100")
        if request.limit_price > width:
            return DailyLossMarkQuality(False, f"impossible_close_price:{managed.candidate_id}")
    return DailyLossMarkQuality(True, "complete_fresh_executable_quotes")
