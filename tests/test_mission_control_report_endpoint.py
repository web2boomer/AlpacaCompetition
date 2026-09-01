from datetime import timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from pydantic import SecretStr

from money_machine.domain.clock import SCORING_STARTS_AT
from money_machine.web import create_app
from test_business_reporting import persist_equity


def test_mission_control_report_requires_bearer(settings, database) -> None:
    with TestClient(create_app(settings, database)) as client:
        missing = client.get("/internal/mission_control/report")
        wrong = client.get(
            "/internal/mission_control/report",
            headers={"Authorization": "Bearer wrong-token"},
        )
    assert missing.status_code == 401
    assert missing.content == b""
    assert wrong.status_code == 401
    assert wrong.content == b""


def test_mission_control_report_returns_current_payload(settings, database, repository) -> None:
    configured = settings.model_copy(update={"mission_control_token": SecretStr("project-token")})
    boundary = SCORING_STARTS_AT + timedelta(hours=2)
    persist_equity(repository, observed_at=boundary, equity=Decimal("101234.56"))
    with TestClient(create_app(configured, database)) as client:
        response = client.get(
            "/internal/mission_control/report",
            headers={"Authorization": "Bearer project-token"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["project"] == "alpaca-competition"
    assert body["schema_version"] == "1.0"
    metrics = {metric["name"]: metric["value"] for metric in body["metrics"]}
    assert metrics["net_profit"] == "1234.56"
    assert metrics["portfolio_value"] == "101234.56"
    assert "project-token" not in response.text


def test_mission_control_report_returns_204_when_empty(settings, database) -> None:
    configured = settings.model_copy(update={"mission_control_token": SecretStr("project-token")})
    with TestClient(create_app(configured, database)) as client:
        response = client.get(
            "/internal/mission_control/report",
            headers={"Authorization": "Bearer project-token"},
        )
    assert response.status_code == 204
    assert response.content == b""
