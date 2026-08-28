from pathlib import Path

import pytest

from money_machine.adapters.replay import ReplayAlpacaAdapter
from money_machine.development_acceptance import run_development_round_trip
from money_machine.persistence.database import Database
from money_machine.persistence.repository import AuditRepository
from money_machine.settings import Settings


@pytest.mark.asyncio
async def test_development_round_trip_opens_closes_and_records_evidence(
    tmp_path: Path,
) -> None:
    settings = Settings(
        app_env="development",
        account_role="development",
        run_mode="live",
        alpaca_api_key="test-key",
        alpaca_secret_key="test-secret",
        alpaca_expected_account_id="REPLAY-PAPER-ACCOUNT",
        database_url=f"sqlite:///{tmp_path / 'round-trip.db'}",
    )
    database = Database(settings.database_url)
    database.create_all_for_tests()
    repository = AuditRepository(database)
    adapter = ReplayAlpacaAdapter()

    report = await run_development_round_trip(
        settings,
        repository,
        adapter,
        now=adapter.observed_at,
    )

    assert report.passed
    assert report.opened
    assert report.closed
    assert report.returned_flat
    assert len(adapter.submitted_requests) == 2
    assert not adapter.submitted_requests[0].is_closing
    assert adapter.submitted_requests[1].is_closing
    assert repository.development_round_trip_verified()
