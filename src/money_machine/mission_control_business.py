"""Business Performance Protocol v1 values and HTTP transport."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


@dataclass(frozen=True, slots=True)
class Metric:
    name: str
    value: Decimal | int | str
    unit: Literal["currency", "count", "percent", "ratio"]
    kind: Literal["flow", "balance", "gauge"]
    label: str | None = None

    def as_payload(self) -> dict[str, str]:
        payload = {
            "name": self.name,
            "value": str(self.value),
            "unit": self.unit,
            "kind": self.kind,
        }
        if self.label:
            payload["label"] = self.label
        return payload


@dataclass(frozen=True, slots=True)
class Report:
    event_id: str
    project: str
    period_start: datetime
    period_end: datetime
    metrics: tuple[Metric, ...]
    environment: str = "production"
    reporting_basis: Literal["cash", "accrual", "operational"] = "operational"
    report_status: Literal["estimated", "final"] = "estimated"
    currency: str = "USD"
    metadata: dict[str, Any] | None = None

    def as_payload(self, *, occurred_at: datetime | None = None) -> dict[str, Any]:
        _require_aware(self.period_start, "period_start")
        _require_aware(self.period_end, "period_end")
        if self.period_end <= self.period_start:
            raise ValueError("period_end must be later than period_start")
        observed_at = occurred_at or datetime.now(UTC)
        _require_aware(observed_at, "occurred_at")
        return {
            "schema_version": "1.0",
            "event_id": self.event_id,
            "project": self.project,
            "environment": self.environment,
            "occurred_at": observed_at.astimezone(UTC).isoformat(),
            "period_start": self.period_start.astimezone(UTC).isoformat(),
            "period_end": self.period_end.astimezone(UTC).isoformat(),
            "reporting_basis": self.reporting_basis,
            "report_status": self.report_status,
            "currency": self.currency.upper(),
            "metrics": [metric.as_payload() for metric in self.metrics],
            "metadata": self.metadata or {},
        }


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    event_id: str
    duplicate: bool = False


class Client:
    """Small BPP client with bounded retries and duplicate-as-success semantics."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout_seconds: float = 5,
        max_attempts: int = 3,
    ) -> None:
        _require_safe_url(base_url)
        if not token:
            raise ValueError("Mission Control token is missing")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts

    def submit(self, report: Report) -> SubmissionResult:
        body = json.dumps(report.as_payload(), separators=(",", ":")).encode()
        request = Request(
            f"{self.base_url}/v1/business/reports",
            data=body,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "alpaca-competition-business-reporter/1.0",
            },
            method="POST",
        )
        for attempt in range(1, self.max_attempts + 1):
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    payload = json.load(response)
                return SubmissionResult(event_id=str(payload["event_id"]))
            except HTTPError as exc:
                if exc.code == 409:
                    return SubmissionResult(event_id=report.event_id, duplicate=True)
                if not _retryable_status(exc.code) or attempt == self.max_attempts:
                    raise
            except (TimeoutError, URLError):
                if attempt == self.max_attempts:
                    raise
            time.sleep(0.25 * (2 ** (attempt - 1)))
        raise RuntimeError("Mission Control retry loop exhausted")  # pragma: no cover


def _retryable_status(status: int) -> bool:
    return status == 429 or 500 <= status < 600


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")


def _require_safe_url(value: str) -> None:
    parsed = urlsplit(value)
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
        raise ValueError("Mission Control URL must use HTTPS (HTTP is allowed only locally)")
