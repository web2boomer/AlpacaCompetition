from fastapi.testclient import TestClient

from money_machine.web import create_app


def test_dashboard_and_health(settings, database) -> None:
    with TestClient(create_app(settings, database)) as client:
        response = client.get("/")
        health = client.get("/api/health")
        passport = client.get("/api/passports/latest")
        activity = client.get("/api/activity")
    assert response.status_code == 200
    assert "Live competition operations" in response.text
    assert "Recent agent activity" in response.text
    assert "Last update" in response.text
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
