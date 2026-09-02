import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any, Protocol

from money_machine.adapters.alpaca_mcp import AlpacaMcpV2Adapter
from money_machine.domain.enums import AccountRole, AppEnvironment, RunMode
from money_machine.domain.schemas import AccountSnapshot, BrokerOrderResult
from money_machine.persistence.repository import AuditRepository
from money_machine.safety import verify_account_identity
from money_machine.settings import Settings

EXPECTED_FINGERPRINT = "2e10efeeb330"
TERMINAL_STATUSES = {"filled", "canceled", "expired", "rejected"}
MAX_ADVERSE_LIMIT_OFFSET = Decimal("0.25")


@dataclass(frozen=True, slots=True)
class AssignmentSpec:
    quantity: Decimal
    parent_client_order_id: str
    parent_broker_order_id: str
    assigned_leg: str

    def lineage(self) -> dict[str, str]:
        return {
            "parent_client_order_id": self.parent_client_order_id,
            "parent_broker_order_id": self.parent_broker_order_id,
            "assigned_leg": self.assigned_leg,
        }


RECOVERY_POSITIONS = {
    "IWM": AssignmentSpec(
        quantity=Decimal("200"),
        parent_client_order_id="mm-comp-df1310bd88a039b9550ec4d9",
        parent_broker_order_id="d48ae638-7bfc-476a-a512-940a522d3f1d",
        assigned_leg="IWM260901P00291000",
    ),
    "QQQ": AssignmentSpec(
        quantity=Decimal("27"),
        parent_client_order_id="mm-comp-81df6bbd24663e7b199558e9",
        parent_broker_order_id="d2f9ef1f-b8d3-4ab6-a0a4-05ea86e98b33",
        assigned_leg="QQQ260901P00708000",
    ),
}


class RecoveryAdapter(Protocol):
    async def account(self) -> AccountSnapshot: ...

    async def market_clock(self) -> dict[str, Any]: ...

    async def orders(self, *, status: str = "open") -> list[dict[str, Any]]: ...

    async def positions(self) -> list[dict[str, Any]]: ...

    async def stock_snapshots(
        self, symbols: list[str], *, feed: str | None = None
    ) -> dict[str, Any]: ...

    async def place_stock_order(
        self,
        *,
        symbol: str,
        quantity: int,
        limit_price: Decimal,
        client_order_id: str,
    ) -> BrokerOrderResult: ...

    async def order_by_id(self, broker_order_id: str) -> dict[str, Any]: ...


async def guarded_assignment_recovery(
    settings: Settings,
    repository: AuditRepository,
    *,
    adapter: RecoveryAdapter | None = None,
) -> dict[str, Any]:
    _verify_configuration(settings)
    settings.assert_live_credentials_present()
    if adapter is None:
        async with AlpacaMcpV2Adapter(settings) as connected:
            return await _recover(settings, repository, connected)
    return await _recover(settings, repository, adapter)


