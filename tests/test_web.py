from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import desc, select

from money_machine.domain.enums import RunMode
from money_machine.persistence.models import AgentRunORM, EquitySnapshotORM
from money_machine.persistence.repository import AuditRepository
from money_machine.web import create_app


def test_dashboard_and_health(settings, database) -> None:
    with TestClient(create_app(settings, database)) as client:
        response = client.get("/")
        health = client.get("/api/health")
        liveness = client.get("/api/liveness")
        passport = client.get("/api/passports/latest")
        activity = client.get("/api/activity")
    assert response.status_code == 200
    assert "Money Machine" in response.text
    assert "Recent agent activity" in response.text
    assert "Each row is a possible defined-risk options trade" in response.text
    assert "These are fixed safety checks, not model opinions" in response.text
    assert "How a decision becomes an audited order" in response.text
    assert 'id="architecture-system-status"' in response.text
    assert "Status and timestamps refresh with the console every 15 seconds" in response.text
    assert "Last update" in response.text
    assert 'aria-label="Time remaining until competition finish"' in response.text
    assert 'class="refresh-meta"' in response.text
    assert 'data-ends-at="2026-09-04T13:30:00+00:00"' in response.text
    assert "window.setInterval(renderCountdown, 1000)" in response.text
    assert 'id="performance-title">Performance</h2>' in response.text
    assert "Official competition performance" not in response.text
    assert "Verified account evidence" not in response.text
    assert "Updates every 15 seconds" not in response.text
    assert "Built by AOB" in response.text
    assert "https://www.linkedin.com/in/alexobyrne" in response.text
    assert 'class="shell ops-grid overview-grid"' in response.text
    assert 'class="panel performance-overview"' in response.text
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
    assert 'id="account-equity" data-baseline="100000" aria-live="polite"' in response.text
    assert 'id="account-equity-return"' in response.text
    assert 'class="pnl-value positive"' in response.text
    assert "Latest Alpaca equity" not in response.text
    assert "Official P&amp;L" not in response.text
    assert 'fetch("/api/account", {cache: "no-store"})' in response.text
    assert 'id="equity-source-state"' in response.text
    assert "renderAccount(await accountResponse.json())" in response.text
    assert "accountEquityReturn.textContent" in response.text
    assert "8 key gates" in response.text
    assert "refresh();" in response.text
    assert "REPLAY — NOT OFFICIAL P&amp;L" not in response.text
    assert health.status_code == 200
    assert liveness.status_code == 200
    assert liveness.json()["database"] == "ok"
    assert health.json()["database"] == "ok"
    assert passport.status_code == 200
    assert passport.json()["official"] is False
    assert activity.status_code == 200
    assert activity.json()["entries"]
    assert activity.json()["latest_run_id"] == passport.json()["run_id"]


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
