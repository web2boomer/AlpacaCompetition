from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

from money_machine.domain.clock import FORCED_FLATTEN_STARTS_AT, NEW_YORK
from money_machine.domain.enums import PositionIntent, Side
from money_machine.domain.schemas import (
    BrokerOrderRequest,
    OptionLeg,
    OptionQuote,
    OptionStructure,
)

STALE_AFTER = timedelta(seconds=90)
SOFT_STALE_AFTER = timedelta(minutes=15)
MAX_REPRICE_ATTEMPTS = 2
REPRICE_INCREMENT = Decimal("0.05")
MAX_TOTAL_CONCESSION = Decimal("0.10")
CREDIT_TAKE_PROFIT_FRACTION = Decimal("0.50")
CREDIT_STOP_LOSS_MULTIPLE = Decimal("2.00")
DEBIT_TAKE_PROFIT_MULTIPLE = Decimal("1.50")
DEBIT_STOP_VALUE_FRACTION = Decimal("0.65")


@dataclass(frozen=True, slots=True)
class OrderLifecycleAction:
    action: str
    next_limit: Decimal | None
    reason: str


@dataclass(frozen=True, slots=True)
class ManagedOrder:
    agent_run_id: str
    candidate_id: str
    client_order_id: str
    broker_order_id: str
    status: str
    quantity: int
    remaining_quantity: int
    original_limit: Decimal
    attempt: int
    submitted_at: datetime
    is_credit: bool
    is_closing: bool
    exit_reason: str | None
    exit_urgency: str | None
    legs: tuple[OptionLeg, ...]


@dataclass(frozen=True, slots=True)
class ManagedStructure:
    agent_run_id: str
    candidate_id: str
    client_order_id: str
    broker_order_id: str
    status: str
    quantity: int
    opened_at: datetime
    maximum_holding_minutes: int
    structure: OptionStructure


@dataclass(frozen=True, slots=True)
class StructureExitSignal:
    should_close: bool
    reason: str
    executable_price: Decimal | None
    urgency: str = "soft"


@dataclass(frozen=True, slots=True)
class EntryHoldingPolicy:
    accepted: bool
    effective_holding_minutes: int
    effective_deadline: datetime
    reason: str


DAILY_HARD_EXIT_TIME = time(15, 50)
MINIMUM_ENTRY_WINDOW = timedelta(minutes=30)


def daily_hard_exit_deadline(at: datetime) -> datetime:
    local = at.astimezone(NEW_YORK)
    return datetime.combine(local.date(), DAILY_HARD_EXIT_TIME, tzinfo=NEW_YORK).astimezone(UTC)


