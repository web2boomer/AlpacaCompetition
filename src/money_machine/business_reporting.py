"""Map persisted competition equity into Mission Control business reports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, Protocol
from urllib.error import HTTPError

import structlog

from money_machine.domain.clock import (
    BASELINE_EQUITY,
    EOD_EQUITY_SNAPSHOT_AT,
    HACKATHON_STARTS_AT,
    SCORING_STARTS_AT,
)
from money_machine.mission_control_business import Client, Metric, Report, SubmissionResult
from money_machine.persistence.repository import AuditRepository, PersistedEquitySnapshot
from money_machine.safety import configured_account_fingerprint
from money_machine.settings import Settings

PROJECT = "alpaca-competition"
COSTS_CONTRACT_VERSION = "v1"
PAPER_PNL_EXCEPTION_NOTE = (
    "Paper-account P&L is reported as real competition P&L for this one-week competition "
    "so equity gains remain visible in Mission Control."
)

logger = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class OperatingCostLineItem:
    cost_key: str
    display_name: str
    category: Literal["data_vendor", "llm_usage", "infra", "observability", "other"]
    cost_type: Literal["recurring", "usage"]
    status: Literal["active", "cancelled", "trial", "paused"]
    amount_usd_monthly: Decimal
    cadence: Literal["monthly", "annual_prorated", "daily_prorated", "variable"]
    proration_rule: str
    effective_start: date
    effective_end: date | None
    source_confidence: Literal["confirmed", "estimated", "list_price"]
    source_type: Literal["receipt", "invoice", "database", "estimate", "manual"]
    evidence_ref: str
    notes: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "cost_key": self.cost_key,
            "display_name": self.display_name,
            "category": self.category,
            "type": self.cost_type,
            "status": self.status,
            "amount_usd_monthly": str(self.amount_usd_monthly),
            "currency": "USD",
            "cadence": self.cadence,
            "proration_rule": self.proration_rule,
            "effective_start": self.effective_start.isoformat(),
            "effective_end": (
                self.effective_end.isoformat() if self.effective_end is not None else None
            ),
            "source_confidence": self.source_confidence,
            "source_type": self.source_type,
            "evidence_ref": self.evidence_ref,
            "notes": self.notes,
        }


# This is deliberately limited to costs whose price is supported by a source that does not
# require guessing an account plan or reconstructing billable usage. Render is excluded because
# Mission Control already attributes the project's Render services directly.
OPERATING_COSTS = (
    OperatingCostLineItem(
        cost_key="alpaca_paper_trading",
        display_name="Alpaca paper trading",
        category="data_vendor",
        cost_type="recurring",
        status="active",
        amount_usd_monthly=Decimal("0.00"),
        cadence="monthly",
        proration_rule="exact",
        effective_start=HACKATHON_STARTS_AT.date(),
        effective_end=None,
        source_confidence="list_price",
        source_type="manual",
        evidence_ref="https://docs.alpaca.markets/us/docs/trading-api#paper-trading",
        notes="Paper trading access only; the separate market-data plan remains unknown.",
    ),
)

# Unknown amounts live outside costs_v1 because amount_usd_monthly is required to be a decimal.
# Keeping the gaps structured prevents an unavailable price from silently becoming zero.
OPERATING_COST_GAPS: tuple[dict[str, Any], ...] = (
    {
        "cost_key": "openai_api_usage",
        "display_name": "OpenAI API",
        "category": "llm_usage",
        "type": "usage",
        "status": "active",
        "cadence": "variable",
        "source_confidence": None,
        "evidence_ref": "openai:organization-costs:api.usage.read-required",
        "gap_reason": (
            "The project key cannot read the OpenAI Costs API and billed usage is not persisted "
            "locally."
        ),
    },
    {
        "cost_key": "alpaca_market_data_subscription",
        "display_name": "Alpaca market data",
        "category": "data_vendor",
        "type": "recurring",
        "status": "active",
        "cadence": "monthly",
        "source_confidence": None,
        "evidence_ref": "alpaca:account-market-data-plan:not-persisted",
        "gap_reason": (
            "The repository does not establish whether the account uses Basic or Algo Trader "
            "Plus market data."
        ),
    },
)


def _cost_metrics() -> tuple[Metric, ...]:
    known_total = sum(
        (item.amount_usd_monthly for item in OPERATING_COSTS if item.status == "active"),
        Decimal("0"),
    )
    return (
        Metric(
            "x_operating_cost_total",
            known_total,
            "currency",
            "flow",
            "Known operating costs (incomplete)",
        ),
    )


def _cost_metadata() -> dict[str, Any]:
    return {
        "costs_contract_version": COSTS_CONTRACT_VERSION,
        "costs_v1": [item.as_payload() for item in OPERATING_COSTS],
        "costs_v1_coverage": "known_costs_only",
        "costs_v1_unknowns": [dict(gap) for gap in OPERATING_COST_GAPS],
        "costs_v1_render_excluded": True,
    }


class ReportClient(Protocol):
    def submit(self, report: Report) -> SubmissionResult: ...


class BusinessReportBuilder:
    def __init__(
        self,
        repository: AuditRepository,
        *,
        interval_minutes: int = 60,
        account_fingerprint: str | None = None,
    ) -> None:
        if interval_minutes < 1:
            raise ValueError("reporting interval must be positive")
        self.repository = repository
        self.interval = timedelta(minutes=interval_minutes)
        self.account_fingerprint = account_fingerprint

    def build(self, *, now: datetime, environment: str = "production") -> Report | None:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("report time must include a timezone")
        report_time = now.astimezone(UTC)
        if report_time >= EOD_EQUITY_SNAPSHOT_AT:
            snapshot = self.repository.latest_official_equity_at_or_before(
                EOD_EQUITY_SNAPSHOT_AT,
                account_fingerprint=self.account_fingerprint,
            )
            if snapshot is None or snapshot.observed_at != EOD_EQUITY_SNAPSHOT_AT:
                return None
            period_end = EOD_EQUITY_SNAPSHOT_AT
            status = "final"
            id_time = EOD_EQUITY_SNAPSHOT_AT
            period_start = SCORING_STARTS_AT
            official_scoring_window = True
        elif report_time < SCORING_STARTS_AT:
            completed_intervals = int((report_time - HACKATHON_STARTS_AT) // self.interval)
            if completed_intervals < 1:
                return None
            period_end = HACKATHON_STARTS_AT + completed_intervals * self.interval
            snapshot = self.repository.latest_pre_scoring_equity_at_or_before(
                period_end,
                account_fingerprint=self.account_fingerprint,
            )
            if snapshot is None:
                return None
            status = "estimated"
            id_time = period_end
            period_start = HACKATHON_STARTS_AT
            official_scoring_window = False
        else:
            completed_intervals = int((report_time - SCORING_STARTS_AT) // self.interval)
            if completed_intervals < 1:
                # Keep Mission Control current across the scoring transition without
                # relabelling weekend telemetry as official competition performance.
                period_end = SCORING_STARTS_AT
                snapshot = self.repository.latest_pre_scoring_equity_at_or_before(
                    period_end,
                    account_fingerprint=self.account_fingerprint,
                )
                official_scoring_window = False
                period_start = HACKATHON_STARTS_AT
            else:
                period_end = SCORING_STARTS_AT + completed_intervals * self.interval
                snapshot = self.repository.latest_official_equity_at_or_before(
                    period_end,
                    account_fingerprint=self.account_fingerprint,
                )
                official_scoring_window = True
                period_start = SCORING_STARTS_AT
            if snapshot is None:
                return None
            status = "estimated"
            id_time = period_end
        return self._report(
            snapshot=snapshot,
            period_start=period_start,
            period_end=period_end,
            id_time=id_time,
            status=status,
            environment=environment,
            official_scoring_window=official_scoring_window,
        )

    @staticmethod
    def _report(
        *,
        snapshot: PersistedEquitySnapshot,
        period_start: datetime,
        period_end: datetime,
        id_time: datetime,
        status: str,
        environment: str,
        official_scoring_window: bool,
    ) -> Report:
        net_profit = snapshot.equity - BASELINE_EQUITY
        return_percent = (net_profit / BASELINE_EQUITY * Decimal("100")).quantize(Decimal("0.0001"))
        suffix = id_time.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        return Report(
            event_id=f"{PROJECT}-business-{suffix}-{status}",
            project=PROJECT,
            period_start=period_start,
            period_end=period_end,
            report_status="final" if status == "final" else "estimated",
            environment=environment,
            currency="USD",
            metrics=(
                Metric(
                    "net_profit",
                    net_profit,
                    "currency",
                    "flow",
                    ("Competition P&L" if official_scoring_window else "Pre-competition paper P&L"),
                ),
                Metric(
                    "portfolio_value",
                    snapshot.portfolio_value,
                    "currency",
                    "balance",
                    "Portfolio value",
                ),
                Metric("cash_balance", snapshot.cash, "currency", "balance", "Cash"),
                Metric(
                    "return_percent",
                    return_percent,
                    "percent",
                    "gauge",
                    (
                        "Competition return"
                        if official_scoring_window
                        else "Pre-competition paper return"
                    ),
                ),
                *_cost_metrics(),
            ),
            metadata={
                "source": (
                    "persisted_official_equity_snapshot"
                    if official_scoring_window
                    else "persisted_pre_scoring_equity_snapshot"
                ),
                "source_snapshot_id": snapshot.id,
                "source_snapshot_observed_at": snapshot.observed_at.astimezone(UTC).isoformat(),
                "pnl_baseline_usd": str(BASELINE_EQUITY),
                "paper_pnl_reported_as_real": True,
                "paper_pnl_exception_note": PAPER_PNL_EXCEPTION_NOTE,
                "official_scoring_window": official_scoring_window,
                "scoring_window_state": ("scoring" if official_scoring_window else "pre_scoring"),
                **_cost_metadata(),
            },
        )


class BusinessReportingOrchestrator:
    def __init__(self, settings: Settings, repository: AuditRepository) -> None:
        self.settings = settings
        self.builder = BusinessReportBuilder(
            repository,
            interval_minutes=settings.mission_control_reporting_interval_minutes,
            account_fingerprint=configured_account_fingerprint(settings),
        )
        self._last_delivered_event_id: str | None = None

    def report_if_due(self, *, now: datetime) -> SubmissionResult | None:
        missing = self._missing_configuration()
        if missing:
            logger.warning("mission_control_reporting_not_configured", missing=missing)
            return None
        if self.settings.mission_control_project != PROJECT:
            logger.warning(
                "mission_control_reporting_project_mismatch",
                configured_project=self.settings.mission_control_project,
            )
            return None
        try:
            report = self.builder.build(
                now=now,
                environment=(
                    self.settings.mission_control_environment or self.settings.app_env.value
                ),
            )
            if report is None or report.event_id == self._last_delivered_event_id:
                return None
            assert self.settings.mission_control_url is not None
            assert self.settings.mission_control_token is not None
            client: ReportClient = Client(
                base_url=self.settings.mission_control_url,
                token=self.settings.mission_control_token.get_secret_value(),
                timeout_seconds=5,
                max_attempts=3,
            )
            result = client.submit(report)
        except HTTPError as exc:
            logger.warning(
                "mission_control_reporting_failed",
                error_type=type(exc).__name__,
                status=exc.code,
            )
            return None
        except Exception as exc:
            logger.warning(
                "mission_control_reporting_failed",
                error_type=type(exc).__name__,
            )
            return None
        self._last_delivered_event_id = report.event_id
        logger.info(
            "mission_control_report_delivered",
            report_id=result.event_id,
            duplicate=result.duplicate,
        )
        return result

    def _missing_configuration(self) -> list[str]:
        missing: list[str] = []
        if not self.settings.mission_control_url:
            missing.append("MISSION_CONTROL_URL")
        if (
            self.settings.mission_control_token is None
            or not self.settings.mission_control_token.get_secret_value()
        ):
            missing.append("MISSION_CONTROL_TOKEN")
        return missing
