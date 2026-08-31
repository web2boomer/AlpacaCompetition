from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from money_machine.adapters.alpaca_mcp import parse_occ_symbol
from money_machine.domain.schemas import AccountSnapshot

NEW_YORK = ZoneInfo("America/New_York")
OPTION_MULTIPLIER = Decimal("100")


def provisional_overnight_mark(
    *,
    account: AccountSnapshot,
    positions: list[dict[str, Any]],
    option_snapshots: dict[str, Any],
    regular_stock_snapshots: dict[str, Any],
    extended_stock_snapshots: dict[str, Any],
    observed_at: datetime,
) -> dict[str, Any]:
    """Estimate post-close option movement from closing delta and underlying moves.

    The estimate is deliberately separate from broker equity. It is a first-order
    sensitivity proxy, not an executable option mark.
    """
    option_positions = [
        position
        for position in positions
        if parse_occ_symbol(str(position.get("symbol") or "")) is not None
        and _decimal(position.get("qty")) != 0
    ]
    if not option_positions:
        return _empty_payload(
            status="not_applicable",
            account=account,
            observed_at=observed_at,
            message="No open option positions require an out-of-hours estimate.",
        )

    regular_by_symbol = _snapshot_map(regular_stock_snapshots)
    extended_by_symbol = _snapshot_map(extended_stock_snapshots)
    options_by_symbol = _snapshot_map(option_snapshots)
    underlying_marks: dict[str, dict[str, Any]] = {}
    for position in option_positions:
        parsed = parse_occ_symbol(str(position["symbol"]))
        assert parsed is not None
        underlying = parsed[0]
        if underlying in underlying_marks:
            continue
        mark = _underlying_mark(
            underlying,
            regular_by_symbol.get(underlying, {}),
            extended_by_symbol.get(underlying, {}),
        )
        if mark is not None:
            underlying_marks[underlying] = mark

    estimated_change = Decimal("0")
    covered = 0
    leg_estimates: list[dict[str, Any]] = []
    for position in option_positions:
        symbol = str(position["symbol"])
        parsed = parse_occ_symbol(symbol)
        assert parsed is not None
        underlying = parsed[0]
        underlying_mark = underlying_marks.get(underlying)
        option_snapshot = options_by_symbol.get(symbol, {})
        delta = _option_delta(option_snapshot)
        if underlying_mark is None or delta is None:
            continue
        quantity = _signed_quantity(position)
        change = _decimal(underlying_mark["extended_price"]) - _decimal(
            underlying_mark["regular_close"]
        )
        leg_change = quantity * delta * change * OPTION_MULTIPLIER
        estimated_change += leg_change
        covered += 1
        leg_estimates.append(
            {
                "symbol": symbol,
                "underlying": underlying,
                "quantity": str(quantity),
                "closing_delta": str(delta),
                "estimated_change": str(leg_change.quantize(Decimal("0.01"))),
            }
        )

    if covered == 0:
        return _empty_payload(
            status="unavailable",
            account=account,
            observed_at=observed_at,
            message=(
                "No fresh extended-hours underlying trade and closing delta overlap is "
                "available yet."
            ),
            position_count=len(option_positions),
        )

    estimated_change = estimated_change.quantize(Decimal("0.01"))
    estimated_equity = (account.equity + estimated_change).quantize(Decimal("0.01"))
    return {
        "status": "provisional",
        "official_equity": str(account.equity),
        "estimated_equity": str(estimated_equity),
        "estimated_change_since_close": str(estimated_change),
        "observed_at": observed_at.astimezone(UTC).isoformat(),
        "method": "closing option delta x extended-hours underlying move x position quantity",
        "disclaimer": (
            "Indicative only; not an Alpaca option quote or executable P&L. Excludes overnight "
            "implied-volatility changes, theta, gamma beyond first order, and opening spreads."
        ),
        "coverage": {
            "covered_option_positions": covered,
            "total_option_positions": len(option_positions),
        },
        "underlyings": list(underlying_marks.values()),
        "legs": leg_estimates,
    }


def _empty_payload(
    *,
    status: str,
    account: AccountSnapshot,
    observed_at: datetime,
    message: str,
    position_count: int = 0,
) -> dict[str, Any]:
    return {
        "status": status,
        "official_equity": str(account.equity),
        "estimated_equity": None,
        "estimated_change_since_close": None,
        "observed_at": observed_at.astimezone(UTC).isoformat(),
        "message": message,
        "coverage": {
            "covered_option_positions": 0,
            "total_option_positions": position_count,
        },
    }


def _snapshot_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    for key in ("snapshots", "snapshot"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            return {
                str(symbol): value for symbol, value in nested.items() if isinstance(value, dict)
            }
    return {str(symbol): value for symbol, value in payload.items() if isinstance(value, dict)}


def _underlying_mark(
    symbol: str,
    regular_snapshot: dict[str, Any],
    extended_snapshot: dict[str, Any],
) -> dict[str, Any] | None:
    daily = _nested(regular_snapshot, "daily_bar", "dailyBar")
    regular_close = _decimal(_first(daily, "close", "c"))
    session_time = _timestamp(_first(daily, "timestamp", "t"))
    if regular_close <= 0 or session_time is None:
        return None
    session_date = session_time.astimezone(NEW_YORK).date()
    close_at = datetime.combine(session_date, datetime.min.time(), NEW_YORK).replace(hour=16)

    candidates = []
    for source, snapshot in (
        ("regular_feed", regular_snapshot),
        ("overnight_feed", extended_snapshot),
    ):
        latest = _nested(snapshot, "latest_trade", "latestTrade")
        price = _decimal(_first(latest, "price", "p"))
        timestamp = _timestamp(_first(latest, "timestamp", "t"))
        if price > 0 and timestamp is not None and timestamp > close_at:
            candidates.append((timestamp, price, source))
    if not candidates:
        return None
    timestamp, extended_price, source = max(candidates, key=lambda item: item[0])
    move = (extended_price / regular_close - Decimal("1")) * Decimal("100")
    return {
        "symbol": symbol,
        "regular_close": str(regular_close),
        "extended_price": str(extended_price),
        "move_percent": str(move.quantize(Decimal("0.0001"))),
        "price_observed_at": timestamp.astimezone(UTC).isoformat(),
        "source": source,
    }


def _option_delta(snapshot: dict[str, Any]) -> Decimal | None:
    greeks = _nested(snapshot, "greeks")
    raw = _first(greeks, "delta")
    if raw is None:
        return None
    delta = _decimal(raw)
    if delta < Decimal("-1") or delta > Decimal("1"):
        return None
    return delta


def _signed_quantity(position: dict[str, Any]) -> Decimal:
    quantity = _decimal(position.get("qty"))
    side = str(position.get("side") or "").lower()
    if side == "short" and quantity > 0:
        return -quantity
    if side == "long" and quantity < 0:
        return -quantity
    return quantity


def _nested(payload: dict[str, Any], *keys: str) -> dict[str, Any]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _first(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