async def _recover(
    settings: Settings,
    repository: AuditRepository,
    adapter: RecoveryAdapter,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    account = await adapter.account()
    identity = verify_account_identity(settings, account)
    if identity.account_fingerprint != EXPECTED_FINGERPRINT:
        raise RuntimeError("official competition account fingerprint mismatch")
    clock, orders, positions = await asyncio.gather(
        adapter.market_clock(), adapter.orders(status="open"), adapter.positions()
    )
    if not bool(clock.get("is_open")):
        raise RuntimeError("regular market session is not open")
    if orders:
        raise RuntimeError("assignment recovery requires zero broker working orders")
    _verify_positions(positions)

    snapshots = await adapter.stock_snapshots(sorted(RECOVERY_POSITIONS))
    limits = {
        symbol: _bounded_sell_limit(_fresh_bid(snapshots, symbol), symbol)
        for symbol in RECOVERY_POSITIONS
    }
    run_id, created = repository.begin_run(
        "assignment-recovery-residual-1:2026-09-02", RunMode.LIVE, now
    )
    if not created:
        raise RuntimeError("assignment recovery was already attempted")

    receipts: list[dict[str, Any]] = []
    for symbol in ("QQQ", "IWM"):
        expected = RECOVERY_POSITIONS[symbol]
        quantity = int(expected.quantity)
        client_order_id = f"mm-comp-ar2-20260902-{symbol.lower()}"
        repository.persist_assignment_recovery_intent(
            run_id,
            symbol=symbol,
            quantity=quantity,
            client_order_id=client_order_id,
            limit_price=limits[symbol],
            lineage=expected.lineage(),
            observed_at=now,
        )
        result = await adapter.place_stock_order(
            symbol=symbol,
            quantity=quantity,
            limit_price=limits[symbol],
            client_order_id=client_order_id,
        )
        if not result.broker_order_id or result.client_order_id != client_order_id:
            raise RuntimeError("broker returned ambiguous assignment recovery identity")
        repository.update_assignment_recovery_order(
            client_order_id,
            broker_order_id=result.broker_order_id,
            status=result.status,
            observed_at=result.submitted_at,
            broker_payload=result.raw,
        )
        terminal = await _terminal_order(adapter, result.broker_order_id)
        status = str(terminal.get("status") or "").lower()
        filled_qty = Decimal(str(terminal.get("filled_qty") or 0))
        repository.update_assignment_recovery_order(
            client_order_id,
            broker_order_id=result.broker_order_id,
            status=status,
            observed_at=datetime.now(UTC),
            broker_payload=_redacted_terminal(terminal),
        )
        live_orders, live_positions = await asyncio.gather(
            adapter.orders(status="open"), adapter.positions()
        )
        if live_orders:
            raise RuntimeError(f"{symbol} terminalized with a broker working order")
        receipts.append(
            {
                "symbol": symbol,
                "quantity": quantity,
                "broker_order_id": result.broker_order_id,
                "status": status,
                "filled_quantity": str(filled_qty),
                "filled_average_price": str(terminal.get("filled_avg_price") or ""),
                "limit_price": str(limits[symbol]),
            }
        )
        if status != "filled" or filled_qty != expected.quantity:
            residual = _position_quantities(live_positions)
            raise RuntimeError(
                f"{symbol} assignment recovery did not fill exactly; residual={residual}"
            )
        _verify_residual_positions(symbol, live_positions)

    final_account, final_orders, final_positions = await asyncio.gather(
        adapter.account(), adapter.orders(status="open"), adapter.positions()
    )
    final_identity = verify_account_identity(settings, final_account)
    if final_identity.account_fingerprint != EXPECTED_FINGERPRINT:
        raise RuntimeError("post-recovery account fingerprint mismatch")
    if final_orders or final_positions:
        raise RuntimeError("post-recovery broker inventory is not flat")
    receipt = {
        "status": "assignment_recovery_filled",
        "account_fingerprint": final_identity.account_fingerprint,
        "orders": receipts,
        "open_positions": 0,
        "working_orders": 0,
        "equity": str(final_account.equity),
        "cash": str(final_account.cash),
        "inventory_defined_loss": "0",
        "incident_cleared_by_command": False,
        "observed_at": datetime.now(UTC).isoformat(),
    }
    repository.complete_run(run_id, completed_at=datetime.now(UTC), passport=receipt)
    return receipt


def _verify_configuration(settings: Settings) -> None:
    if (
        settings.app_env is not AppEnvironment.PRODUCTION
        or settings.account_role is not AccountRole.COMPETITION
        or settings.run_mode is not RunMode.LIVE
    ):
        raise RuntimeError("assignment recovery requires production competition live mode")


def _verify_positions(positions: list[dict[str, Any]]) -> None:
    actual = _position_quantities(positions)
    expected = {symbol: values.quantity for symbol, values in RECOVERY_POSITIONS.items()}
    if actual != expected:
        raise RuntimeError("broker positions differ from the exact authorized assignments")


def _position_quantities(positions: list[dict[str, Any]]) -> dict[str, Decimal]:
    actual: dict[str, Decimal] = {}
    for position in positions:
        symbol = str(position.get("symbol") or "")
        quantity = Decimal(str(position.get("qty") or 0))
        side = str(position.get("side") or "").lower()
        asset_class = str(position.get("asset_class") or "").lower()
        if not symbol or side != "long" or asset_class not in {"us_equity", "stock"}:
            raise RuntimeError("broker position is not an expected long stock assignment")
        actual[symbol] = quantity
    return actual


def _verify_residual_positions(filled_symbol: str, positions: list[dict[str, Any]]) -> None:
    if filled_symbol == "QQQ":
        expected = {"IWM": RECOVERY_POSITIONS["IWM"].quantity}
    elif filled_symbol == "IWM":
        expected = {}
    else:
        raise RuntimeError("unauthorized assignment recovery symbol")
    if _position_quantities(positions) != expected:
        raise RuntimeError(f"unexpected broker residual after {filled_symbol} recovery")


def _fresh_bid(payload: dict[str, Any], symbol: str) -> Decimal:
    snapshots = payload.get("snapshots") if isinstance(payload.get("snapshots"), dict) else payload
    raw = snapshots.get(symbol) if isinstance(snapshots, dict) else None
    if not isinstance(raw, dict):
        raise RuntimeError(f"missing fresh {symbol} stock snapshot")
    quote = raw.get("latestQuote") or raw.get("latest_quote")
    if not isinstance(quote, dict):
        raise RuntimeError(f"missing fresh {symbol} quote")
    bid = Decimal(str(quote.get("bp") or quote.get("bid_price") or 0))
    timestamp = quote.get("t") or quote.get("timestamp")
    if not timestamp:
        raise RuntimeError(f"missing {symbol} quote timestamp")
    observed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00")).astimezone(UTC)
    if (datetime.now(UTC) - observed).total_seconds() > 15:
        raise RuntimeError(f"stale {symbol} quote")
    if bid <= Decimal("0.10"):
        raise RuntimeError(f"invalid {symbol} bid")
    return bid


def _bounded_sell_limit(bid: Decimal, symbol: str) -> Decimal:
    if symbol not in RECOVERY_POSITIONS:
        raise RuntimeError("unauthorized assignment recovery symbol")
    return (bid - MAX_ADVERSE_LIMIT_OFFSET).quantize(Decimal("0.01"), rounding=ROUND_DOWN)


async def _terminal_order(adapter: RecoveryAdapter, broker_order_id: str) -> dict[str, Any]:
    for _ in range(20):
        order = await adapter.order_by_id(broker_order_id)
        if str(order.get("status") or "").lower() in TERMINAL_STATUSES:
            return order
        await asyncio.sleep(0.5)
    raise RuntimeError("assignment recovery order did not reach a terminal status")


def _redacted_terminal(order: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "id",
        "client_order_id",
        "symbol",
        "side",
        "qty",
        "filled_qty",
        "filled_avg_price",
        "status",
        "type",
        "time_in_force",
        "limit_price",
        "submitted_at",
        "filled_at",
        "canceled_at",
        "expired_at",
    }
    return {key: value for key, value in order.items() if key in allowed}
