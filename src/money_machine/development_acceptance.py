import asyncio
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_UP, Decimal
from time import monotonic
from typing import Any

from money_machine.domain.enums import (
    AccountRole,
    Action,
    AppEnvironment,
    OptionRight,
    PositionIntent,
    RunMode,
    Side,
)
from money_machine.domain.schemas import (
    BrokerOrderRequest,
    BrokerOrderResult,
    OptionLeg,
    OptionQuote,
    OptionStructure,
)
from money_machine.execution import ManagedStructure, close_request
from money_machine.persistence.repository import AuditRepository, deterministic_client_order_id
from money_machine.ports import AlpacaPort
from money_machine.safety import verify_account_identity
from money_machine.settings import Settings

MAXIMUM_SMOKE_RISK = Decimal("250.00")
MAXIMUM_LEG_SPREAD = Decimal("0.25")
ORDER_TIMEOUT_SECONDS = 30.0
TERMINAL_FAILURES = {"canceled", "expired", "rejected", "replaced", "stopped", "suspended"}


@dataclass(frozen=True, slots=True)
class DevelopmentRoundTripReport:
    passed: bool
    opened: bool
    closed: bool
    returned_flat: bool
    symbol: str
    maximum_risk: str
    open_status: str
    close_status: str
    run_id: str

    def safe_dict(self) -> dict[str, Any]:
        return asdict(self)


async def run_development_round_trip(
    settings: Settings,
    repository: AuditRepository,
    adapter: AlpacaPort,
    *,
    now: datetime | None = None,
) -> DevelopmentRoundTripReport:
    if (
        settings.app_env is not AppEnvironment.DEVELOPMENT
        or settings.account_role is not AccountRole.DEVELOPMENT
    ):
        raise ValueError("development round trip requires the development role")
    if not settings.alpaca_paper_trade:
        raise ValueError("development round trip requires Alpaca paper trading")

    observed_at = (now or datetime.now(UTC)).astimezone(UTC)
    account, clock, open_orders, positions = await asyncio.gather(
        adapter.account(),
        adapter.market_clock(),
        adapter.orders(status="open"),
        adapter.positions(),
    )
    verify_account_identity(settings, account)
    if not bool(clock.get("is_open", False)):
        raise ValueError("development round trip requires an open market")
    if open_orders or positions:
        raise ValueError("development round trip requires a flat account with no open orders")

    snapshot, chain = await asyncio.gather(
        adapter.underlying_snapshot("SPY"), adapter.option_chain("SPY")
    )
    structure = _select_smoke_spread(snapshot.spot, list(chain), observed_at)
    cycle_key = f"development-roundtrip:{observed_at.isoformat()}"
    run_id, created = repository.begin_run(cycle_key, RunMode.LIVE, observed_at)
    if not created:
        raise ValueError("development round trip cycle already exists")

    candidate_id = _structure_id(structure)
    open_client_id = deterministic_client_order_id(
        settings.client_order_prefix,
        cycle_key=cycle_key,
        candidate_id=candidate_id,
        quantity=1,
    )
    opening = BrokerOrderRequest(
        client_order_id=open_client_id,
        candidate_id=candidate_id,
        quantity=1,
        limit_price=structure.net_price,
        is_credit=False,
        legs=structure.legs,
        environment_role=settings.account_role.value,
    )

    open_result: BrokerOrderResult | None = None
    close_result: BrokerOrderResult | None = None
    open_status = "not_submitted"
    close_status = "not_submitted"
    try:
        open_result = await adapter.place_option_order(opening)
        repository.persist_order(run_id, opening, open_result)
        open_status = await _wait_for_fill(adapter, open_result)
        if open_status != "filled":
            raise RuntimeError(f"development opening order ended {open_status}")

        managed = ManagedStructure(
            agent_run_id=run_id,
            candidate_id=candidate_id,
            client_order_id=open_client_id,
            broker_order_id=open_result.broker_order_id,
            status="filled",
            quantity=1,
            structure=structure,
        )
        close_result, close_status = await _close_with_retries(
            settings=settings,
            repository=repository,
            adapter=adapter,
            managed=managed,
            run_id=run_id,
            cycle_key=cycle_key,
        )
        if close_status != "filled":
            raise RuntimeError(f"development closing order ended {close_status}")

        returned_flat = await _wait_until_flat(adapter, structure)
        if not returned_flat:
            raise RuntimeError("development round trip did not return the account flat")
        repository.persist_fills(list(await adapter.activities()))
        passport = _passport(
            run_id=run_id,
            observed_at=observed_at,
            structure=structure,
            opening=opening,
            open_result=open_result,
            closing=close_result,
        )
        repository.complete_run(run_id, completed_at=datetime.now(UTC), passport=passport)
        return DevelopmentRoundTripReport(
            passed=True,
            opened=True,
            closed=True,
            returned_flat=True,
            symbol=structure.underlying,
            maximum_risk=str(structure.maximum_loss),
            open_status=open_status,
            close_status=close_status,
            run_id=run_id,
        )
    except Exception as exc:
        residual = await _positions_for_structure(adapter, structure)
        if residual:
            repository.set_kill_switch(active=True, now=datetime.now(UTC))
        passport = {
            "run_id": run_id,
            "mode": "development_round_trip",
            "official": False,
            "status": "failed_closed",
            "incident": {"type": type(exc).__name__},
            "execution": {
                "open_status": open_status,
                "close_status": close_status,
                "returned_flat": not residual,
            },
        }
        repository.complete_run(
            run_id,
            completed_at=datetime.now(UTC),
            passport=passport,
            incident=type(exc).__name__,
        )
        raise


