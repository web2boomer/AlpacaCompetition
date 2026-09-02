from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import SecretStr

from money_machine.adapters.replay import infer_atm_implied_move
from money_machine.domain.candidates import build_candidates
from money_machine.domain.enums import Action, RunMode
from money_machine.domain.risk import (
    HIGH_CONVICTION_INDEX_PER_STRUCTURE_PCT,
    HIGH_CONVICTION_MAX_DEBIT_TO_WIDTH_RATIO,
    HIGH_CONVICTION_MIN_CONFIDENCE,
    HIGH_CONVICTION_MIN_DIRECTIONAL_REWARD_RISK_RATIO,
    HIGH_CONVICTION_MIN_DIRECTIONAL_TREND_STRENGTH,
    INDEX_CLUSTER_PCT,
    INDEX_PER_STRUCTURE_PCT,
    TOTAL_DEFINED_LOSS_PCT,
)
from money_machine.model_provider import ReplayModelProvider
from money_machine.persistence.repository import PriorMarketObservation
from money_machine.service import AgentService, _directional_policy_exclusions
from money_machine.settings import Settings


async def _market(replay_adapter):
    snapshots = []
    chains = {}
    for symbol in ("SPY", "QQQ", "IWM"):
        chain = await replay_adapter.option_chain(symbol)
        snapshot = await replay_adapter.underlying_snapshot(symbol)
        chains[symbol] = chain
        snapshots.append(infer_atm_implied_move(snapshot, chain))
    return snapshots, build_candidates(snapshots, chains, replay_adapter.observed_at)


class PriorRepository:
    def __init__(self, prior) -> None:
        self.prior = prior

    def prior_market_observation(self, **_kwargs):
        return self.prior


@pytest.mark.asyncio
async def test_condors_are_audit_only_and_confirmed_directional_ids_remain_exact(
    replay_adapter,
) -> None:
    snapshots, report = await _market(replay_adapter)
    now = replay_adapter.observed_at
    prior_snapshot = next(snapshot for snapshot in snapshots if snapshot.symbol == "IWM")
    repository = PriorRepository(
        PriorMarketObservation(
            cycle_at=now - timedelta(minutes=5),
            observed_at=now - timedelta(minutes=5),
            snapshot=prior_snapshot,
        )
    )

    exclusions, evidence = _directional_policy_exclusions(
        report.candidates,
        snapshots=snapshots,
        repository=repository,
        run_id="current",
        mode=RunMode.LIVE,
        now=now,
    )

    condors = [item for item in report.candidates if item.action is Action.INDEX_CONDOR]
    directional = [
        item
        for item in report.candidates
        if item.action in {Action.CALL_DEBIT_SPREAD, Action.PUT_DEBIT_SPREAD}
    ]
    assert condors
    assert directional
    assert all(
        exclusions[item.candidate_id] == ("competition_directional_only_policy",)
        for item in condors
    )
    iwm = next(item for item in directional if item.structure.underlying == "IWM")
    assert iwm.candidate_id not in exclusions
    assert evidence[iwm.candidate_id]["passed"] is True
    assert evidence[iwm.candidate_id]["reason"] == "two_cycle_direction_confirmed"
    assert evidence[iwm.candidate_id]["previous_observed_at"] is not None
    assert set(evidence) == {item.candidate_id for item in report.candidates}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prior_kind", "expected_reason"),
    [
        ("missing", "directional_confirmation_missing_history"),
        ("stale", "directional_confirmation_stale_or_nonconsecutive"),
        ("reversed", "directional_confirmation_direction_reversed"),
        ("weak", "directional_confirmation_previous_below_threshold"),
        ("current_weak", "directional_confirmation_current_below_threshold"),
        ("malformed", "directional_confirmation_malformed_history"),
    ],
)
async def test_directional_confirmation_fails_closed(
    replay_adapter, prior_kind, expected_reason
) -> None:
    snapshots, report = await _market(replay_adapter)
    now = replay_adapter.observed_at
    current = next(snapshot for snapshot in snapshots if snapshot.symbol == "IWM")
    if prior_kind == "current_weak":
        snapshots = [
            snapshot.model_copy(update={"trend_return_pct": Decimal("-0.003")})
            if snapshot.symbol == "IWM"
            else snapshot
            for snapshot in snapshots
        ]
    if prior_kind == "missing":
        prior = None
    elif prior_kind == "malformed":
        prior = PriorMarketObservation(
            cycle_at=now - timedelta(minutes=5),
            observed_at=now - timedelta(minutes=5),
            snapshot=None,
            validation_error="ValidationError",
        )
    else:
        trend = current.trend_return_pct
        if prior_kind == "reversed":
            trend = -trend
        elif prior_kind == "weak":
            trend = Decimal("-0.003")
        gap = 15 if prior_kind == "stale" else 5
        prior = PriorMarketObservation(
            cycle_at=now - timedelta(minutes=gap),
            observed_at=now - timedelta(minutes=gap),
            snapshot=current.model_copy(update={"trend_return_pct": trend}),
        )
    exclusions, evidence = _directional_policy_exclusions(
        report.candidates,
        snapshots=snapshots,
        repository=PriorRepository(prior),
        run_id="current",
        mode=RunMode.LIVE,
        now=now,
    )
    iwm = next(
        item
        for item in report.candidates
        if item.structure.underlying == "IWM" and item.action is Action.PUT_DEBIT_SPREAD
    )
    assert exclusions[iwm.candidate_id] == (expected_reason,)
    assert evidence[iwm.candidate_id]["passed"] is False


