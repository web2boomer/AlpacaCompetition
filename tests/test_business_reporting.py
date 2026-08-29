from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import SecretStr

from money_machine import business_reporting
from money_machine.business_reporting import (
    PAPER_PNL_EXCEPTION_NOTE,
    PROJECT,
    BusinessReportBuilder,
    BusinessReportingOrchestrator,
)
from money_machine.domain.clock import (
    BASELINE_EQUITY,
    EOD_EQUITY_SNAPSHOT_AT,
    HACKATHON_STARTS_AT,
    SCORING_STARTS_AT,
)
from money_machine.persistence.models import AgentRunORM, EquitySnapshotORM
from money_machine.persistence.repository import AuditRepository
from money_machine.safety import configured_account_fingerprint
from money_machine.settings import Settings


def persist_equity(
    repository: AuditRepository,
    *,
    observed_at: datetime,
    equity: Decimal,
    official: bool = True,
    status: str = "completed",
    fingerprint: str | None = None,
    mode: str | None = None,
) -> None:
    run_id = f"run-{observed_at.timestamp()}-{official}"
    with repository.database.session() as session:
        session.add(
            AgentRunORM(
                id=run_id,
                cycle_key=f"cycle-{run_id}",
                correlation_id=f"correlation-{run_id}",
                mode=mode or ("live" if official else "replay"),
                status=status,
                started_at=observed_at,
                completed_at=observed_at,
                passport_json=({"account": {"fingerprint": fingerprint}} if fingerprint else None),
            )
        )
        session.add(
            EquitySnapshotORM(
                agent_run_id=run_id,
                observed_at=observed_at,
                equity=equity,
                cash=Decimal("75321.09"),
                buying_power=Decimal("150642.18"),
                portfolio_value=equity,
                realized_pl=Decimal("0"),
                unrealized_pl=Decimal("0"),
                peak_equity=max(BASELINE_EQUITY, equity),
                drawdown=max(Decimal("0"), BASELINE_EQUITY - equity),
                official=official,
            )
        )


def test_builder_maps_persisted_paper_equity_to_real_competition_pnl(
    repository: AuditRepository,
) -> None:
    boundary = SCORING_STARTS_AT + timedelta(hours=2)
    persist_equity(repository, observed_at=boundary, equity=Decimal("101234.56"))

    report = BusinessReportBuilder(repository).build(now=boundary + timedelta(minutes=17))

    assert report is not None
    assert report.project == PROJECT == "alpaca-competition"
    assert report.period_start == SCORING_STARTS_AT
    assert report.period_end == boundary
    assert report.report_status == "estimated"
    metrics = {metric.name: metric for metric in report.metrics}
    assert metrics["net_profit"].value == Decimal("1234.56")
    assert metrics["net_profit"].kind == "flow"
    assert metrics["portfolio_value"].value == Decimal("101234.56")
    assert metrics["cash_balance"].value == Decimal("75321.09")
    assert metrics["return_percent"].value == Decimal("1.2346")
    assert report.metadata is not None
    assert report.metadata["paper_pnl_reported_as_real"] is True
    assert report.metadata["paper_pnl_exception_note"] == PAPER_PNL_EXCEPTION_NOTE
    assert report.metadata["official_scoring_window"] is True


def test_builder_reports_verified_pre_scoring_equity_without_calling_it_official(
    repository: AuditRepository,
) -> None:
    boundary = HACKATHON_STARTS_AT + timedelta(hours=2)
    fingerprint = "verified-account"
    persist_equity(
        repository,
        observed_at=boundary,
        equity=Decimal("100125.50"),
        official=False,
        fingerprint=fingerprint,
        mode="live",
    )

    report = BusinessReportBuilder(repository, account_fingerprint=fingerprint).build(
        now=boundary + timedelta(minutes=17)
    )

    assert report is not None
    assert report.period_start == HACKATHON_STARTS_AT
    assert report.period_end == boundary
    assert report.report_status == "estimated"
    metrics = {metric.name: metric for metric in report.metrics}
    assert metrics["net_profit"].value == Decimal("125.50")
    assert metrics["net_profit"].label == "Pre-competition paper P&L"
    assert report.metadata is not None
    assert report.metadata["official_scoring_window"] is False
    assert report.metadata["scoring_window_state"] == "pre_scoring"


def test_pre_scoring_report_requires_completed_live_verified_account_snapshot(
    repository: AuditRepository,
) -> None:
    boundary = HACKATHON_STARTS_AT + timedelta(hours=1)
    fingerprint = "verified-account"
    persist_equity(
        repository,
        observed_at=boundary - timedelta(minutes=1),
        equity=Decimal("101000"),
        official=False,
        fingerprint="wrong-account",
        mode="live",
    )
    persist_equity(
        repository,
        observed_at=boundary,
        equity=Decimal("102000"),
        official=False,
        fingerprint=fingerprint,
        status="failed",
        mode="live",
    )

    assert (
        BusinessReportBuilder(repository, account_fingerprint=fingerprint).build(
            now=boundary + timedelta(minutes=1)
        )
        is None
    )


