from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from money_machine.domain.enums import PositionIntent, Side
from money_machine.domain.schemas import (
    BrokerOrderRequest,
    OptionLeg,
    OptionQuote,
    OptionStructure,
)

STALE_AFTER = timedelta(seconds=90)
MAX_REPRICE_ATTEMPTS = 2
REPRICE_INCREMENT = Decimal("0.05")
MAX_TOTAL_CONCESSION = Decimal("0.10")


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
    original_limit: Decimal
    attempt: int
    submitted_at: datetime
    is_credit: bool
    is_closing: bool
    legs: tuple[OptionLeg, ...]


@dataclass(frozen=True, slots=True)
class ManagedStructure:
    agent_run_id: str
    candidate_id: str
    client_order_id: str
    broker_order_id: str
    status: str
    quantity: int
    structure: OptionStructure


def stale_order_action(
    *,
    submitted_at: datetime,
    now: datetime,
    attempt: int,
    original_limit: Decimal,
    is_credit: bool,
) -> OrderLifecycleAction:
    if now - submitted_at < STALE_AFTER:
        return OrderLifecycleAction("wait", None, "order remains inside stale threshold")
    if attempt >= MAX_REPRICE_ATTEMPTS:
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
        quantity=order.quantity,
        limit_price=next_limit,
        is_credit=order.is_credit,
        legs=order.legs,
        environment_role=environment_role,
        attempt=order.attempt + 1,
        is_closing=order.is_closing,
    )


def close_request(
    managed: ManagedStructure,
    *,
    quotes: dict[str, OptionQuote],
    client_order_id: str,
    quantity: int,
    environment_role: str,
) -> BrokerOrderRequest:
    close_legs: list[OptionLeg] = []
    cash_required = Decimal("0")
    for opening_leg in managed.structure.legs:
        quote = quotes.get(opening_leg.symbol)
        if quote is None:
            raise ValueError("cannot close structure without a current quote for every leg")
        if opening_leg.side is Side.SELL:
            side = Side.BUY
            intent = PositionIntent.BUY_TO_CLOSE
            cash_required += quote.ask * opening_leg.ratio_qty
        else:
            side = Side.SELL
            intent = PositionIntent.SELL_TO_CLOSE
            cash_required -= quote.bid * opening_leg.ratio_qty
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
    )