@pytest.mark.asyncio
async def test_production_passport_preserves_condors_and_records_policy_exclusions(
    settings, repository, replay_adapter, monkeypatch
) -> None:
    production = Settings(
        app_env="production",
        account_role="competition",
        run_mode="live",
        database_url=settings.database_url,
        alpaca_api_key=SecretStr("present"),
        alpaca_secret_key=SecretStr("present"),
        alpaca_expected_account_id=SecretStr("REPLAY-PAPER-ACCOUNT"),
        client_order_prefix="mm-comp",
    )
    monkeypatch.setattr(repository, "has_managed_orders", lambda *_args: True)
    snapshots, _report = await _market(replay_adapter)
    now = replay_adapter.observed_at
    prior_run, created = repository.begin_run(
        "live:prior", RunMode.LIVE, now - timedelta(minutes=5)
    )
    assert created
    repository.persist_market_observations(
        prior_run,
        source="alpaca_mcp_v2",
        snapshots=[
            snapshot.model_copy(update={"observed_at": now - timedelta(minutes=5)})
            for snapshot in snapshots
        ],
    )
    repository.complete_run(prior_run, completed_at=now - timedelta(minutes=5), passport={})

    outcome = await AgentService(production, repository).run_cycle(
        adapter=replay_adapter,
        model=ReplayModelProvider(),
        now=now,
        mode=RunMode.LIVE,
    )

    assert "candidates" in outcome.passport, outcome.passport
    assert outcome.passport["candidates"]
    condor_ids = {
        item["candidate_id"]
        for item in outcome.passport["candidates"]
        if item["action"] == "index_condor"
    }
    assert condor_ids
    assert all(
        "competition_directional_only_policy"
        in outcome.passport["portfolio_candidate_exclusions"][candidate_id]
        for candidate_id in condor_ids
    )
    assert all(
        candidate_id in outcome.passport["directional_confirmation"] for candidate_id in condor_ids
    )
    assert not outcome.order_submitted


def test_recovery_does_not_change_live_directional_risk_policy() -> None:
    assert Decimal("0.015") == INDEX_PER_STRUCTURE_PCT
    assert Decimal("0.04") == HIGH_CONVICTION_INDEX_PER_STRUCTURE_PCT
    assert Decimal("0.08") == INDEX_CLUSTER_PCT
    assert Decimal("0.10") == TOTAL_DEFINED_LOSS_PCT
    assert Decimal("0.80") == HIGH_CONVICTION_MIN_CONFIDENCE
    assert Decimal("0.005") == HIGH_CONVICTION_MIN_DIRECTIONAL_TREND_STRENGTH
    assert Decimal("2.00") == HIGH_CONVICTION_MIN_DIRECTIONAL_REWARD_RISK_RATIO
    assert Decimal("1") / Decimal("3") == HIGH_CONVICTION_MAX_DEBIT_TO_WIDTH_RATIO