def _select_smoke_spread(spot: Decimal, chain: list[OptionQuote], now: datetime) -> OptionStructure:
    minimum_expiration = (now + timedelta(days=3)).date()
    calls = [
        quote
        for quote in chain
        if quote.right is OptionRight.CALL
        and quote.expiration.date() >= minimum_expiration
        and quote.bid > 0
        and quote.ask > quote.bid
        and quote.spread <= MAXIMUM_LEG_SPREAD
    ]
    pairs: list[tuple[tuple[Any, ...], OptionQuote, OptionQuote, Decimal]] = []
    for long_quote in calls:
        for short_quote in calls:
            width = short_quote.strike - long_quote.strike
            if (
                short_quote.expiration != long_quote.expiration
                or width <= 0
                or width > Decimal("5")
            ):
                continue
            debit = (long_quote.ask - short_quote.bid).quantize(Decimal("0.01"), rounding=ROUND_UP)
            if debit <= 0 or debit >= width or debit * 100 > MAXIMUM_SMOKE_RISK:
                continue
            rank = (
                long_quote.expiration,
                abs(long_quote.strike - spot),
                long_quote.spread + short_quote.spread,
                width,
            )
            pairs.append((rank, long_quote, short_quote, debit))
    if not pairs:
        raise ValueError("no bounded, liquid SPY smoke-test spread is available")
    _, long_quote, short_quote, debit = min(pairs, key=lambda pair: pair[0])
    width = short_quote.strike - long_quote.strike
    legs = (
        _leg(long_quote, Side.BUY, PositionIntent.BUY_TO_OPEN),
        _leg(short_quote, Side.SELL, PositionIntent.SELL_TO_OPEN),
    )
    return OptionStructure(
        strategy=Action.CALL_DEBIT_SPREAD,
        underlying="SPY",
        expiration=long_quote.expiration,
        legs=legs,
        net_price=debit,
        maximum_loss=debit * 100,
        maximum_profit=(width - debit) * 100,
        is_credit=False,
    )


def _leg(quote: OptionQuote, side: Side, intent: PositionIntent) -> OptionLeg:
    return OptionLeg(
        symbol=quote.symbol,
        underlying=quote.underlying,
        expiration=quote.expiration,
        right=quote.right,
        strike=quote.strike,
        side=side,
        position_intent=intent,
        bid=quote.bid,
        ask=quote.ask,
        volume=quote.volume,
        open_interest=quote.open_interest,
    )


