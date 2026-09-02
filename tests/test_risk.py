from decimal import Decimal

import pytest

from money_machine.domain.enums import (
    Action,
    ExecutionState,
    PositionIntent,
    RiskReason,
    Side,
)
from money_machine.domain.risk import (
    COMPETITION_DRAWDOWN_PCT,
    DAILY_LOSS_PCT,
    EARNINGS_PER_STRUCTURE_PCT,
    HIGH_CONVICTION_INDEX_PER_STRUCTURE_PCT,
    HIGH_CONVICTION_MAX_DEBIT_TO_WIDTH_RATIO,
    HIGH_CONVICTION_MIN_CONDOR_REWARD_RISK_RATIO,
    HIGH_CONVICTION_MIN_CONFIDENCE,
    HIGH_CONVICTION_MIN_DIRECTIONAL_REWARD_RISK_RATIO,
    HIGH_CONVICTION_MIN_DIRECTIONAL_TREND_STRENGTH,
    HIGH_CONVICTION_MIN_RICHNESS_RATIO,
    INDEX_CLUSTER_PCT,
    INDEX_PER_STRUCTURE_PCT,
    TOTAL_DEFINED_LOSS_PCT,
    evaluate_risk,
)
from money_machine.domain.schemas import Candidate, ModelDecision, OptionStructure, RiskContext


def decision(candidate, **updates) -> ModelDecision:
    values = {
        "regime": "calm",
        "action": candidate.action,
        "candidate_id": candidate.candidate_id,
        "confidence": 0.79,
        "thesis": "eligible",
        "evidence": ["fact"],
        "invalidation": ["condition"],
        "maximum_holding_minutes": 60,
    }
    values.update(updates)
    return ModelDecision(**values)


def context(**updates) -> RiskContext:
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


def candidate_with(replay_candidate, **updates):
    action = updates.pop("action", replay_candidate.action)
    structure = replay_candidate.structure.model_copy(update={"strategy": action})
    return replay_candidate.model_copy(update={"action": action, "structure": structure, **updates})


def candidate_for_underlying(replay_candidate, underlying: str, **updates):
    original = replay_candidate.structure.underlying
    legs = tuple(
        leg.model_copy(
            update={
                "underlying": underlying,
                "symbol": leg.symbol.replace(original, underlying, 1),
            }
        )
        for leg in replay_candidate.structure.legs
    )
    structure = replay_candidate.structure.model_copy(
        update={"underlying": underlying, "legs": legs}
    )
    return replay_candidate.model_copy(
        update={
            "candidate_id": f"{underlying.lower()}-legacy-diversification",
            "structure": structure,
            **updates,
        }
    )


def directional_candidate(
    replay_candidate,
    *,
    confidence_richness: Decimal = Decimal("0.50"),
    trend_strength: Decimal = Decimal("0.005"),
    debit: Decimal = Decimal("1.00"),
    maximum_profit: Decimal = Decimal("200.00"),
) -> Candidate:
    calls = sorted(
        (leg for leg in replay_candidate.structure.legs if leg.right.value == "call"),
        key=lambda leg: leg.strike,
    )
    lower, upper = calls
    upper = upper.model_copy(update={"strike": lower.strike + Decimal("3")})
    legs = (
        lower.model_copy(update={"side": Side.BUY, "position_intent": PositionIntent.BUY_TO_OPEN}),
        upper.model_copy(
            update={"side": Side.SELL, "position_intent": PositionIntent.SELL_TO_OPEN}
        ),
    )
    structure = OptionStructure(
        strategy=Action.CALL_DEBIT_SPREAD,
        underlying=replay_candidate.structure.underlying,
        expiration=replay_candidate.structure.expiration,
        legs=legs,
        net_price=debit,
        maximum_loss=(debit * Decimal("100")).quantize(Decimal("0.01")),
        maximum_profit=maximum_profit,
        is_credit=False,
    )
    return Candidate(
        candidate_id="directional-boundary-candidate",
        action=Action.CALL_DEBIT_SPREAD,
        structure=structure,
        score=Decimal("100"),
        expected_credit_or_debit=debit,
        structure_spread=Decimal("0.10"),
        richness_ratio=confidence_richness,
        data_age_seconds=10,
        event_risk=False,
        liquidity_passed=True,
        trend_strength=trend_strength,
        direction_agrees=True,
        minimum_confidence=Decimal("0.72"),
        gate_evidence=("synthetic deterministic directional boundary",),
    )


def check(result, name: str):
    return next(item for item in result.checks if item.name == name)


@pytest.mark.asyncio
async def test_quantity_rounds_down_to_per_structure_cap(replay_candidate) -> None:
    result = evaluate_risk(decision(replay_candidate), replay_candidate, context())
    assert result.approved
    assert result.quantity == 3
    assert result.awarded_risk == replay_candidate.structure.maximum_loss * 3
    assert result.awarded_risk <= Decimal("1500")


