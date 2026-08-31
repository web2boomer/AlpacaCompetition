from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from money_machine.adapters.replay import infer_atm_implied_move
from money_machine.domain.candidates import DIRECTIONAL_SPREAD_WIDTH, build_candidates
from money_machine.domain.enums import Action, ExecutionState, OptionRight, Regime, Side
from money_machine.domain.events import scheduled_macro_event_risk
from money_machine.domain.options import calculate_maximum_loss, validate_defined_risk
from money_machine.domain.risk import evaluate_risk
from money_machine.domain.schemas import ModelDecision, RiskContext


async def iwm_directional_inputs(replay_adapter, *, bullish: bool):
    snapshot = await replay_adapter.underlying_snapshot("IWM")
    snapshot = snapshot.model_copy(
        update={
            "trend_return_pct": Decimal("0.0125") if bullish else Decimal("-0.0125"),
            "realized_move_pct": Decimal("0.02"),
            "implied_move_pct": Decimal("0.01"),
        }
    )
    chain = await replay_adapter.option_chain("IWM")
    if bullish:
        template = next(
            quote
            for quote in chain
            if quote.right is OptionRight.CALL and quote.strike == Decimal("303")
        )
        chain.append(
            template.model_copy(
                update={
                    "symbol": "IWM260904C00300000",
                    "strike": Decimal("300"),
                }
            )
        )
    return snapshot, chain


def only_directional_candidate(report):
    candidates = [
        candidate
        for candidate in report.candidates
        if candidate.action in {Action.CALL_DEBIT_SPREAD, Action.PUT_DEBIT_SPREAD}
    ]
    assert len(candidates) == 1
    return candidates[0]


def model_decision(candidate, *, confidence: float = 0.79) -> ModelDecision:
    return ModelDecision(
        regime=(
            Regime.DIRECTIONAL_UP
            if candidate.action is Action.CALL_DEBIT_SPREAD
            else Regime.DIRECTIONAL_DOWN
        ),
        action=candidate.action,
        candidate_id=candidate.candidate_id,
        confidence=confidence,
        thesis="IWM direction agrees with deterministic trend",
        evidence=("trend and structure gates pass",),
        invalidation=("trend reverses",),
        maximum_holding_minutes=60,
    )


def risk_context(**updates) -> RiskContext:
    values = {
        "now": "2026-08-28T15:05:00Z",
        "execution_state": ExecutionState.FULL_EXECUTION,
        "equity": Decimal("100000"),
        "start_of_day_equity": Decimal("100000"),
        "peak_equity": Decimal("100000"),
        "total_open_defined_loss": Decimal("0"),
        "index_cluster_defined_loss": Decimal("0"),
        "open_alpha_structures": 0,
        "pending_underlyings": frozenset(),
        "open_underlyings": frozenset(),
        "kill_switch_active": False,
        "reconciliation_clean": True,
    }
    values.update(updates)
    return RiskContext(**values)


def risk_check(result, name):
    return next(check for check in result.checks if check.name == name)


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bullish", "expected_action", "expected_right"),
    [
        (True, Action.CALL_DEBIT_SPREAD, OptionRight.CALL),
        (False, Action.PUT_DEBIT_SPREAD, OptionRight.PUT),
    ],
)
async def test_iwm_directional_compiler_builds_exact_defined_risk_spreads(
    replay_adapter, bullish, expected_action, expected_right
) -> None:
    snapshot, chain = await iwm_directional_inputs(replay_adapter, bullish=bullish)
    report = build_candidates([snapshot], {"IWM": chain}, replay_adapter.observed_at)
    candidate = only_directional_candidate(report)

    assert candidate.action is expected_action
    assert candidate.structure.underlying == "IWM"
    assert {leg.right for leg in candidate.structure.legs} == {expected_right}
    strikes = [leg.strike for leg in candidate.structure.legs]
    assert abs(strikes[0] - strikes[1]) == DIRECTIONAL_SPREAD_WIDTH
    assert candidate.structure.maximum_loss == candidate.structure.net_price * Decimal("100")
    assert candidate.structure.maximum_profit == (
        DIRECTIONAL_SPREAD_WIDTH - candidate.structure.net_price
    ) * Decimal("100")
    validate_defined_risk(candidate.structure)


