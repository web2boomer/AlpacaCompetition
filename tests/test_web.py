from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import desc, select

from money_machine.domain.clock import SCORING_STARTS_AT
from money_machine.domain.enums import RunMode
from money_machine.persistence.models import AgentRunORM, EquitySnapshotORM
from money_machine.persistence.repository import AuditRepository
from money_machine.web import _equity_chart, create_app


def test_dashboard_and_health(settings, database) -> None:
    with TestClient(create_app(settings, database)) as client:
        response = client.get("/")
        health = client.get("/api/health")
        liveness = client.get("/api/liveness")
        passport = client.get("/api/passports/latest")
        activity = client.get("/api/activity")
        overnight = client.get("/api/overnight-estimate")
    assert response.status_code == 200
    assert "Money Machine" in response.text
    assert "Recent agent activity" in response.text
    assert "Each row is a possible defined-risk options trade" in response.text
    assert "These are fixed safety checks, not model opinions" in response.text
    assert "How a decision becomes an audited order" in response.text
    assert 'id="architecture-system-status"' in response.text
    assert "Status and timestamps refresh with the console every 10 seconds" in response.text
    assert "Last update" in response.text
    assert "Official equity locks in" in response.text
    assert 'aria-label="Time remaining until official equity locks in"' in response.text
    assert 'class="refresh-meta"' in response.text
    assert 'data-locks-at="2026-09-03T20:00:00+00:00"' in response.text
    assert 'datetime="2026-09-04T13:30:00+00:00"' in response.text
    assert "not additional trading time" in response.text
    assert "Official trading complete" in response.text
    assert "data-ends-at=" not in response.text
    assert "window.setInterval(renderCountdown, 1000)" in response.text
    assert 'id="performance-title">Performance</h2>' in response.text
    assert "Official competition performance" not in response.text
    assert "Verified account evidence" not in response.text
    assert "Updates every 15 seconds" not in response.text
    assert "BUILT BY WEB2BOOMER" in response.text
    assert 'href="https://webtwoboomer.com/"' in response.text
    assert "Built by AOB" not in response.text
    assert "linkedin.com" not in response.text
    assert 'class="shell ops-grid overview-grid"' in response.text
    assert 'class="panel performance-overview"' in response.text
    assert (
        "Paper-account equity performance from the competition start through now" in response.text
    )
    assert "Competition start · Aug 31 9:30 ET" in response.text
    assert 'class="equity-day-line"' in response.text
    assert 'class="equity-baseline-line"' in response.text
    assert 'class="equity-baseline-label"' in response.text
    assert "$100,000</text>" in response.text
    assert "Raw 9:30 broker mark" not in response.text
    assert 'class="equity-anomaly"' not in response.text
    assert 'class="decision-left-stack"' in response.text
    assert 'class="shell gate-section section-gap"' in response.text
    assert "Starting equity" not in response.text
    assert "Flat status" not in response.text
    assert response.text.index("Candidate evaluation") < response.text.index(
        "Recent agent activity"
    )
    assert response.text.index("Recent agent activity") < response.text.index(
        "Latest model decision"
    )
    assert 'aria-label="Current agent metrics"' in response.text
    assert 'class="shell metric-grid ops-metrics"' not in response.text
    assert "Alpaca remains authoritative" in response.text
    assert "System health" in response.text
    assert "https://aob.io" in response.text
    assert '<header class="nav shell">' not in response.text
    assert "window.setInterval(refresh, intervalMs)" in response.text
    assert "const intervalMs = 10000" in response.text
    assert 'id="market-session-state"' in response.text
    assert "renderMarketSession(health.market_session)" in response.text
    assert 'id="trading-availability"' in response.text
    assert "renderTradingAvailability(health)" in response.text
    assert 'marketSession === "market_hours"' in response.text
    assert 'id="account-equity" data-baseline="100000" aria-live="polite"' in response.text
    assert 'id="account-equity-return"' in response.text
    assert 'class="pnl-value positive"' in response.text
    assert "Latest Alpaca equity" not in response.text
    assert "Official P&amp;L" not in response.text
    assert 'fetch("/api/account", {cache: "no-store"})' in response.text
    assert 'id="equity-source-state"' in response.text
    assert "Provisional out-of-hours mark-to-market" in response.text
    assert "Estimate · not official P&amp;L" in response.text
    assert 'fetch("/api/overnight-estimate", {cache: "no-store"})' in response.text
    assert "renderAccount(await accountResponse.json())" in response.text
    assert "accountEquityReturn.textContent" in response.text
    assert "8 key gates" in response.text
    assert "refresh();" in response.text
    assert "REPLAY — NOT OFFICIAL P&amp;L" not in response.text
    assert health.status_code == 200
    assert liveness.status_code == 200
    assert liveness.json()["database"] == "ok"
    assert health.json()["database"] == "ok"
    assert health.json()["market_session"] in {"market_hours", "extended_hours", "overnight"}
    assert health.json()["kill_switch_active"] is False
    assert health.json()["entry_authority"] in {
        "enabled",
        "entry_disabled",
        "observe_only",
        "halted",
    }
    assert passport.status_code == 200
    assert passport.json()["official"] is False
    assert activity.status_code == 200
    assert activity.json()["entries"]
    assert activity.json()["latest_run_id"] == passport.json()["run_id"]
    assert overnight.status_code == 200
    assert overnight.json()["status"] == "market_open"