def test_daily_loss_provisional_or_latched_state_blocks_entries(replay_candidate) -> None:
    result = evaluate_risk(
        decision(replay_candidate),
        replay_candidate,
        context(daily_loss_entry_halt_active=True),
    )
    assert not result.approved
    assert check(result, "daily_loss_entry_halt").passed is False
    assert RiskReason.DAILY_LOSS in result.reason_codes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("confidence", "richness", "tier_applied", "expected_quantity"),
    [
        (0.79, Decimal("1.50"), False, 3),
        (0.80, Decimal("1.49"), False, 3),
        (0.80, Decimal("1.50"), True, 10),
    ],
)
async def test_high_conviction_index_tier_boundaries(
    replay_candidate, confidence, richness, tier_applied, expected_quantity
) -> None:
    candidate = candidate_with(replay_candidate, richness_ratio=richness).model_copy(
        update={"payoff_quality_ratio": Decimal("0.25")}
    )

    result = evaluate_risk(decision(candidate, confidence=confidence), candidate, context())

    assert result.approved
    assert result.quantity == expected_quantity
    assert (
        f"applied={str(tier_applied).lower()}" in check(result, "high_conviction_index_tier").actual
    )
    expected_pct = (
        HIGH_CONVICTION_INDEX_PER_STRUCTURE_PCT if tier_applied else INDEX_PER_STRUCTURE_PCT
    )
    assert check(result, "effective_per_structure_percent").actual == str(expected_pct)
    assert check(result, "effective_per_structure_budget").actual == str(
        Decimal("100000") * expected_pct
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("maximum_profit", "tier_applied"),
    [
        (Decimal("99.99"), False),
        (Decimal("100.00"), True),
    ],
)
async def test_condor_high_conviction_payoff_boundary(
    replay_candidate, maximum_profit, tier_applied
) -> None:
    structure = replay_candidate.structure.model_copy(update={"maximum_profit": maximum_profit})
    candidate = replay_candidate.model_copy(
        update={
            "richness_ratio": Decimal("1.50"),
            "structure": structure,
            "payoff_quality_ratio": maximum_profit / Decimal("400"),
        }
    )

    result = evaluate_risk(decision(candidate, confidence=0.80), candidate, context())

    assert result.approved
    assert result.quantity == (10 if tier_applied else 3)
    evidence = check(result, "high_conviction_index_tier").actual
    assert f"applied={str(tier_applied).lower()}" in evidence
    expected_reason = "qualified" if tier_applied else "condor_reward_risk_below_threshold"
    assert f"qualification_reason={expected_reason}" in evidence


@pytest.mark.asyncio
async def test_current_102_credit_398_loss_condor_qualifies(replay_candidate) -> None:
    structure = replay_candidate.structure.model_copy(
        update={
            "net_price": Decimal("1.02"),
            "maximum_profit": Decimal("102.00"),
            "maximum_loss": Decimal("398.00"),
        }
    )
    candidate = replay_candidate.model_copy(
        update={
            "expected_credit_or_debit": Decimal("1.02"),
            "richness_ratio": Decimal("1.50"),
            "structure": structure,
            "payoff_quality_ratio": Decimal("102") / Decimal("398"),
        }
    )

    result = evaluate_risk(decision(candidate, confidence=0.80), candidate, context())

    assert result.approved
    assert result.quantity == 10
    assert result.awarded_risk == Decimal("3980.00")
    evidence = check(result, "high_conviction_index_tier").actual
    assert "applied=true" in evidence
    assert "reward_risk=0.2562814070351758793969849246" in evidence


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("confidence", "trend", "debit", "maximum_profit", "tier_applied", "reason"),
    [
        (
            0.79,
            Decimal("0.0050"),
            Decimal("1.00"),
            Decimal("200.00"),
            False,
            "confidence_below_threshold",
        ),
        (
            0.80,
            Decimal("0.0049"),
            Decimal("1.00"),
            Decimal("200.00"),
            False,
            "directional_trend_below_threshold",
        ),
        (
            0.80,
            Decimal("0.0050"),
            Decimal("1.00"),
            Decimal("199.99"),
            False,
            "directional_reward_risk_below_threshold",
        ),
        (
            0.80,
            Decimal("0.0050"),
            Decimal("1.01"),
            Decimal("199.00"),
            False,
            "directional_debit_to_width_above_threshold",
        ),
        (0.80, Decimal("0.0050"), Decimal("1.00"), Decimal("200.00"), True, "qualified"),
    ],
)
async def test_directional_high_conviction_boundaries_do_not_use_richness(
    replay_candidate,
    confidence,
    trend,
    debit,
    maximum_profit,
    tier_applied,
    reason,
) -> None:
    candidate = directional_candidate(
        replay_candidate,
        confidence_richness=Decimal("0.50"),
        trend_strength=trend,
        debit=debit,
        maximum_profit=maximum_profit,
    )

    result = evaluate_risk(decision(candidate, confidence=confidence), candidate, context())

    assert result.approved
    evidence = check(result, "high_conviction_index_tier").actual
    assert "qualification_path=directional" in evidence
    assert "richness_ratio=0.50" in evidence
    assert f"applied={str(tier_applied).lower()}" in evidence
    assert reason in evidence
    assert check(result, "effective_per_structure_percent").actual == str(
        HIGH_CONVICTION_INDEX_PER_STRUCTURE_PCT if tier_applied else INDEX_PER_STRUCTURE_PCT
    )


