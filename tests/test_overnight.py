from datetime import UTC, datetime
from decimal import Decimal

from money_machine.domain.schemas import AccountSnapshot
from money_machine.overnight import provisional_overnight_mark


def test_provisional_overnight_mark_aggregates_signed_leg_delta() -> None:
    account = _account(Decimal("99950"))
    positions = [
        {"symbol": "QQQ260901C00700000", "qty": "2", "side": "long"},
        {"symbol": "QQQ260901C00705000", "qty": "1", "side": "short"},
    ]
    option_snapshots = {
        "snapshots": {
            "QQQ260901C00700000": {"greeks": {"delta": "0.40"}},
            "QQQ260901C00705000": {"greeks": {"delta": "0.20"}},
        }
    }
    regular = {
        "snapshots": {
            "QQQ": {
                "dailyBar": {"c": "700", "t": "2026-08-31T04:00:00Z"},
                "latestTrade": {"p": "700.25", "t": "2026-08-31T20:10:00Z"},
            }
        }
    }
    extended = {"snapshots": {"QQQ": {"latestTrade": {"p": "701", "t": "2026-08-31T20:30:00Z"}}}}

    result = provisional_overnight_mark(
        account=account,
        positions=positions,
        option_snapshots=option_snapshots,
        regular_stock_snapshots=regular,
        extended_stock_snapshots=extended,
        observed_at=datetime(2026, 8, 31, 20, 31, tzinfo=UTC),
    )

    assert result["status"] == "provisional"
    assert result["estimated_change_since_close"] == "60.00"
    assert result["estimated_equity"] == "100010.00"
    assert result["coverage"] == {
        "covered_option_positions": 2,
        "total_option_positions": 2,
    }
    assert result["underlyings"][0]["source"] == "overnight_feed"
    assert result["underlyings"][0]["move_percent"] == "0.1429"


def test_provisional_overnight_mark_does_not_report_stale_close_as_zero_move() -> None:
    symbol = "IWM260901P00290000"
    result = provisional_overnight_mark(
        account=_account(Decimal("100000")),
        positions=[{"symbol": symbol, "qty": "1", "side": "long"}],
        option_snapshots={"snapshots": {symbol: {"greeks": {"delta": "-0.3"}}}},
        regular_stock_snapshots={
            "snapshots": {
                "IWM": {
                    "dailyBar": {"c": "295", "t": "2026-08-31T04:00:00Z"},
                    "latestTrade": {"p": "295", "t": "2026-08-31T20:00:00Z"},
                }
            }
        },
        extended_stock_snapshots={},
        observed_at=datetime(2026, 8, 31, 20, 30, tzinfo=UTC),
    )

    assert result["status"] == "unavailable"
    assert result["estimated_change_since_close"] is None
    assert "fresh extended-hours" in result["message"]


def _account(equity: Decimal) -> AccountSnapshot:
    return AccountSnapshot(
        account_id="paper",
        is_paper=True,
        equity=equity,
        cash=Decimal("98000"),
        buying_power=Decimal("196000"),
        portfolio_value=equity,
    )
