import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from money_machine.adapters.alpaca_mcp import AlpacaMcpV2Adapter, parse_occ_symbol
from money_machine.adapters.replay import ReplayAlpacaAdapter
from money_machine.domain.clock import BASELINE_EQUITY, ENDS_AT
from money_machine.domain.enums import RunMode
from money_machine.model_provider import ReplayModelProvider
from money_machine.overnight import provisional_overnight_mark
from money_machine.persistence.database import Database
from money_machine.persistence.repository import AuditRepository
from money_machine.ports import AlpacaPort, BrokeragePort
from money_machine.safety import configured_account_fingerprint
from money_machine.service import AgentService
from money_machine.settings import Settings

PACKAGE_DIR = Path(__file__).parent
ACCOUNT_CACHE_SECONDS = 10
ACCOUNT_READ_TIMEOUT_SECONDS = 12
OVERNIGHT_CACHE_SECONDS = 60
logger = structlog.get_logger()


def create_app(settings: Settings | None = None, database: Database | None = None) -> FastAPI:
    app_settings = settings or Settings()
    db = database or Database(app_settings.database_url)
    repository = AuditRepository(db)
    account_lock = asyncio.Lock()
    account_cache: tuple[datetime, dict[str, Any]] | None = None
    overnight_lock = asyncio.Lock()
    overnight_cache: tuple[datetime, dict[str, Any]] | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        del app
        if app_settings.run_mode is RunMode.REPLAY:
            db.create_all_for_tests()
            if repository.latest_passport() is None:
                adapter = ReplayAlpacaAdapter()
                await AgentService(app_settings, repository).run_cycle(
                    adapter=adapter,
                    model=ReplayModelProvider(),
                    now=adapter.observed_at,
                    mode=RunMode.REPLAY,
                )
        yield

    app = FastAPI(
        title="Money Machine",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = app_settings
    app.state.database = db
    app.state.repository = repository
    templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
    templates.env.filters["money"] = _money
    templates.env.filters["pct"] = _pct
    app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> HTMLResponse:
        summary = repository.dashboard_summary()
        fingerprint = configured_account_fingerprint(app_settings)
        performance = repository.competition_performance_summary(
            account_fingerprint=fingerprint,
            now=datetime.now(UTC),
        )
        summary["performance"] = performance
        official_curve = repository.official_equity_curve(account_fingerprint=fingerprint)
        if official_curve:
            summary["equities"] = official_curve
            summary["latest_equity"] = official_curve[-1]
        passport = summary.get("latest_passport") or {}
        chart = _equity_chart(summary.get("equities", []))
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "summary": summary,
                "passport": passport,
                "chart": chart,
                "activity": repository.recent_activity(),
                "replay_enabled": app_settings.run_mode is RunMode.REPLAY,
                "competition_ends_at": ENDS_AT.isoformat(),
                "refreshed_at": datetime.now(UTC).isoformat(),
            },
        )

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    async def passport_page(request: Request, run_id: str) -> HTMLResponse:
        passport = repository.passport_for_run(run_id)
        if passport is None:
            raise HTTPException(status_code=404, detail="Decision Passport not found")
        return templates.TemplateResponse(request, "passport.html", {"passport": passport})

    @app.post("/replay")
    async def replay() -> RedirectResponse:
        if app_settings.run_mode is not RunMode.REPLAY:
            raise HTTPException(status_code=404, detail="Replay is disabled in live mode")
        adapter = ReplayAlpacaAdapter()
        outcome = await AgentService(app_settings, repository).run_cycle(
            adapter=adapter,
            model=ReplayModelProvider(),
            now=adapter.observed_at,
            mode=RunMode.REPLAY,
        )
        return RedirectResponse(f"/runs/{outcome.run_id}", status_code=303)

    @app.get("/api/passports/latest")
    async def latest_passport() -> JSONResponse:
        passport = repository.latest_passport()
        if passport is None:
            raise HTTPException(status_code=404, detail="No Decision Passport available")
        return JSONResponse(passport)

    @app.get("/api/activity")
    async def recent_activity() -> JSONResponse:
        activity = repository.recent_activity()
        return JSONResponse(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "latest_run_id": activity[0]["run_id"] if activity else None,
                "entries": activity,
            }
        )

    @app.get("/api/performance")
    async def competition_performance() -> JSONResponse:
        return JSONResponse(
            repository.competition_performance_summary(
                account_fingerprint=configured_account_fingerprint(app_settings),
                now=datetime.now(UTC),
            )
        )

    @app.get("/api/performance/final")
    async def final_competition_performance() -> JSONResponse:
        summary = repository.competition_performance_summary(
            account_fingerprint=configured_account_fingerprint(app_settings),
            now=datetime.now(UTC),
        )
        if summary["result_status"] != "final_eod_snapshot":
            raise HTTPException(status_code=404, detail="Final EOD performance is not available")
        return JSONResponse(summary)

    @app.get("/api/account")
    async def broker_account() -> JSONResponse:
        nonlocal account_cache
        requested_at = datetime.now(UTC)
        if account_cache is not None:
            cached_at, cached_payload = account_cache
            if requested_at - cached_at < timedelta(seconds=ACCOUNT_CACHE_SECONDS):
                return _no_store_response(cached_payload)
        async with account_lock:
            requested_at = datetime.now(UTC)
            if account_cache is not None:
                cached_at, cached_payload = account_cache
                if requested_at - cached_at < timedelta(seconds=ACCOUNT_CACHE_SECONDS):
                    return _no_store_response(cached_payload)
            try:
                async with asyncio.timeout(ACCOUNT_READ_TIMEOUT_SECONDS):
                    payload = await _broker_account_payload(app_settings)
            except Exception as exc:
                logger.warning("broker_account_endpoint_degraded", error=type(exc).__name__)
                return _no_store_response(
                    {
                        "status": "degraded",
                        "equity": None,
                        "pnl": None,
                        "observed_at": requested_at.isoformat(),
                        "cash": None,
                        "buying_power": None,
                        "portfolio_value": None,
                        "realized_pl": None,
                        "unrealized_pl": None,
                        "open_position_count": None,
                        "working_order_count": None,
                        "broker_confirmed_flat": None,
                    },
                    status_code=503,
                )
            account_cache = (requested_at, payload)
            return _no_store_response(payload)

    @app.get("/api/overnight-estimate")
    async def overnight_estimate() -> JSONResponse:
        nonlocal overnight_cache
        requested_at = datetime.now(UTC)
        if overnight_cache is not None:
            cached_at, cached_payload = overnight_cache
            if requested_at - cached_at < timedelta(seconds=OVERNIGHT_CACHE_SECONDS):
                return _no_store_response(cached_payload)
        async with overnight_lock:
            requested_at = datetime.now(UTC)
            if overnight_cache is not None:
                cached_at, cached_payload = overnight_cache
                if requested_at - cached_at < timedelta(seconds=OVERNIGHT_CACHE_SECONDS):
                    return _no_store_response(cached_payload)
            try:
                async with asyncio.timeout(ACCOUNT_READ_TIMEOUT_SECONDS):
                    payload = await _broker_overnight_payload(app_settings)
            except Exception as exc:
                logger.warning("overnight_estimate_endpoint_degraded", error=type(exc).__name__)
                payload = {
                    "status": "unavailable",
                    "estimated_equity": None,
                    "estimated_change_since_close": None,
                    "observed_at": requested_at.isoformat(),
                    "message": "The provisional out-of-hours estimate is temporarily unavailable.",
                }
            overnight_cache = (requested_at, payload)
            return _no_store_response(payload)

    @app.get("/api/health")
    async def health() -> JSONResponse:
        state = repository.latest_operational_state()
        database_ok = db.healthcheck()
        now = datetime.now(UTC)
        replay_mode = app_settings.run_mode is RunMode.REPLAY
        last_success = _aware(state.get("last_success_at"))
        heartbeat = _aware(state.get("scheduler_heartbeat_at"))
        mcp_ok = replay_mode or last_success is not None
        heartbeat_ok = replay_mode or (
            heartbeat is not None and now - heartbeat <= timedelta(minutes=10)
        )
        reconciliation_ok = bool(state.get("reconciliation_clean", True))
        healthy = database_ok and mcp_ok and heartbeat_ok and reconciliation_ok
        payload: dict[str, Any] = {
            "status": "healthy" if healthy else "degraded",
            "database": "ok" if database_ok else "failed",
            "alpaca_mcp": "replay" if replay_mode else ("ok" if mcp_ok else "unverified"),
            "scheduler_heartbeat": (
                "not_applicable" if replay_mode else ("ok" if heartbeat_ok else "stale_or_missing")
            ),
            "reconciliation": "clean" if reconciliation_ok else "halted",
            "execution_state": state.get("execution_state", "observe_only"),
            "timestamp": now.isoformat(),
        }
        return JSONResponse(payload, status_code=200 if healthy else 503)

    @app.get("/api/liveness")
    async def liveness() -> JSONResponse:
        database_ok = db.healthcheck()
        return JSONResponse(
            {
                "status": "alive" if database_ok else "unavailable",
                "database": "ok" if database_ok else "failed",
                "timestamp": datetime.now(UTC).isoformat(),
            },
            status_code=200 if database_ok else 503,
        )

    return app


