from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from money_machine.adapters.alpaca_mcp import AlpacaMcpV2Adapter
from money_machine.domain.candidates import build_candidates
from money_machine.domain.schemas import UnderlyingSnapshot
from money_machine.settings import Settings


@pytest.mark.asyncio
async def test_alpaca_option_snapshot_normalizes_liquidity_and_builds_candidate() -> None:
    observed_at = datetime(2026, 8, 31, 14, 0, tzinfo=UTC)
    payload = {
        "snapshots": {
            "SPY260904P00640000": _option_snapshot(
                bid="0.75", ask="0.80", bid_size=48, ask_size=37, volume=120
            ),
            "SPY260904P00645000": _option_snapshot(
                bid="1.50", ask="1.55", bid_size=82, ask_size=65, volume=500
            ),
            "SPY260904C00655000": _option_snapshot(
                bid="1.45", ask="1.50", bid_size=76, ask_size=91, volume=450
            ),
            "SPY260904C00660000": _option_snapshot(
                bid="0.70", ask="0.75", bid_size=39, ask_size=44, volume=100
            ),
        }
    }
    adapter = AlpacaMcpV2Adapter(Settings())
    adapter.call_tool = AsyncMock(return_value=payload)  # type: ignore[method-assign]

    chain = await adapter.option_chain("SPY")

    assert len(chain) == 4
    assert all(quote.volume is not None and quote.volume > 0 for quote in chain)
    assert all(quote.bid_size is not None and quote.bid_size > 0 for quote in chain)
    assert all(quote.ask_size is not None and quote.ask_size > 0 for quote in chain)
    assert all(quote.open_interest is None for quote in chain)

    snapshot = UnderlyingSnapshot(
        symbol="SPY",
        spot=Decimal("650"),
        previous_close=Decimal("649"),
        realized_move_pct=Decimal("0.005"),
        implied_move_pct=Decimal("0.01"),
        trend_return_pct=Decimal("0.0015"),
        observed_at=observed_at,
    )
    report = build_candidates([snapshot], {"SPY": chain}, observed_at)

    assert len(report.candidates) == 1
    assert report.candidates[0].liquidity_passed
    assert report.rejections == {}


@pytest.mark.asyncio
async def test_alpaca_option_snapshot_preserves_missing_liquidity_as_unknown() -> None:
    payload = {
        "SPY260904C00650000": {
            "latestQuote": {
                "bp": 1.0,
                "ap": 1.1,
                "t": "2026-08-31T14:00:00Z",
            },
            "impliedVolatility": 0.2,
        }
    }
    adapter = AlpacaMcpV2Adapter(Settings())
    adapter.call_tool = AsyncMock(return_value=payload)  # type: ignore[method-assign]

    chain = await adapter.option_chain("SPY")

    assert len(chain) == 1
    assert chain[0].volume is None
    assert chain[0].open_interest is None
    assert chain[0].bid_size is None
    assert chain[0].ask_size is None


@pytest.mark.asyncio
async def test_targeted_snapshot_helpers_use_bounded_symbol_requests() -> None:
    adapter = AlpacaMcpV2Adapter(Settings())
    adapter.call_tool = AsyncMock(return_value={"snapshots": {}})  # type: ignore[method-assign]

    await adapter.stock_snapshots(["QQQ", "IWM"], feed="overnight")
    await adapter.option_snapshots(["QQQ260901C00700000", "IWM260901P00290000"])

    assert adapter.call_tool.await_args_list[0].args == (
        "get_stock_snapshot",
        {"symbols": "QQQ,IWM", "feed": "overnight"},
    )
    assert adapter.call_tool.await_args_list[1].args == (
        "get_option_snapshot",
        {"symbols": "QQQ260901C00700000,IWM260901P00290000"},
    )


@pytest.mark.asyncio
async def test_stock_recovery_order_is_bounded_ioc_sell() -> None:
    adapter = AlpacaMcpV2Adapter(Settings())
    adapter.call_tool = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "id": "broker-recovery",
            "client_order_id": "mm-comp-ar-20260902-qqq",
            "status": "accepted",
            "submitted_at": "2026-09-02T13:30:01Z",
        }
    )

    result = await adapter.place_stock_order(
        symbol="QQQ",
        quantity=100,
        limit_price=Decimal("706.90"),
        client_order_id="mm-comp-ar-20260902-qqq",
    )

    assert result.broker_order_id == "broker-recovery"
    assert adapter.call_tool.await_args.args == (
        "place_stock_order",
        {
            "symbol": "QQQ",
            "side": "sell",
            "qty": "100",
            "type": "limit",
            "time_in_force": "ioc",
            "limit_price": "706.90",
            "extended_hours": False,
            "client_order_id": "mm-comp-ar-20260902-qqq",
            "order_class": "simple",
        },
    )


def _option_snapshot(
    *, bid: str, ask: str, bid_size: int, ask_size: int, volume: int
) -> dict[str, object]:
    return {
        "latestQuote": {
            "bp": bid,
            "ap": ask,
            "bs": bid_size,
            "as": ask_size,
            "t": "2026-08-31T14:00:00Z",
        },
        "dailyBar": {
            "v": volume,
            "t": "2026-08-31T04:00:00Z",
        },
        "impliedVolatility": "0.22",
        "greeks": {"delta": "0.20"},
    }
