import json
from datetime import UTC, datetime
from decimal import Decimal
from types import TracebackType
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from money_machine import mission_control_business as client


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback

    def read(self, _amount: int | None = None) -> bytes:
        return json.dumps(self.payload).encode()


def report() -> client.Report:
    return client.Report(
        event_id="alpaca-competition-business-1-estimated",
        project="alpaca-competition",
        period_start=datetime(2026, 8, 28, 13, 30, tzinfo=UTC),
        period_end=datetime(2026, 8, 28, 14, 30, tzinfo=UTC),
        metrics=(client.Metric("net_profit", Decimal("12.34"), "currency", "flow"),),
    )


def test_client_sends_bearer_authenticated_decimal_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Request, timeout: float) -> FakeResponse:
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse({"event_id": report().event_id})

    monkeypatch.setattr(client, "urlopen", fake_urlopen)
    result = client.Client(base_url="https://mc.example.com", token="secret").submit(report())

    request: Request = captured["request"]
    payload = json.loads(request.data or b"{}")
    assert result.event_id == report().event_id
    assert request.get_header("Authorization") == "Bearer secret"
    assert payload["metrics"][0]["value"] == "12.34"
    assert captured["timeout"] == 5


def test_client_retries_5xx_and_treats_409_as_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def fake_urlopen(_request: Request, timeout: float) -> FakeResponse:
        nonlocal calls
        del timeout
        calls += 1
        status = 500 if calls == 1 else 409
        raise HTTPError("https://mc.example.com", status, "failed", {}, None)

    monkeypatch.setattr(client, "urlopen", fake_urlopen)
    monkeypatch.setattr(client.time, "sleep", lambda _delay: None)

    result = client.Client(base_url="https://mc.example.com", token="secret").submit(report())

    assert result.duplicate
    assert calls == 2


def test_client_does_not_retry_validation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def fake_urlopen(_request: Request, timeout: float) -> FakeResponse:
        nonlocal calls
        del timeout
        calls += 1
        raise HTTPError("https://mc.example.com", 422, "invalid", {}, None)

    monkeypatch.setattr(client, "urlopen", fake_urlopen)

    with pytest.raises(HTTPError):
        client.Client(base_url="https://mc.example.com", token="secret").submit(report())
    assert calls == 1