@pytest.mark.asyncio
async def test_earnings_risk_stays_at_point_three_five_percent(replay_candidate) -> None:
    candidate = candidate_with(
        replay_candidate,
        action=Action.EARNINGS_CONDOR,
        richness_ratio=Decimal("2.00"),
    )

    result = evaluate_risk(decision(candidate, confidence=0.99), candidate, context())

    assert not result.approved  # One $400 contract exceeds the $350 earnings budget.
    assert "applied=false" in check(result, "high_conviction_index_tier").actual
    assert check(result, "effective_per_structure_percent").actual == str(
        EARNINGS_PER_STRUCTURE_PCT
    )
    assert check(result, "effective_per_structure_budget").actual == "350.0000"


def test_competition_risk_policy_constants_are_fixed() -> None:
    assert Decimal("0.015") == INDEX_PER_STRUCTURE_PCT
    assert Decimal("0.04") == HIGH_CONVICTION_INDEX_PER_STRUCTURE_PCT
    assert Decimal("0.0035") == EARNINGS_PER_STRUCTURE_PCT
    assert Decimal("0.80") == HIGH_CONVICTION_MIN_CONFIDENCE
    assert Decimal("1.50") == HIGH_CONVICTION_MIN_RICHNESS_RATIO
    assert Decimal("0.25") == HIGH_CONVICTION_MIN_CONDOR_REWARD_RISK_RATIO
    assert Decimal("0.005") == HIGH_CONVICTION_MIN_DIRECTIONAL_TREND_STRENGTH
    assert Decimal("2.00") == HIGH_CONVICTION_MIN_DIRECTIONAL_REWARD_RISK_RATIO
    assert Decimal("1") / Decimal("3") == HIGH_CONVICTION_MAX_DEBIT_TO_WIDTH_RATIO
    assert Decimal("0.08") == INDEX_CLUSTER_PCT
    assert Decimal("0.10") == TOTAL_DEFINED_LOSS_PCT
    assert Decimal("0.04") == DAILY_LOSS_PCT
    assert Decimal("0.08") == COMPETITION_DRAWDOWN_PCT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("equity", "start_of_day_equity", "peak_equity", "approved", "reason"),
    [
        (Decimal("96000.01"), Decimal("100000"), Decimal("100000"), True, None),
        (
            Decimal("96000.00"),
            Decimal("100000"),
            Decimal("100000"),
            False,
            RiskReason.DAILY_LOSS,
        ),
        (Decimal("92000.01"), Decimal("92000.01"), Decimal("100000"), True, None),
        (
            Decimal("92000.00"),
            Decimal("92000.00"),
            Decimal("100000"),
            False,
            RiskReason.DRAWDOWN,
        ),
    ],
)
async def test_loss_stops_enforce_exact_authorized_boundaries(
    replay_candidate, equity, start_of_day_equity, peak_equity, approved, reason
) -> None:
    result = evaluate_risk(
        decision(replay_candidate),
        replay_candidate,
        context(
            equity=equity,
            start_of_day_equity=start_of_day_equity,
            peak_equity=peak_equity,
        ),
    )

    assert result.approved is approved
    if reason is not None:
        assert reason in result.reason_codes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"execution_state": ExecutionState.CLOSE_ONLY}, RiskReason.NOT_FULL_EXECUTION),
        ({"kill_switch_active": True}, RiskReason.KILL_SWITCH),
        ({"reconciliation_clean": False}, RiskReason.RECONCILIATION),
        ({"equity": Decimal("95999")}, RiskReason.DAILY_LOSS),
        (
            {"equity": Decimal("91999"), "start_of_day_equity": Decimal("91999")},
            RiskReason.DRAWDOWN,
        ),
    ],
)
async def test_each_hard_portfolio_gate_rejects(replay_candidate, updates, reason) -> None:
    result = evaluate_risk(decision(replay_candidate), replay_candidate, context(**updates))
    assert not result.approved
    assert reason in result.reason_codes


