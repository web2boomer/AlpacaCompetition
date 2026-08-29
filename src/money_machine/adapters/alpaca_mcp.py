import json
import os
import re
import sys
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from money_machine.domain.enums import AccountRole, OptionRight
from money_machine.domain.schemas import (
    AccountSnapshot,
    BrokerOrderRequest,
    BrokerOrderResult,
    OptionQuote,
    UnderlyingSnapshot,
)
from money_machine.safety import new_entry_authorized
from money_machine.settings import Settings

OCC_PATTERN = re.compile(r"^([A-Z.]+)(\d{6})([CP])(\d{8})$")


class AlpacaMcpError(RuntimeError):
    pass


class AlpacaMcpV2Adapter:
    """Explicit stdio adapter for Alpaca's official MCP Server V2."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._tools: set[str] = set()

    async def __aenter__(self) -> Self:
        self.settings.assert_live_credentials_present()
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        env = os.environ.copy()
        env.update(
            {
                "ALPACA_API_KEY": self.settings.alpaca_api_key.get_secret_value(),  # type: ignore[union-attr]
                "ALPACA_SECRET_KEY": self.settings.alpaca_secret_key.get_secret_value(),  # type: ignore[union-attr]
                "ALPACA_PAPER_TRADE": "true",
                "ALPACA_TOOLSETS": self.settings.alpaca_toolsets,
            }
        )
        command = self.settings.mcp_command
        if command == "alpaca-mcp-server":
            installed_script = Path(sys.executable).with_name(command)
            if installed_script.is_file():
                command = str(installed_script)
        params = StdioServerParameters(
            command=command,
            args=self.settings.mcp_arguments,
            env=env,
        )
        read_stream, write_stream = await self._stack.enter_async_context(stdio_client(params))
        self._session = await self._stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await self._session.initialize()
        tools = await self._session.list_tools()
        self._tools = {tool.name for tool in tools.tools}
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._stack is not None:
            await self._stack.__aexit__(exc_type, exc, traceback)
        self._session = None
        self._stack = None

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        if self._session is None:
            raise AlpacaMcpError("Alpaca MCP adapter is not connected")
        if name not in self._tools:
            raise AlpacaMcpError(f"required Alpaca MCP V2 tool is unavailable: {name}")
        result = await self._session.call_tool(name, arguments or {})
        if result.isError:
            raise AlpacaMcpError(f"Alpaca MCP tool failed: {name}")
        structured = getattr(result, "structuredContent", None)
        if structured:
            return _unwrap(structured)
        for block in result.content:
            text = getattr(block, "text", None)
            if text:
                try:
                    return _unwrap(json.loads(text))
                except json.JSONDecodeError:
                    return text
        return {}

    async def account(self) -> AccountSnapshot:
        payload = _as_dict(await self.call_tool("get_account_info"))
        return AccountSnapshot(
            account_id=str(payload.get("id") or payload.get("account_id") or ""),
            account_number=(
                str(payload["account_number"]) if payload.get("account_number") else None
            ),
            status=str(payload.get("status") or "UNKNOWN"),
            is_paper=self.settings.alpaca_paper_trade,
            equity=_decimal(payload.get("equity")),
            cash=_decimal(payload.get("cash")),
            buying_power=_decimal(payload.get("buying_power")),
            portfolio_value=_decimal(payload.get("portfolio_value") or payload.get("equity")),
            realized_pl=_decimal(payload.get("realized_pl")),
            unrealized_pl=_decimal(payload.get("unrealized_pl")),
        )

    async def market_clock(self) -> dict[str, Any]:
        return _as_dict(await self.call_tool("get_clock"))

    async def underlying_snapshot(self, symbol: str) -> UnderlyingSnapshot:
        payload = _as_dict(await self.call_tool("get_stock_snapshot", {"symbols": symbol}))
        snapshot = _symbol_payload(payload, symbol)
        latest = _as_dict(snapshot.get("latest_trade") or snapshot.get("latestTrade") or {})
        daily = _as_dict(snapshot.get("daily_bar") or snapshot.get("dailyBar") or {})
        previous = _as_dict(
            snapshot.get("previous_daily_bar")
            or snapshot.get("previousDailyBar")
            or snapshot.get("prevDailyBar")
            or {}
        )
        spot = _decimal(
            latest.get("price") or latest.get("p") or daily.get("close") or daily.get("c")
        )
        previous_close = _decimal(previous.get("close") or previous.get("c"))
        if spot <= 0 or previous_close <= 0:
            raise AlpacaMcpError("incomplete Alpaca underlying snapshot")
        absolute_daily_move = abs(spot / previous_close - Decimal("1"))
        observed = _parse_time(
            latest.get("timestamp") or latest.get("t") or daily.get("timestamp") or daily.get("t")
        )
        return UnderlyingSnapshot(
            symbol=symbol,
            spot=spot,
            previous_close=previous_close,
            realized_move_pct=max(absolute_daily_move, Decimal("0.004")),
            implied_move_pct=Decimal("0.001"),  # replaced from ATM straddle by the collector
            trend_return_pct=spot / previous_close - Decimal("1"),
            event_risk=False,
            observed_at=observed,
        )

    async def option_chain(self, symbol: str) -> list[OptionQuote]:
        payload = await self.call_tool(
            "get_option_chain",
            {
                "underlying_symbol": symbol,
                # The Alpaca endpoint defaults to 100 contracts, which is not a
                # complete chain for liquid index ETFs and can omit both wings.
                "limit": 1000,
            },
        )
        chain = _chain_payload(payload)
        quotes: list[OptionQuote] = []
        for option_symbol, raw_snapshot in chain.items():
            parsed = parse_occ_symbol(option_symbol)
            if parsed is None:
                continue
            underlying, expiration, right, strike = parsed
            raw = _as_dict(raw_snapshot)
            latest = _as_dict(raw.get("latest_quote") or raw.get("latestQuote") or {})
            greeks = _as_dict(raw.get("greeks") or {})
            try:
                quote = OptionQuote(
                    symbol=option_symbol,
                    underlying=underlying,
                    expiration=expiration,
                    right=right,
                    strike=strike,
                    bid=_decimal(latest.get("bid_price") or latest.get("bp")),
                    ask=_decimal(latest.get("ask_price") or latest.get("ap")),
                    volume=int(raw.get("volume") or 0),
                    open_interest=int(raw.get("open_interest") or raw.get("openInterest") or 0),
                    implied_volatility=_decimal(
                        raw.get("implied_volatility") or raw.get("impliedVolatility")
                    ),
                    delta=(
                        _decimal(greeks.get("delta")) if greeks.get("delta") is not None else None
                    ),
                    observed_at=_parse_time(
                        latest.get("timestamp") or latest.get("t") or datetime.now(UTC)
                    ),
                )
            except (TypeError, ValueError):
                continue
            quotes.append(quote)
        return quotes

    async def orders(self, *, status: str = "open") -> list[dict[str, Any]]:
        payload = await self.call_tool("get_orders", {"status": status})
        return _as_list_of_dicts(payload)

    async def order_by_id(self, broker_order_id: str) -> dict[str, Any]:
        return _as_dict(
            await self.call_tool(
                "get_order_by_id",
                {"order_id": broker_order_id, "nested": True},
            )
        )

    async def positions(self) -> list[dict[str, Any]]:
        return _as_list_of_dicts(await self.call_tool("get_all_positions"))

    async def portfolio_history(self) -> dict[str, Any]:
        return _as_dict(await self.call_tool("get_portfolio_history"))

    async def activities(self) -> list[dict[str, Any]]:
        payload = await self.call_tool(
            "get_account_activities",
            {"activity_types": ["FILL"], "page_size": 100},
        )
        return _as_list_of_dicts(payload)

    async def place_option_order(self, request: BrokerOrderRequest) -> BrokerOrderResult:
        if (
            self.settings.account_role is AccountRole.COMPETITION
            and not request.is_closing
            and not new_entry_authorized(self.settings.account_role)
        ):
            raise AlpacaMcpError("competition entry blocked by the competition clock")
        legs = [
            {
                "symbol": leg.symbol,
                "ratio_qty": str(leg.ratio_qty),
                "side": leg.side.value,
                "position_intent": leg.position_intent.value,
            }
            for leg in request.legs
        ]
        signed_limit = -request.limit_price if request.is_credit else request.limit_price
        payload = _as_dict(
            await self.call_tool(
                "place_option_order",
                {
                    "qty": str(request.quantity),
                    "type": "limit",
                    "time_in_force": "day",
                    "limit_price": str(signed_limit),
                    "client_order_id": request.client_order_id,
                    "order_class": "mleg",
                    "legs": legs,
                },
            )
        )
        return BrokerOrderResult(
            broker_order_id=str(payload.get("id") or ""),
            client_order_id=str(payload.get("client_order_id") or request.client_order_id),
            status=str(payload.get("status") or "submitted"),
            submitted_at=_parse_time(payload.get("submitted_at") or datetime.now(UTC)),
            raw=_redacted_order_payload(payload),
        )

    async def cancel_order(self, broker_order_id: str) -> None:
        await self.call_tool("cancel_order_by_id", {"order_id": broker_order_id})


def parse_occ_symbol(symbol: str) -> tuple[str, datetime, OptionRight, Decimal] | None:
    match = OCC_PATTERN.fullmatch(symbol)
    if not match:
        return None
    underlying, date_text, right_text, strike_text = match.groups()
    expiration = datetime.strptime(date_text, "%y%m%d").replace(tzinfo=UTC)
    right = OptionRight.CALL if right_text == "C" else OptionRight.PUT
    return underlying, expiration, right, Decimal(strike_text) / Decimal("1000")


def _unwrap(payload: Any) -> Any:
    if isinstance(payload, dict):
        for key in ("result", "data"):
            if key in payload and len(payload) <= 3:
                value = payload[key]
                if isinstance(value, str):
                    try:
                        return _unwrap(json.loads(value))
                    except json.JSONDecodeError:
                        return value
                return _unwrap(value)
    return payload


def _as_dict(payload: Any) -> dict[str, Any]:
    value = _unwrap(payload)
    return value if isinstance(value, dict) else {}


def _as_list_of_dicts(payload: Any) -> list[dict[str, Any]]:
    value = _unwrap(payload)
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in ("orders", "positions", "activities"):
            if isinstance(value.get(key), list):
                return [item for item in value[key] if isinstance(item, dict)]
    return []


def _symbol_payload(payload: dict[str, Any], symbol: str) -> dict[str, Any]:
    for key in ("snapshots", "snapshot"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            return _as_dict(nested.get(symbol) or nested)
    return _as_dict(payload.get(symbol) or payload)


def _chain_payload(payload: Any) -> dict[str, Any]:
    value = _as_dict(payload)
    for key in ("chain", "snapshots"):
        nested = value.get(key)
        if isinstance(nested, dict):
            return dict(nested)
    return value


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except Exception as exc:
        raise AlpacaMcpError("invalid numeric field in Alpaca response") from exc


def _parse_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise AlpacaMcpError("missing timestamp in Alpaca response")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _redacted_order_payload(payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {"id", "client_order_id", "status", "submitted_at", "order_class", "type"}
    return {key: value for key, value in payload.items() if key in allowed}