def entry_holding_policy(at: datetime, model_holding_minutes: int) -> EntryHoldingPolicy:
    now = at.astimezone(UTC)
    boundary = min(daily_hard_exit_deadline(now), FORCED_FLATTEN_STARTS_AT)
    available = max(timedelta(0), boundary - now)
    effective = min(timedelta(minutes=model_holding_minutes), available)
    accepted = model_holding_minutes > 0 and available >= MINIMUM_ENTRY_WINDOW
    reason = (
        "model_hold" if effective == timedelta(minutes=model_holding_minutes) else "daily_boundary"
    )
    if not accepted:
        reason = "insufficient_tradable_session_window"
    return EntryHoldingPolicy(
        accepted=accepted,
        effective_holding_minutes=max(0, int(effective.total_seconds() // 60)),
        effective_deadline=now + effective,
        reason=reason,
    )


def stale_order_action(
    *,
    submitted_at: datetime,
    now: datetime,
    attempt: int,
    original_limit: Decimal,
    is_credit: bool,
    soft_close: bool = False,
    quote_materially_changed: bool = True,
) -> OrderLifecycleAction:
    threshold = SOFT_STALE_AFTER if soft_close else STALE_AFTER
    if now - submitted_at < threshold:
        return OrderLifecycleAction("wait", None, "order remains inside stale threshold")
    if soft_close and not quote_materially_changed:
        return OrderLifecycleAction("wait", None, "soft exit quote has not materially changed")
    if attempt >= MAX_REPRICE_ATTEMPTS:
        if soft_close:
            return OrderLifecycleAction(
                "wait",
                None,
                "soft exit retry budget exhausted; hard boundary remains authoritative",
            )
        return OrderLifecycleAction("cancel", None, "bounded repricing budget exhausted")
    concession = min(REPRICE_INCREMENT * (attempt + 1), MAX_TOTAL_CONCESSION)
    next_limit = original_limit - concession if is_credit else original_limit + concession
    if next_limit <= 0:
        return OrderLifecycleAction("cancel", None, "repricing would create invalid limit")
    return OrderLifecycleAction("cancel_and_replace", next_limit, "deterministic concession")


def replacement_request(
    order: ManagedOrder,
    *,
    client_order_id: str,
    next_limit: Decimal,
    environment_role: str,
) -> BrokerOrderRequest:
    return BrokerOrderRequest(
        client_order_id=client_order_id,
        candidate_id=order.candidate_id,
        quantity=order.remaining_quantity,
        limit_price=next_limit,
        is_credit=order.is_credit,
        legs=order.legs,
        environment_role=environment_role,
        attempt=order.attempt + 1,
        is_closing=order.is_closing,
        exit_reason=order.exit_reason,
        exit_urgency=order.exit_urgency,
    )


def closing_quote_materially_changed(order: ManagedOrder, quotes: dict[str, OptionQuote]) -> bool:
    cash = Decimal("0")
    for leg in order.legs:
        quote = quotes.get(leg.symbol)
        if quote is None:
            return False
        cash += quote.bid * leg.ratio_qty if leg.side is Side.SELL else -(quote.ask * leg.ratio_qty)
    return abs(abs(cash) - order.original_limit) >= REPRICE_INCREMENT


def structure_exit_signal(
    managed: ManagedStructure,
    *,
    quotes: dict[str, OptionQuote],
    now: datetime,
    force_close_reason: str | None = None,
) -> StructureExitSignal:
    """Evaluate deterministic profit, loss, holding-time, and portfolio exits."""
    if force_close_reason is not None:
        return StructureExitSignal(True, force_close_reason, None, "urgent")
    if now.astimezone(UTC) >= min(daily_hard_exit_deadline(now), FORCED_FLATTEN_STARTS_AT):
        return StructureExitSignal(True, "daily_hard_exit_boundary", None, "urgent")
    if managed.maximum_holding_minutes > 0 and now >= managed.opened_at + timedelta(
        minutes=managed.maximum_holding_minutes
    ):
        return StructureExitSignal(True, "maximum_holding_time", None, "soft")

    cash_required = _closing_cash_required(managed, quotes)
    if cash_required is None:
        return StructureExitSignal(False, "incomplete_close_quotes", None)
    opening_price = managed.structure.net_price
    if managed.structure.is_credit:
        close_debit = max(Decimal("0"), cash_required)
        if close_debit <= opening_price * CREDIT_TAKE_PROFIT_FRACTION:
            return StructureExitSignal(True, "credit_take_profit", close_debit, "soft")
        if close_debit >= opening_price * CREDIT_STOP_LOSS_MULTIPLE:
            return StructureExitSignal(True, "credit_stop_loss", close_debit, "urgent")
        return StructureExitSignal(False, "credit_exit_not_reached", close_debit)

    close_credit = max(Decimal("0"), -cash_required)
    if close_credit >= opening_price * DEBIT_TAKE_PROFIT_MULTIPLE:
        return StructureExitSignal(True, "debit_take_profit", close_credit, "soft")
    if close_credit <= opening_price * DEBIT_STOP_VALUE_FRACTION:
        return StructureExitSignal(True, "debit_stop_loss", close_credit, "urgent")
    return StructureExitSignal(False, "debit_exit_not_reached", close_credit)


def _closing_cash_required(
    managed: ManagedStructure, quotes: dict[str, OptionQuote]
) -> Decimal | None:
    cash_required = Decimal("0")
    for opening_leg in managed.structure.legs:
        quote = quotes.get(opening_leg.symbol)
        if quote is None:
            return None
        if opening_leg.side is Side.SELL:
            cash_required += quote.ask * opening_leg.ratio_qty
        else:
            cash_required -= quote.bid * opening_leg.ratio_qty
    return cash_required


def close_request(
    managed: ManagedStructure,
    *,
    quotes: dict[str, OptionQuote],
    client_order_id: str,
    quantity: int,
    environment_role: str,
    exit_reason: str = "explicit_close",
    exit_urgency: str = "urgent",
) -> BrokerOrderRequest:
    close_legs: list[OptionLeg] = []
    cash_required = _closing_cash_required(managed, quotes)
    if cash_required is None:
        raise ValueError("cannot close structure without a current quote for every leg")
    for opening_leg in managed.structure.legs:
        quote = quotes.get(opening_leg.symbol)
        if quote is None:  # narrowed by _closing_cash_required
            raise AssertionError("missing quote after complete close-price calculation")
        if opening_leg.side is Side.SELL:
            side = Side.BUY
            intent = PositionIntent.BUY_TO_CLOSE
        else:
            side = Side.SELL
            intent = PositionIntent.SELL_TO_CLOSE
        close_legs.append(
            opening_leg.model_copy(
                update={
                    "side": side,
                    "position_intent": intent,
                    "bid": quote.bid,
                    "ask": quote.ask,
                    "volume": quote.volume,
                    "open_interest": quote.open_interest,
                }
            )
        )
    if cash_required == 0:
        raise ValueError("close quote has no executable net price")
    return BrokerOrderRequest(
        client_order_id=client_order_id,
        candidate_id=f"{managed.candidate_id}:close",
        quantity=quantity,
        limit_price=abs(cash_required),
        is_credit=cash_required < 0,
        legs=tuple(close_legs),
        environment_role=environment_role,
        is_closing=True,
        exit_reason=exit_reason,
        exit_urgency=exit_urgency,
    )