def test_kill_switch_is_prominent_and_degrades_health(settings, database) -> None:
    app = create_app(settings, database)
    with TestClient(app) as client:
        app.state.repository.set_kill_switch(
            active=True,
            now=datetime(2026, 9, 1, 14, 30, tzinfo=UTC),
        )
        dashboard = client.get("/")
        health = client.get("/api/health")

    assert health.status_code == 503
    assert health.json()["kill_switch_active"] is True
    assert health.json()["entry_authority"] == "entry_disabled"
    assert 'data-state="entry_disabled"' in dashboard.text
    assert "Trading availability" in dashboard.text


def test_equity_chart_shows_only_trading_sessions_with_daily_markers() -> None:
    now = SCORING_STARTS_AT + timedelta(days=2, hours=2)
    chart = _equity_chart(
        [
            SimpleNamespace(
                observed_at=SCORING_STARTS_AT - timedelta(minutes=1), equity=Decimal("1.00")
            ),
            SimpleNamespace(
                observed_at=SCORING_STARTS_AT + timedelta(hours=1),
                equity=Decimal("100100.00"),
            ),
            SimpleNamespace(
                observed_at=SCORING_STARTS_AT + timedelta(days=1, hours=12),
                equity=Decimal("100250.00"),
            ),
            SimpleNamespace(observed_at=now + timedelta(minutes=1), equity=Decimal("1.00")),
        ],
        now=now,
    )

    assert chart.points.split()[0].startswith("12.0,")
    assert chart.points.split()[-1].startswith("708.0,")
    assert len(chart.points.split()) == 3
    assert len(chart.day_markers) == 2
    assert [marker.label for marker in chart.day_markers] == ["Tue Sep 1", "Wed Sep 2"]
    assert chart.day_markers[0].x < chart.day_markers[1].x
    assert chart.day_markers[0].x > 300
    assert chart.start_label == "Competition start · Aug 31 9:30 ET"
    assert chart.end_label == "Now · last audit Aug 31 10:30 AM ET"
    assert chart.peak_equity == 100100.0
    assert 12 <= chart.baseline_y <= 168
    assert "1.0," not in chart.points
    assert chart.anomalies == ()


def test_equity_chart_quarantines_isolated_opening_mark_without_deleting_audit() -> None:
    opening = datetime(2026, 9, 1, 13, 30, tzinfo=UTC)
    chart = _equity_chart(
        [
            SimpleNamespace(observed_at=opening - timedelta(minutes=5), equity=Decimal("99948.52")),
            SimpleNamespace(observed_at=opening, equity=Decimal("95849.52")),
            SimpleNamespace(observed_at=opening + timedelta(minutes=5), equity=Decimal("99642.32")),
        ],
        now=opening + timedelta(minutes=10),
    )

    assert len(chart.anomalies) == 1
    assert "$95,849.52" in chart.anomalies[0].label
    assert "95849.52" not in chart.points
    assert chart.maximum_drawdown < 1000


def test_cash_retained_activity_explains_why(settings, database) -> None:
    with TestClient(create_app(settings, database)):
        pass
    repository = AuditRepository(database)
    now = datetime.now(UTC)
    run_id, created = repository.begin_run("replay:cash-retained", RunMode.REPLAY, now)
    assert created
    repository.complete_run(
        run_id,
        completed_at=now,
        passport={
            "decision": {
                "action": "abstain",
                "thesis": (
                    "No eligible candidates are available; all symbols failed mandatory "
                    "liquidity gates."
                ),
            },
            "candidate_rejections": {
                "SPY": ["volume, open interest, or quote liquidity gate failed"]
            },
            "risk": {
                "approved": False,
                "reason_codes": ["model_abstained"],
                "checks": [],
            },
            "execution": {"submitted": False},
            "operational_state": {"execution_state": "observe_only"},
        },
    )

    entry = repository.recent_activity(limit=1)[0]
    assert entry["label"] == "Cash retained"
    assert entry["reason"] == (
        "No eligible candidates are available; all symbols failed mandatory liquidity gates."
    )


def test_live_health_is_degraded_until_mcp_cycle_and_heartbeat(settings, database) -> None:
    live_settings = settings.model_copy(update={"run_mode": "live"})
    with TestClient(create_app(live_settings, database)) as client:
        health = client.get("/api/health")
        replay = client.post("/replay")
    assert health.status_code == 503
    assert health.json()["alpaca_mcp"] == "unverified"
    assert health.json()["scheduler_heartbeat"] == "stale_or_missing"
    assert replay.status_code == 404


def test_dashboard_equity_is_scoped_to_latest_account_fingerprint(settings, database) -> None:
    with TestClient(create_app(settings, database)):
        pass
    with database.session() as session:
        latest = session.scalar(select(AgentRunORM).order_by(desc(AgentRunORM.started_at)))
        assert latest is not None
        old_run_id = str(uuid4())
        old_time = latest.started_at - timedelta(minutes=5)
        session.add(
            AgentRunORM(
                id=old_run_id,
                cycle_key=f"prior-account:{old_run_id}",
                correlation_id=str(uuid4()),
                mode="replay",
                status="completed",
                started_at=old_time,
                completed_at=old_time,
                passport_json={
                    "official": False,
                    "account": {"fingerprint": "prior-account"},
                },
            )
        )
        session.add(
            EquitySnapshotORM(
                agent_run_id=old_run_id,
                observed_at=old_time,
                equity=Decimal("1.00"),
                cash=Decimal("1.00"),
                buying_power=Decimal("1.00"),
                portfolio_value=Decimal("1.00"),
                realized_pl=Decimal("0.00"),
                unrealized_pl=Decimal("0.00"),
                peak_equity=Decimal("1.00"),
                drawdown=Decimal("0.00"),
                official=False,
            )
        )

    summary = AuditRepository(database).dashboard_summary()
    assert summary["equities"]
    assert all(snapshot.equity != Decimal("1.00") for snapshot in summary["equities"])