def _money(value: Any) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def _pct(value: Any) -> str:
    try:
        return f"{float(value) * 100:.2f}%"
    except (TypeError, ValueError):
        return "0.00%"


def _equity_chart(equities: list[Any]) -> str:
    values = [float(snapshot.equity) for snapshot in equities]
    if not values:
        values = [100000.0]
    if len(values) == 1:
        values.insert(0, 100000.0)
    width, height, padding = 720, 180, 12
    low, high = min(values), max(values)
    span = high - low
    points = []
    for index, value in enumerate(values):
        x = padding + index * ((width - 2 * padding) / (len(values) - 1))
        y = (
            height / 2
            if span == 0
            else height - padding - ((value - low) / span) * (height - 2 * padding)
        )
        points.append(f"{x:.1f},{y:.1f}")
    return " ".join(points)


def _aware(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def _broker_account_payload(settings: Settings) -> dict[str, Any]:
    if settings.run_mode is RunMode.REPLAY:
        return await _read_broker_account(ReplayAlpacaAdapter())
    async with AlpacaMcpV2Adapter(settings) as adapter:
        return await _read_broker_account(adapter)


async def _broker_overnight_payload(settings: Settings) -> dict[str, Any]:
    if settings.run_mode is RunMode.REPLAY:
        return await _read_overnight_estimate(ReplayAlpacaAdapter())
    async with AlpacaMcpV2Adapter(settings) as adapter:
        return await _read_overnight_estimate(adapter)


async def _read_overnight_estimate(adapter: AlpacaPort) -> dict[str, Any]:
    account, positions_raw, market_clock = await asyncio.gather(
        adapter.account(),
        adapter.positions(),
        adapter.market_clock(),
    )
    observed_at = datetime.now(UTC)
    positions = list(positions_raw)
    if bool(market_clock.get("is_open") or market_clock.get("isOpen")):
        return {
            "status": "market_open",
            "official_equity": str(account.equity),
            "estimated_equity": None,
            "estimated_change_since_close": None,
            "observed_at": observed_at.isoformat(),
            "message": "Options are open; Alpaca's live account mark is authoritative.",
        }

    option_symbols = sorted(
        {
            str(position.get("symbol") or "")
            for position in positions
            if parse_occ_symbol(str(position.get("symbol") or "")) is not None
        }
    )
    underlying_symbols = sorted(
        {
            symbol
            for option_symbol in option_symbols
            if (parsed := parse_occ_symbol(option_symbol)) is not None
            for symbol in (parsed[0],)
        }
    )
    if not option_symbols or not underlying_symbols:
        return provisional_overnight_mark(
            account=account,
            positions=positions,
            option_snapshots={},
            regular_stock_snapshots={},
            extended_stock_snapshots={},
            observed_at=observed_at,
        )

    option_snapshots, regular_snapshots = await asyncio.gather(
        adapter.option_snapshots(option_symbols),
        adapter.stock_snapshots(underlying_symbols),
    )
    try:
        extended_snapshots = await adapter.stock_snapshots(underlying_symbols, feed="overnight")
    except Exception:
        extended_snapshots = {}
    return provisional_overnight_mark(
        account=account,
        positions=positions,
        option_snapshots=option_snapshots,
        regular_stock_snapshots=regular_snapshots,
        extended_stock_snapshots=extended_snapshots,
        observed_at=observed_at,
    )


async def _read_broker_account(adapter: BrokeragePort) -> dict[str, Any]:
    account, positions, orders = await asyncio.gather(
        adapter.account(),
        adapter.positions(),
        adapter.orders(status="open"),
    )
    observed_at = datetime.now(UTC)
    return {
        "status": "ok",
        "equity": str(account.equity),
        "pnl": str(account.equity - BASELINE_EQUITY),
        "observed_at": observed_at.isoformat(),
        "cash": str(account.cash),
        "buying_power": str(account.buying_power),
        "portfolio_value": str(account.portfolio_value),
        "realized_pl": str(account.realized_pl),
        "unrealized_pl": str(account.unrealized_pl),
        "open_position_count": len(positions),
        "working_order_count": len(orders),
        "broker_confirmed_flat": not positions and not orders,
    }


def _no_store_response(payload: dict[str, Any], *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        payload,
        status_code=status_code,
        headers={"Cache-Control": "no-store, max-age=0"},
    )
