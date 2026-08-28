from pathlib import Path

import pytest
import pytest_asyncio

from money_machine.adapters.replay import ReplayAlpacaAdapter, infer_atm_implied_move
from money_machine.domain.candidates import build_candidates
from money_machine.persistence.database import Database
from money_machine.persistence.repository import AuditRepository
from money_machine.settings import Settings


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-development-order-integration",
        action="store_true",
        default=False,
        help="explicitly authorize the development-account paper round-trip integration test",
    )


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.delenv("EXECUTION_ENABLED", raising=False)
    return Settings(database_url=f"sqlite:///{tmp_path / 'test.db'}", run_mode="replay")


@pytest.fixture
def database(settings: Settings) -> Database:
    db = Database(settings.database_url)
    db.create_all_for_tests()
    return db


@pytest.fixture
def repository(database: Database) -> AuditRepository:
    return AuditRepository(database)


@pytest.fixture
def replay_adapter() -> ReplayAlpacaAdapter:
    return ReplayAlpacaAdapter()


@pytest_asyncio.fixture
async def replay_candidate(replay_adapter: ReplayAlpacaAdapter):
    snapshots = []
    chains = {}
    for symbol in ("SPY", "QQQ", "IWM"):
        chain = await replay_adapter.option_chain(symbol)
        snapshot = await replay_adapter.underlying_snapshot(symbol)
        chains[symbol] = chain
        snapshots.append(infer_atm_implied_move(snapshot, chain))
    report = build_candidates(snapshots, chains, replay_adapter.observed_at)
    assert report.candidates
    return report.candidates[0]
