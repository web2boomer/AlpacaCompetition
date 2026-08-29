from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from money_machine.adapters.replay import infer_atm_implied_move
from money_machine.domain.candidates import build_candidates
from money_machine.domain.enums import Side
from money_machine.domain.events import scheduled_macro_event_risk
from money_machine.domain.options import calculate_maximum_loss, validate_defined_risk


@pytest.mark.asyncio
async def test_replay_builds_only_defined_risk_candidates(replay_adapter) -> None:
    snapshots = []
    chains = {}
    for symbol in ("SPY", "QQQ", "IWM"):
        chain = await replay_adapter.option_chain(symbol)
        snapshot = await replay_adapter.underlying_snapshot(symbol)
        snapshots.append(infer_atm_implied_move(snapshot, chain))
        chains[symbol] = chain
    report = build_candidates(snapshots, chains, replay_adapter.observed_at)
    assert len(report.candidates) >= 3
    for candidate in report.candidates:
        validate_defined_risk(candidate.structure)
        assert {leg.side for leg in candidate.structure.legs} == {Side.BUY, Side.SELL}
        assert candidate.structure.maximum_loss > 0


@pytest.mark.asyncio
async def test_condor_maximum_loss_geometry(replay_candidate) -> None:
    structure = replay_candidate.structure
    calculated = calculate_maximum_loss(structure.strategy, structure.legs, structure.net_price)
    assert calculated == structure.maximum_loss
    assert calculated <= Decimal("500")


@pytest.mark.asyncio
async def test_naked_or_missing_leg_rejected(replay_candidate) -> None:
    invalid = replay_candidate.structure.model_copy(
        update={"legs": replay_candidate.structure.legs[:-1]}
    )
    with pytest.raises(ValueError, match="two-leg spreads and four-leg condors"):
        validate_defined_risk(invalid)


@pytest.mark.asyncio
async def test_stale_chain_returns_no_candidates(replay_adapter) -> None:
    snapshots = []
    chains = {}
    for symbol in ("SPY", "QQQ", "IWM"):
        chain = await replay_adapter.option_chain(symbol)
        snapshots.append(await replay_adapter.underlying_snapshot(symbol))
        chains[symbol] = chain
    report = build_candidates(snapshots, chains, replay_adapter.observed_at + timedelta(minutes=3))
    assert report.candidates == ()
    assert all("stale" in " ".join(reasons) for reasons in report.rejections.values())


@pytest.mark.asyncio
async def test_incomplete_chain_returns_no_candidate(replay_adapter) -> None:
    snapshot = await replay_adapter.underlying_snapshot("SPY")
    report = build_candidates([snapshot], {"SPY": []}, replay_adapter.observed_at)
    assert report.candidates == ()
    assert "empty" in " ".join(report.rejections["SPY"])


def test_competition_macro_release_blocks_crossing_hold_and_cools_down() -> None:
    assert scheduled_macro_event_risk(datetime(2026, 9, 1, 13, 30, tzinfo=UTC))
    assert scheduled_macro_event_risk(datetime(2026, 9, 1, 14, 15, tzinfo=UTC))
    assert not scheduled_macro_event_risk(datetime(2026, 9, 1, 14, 31, tzinfo=UTC))