@pytest.mark.asyncio
async def test_iwm_directional_compiler_rejects_missing_exact_wing(replay_adapter) -> None:
    snapshot, chain = await iwm_directional_inputs(replay_adapter, bullish=False)
    chain = [quote for quote in chain if quote.strike != Decimal("290")]

    report = build_candidates([snapshot], {"IWM": chain}, replay_adapter.observed_at)

    assert report.candidates == ()
    assert "missing directional short wing at configured width" in report.rejections["IWM"]


@pytest.mark.asyncio
async def test_iwm_directional_compiler_rejects_illiquid_wing(replay_adapter) -> None:
    snapshot, chain = await iwm_directional_inputs(replay_adapter, bullish=False)
    chain = [
        quote.model_copy(update={"volume": 0})
        if quote.right is OptionRight.PUT and quote.strike == Decimal("290")
        else quote
        for quote in chain
    ]

    report = build_candidates([snapshot], {"IWM": chain}, replay_adapter.observed_at)

    assert report.candidates == ()
    assert "directional spread liquidity gate failed" in report.rejections["IWM"]


@pytest.mark.asyncio
async def test_iwm_directional_compiler_honors_event_veto(replay_adapter) -> None:
    snapshot, chain = await iwm_directional_inputs(replay_adapter, bullish=False)
    snapshot = snapshot.model_copy(update={"event_risk": True})

    report = build_candidates([snapshot], {"IWM": chain}, replay_adapter.observed_at)

    assert report.candidates == ()
    assert "event risk vetoed directional structure" in report.rejections["IWM"]


@pytest.mark.asyncio
async def test_iwm_directional_candidate_sizes_with_existing_index_caps(replay_adapter) -> None:
    snapshot, chain = await iwm_directional_inputs(replay_adapter, bullish=False)
    candidate = only_directional_candidate(
        build_candidates([snapshot], {"IWM": chain}, replay_adapter.observed_at)
    )

    result = evaluate_risk(model_decision(candidate), candidate, risk_context())

    assert result.approved
    assert result.quantity == int(Decimal("1000") // candidate.structure.maximum_loss)
    assert result.awarded_risk == candidate.structure.maximum_loss * result.quantity
    assert result.awarded_risk <= Decimal("1000")
    assert risk_check(result, "effective_per_structure_percent").actual == "0.01"


@pytest.mark.asyncio
async def test_iwm_directional_legacy_exception_forces_standard_tier(replay_adapter) -> None:
    snapshot, chain = await iwm_directional_inputs(replay_adapter, bullish=False)
    chain = [
        quote.model_copy(update={"bid": Decimal("2.15"), "ask": Decimal("2.25")})
        if quote.right is OptionRight.PUT and quote.strike == Decimal("295")
        else quote.model_copy(update={"bid": Decimal("0.95"), "ask": Decimal("1.05")})
        if quote.right is OptionRight.PUT and quote.strike == Decimal("290")
        else quote
        for quote in chain
    ]
    candidate = only_directional_candidate(
        build_candidates([snapshot], {"IWM": chain}, replay_adapter.observed_at)
    )

    result = evaluate_risk(
        model_decision(candidate, confidence=0.99),
        candidate,
        risk_context(
            open_alpha_structures=5,
            open_underlyings=frozenset({"QQQ"}),
            index_cluster_defined_loss=Decimal("1979"),
            total_open_defined_loss=Decimal("1979"),
        ),
    )

    assert result.approved
    assert candidate.structure.maximum_loss == Decimal("120.00")
    assert candidate.structure.maximum_profit == Decimal("380.00")
    assert result.quantity == 8
    assert result.awarded_risk == Decimal("960.00")
    assert risk_check(result, "effective_per_structure_percent").actual == "0.01"
    assert "applied=true" in risk_check(result, "legacy_qqq_diversification_exception").actual
    assert "applied=false" in risk_check(result, "high_conviction_index_tier").actual
