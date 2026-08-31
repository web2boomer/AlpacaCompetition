import pytest
from fastapi.testclient import TestClient

from money_machine.web import create_app


def test_account_api_returns_broker_state_without_caching(settings, database) -> None:
    with TestClient(create_app(settings, database)) as client:
        response = client.get("/api/account")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert response.json() == {
        "status": "ok",
        "equity": "100420.00",
        "pnl": "420.00",
        "observed_at": response.json()["observed_at"],
        "cash": "100420.00",
        "buying_power": "200840.00",
        "portfolio_value": "100420.00",
        "realized_pl": "320.00",
        "unrealized_pl": "100.00",
        "open_position_count": 0,
        "working_order_count": 0,
        "broker_confirmed_flat": True,
    }


def test_account_api_degrades_without_affecting_health(
    settings, database, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail_account_read(_settings) -> dict[str, object]:
        raise TimeoutError

    monkeypatch.setattr("money_machine.web._broker_account_payload", fail_account_read)
    with TestClient(create_app(settings, database)) as client:
        account = client.get("/api/account")
        health = client.get("/api/health")

    assert account.status_code == 503
    assert account.headers["cache-control"] == "no-store, max-age=0"
    assert account.json()["status"] == "degraded"
    assert account.json()["equity"] is None
    assert health.status_code == 200
    assert health.json()["status"] == "healthy"
