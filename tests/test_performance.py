from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from fastapi.testclient import TestClient
from pydantic import SecretStr

from money_machine.domain.clock import EOD_EQUITY_SNAPSHOT_AT, SCORING_STARTS_AT
from money_machine.persistence.models import AgentRunORM, EquitySnapshotORM
from money_machine.persistence.repository import AuditRepository
from money_machine.settings import Settings
from money_machine.web import create_app


def persist_performance_row(
    repository: AuditRepository,
    *,
    observed_at,
    equity: str,
    fingerprint: str,
    official: bool = True,
    positions: int = 0,
    orders: int = 0,
) -> None:
    run_id = str(uuid4())
    with repository.database.session() as session:
        session.add(
            AgentRunORM(
                id=run_id,
                cycle_key=f"performance:{run_id}",
                correlation_id=str(uuid4()),
                mode="live",
                status="completed",
                started_at=observed_at,
                completed_at=observed_at,
                passport_json={
                    "official": official,
                    "production_account": True,
                    "account": {
                        "fingerprint": fingerprint,
                        "open_position_count": positions,
                        "working_order_count": orders,
                        "broker_confirmed_flat": positions == 0 and orders == 0,
                    },
                },
            )
        )
        value = Decimal(equity)
        session.add(
            EquitySnapshotORM(
                agent_run_id=run_id,
                observed_at=observed_at,
                equity=value,
                cash=value,
                buying_power=value * 2,
                portfolio_value=value,
                realized_pl=Decimal("0"),
                unrealized_pl=Decimal("0"),
                peak_equity=value,
                drawdown=Decimal("0"),
                official=official,
            )
        )


def test_official_performance_uses_only_verified_account_and_scoring_window(
    repository: AuditRepository,
) -> None:
    target = "verified-target"
    persist_performance_row(
        repository,
        observed_at=SCORING_STARTS_AT - timedelta(days=2),
        equity="180000",
        fingerprint=target,
    )
    persist_performance_row(
        repository,
        observed_at=SCORING_STARTS_AT,
        equity="100000",
        fingerprint=target,
    )
    persist_performance_row(
        repository,
        observed_at=SCORING_STARTS_AT + timedelta(hours=2),
        equity="102000",
        fingerprint=target,
    )
    persist_performance_row(
        repository,
        observed_at=EOD_EQUITY_SNAPSHOT_AT,
        equity="101000",
        fingerprint=target,
        positions=1,
        orders=2,
    )
    persist_performance_row(
        repository,
        observed_at=SCORING_STARTS_AT + timedelta(hours=3),
        equity="250000",
        fingerprint="different-account",
    )
    persist_performance_row(
        repository,
        observed_at=EOD_EQUITY_SNAPSHOT_AT + timedelta(seconds=1),
        equity="190000",
        fingerprint=target,
    )

    summary = repository.competition_performance_summary(
        account_fingerprint=target,
        now=EOD_EQUITY_SNAPSHOT_AT + timedelta(hours=1),
    )

    assert summary["latest_equity"] == "101000.00"
    assert summary["dollar_pnl"] == "1000.00"
    assert summary["percentage_return"] == "1.0000"
    assert summary["peak_equity"] == "102000.00"
    assert summary["maximum_drawdown"] == "1000.00"
    assert summary["open_position_count"] == 1
    assert summary["working_order_count"] == 2
    assert summary["broker_confirmed_flat"] is False
    assert summary["result_status"] == "final_eod_snapshot"
    assert len(repository.official_equity_curve(account_fingerprint=target)) == 3


def test_performance_endpoint_is_redacted_and_read_only(database) -> None:
    settings = Settings(
        app_env="production",
        account_role="competition",
        run_mode="live",
        database_url=str(database.engine.url),
        alpaca_api_key=SecretStr("api-key-must-not-appear"),
        alpaca_secret_key=SecretStr("secret-must-not-appear"),
        alpaca_expected_account_id=SecretStr("private-account-id"),
    )
    repository = AuditRepository(database)
    from money_machine.safety import configured_account_fingerprint

    fingerprint = configured_account_fingerprint(settings)
    assert fingerprint is not None
    persist_performance_row(
        repository,
        observed_at=EOD_EQUITY_SNAPSHOT_AT,
        equity="101500",
        fingerprint=fingerprint,
    )

    with TestClient(create_app(settings, database)) as client:
        response = client.get("/api/performance")
        final = client.get("/api/performance/final")

    assert response.status_code == 200
    assert final.status_code == 200
    body = response.text
    assert fingerprint not in body
    assert "private-account-id" not in body
    assert "api-key-must-not-appear" not in body
    assert "secret-must-not-appear" not in body
    assert response.json()["latest_equity"] == "101500.00"
