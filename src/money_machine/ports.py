from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol

from money_machine.domain.schemas import (
    AccountSnapshot,
    BrokerOrderRequest,
    BrokerOrderResult,
    Candidate,
    ModelDecisionEnvelope,
    OptionQuote,
    UnderlyingSnapshot,
)


class Clock(Protocol):
    def now(self) -> datetime: ...


class MarketDataPort(Protocol):
    async def market_clock(self) -> dict[str, Any]: ...

    async def underlying_snapshot(self, symbol: str) -> UnderlyingSnapshot: ...

    async def option_chain(self, symbol: str) -> Sequence[OptionQuote]: ...


class BrokeragePort(Protocol):
    async def account(self) -> AccountSnapshot: ...

    async def orders(self, *, status: str = "open") -> Sequence[dict[str, Any]]: ...

    async def order_by_id(self, broker_order_id: str) -> dict[str, Any]: ...

    async def positions(self) -> Sequence[dict[str, Any]]: ...

    async def portfolio_history(self) -> dict[str, Any]: ...

    async def activities(self) -> Sequence[dict[str, Any]]: ...

    async def place_option_order(self, request: BrokerOrderRequest) -> BrokerOrderResult: ...

    async def cancel_order(self, broker_order_id: str) -> None: ...


class AlpacaPort(MarketDataPort, BrokeragePort, Protocol):
    """Combined market-data and brokerage surface used by one agent cycle."""


class ModelProvider(Protocol):
    async def decide(
        self,
        *,
        candidates: Sequence[Candidate],
        market_context: dict[str, Any],
        portfolio_context: dict[str, Any],
    ) -> ModelDecisionEnvelope: ...
