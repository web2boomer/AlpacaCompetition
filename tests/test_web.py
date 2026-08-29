from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import desc, select

from money_machine.persistence.models import AgentRunORM, EquitySnapshotORM
from money_machine.persistence.repository import AuditRepository
from money_machine.web import create_app


def test_dashboard_and_health(settings, database) -> None:
    with TestClient(create_app(settings, database)) as client:
        response = client.get("/")
        health = client.get("/api/health")
        passport = client.get("/api/passports/latest")
        activity = client.get("/api/activity")
    assert response.status_code == 200
    assert "Money Machine" in response.text
    assert "Recent agent activity" in response.text
    assert "Last update" in response.text
    assert "System health" in response.text
    assert "https://aob.io" in response.text
    assert '<header class="nav shell">' not in response.text
    assert "window.setInterval(refresh, intervalMs)" in response.text
    assert "REPLAY — NOT OFFICIAL P&amp;L" in response.text
    assert health.status_code == 200
    assert health.json()["database"] == "ok"
    assert passport.status_code == 200
    assert passport.json()["official"] is False
    assert activity.status_code == 200
    assert activity.json()["entries"]
    assert activity.json()["latest_run_id"] == passport.json()["run_id"]


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