async def _wait_for_fill(
    adapter: AlpacaPort,
    result: BrokerOrderResult,
    *,
    timeout_seconds: float = ORDER_TIMEOUT_SECONDS,
) -> str:
    status = result.status.lower()
    if status == "filled" or status in TERMINAL_FAILURES:
        return status
    deadline = monotonic() + timeout_seconds
    while monotonic() < deadline:
        await asyncio.sleep(1)
        order = await adapter.order_by_id(result.broker_order_id)
        status = str(order.get("status") or status).lower()
        if status == "filled" or status in TERMINAL_FAILURES:
            return status
    await adapter.cancel_order(result.broker_order_id)
    return "canceled_after_timeout"


async def _close_with_retries(
    *,
    settings: Settings,
    repository: AuditRepository,
    adapter: AlpacaPort,
    managed: ManagedStructure,
    run_id: str,
    cycle_key: str,
) -> tuple[BrokerOrderResult, str]:
    last_result: BrokerOrderResult | None = None
    last_status = "not_submitted"
    for attempt in range(3):
        chain = list(await adapter.option_chain(managed.structure.underlying))
        quote_map = {quote.symbol: quote for quote in chain}
        client_order_id = deterministic_client_order_id(
            settings.client_order_prefix,
            cycle_key=cycle_key,
            candidate_id=f"{managed.candidate_id}:close",
            quantity=1,
            attempt=attempt,
        )
        request = close_request(
            managed,
            quotes=quote_map,
            client_order_id=client_order_id,
            quantity=1,
            environment_role=settings.account_role.value,
        )
        if attempt:
            concession = Decimal("0.05") * attempt
            next_limit = (
                max(Decimal("0.01"), request.limit_price - concession)
                if request.is_credit
                else request.limit_price + concession
            )
            request = request.model_copy(update={"limit_price": next_limit, "attempt": attempt})
        last_result = await adapter.place_option_order(request)
        repository.persist_order(run_id, request, last_result)
        last_status = await _wait_for_fill(adapter, last_result)
        if last_status == "filled":
            return last_result, last_status
        residual = await _positions_for_structure(adapter, managed.structure)
        if not residual:
            return last_result, "filled"
        if len(residual) != len(managed.structure.legs):
            break
    if last_result is None:
        raise RuntimeError("development close was not submitted")
    return last_result, last_status


async def _wait_until_flat(adapter: AlpacaPort, structure: OptionStructure) -> bool:
    deadline = monotonic() + 15
    while monotonic() < deadline:
        if not await _positions_for_structure(adapter, structure):
            return True
        await asyncio.sleep(1)
    return False


async def _positions_for_structure(
    adapter: AlpacaPort, structure: OptionStructure
) -> list[dict[str, Any]]:
    symbols = {leg.symbol for leg in structure.legs}
    return [
        position
        for position in await adapter.positions()
        if str(position.get("symbol") or "") in symbols
        and Decimal(str(position.get("qty") or 0)) != 0
    ]


def _structure_id(structure: OptionStructure) -> str:
    legs = "-".join(leg.symbol for leg in structure.legs)
    return f"development-smoke:{legs}"


def _passport(
    *,
    run_id: str,
    observed_at: datetime,
    structure: OptionStructure,
    opening: BrokerOrderRequest,
    open_result: BrokerOrderResult,
    closing: BrokerOrderResult,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "observed_at": observed_at.isoformat(),
        "mode": "development_round_trip",
        "official": False,
        "result_label": "DEVELOPMENT PAPER EXECUTION TEST — NOT COMPETITION P&L",
        "structure": structure.model_dump(mode="json"),
        "risk": {"maximum_loss": str(structure.maximum_loss), "quantity": 1},
        "execution": {
            "submitted": True,
            "entry_submitted": True,
            "open_client_order_id": opening.client_order_id,
            "open_broker_order_id": open_result.broker_order_id,
            "open_status": "filled",
            "close_broker_order_id": closing.broker_order_id,
            "close_status": "filled",
            "returned_flat": True,
        },
    }