def test_scoring_transition_reports_last_pre_scoring_checkpoint_as_non_official(
    repository: AuditRepository,
) -> None:
    fingerprint = "verified-account"
    persist_equity(
        repository,
        observed_at=SCORING_STARTS_AT - timedelta(minutes=5),
        equity=Decimal("100010"),
        official=False,
        fingerprint=fingerprint,
        mode="live",
    )
    persist_equity(
        repository,
        observed_at=SCORING_STARTS_AT,
        equity=Decimal("100020"),
        fingerprint=fingerprint,
    )

    report = BusinessReportBuilder(repository, account_fingerprint=fingerprint).build(
        now=SCORING_STARTS_AT + timedelta(minutes=5)
    )

    assert report is not None
    assert report.period_end == SCORING_STARTS_AT
    assert {metric.name: metric.value for metric in report.metrics}["net_profit"] == Decimal(
        "10.00"
    )
    assert report.metadata is not None
    assert report.metadata["official_scoring_window"] is False


def test_report_id_is_stable_within_the_reporting_interval(
    repository: AuditRepository,
) -> None:
    boundary = SCORING_STARTS_AT + timedelta(hours=3)
    persist_equity(repository, observed_at=boundary, equity=Decimal("99900.00"))
    builder = BusinessReportBuilder(repository)

    first = builder.build(now=boundary + timedelta(minutes=5))
    retry = builder.build(now=boundary + timedelta(minutes=55))

    assert first is not None and retry is not None
    assert retry.event_id == first.event_id
    assert (
        retry.as_payload(occurred_at=boundary)["metrics"]
        == first.as_payload(occurred_at=boundary)["metrics"]
    )


def test_builder_omits_replay_snapshots(repository: AuditRepository) -> None:
    boundary = SCORING_STARTS_AT + timedelta(hours=1)
    persist_equity(
        repository,
        observed_at=boundary,
        equity=Decimal("110000"),
        official=False,
    )
    assert BusinessReportBuilder(repository).build(now=boundary + timedelta(minutes=1)) is None


def test_final_requires_the_authoritative_eod_snapshot(
    repository: AuditRepository,
) -> None:
    close_snapshot = EOD_EQUITY_SNAPSHOT_AT
    persist_equity(repository, observed_at=close_snapshot, equity=Decimal("103000"))
    persist_equity(
        repository,
        observed_at=close_snapshot + timedelta(minutes=5),
        equity=Decimal("104000"),
    )

    report = BusinessReportBuilder(repository).build(
        now=EOD_EQUITY_SNAPSHOT_AT + timedelta(minutes=15)
    )

    assert report is not None
    assert report.report_status == "final"
    assert report.period_end == close_snapshot
    assert {metric.name: metric.value for metric in report.metrics}["net_profit"] == Decimal(
        "3000.00"
    )
    assert report.event_id.endswith("-final")


def test_final_is_omitted_without_exact_eod_checkpoint(repository: AuditRepository) -> None:
    persist_equity(
        repository,
        observed_at=EOD_EQUITY_SNAPSHOT_AT - timedelta(minutes=1),
        equity=Decimal("103000"),
    )
    assert (
        BusinessReportBuilder(repository).build(now=EOD_EQUITY_SNAPSHOT_AT + timedelta(minutes=1))
        is None
    )


def test_report_payload_serializes_utc_and_decimals_as_strings(
    repository: AuditRepository,
) -> None:
    boundary = SCORING_STARTS_AT + timedelta(hours=4)
    persist_equity(repository, observed_at=boundary, equity=Decimal("100001.10"))
    report = BusinessReportBuilder(repository).build(now=boundary + timedelta(minutes=1))

    assert report is not None
    payload = report.as_payload(occurred_at=datetime(2026, 8, 29, 12, tzinfo=UTC))
    assert payload["period_start"].endswith("+00:00")
    assert payload["period_end"].endswith("+00:00")
    assert all(isinstance(metric["value"], str) for metric in payload["metrics"])


def test_orchestrator_delivers_once_per_event_and_contains_failures(
    repository: AuditRepository,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary = SCORING_STARTS_AT + timedelta(hours=5)
    configured = settings.model_copy(
        update={
            "mission_control_url": "https://mc.example.com",
            "mission_control_project": PROJECT,
            "mission_control_token": SecretStr("must-not-appear"),
        }
    )
    persist_equity(
        repository,
        observed_at=boundary,
        equity=Decimal("100500"),
        fingerprint=configured_account_fingerprint(configured),
    )
    calls = 0

    def delivered(
        _client: business_reporting.Client, _report: business_reporting.Report
    ) -> business_reporting.SubmissionResult:
        nonlocal calls
        calls += 1
        return business_reporting.SubmissionResult(_report.event_id)

    monkeypatch.setattr(business_reporting.Client, "submit", delivered)
    orchestrator = BusinessReportingOrchestrator(configured, repository)

    assert orchestrator.report_if_due(now=boundary + timedelta(minutes=1)) is not None
    assert orchestrator.report_if_due(now=boundary + timedelta(minutes=2)) is None
    assert calls == 1

    def failed(
        _client: business_reporting.Client, _report: business_reporting.Report
    ) -> business_reporting.SubmissionResult:
        raise RuntimeError("transport failed")

    monkeypatch.setattr(business_reporting.Client, "submit", failed)
    retrying = BusinessReportingOrchestrator(configured, repository)
    assert retrying.report_if_due(now=boundary + timedelta(minutes=3)) is None
