import asyncio
import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from money_machine.adapters.alpaca_mcp import AlpacaMcpV2Adapter, parse_occ_symbol
from money_machine.adapters.replay import ReplayAlpacaAdapter
from money_machine.business_reporting import PROJECT, BusinessReportBuilder
from money_machine.domain.clock import (
    BASELINE_EQUITY,
    ENDS_AT,
    EOD_EQUITY_SNAPSHOT_AT,
    NEW_YORK,
    SCORING_STARTS_AT,
    market_session_phase,
)
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


@dataclass(frozen=True, slots=True)
class EquityChartMarker:
    x: float
    label: str


@dataclass(frozen=True, slots=True)
class EquityChartAnomaly:
    x: float
    y: float
    label: str


@dataclass(frozen=True, slots=True)
class EquityChart:
    points: str
    baseline_y: float
    day_markers: tuple[EquityChartMarker, ...]
    anomalies: tuple[EquityChartAnomaly, ...]
    start_label: str
    end_label: str
    peak_equity: float
    maximum_drawdown: float
    maximum_drawdown_percent: float


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
        now = datetime.now(UTC)
        summary = repository.dashboard_summary()
        operational_state = repository.latest_operational_state()
        fingerprint = configured_account_fingerprint(app_settings)
        performance = repository.competition_performance_summary(
            account_fingerprint=fingerprint,
            now=now,
        )
        summary["performance"] = performance
        official_curve = repository.official_equity_curve(account_fingerprint=fingerprint)
        if official_curve:
            summary["equities"] = official_curve
            summary["latest_equity"] = official_curve[-1]
        passport = summary.get("latest_passport") or {}
        chart = _equity_chart(summary.get("equities", []), now=now)
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "summary": summary,
                "passport": passport,
                "chart": chart,
                "activity": repository.recent_activity(),
                "replay_enabled": app_settings.run_mode is RunMode.REPLAY,
                "official_equity_locks_at": EOD_EQUITY_SNAPSHOT_AT.isoformat(),
                "hackathon_ends_at": ENDS_AT.isoformat(),
                "market_session": market_session_phase(now),
                "entry_authority": _entry_authority(operational_state),
                "kill_switch_active": bool(operational_state.get("kill_switch_active", False)),
                "refreshed_at": now.isoformat(),
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
        kill_switch_active = bool(state.get("kill_switch_active", False))
        healthy = (
            database_ok and mcp_ok and heartbeat_ok and reconciliation_ok and not kill_switch_active
        )
        payload: dict[str, Any] = {
            "status": "healthy" if healthy else "degraded",
            "database": "ok" if database_ok else "failed",
            "alpaca_mcp": "replay" if replay_mode else ("ok" if mcp_ok else "unverified"),
            "scheduler_heartbeat": (
                "not_applicable" if replay_mode else ("ok" if heartbeat_ok else "stale_or_missing")
            ),
            "reconciliation": "clean" if reconciliation_ok else "halted",
            "execution_state": state.get("execution_state", "observe_only"),
            "kill_switch_active": kill_switch_active,
            "entry_authority": _entry_authority(state, healthy=healthy),
            "kill_switch_reason": state.get("incident_code"),
            "market_session": market_session_phase(now),
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

    @app.get("/internal/mission_control/report")
    async def mission_control_report(request: Request) -> Response:
        if not _mission_control_authorized(request, app_settings):
            return Response(status_code=401)
        if app_settings.mission_control_project != PROJECT:
            logger.warning(
                "mission_control_reporting_project_mismatch",
                configured_project=app_settings.mission_control_project,
            )
            return Response(status_code=503, headers={"Retry-After": "60"})
        try:
            report = BusinessReportBuilder(
                repository,
                interval_minutes=app_settings.mission_control_reporting_interval_minutes,
                account_fingerprint=configured_account_fingerprint(app_settings),
            ).build(
                now=datetime.now(UTC),
                environment=(
                    app_settings.mission_control_environment or app_settings.app_env.value
                ),
            )
        except Exception as exc:
            logger.warning("mission_control_report_failed", error_type=type(exc).__name__)
            return Response(status_code=503, headers={"Retry-After": "60"})
        if report is None:
            return Response(status_code=204)
        return JSONResponse(report.as_payload())

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


def _entry_authority(state: dict[str, Any], *, healthy: bool = True) -> str:
    if bool(state.get("kill_switch_active", False)):
        return "entry_disabled"
    if not healthy or not bool(state.get("reconciliation_clean", True)):
        return "halted"
    execution_state = str(state.get("execution_state", "observe_only"))
    if execution_state == "full_execution":
        return "enabled"
    if execution_state == "observe_only":
        return "observe_only"
    return "entry_disabled"


def _equity_chart(equities: list[Any], *, now: datetime) -> EquityChart:
    width, height, padding = 720, 180, 12
    chart_end = min(
        max(_aware(now) or SCORING_STARTS_AT, SCORING_STARTS_AT), EOD_EQUITY_SNAPSHOT_AT
    )
    audit_observations = sorted(
        (
            (observed_at, float(snapshot.equity))
            for snapshot in equities
            if (observed_at := _aware(getattr(snapshot, "observed_at", None))) is not None
            and SCORING_STARTS_AT <= observed_at <= chart_end
        ),
        key=lambda item: item[0],
    )
    anomalous_timestamps = {
        audit_observations[index][0]
        for index in range(1, len(audit_observations) - 1)
        if _isolated_opening_mark_anomaly(audit_observations, index)
    }
    observations = [
        observation
        for observation in audit_observations
        if _is_regular_market_observation(observation[0])
    ]
    anomalous_indexes = {
        index
        for index, observation in enumerate(observations)
        if observation[0] in anomalous_timestamps
    }
    clean_observations = [
        observation
        for index, observation in enumerate(observations)
        if index not in anomalous_indexes
    ]
    series = [(SCORING_STARTS_AT, float(BASELINE_EQUITY)), *clean_observations]
    if series[-1][0] < chart_end:
        series.append((chart_end, series[-1][1]))
    values = [value for _, value in series]
    low, high = min(values), max(values)
    span = high - low
    baseline_y = (
        height / 2
        if span == 0
        else height - padding - ((float(BASELINE_EQUITY) - low) / span) * (height - 2 * padding)
    )
    points = []
    duration = max(_trading_elapsed_seconds(chart_end), 1.0)
    for observed_at, value in series:
        elapsed = _trading_elapsed_seconds(observed_at)
        x = padding + (elapsed / duration) * (width - 2 * padding)
        y = (
            height / 2
            if span == 0
            else height - padding - ((value - low) / span) * (height - 2 * padding)
        )
        points.append(f"{x:.1f},{y:.1f}")

    anomalies: list[EquityChartAnomaly] = []
    for index in sorted(anomalous_indexes):
        observed_at, value = observations[index]
        elapsed = _trading_elapsed_seconds(observed_at)
        x = padding + (elapsed / duration) * (width - 2 * padding)
        raw_y = (
            height / 2
            if span == 0
            else height - padding - ((value - low) / span) * (height - 2 * padding)
        )
        anomalies.append(
            EquityChartAnomaly(
                x=round(x, 1),
                y=round(min(max(raw_y, padding), height - padding), 1),
                label=(
                    f"Quarantined raw broker mark · "
                    f"{observed_at.astimezone(NEW_YORK).strftime('%b %-d %-I:%M %p')} ET · "
                    f"${value:,.2f}"
                ),
            )
        )

    markers: list[EquityChartMarker] = []
    local_start = SCORING_STARTS_AT.astimezone(NEW_YORK)
    next_session = (local_start + timedelta(days=1)).replace(
        hour=9, minute=30, second=0, microsecond=0
    )
    while next_session.astimezone(UTC) < chart_end:
        if next_session.weekday() >= 5:
            next_session += timedelta(days=1)
            continue
        marker_at = next_session.astimezone(UTC)
        elapsed = _trading_elapsed_seconds(marker_at)
        x = padding + (elapsed / duration) * (width - 2 * padding)
        markers.append(EquityChartMarker(x=round(x, 1), label=next_session.strftime("%a %b %-d")))
        next_session += timedelta(days=1)

    latest_at = observations[-1][0] if observations else SCORING_STARTS_AT
    peak = float(BASELINE_EQUITY)
    maximum_drawdown = 0.0
    for _, value in series:
        peak = max(peak, value)
        maximum_drawdown = max(maximum_drawdown, peak - value)
    return EquityChart(
        points=" ".join(points),
        baseline_y=round(baseline_y, 1),
        day_markers=tuple(markers),
        anomalies=tuple(anomalies),
        start_label="Competition start · Aug 31 9:30 ET",
        end_label=(
            f"Now · last audit {latest_at.astimezone(NEW_YORK).strftime('%b %-d %-I:%M %p')} ET"
        ),
        peak_equity=peak,
        maximum_drawdown=maximum_drawdown,
        maximum_drawdown_percent=(maximum_drawdown / peak * 100 if peak else 0.0),
    )


def _is_regular_market_observation(at: datetime) -> bool:
    local = at.astimezone(NEW_YORK)
    wall_time = local.time().replace(tzinfo=None)
    return local.weekday() < 5 and time(9, 30) <= wall_time <= time(16)


def _trading_elapsed_seconds(at: datetime) -> float:
    target = min(max(at.astimezone(UTC), SCORING_STARTS_AT), EOD_EQUITY_SNAPSHOT_AT)
    target_local = target.astimezone(NEW_YORK)
    scoring_start_local = SCORING_STARTS_AT.astimezone(NEW_YORK)
    equity_lock_local = EOD_EQUITY_SNAPSHOT_AT.astimezone(NEW_YORK)
    session_date = scoring_start_local.date()
    total = 0.0
    while session_date <= target_local.date():
        if session_date.weekday() < 5:
            session_open = datetime.combine(session_date, time(9, 30), tzinfo=NEW_YORK)
            session_close = datetime.combine(session_date, time(16), tzinfo=NEW_YORK)
            session_open = max(session_open, scoring_start_local)
            session_close = min(session_close, equity_lock_local)
            if target_local > session_open:
                total += max((min(target_local, session_close) - session_open).total_seconds(), 0.0)
        session_date += timedelta(days=1)
    return total


def _isolated_opening_mark_anomaly(observations: list[tuple[datetime, float]], index: int) -> bool:
    observed_at, value = observations[index]
    local = observed_at.astimezone(NEW_YORK)
    previous_at, previous = observations[index - 1]
    following_at, following = observations[index + 1]
    baseline = max(previous, following, 1.0)
    return (
        local.hour == 9
        and local.minute == 30
        and observed_at - previous_at <= timedelta(minutes=10)
        and following_at - observed_at <= timedelta(minutes=10)
        and abs(previous - following) / baseline <= 0.01
        and (previous - value) / baseline >= 0.02
        and (following - value) / baseline >= 0.02
    )


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


def _mission_control_authorized(request: Request, settings: Settings) -> bool:
    configured = settings.mission_control_token
    if configured is None:
        return False
    expected = configured.get_secret_value()
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        return False
    provided = authorization.removeprefix("Bearer ")
    if not expected or not provided or len(expected) != len(provided):
        return False
    return hmac.compare_digest(provided, expected)
