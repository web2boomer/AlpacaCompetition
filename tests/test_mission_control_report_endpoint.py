from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from money_machine.domain.clock import EOD_EQUITY_SNAPSHOT_AT, SCORING_STARTS_AT
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


@pytest.mark.parametrize(
    ("snapshot_time", "request_time", "expected_status"),
    [
        (
            SCORING_STARTS_AT + timedelta(hours=2),
            SCORING_STARTS_AT + timedelta(hours=2),
            "estimated",
        ),
        (EOD_EQUITY_SNAPSHOT_AT, EOD_EQUITY_SNAPSHOT_AT + timedelta(days=7), "final"),
    ],
)
def test_mission_control_report_returns_current_payload(
    settings, database, repository, snapshot_time, request_time, expected_status
) -> None:
    configured = settings.model_copy(update={"mission_control_token": SecretStr("project-token")})
    persist_equity(repository, observed_at=snapshot_time, equity=Decimal("101234.56"))
    with (
        patch("money_machine.web.datetime", wraps=datetime) as clock,
        TestClient(create_app(configured, database)) as client,
    ):
        clock.now.return_value = request_time
        response = client.get(
            "/internal/mission_control/report",
            headers={"Authorization": "Bearer project-token"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["project"] == "alpaca-competition"
    assert body["schema_version"] == "1.0"
    assert body["report_status"] == expected_status
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