@pytest.mark.asyncio
async def test_pending_underlying_rejects(replay_candidate) -> None:
    result = evaluate_risk(
        decision(replay_candidate),
        replay_candidate,
        context(pending_underlyings=frozenset({replay_candidate.structure.underlying})),
    )
    assert RiskReason.PENDING_UNDERLYING in result.reason_codes


@pytest.mark.asyncio
async def test_existing_managed_structure_is_never_resized_or_added_to(
    replay_candidate,
) -> None:
    underlying = replay_candidate.structure.underlying

    result = evaluate_risk(
        decision(replay_candidate, confidence=0.90),
        replay_candidate,
        context(open_underlyings=frozenset({underlying})),
    )

    assert not result.approved
    assert result.quantity == 0
    assert result.awarded_risk == Decimal("0")
    assert RiskReason.EXISTING_STRUCTURE in result.reason_codes
    assert "hard_gates_passed=false" in check(result, "high_conviction_index_tier").actual


@pytest.mark.asyncio
async def test_raw_structure_count_does_not_block_distinct_underlying_with_headroom(
    replay_candidate,
) -> None:
    candidate = candidate_for_underlying(replay_candidate, "SPY")
    result = evaluate_risk(
        decision(candidate, confidence=0.79),
        candidate,
        context(
            open_alpha_structures=3,
            open_underlyings=frozenset({"QQQ", "IWM"}),
            index_cluster_defined_loss=Decimal("1357"),
            total_open_defined_loss=Decimal("1357"),
        ),
    )

    assert result.approved
    assert result.quantity == 3
    assert result.awarded_risk == Decimal("1194")
    diversification = check(result, "portfolio_underlying_diversification")
    assert "open_underlyings=IWM,QQQ" in diversification.actual
    assert "candidate_underlying=SPY" in diversification.actual


@pytest.mark.asyncio
async def test_live_style_spy_debit_spread_sizes_to_twelve_with_distinct_underlying(
    replay_candidate,
) -> None:
    candidate = candidate_for_underlying(
        directional_candidate(
            replay_candidate,
            trend_strength=Decimal("0.0051"),
            debit=Decimal("1.20"),
            maximum_profit=Decimal("380.00"),
        ),
        "SPY",
    )

    result = evaluate_risk(
        decision(candidate, confidence=0.78),
        candidate,
        context(
            open_alpha_structures=5,
            open_underlyings=frozenset({"QQQ"}),
            index_cluster_defined_loss=Decimal("1979"),
            total_open_defined_loss=Decimal("1979"),
        ),
    )

    assert result.approved
    assert result.quantity == 12
    assert result.awarded_risk == Decimal("1440.00")
    assert check(result, "effective_per_structure_percent").actual == "0.015"
    assert check(result, "effective_per_structure_budget").actual == "1500.000"
    assert "applied=false" in check(result, "high_conviction_index_tier").actual


@pytest.mark.asyncio
async def test_correlated_index_cap_rounds_quantity_to_zero(replay_candidate) -> None:
    result = evaluate_risk(
        decision(replay_candidate),
        replay_candidate,
        context(index_cluster_defined_loss=Decimal("7800")),
    )
    assert not result.approved
    assert RiskReason.ZERO_QUANTITY in result.reason_codes


@pytest.mark.asyncio
async def test_total_defined_loss_cap_rejects(replay_candidate) -> None:
    result = evaluate_risk(
        decision(replay_candidate),
        replay_candidate,
        context(total_open_defined_loss=Decimal("9800")),
    )
    assert not result.approved
    assert RiskReason.ZERO_QUANTITY in result.reason_codes


@pytest.mark.asyncio
async def test_quantity_floors_to_smallest_remaining_budget(replay_candidate) -> None:
    candidate = candidate_with(replay_candidate, richness_ratio=Decimal("1.50"))

    result = evaluate_risk(
        decision(candidate, confidence=0.80),
        candidate,
        context(
            index_cluster_defined_loss=Decimal("7400"),
            total_open_defined_loss=Decimal("9200"),
        ),
    )

    assert result.approved
    assert result.quantity == 1
    assert result.awarded_risk == Decimal("398")
    assert result.awarded_risk <= Decimal("3000")
    assert result.awarded_risk <= Decimal("750")
    assert result.awarded_risk <= Decimal("800")


@pytest.mark.asyncio
async def test_model_action_must_match_candidate(replay_candidate) -> None:
    alternate = (
        Action.PUT_DEBIT_SPREAD
        if replay_candidate.action is not Action.PUT_DEBIT_SPREAD
        else Action.INDEX_CONDOR
    )
    result = evaluate_risk(
        decision(replay_candidate, action=alternate), replay_candidate, context()
    )
    assert RiskReason.ACTION_MISMATCH in result.reason_codes
