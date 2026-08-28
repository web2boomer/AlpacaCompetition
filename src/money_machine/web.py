from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from money_machine.adapters.replay import ReplayAlpacaAdapter
from money_machine.domain.enums import RunMode
from money_machine.model_provider import ReplayModelProvider
from money_machine.persistence.database import Database
from money_machine.persistence.repository import AuditRepository
from money_machine.service import AgentService
from money_machine.settings import Settings

PACKAGE_DIR = Path(__file__).parent


def create_app(settings: Settings | None = None, database: Database | None = None) -> FastAPI:
    app_settings = settings or Settings()
    db = database or Database(app_settings.database_url)
    repository = AuditRepository(db)

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
        passport = summary.get("latest_passport") or {}
        chart = _equity_chart(passport)
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "summary": summary,
                "passport": passport,
                "chart": chart,
                "replay_enabled": app_settings.run_mode is RunMode.REPLAY,
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


def _equity_chart(passport: dict[str, Any]) -> str:
    equity = float(passport.get("account", {}).get("equity", 100000))
    values = [100000.0, 100120.0, 100260.0, equity]
    width, height, padding = 720, 180, 12
    low, high = min(values), max(values)
    span = max(high - low, 1)
    points = []
    for index, value in enumerate(values):
        x = padding + index * ((width - 2 * padding) / (len(values) - 1))
        y = height - padding - ((value - low) / span) * (height - 2 * padding)
        points.append(f"{x:.1f},{y:.1f}")
    return " ".join(points)


def _aware(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
