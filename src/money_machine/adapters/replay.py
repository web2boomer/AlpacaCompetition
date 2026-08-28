import json
from datetime import UTC, datetime
from decimal import Decimal
from importlib.resources import files
from typing import Any

from money_machine.domain.schemas import (
    AccountSnapshot,
    BrokerOrderRequest,
    BrokerOrderResult,
    OptionQuote,
    UnderlyingSnapshot,
)


class ReplayAlpacaAdapter:
    def __init__(self, fixture_name: str = "canonical_cycle.json") -> None:
        fixture_path = files("money_machine.fixtures").joinpath(fixture_name)
        self.data = json.loads(fixture_path.read_text(encoding="utf-8"))
        self.submitted_requests: list[BrokerOrderRequest] = []
        self.submitted_orders: list[BrokerOrderResult] = []
        self.canceled_order_ids: list[str] = []

    @property
    def observed_at(self) -> datetime:
        return _time(self.data["observed_at"])

    async def account(self) -> AccountSnapshot:
        return AccountSnapshot.model_validate(self.data["account"])

    async def market_clock(self) -> dict[str, Any]:
        return dict(self.data["market_clock"])

    async def underlying_snapshot(self, symbol: str) -> UnderlyingSnapshot:
        raw = next(item for item in self.data["underlyings"] if item["symbol"] == symbol)
        return UnderlyingSnapshot.model_validate(raw)

    async def option_chain(self, symbol: str) -> list[OptionQuote]:
        return [OptionQuote.model_validate(item) for item in self.data["chains"][symbol]]

    async def orders(self, *, status: str = "open") -> list[dict[str, Any]]:
        del status
        return list(self.data.get("orders", []))

    async def positions(self) -> list[dict[str, Any]]:
        return list(self.data.get("positions", []))

    async def portfolio_history(self) -> dict[str, Any]:
        return dict(self.data["portfolio_history"])

    async def activities(self) -> list[dict[str, Any]]:
        return list(self.data.get("activities", []))

    async def place_option_order(self, request: BrokerOrderRequest) -> BrokerOrderResult:
        self.submitted_requests.append(request)
        digest = request.client_order_id[-12:]
        result = BrokerOrderResult(
            broker_order_id=f"replay-{digest}",
            client_order_id=request.client_order_id,
            status="filled",
            submitted_at=self.observed_at,
            raw={"source": "replay", "official": False},
        )
        self.submitted_orders.append(result)
        return result

    async def cancel_order(self, broker_order_id: str) -> None:
        self.canceled_order_ids.append(broker_order_id)


def infer_atm_implied_move(
    snapshot: UnderlyingSnapshot, chain: list[OptionQuote]
) -> UnderlyingSnapshot:
    by_strike: dict[Decimal, dict[str, OptionQuote]] = {}
    for quote in chain:
        bucket = by_strike.setdefault(quote.strike, {})
        bucket[quote.right.value] = quote
    complete = [
        (strike, pair) for strike, pair in by_strike.items() if "call" in pair and "put" in pair
    ]
    if not complete:
        return snapshot
    _, pair = min(complete, key=lambda item: abs(item[0] - snapshot.spot))
    implied = (pair["call"].midpoint + pair["put"].midpoint) / snapshot.spot
    return snapshot.model_copy(update={"implied_move_pct": implied})


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(UTC)
