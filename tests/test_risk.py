from datetime import UTC, datetime
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
    FINAL_DAY_DAILY_LOSS_PCT,
    FINAL_DAY_DEFINED_LOSS_PCT,
    HIGH_CONVICTION_INDEX_PER_STRUCTURE_PCT,
    HIGH_CONVICTION_MAX_DEBIT_TO_WIDTH_RATIO,
    HIGH_CONVICTION_MIN_CONDOR_REWARD_RISK_RATIO,
    HIGH_CONVICTION_MIN_CONFIDENCE,
    HIGH_CONVICTION_MIN_DIRECTIONAL_REWARD_RISK_RATIO,
    HIGH_CONVICTION_MIN_DIRECTIONAL_TREND_STRENGTH,
    HIGH_CONVICTION_MIN_RICHNESS_RATIO,
    INDEX_CLUSTER_PCT,
    INDEX_PER_STRUCTURE_PCT,
    MAVERICK_INDEX_PER_STRUCTURE_PCT,
    TOTAL_DEFINED_LOSS_PCT,
    daily_loss_pct_at,
    evaluate_risk,
    index_cluster_pct_at,
    total_defined_loss_pct_at,
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
    assert result.quantity == 7
    assert result.awarded_risk == replay_candidate.structure.maximum_loss * 7
    assert result.awarded_risk <= Decimal("3000")


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
        (0.79, Decimal("1.50"), False, 7),
        (0.80, Decimal("1.49"), False, 7),
        (0.80, Decimal("1.50"), True, 15),
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
    assert result.quantity == (15 if tier_applied else 7)
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
    assert result.quantity == 15
    assert result.awarded_risk == Decimal("5970.00")
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
    assert Decimal("0.03") == INDEX_PER_STRUCTURE_PCT
    assert Decimal("0.06") == HIGH_CONVICTION_INDEX_PER_STRUCTURE_PCT
    assert Decimal("0.0035") == EARNINGS_PER_STRUCTURE_PCT
    assert Decimal("0.80") == HIGH_CONVICTION_MIN_CONFIDENCE
    assert Decimal("1.50") == HIGH_CONVICTION_MIN_RICHNESS_RATIO
    assert Decimal("0.25") == HIGH_CONVICTION_MIN_CONDOR_REWARD_RISK_RATIO
    assert Decimal("0.005") == HIGH_CONVICTION_MIN_DIRECTIONAL_TREND_STRENGTH
    assert Decimal("2.00") == HIGH_CONVICTION_MIN_DIRECTIONAL_REWARD_RISK_RATIO
    assert Decimal("1") / Decimal("3") == HIGH_CONVICTION_MAX_DEBIT_TO_WIDTH_RATIO
    assert Decimal("0.12") == INDEX_CLUSTER_PCT
    assert Decimal("0.15") == TOTAL_DEFINED_LOSS_PCT
    assert Decimal("0.06") == DAILY_LOSS_PCT
    assert Decimal("0.11") == FINAL_DAY_DAILY_LOSS_PCT
    assert Decimal("0.12") == MAVERICK_INDEX_PER_STRUCTURE_PCT
    assert Decimal("0.12") == COMPETITION_DRAWDOWN_PCT


def test_daily_loss_boundary_is_eleven_percent_only_on_final_day() -> None:
    assert daily_loss_pct_at(datetime(2026, 9, 3, 14, tzinfo=UTC)) == Decimal("0.11")
    assert daily_loss_pct_at(datetime(2026, 9, 2, 14, tzinfo=UTC)) == Decimal("0.06")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "equity",
    [Decimal("88326.49"), Decimal("88326.4836")],
)
async def test_final_day_daily_loss_boundary_is_audited_but_does_not_halt_entry(
    replay_candidate, equity
) -> None:
    result = evaluate_risk(
        decision(replay_candidate),
        replay_candidate,
        context(
            now="2026-09-03T14:00:00Z",
            equity=equity,
            start_of_day_equity=Decimal("99243.24"),
            peak_equity=equity,
        ),
    )

    assert check(result, "daily_loss").passed
    assert check(result, "daily_loss").limit == "10916.7564"


@pytest.mark.asyncio
async def test_maverick_final_day_is_one_shot_flat_cluster_and_twelve_percent(
    replay_candidate,
) -> None:
    candidate = directional_candidate(
        replay_candidate,
        trend_strength=Decimal("0.006"),
        debit=Decimal("1.00"),
        maximum_profit=Decimal("200.00"),
    )
    eligible = evaluate_risk(
        decision(candidate, confidence=0.80),
        candidate,
        context(
            now="2026-09-03T14:00:00Z",
            equity=Decimal("92931.80"),
            start_of_day_equity=Decimal("99243.24"),
            maverick_candidate_ids=frozenset({candidate.candidate_id}),
        ),
    )
    assert eligible.approved
    assert check(eligible, "effective_per_structure_percent").actual == "0.12"
    assert eligible.awarded_risk <= Decimal("11151.8160")
    assert "applied=true" in check(eligible, "maverick_final_day_tier").actual

    for updates in (
        {"maverick_entry_already_used": True},
        {"index_cluster_defined_loss": Decimal("1")},
    ):
        fallback = evaluate_risk(
            decision(candidate, confidence=0.80),
            candidate,
            context(
                now="2026-09-03T14:00:00Z",
                maverick_candidate_ids=frozenset({candidate.candidate_id}),
                **updates,
            ),
        )
        assert check(fallback, "effective_per_structure_percent").actual == "0.06"
        assert "applied=false" in check(fallback, "maverick_final_day_tier").actual


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("equity", "start_of_day_equity", "peak_equity", "approved", "reason"),
    [
        (Decimal("94000.01"), Decimal("100000"), Decimal("100000"), True, None),
        (
            Decimal("94000.00"),
            Decimal("100000"),
            Decimal("100000"),
            False,
            RiskReason.DAILY_LOSS,
        ),
        (Decimal("88000.01"), Decimal("88000.01"), Decimal("100000"), True, None),
        (
            Decimal("88000.00"),
            Decimal("88000.00"),
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
async def test_clean_competition_peak_allows_directional_but_real_drawdown_still_blocks(
    replay_candidate,
) -> None:
    candidate = directional_candidate(replay_candidate)
    selected = decision(candidate, confidence=0.90)

    clean = evaluate_risk(
        selected,
        candidate,
        context(
            equity=Decimal("100053.14"),
            start_of_day_equity=Decimal("100202.04"),
            peak_equity=Decimal("100202.04"),
        ),
    )
    genuine_drawdown = evaluate_risk(
        selected,
        candidate,
        context(
            equity=Decimal("100000"),
            start_of_day_equity=Decimal("100000"),
            peak_equity=Decimal("115000"),
        ),
    )

    assert clean.approved
    assert RiskReason.DRAWDOWN not in clean.reason_codes
    assert not genuine_drawdown.approved
    assert RiskReason.DRAWDOWN in genuine_drawdown.reason_codes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"execution_state": ExecutionState.CLOSE_ONLY}, RiskReason.NOT_FULL_EXECUTION),
        ({"kill_switch_active": True}, RiskReason.KILL_SWITCH),
        ({"reconciliation_clean": False}, RiskReason.RECONCILIATION),
        ({"equity": Decimal("93999")}, RiskReason.DAILY_LOSS),
        (
            {"equity": Decimal("87999"), "start_of_day_equity": Decimal("87999")},
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
async def test_final_day_allows_only_one_additional_index_structure_and_uses_twenty_four_percent(
    replay_candidate,
) -> None:
    underlying = replay_candidate.structure.underlying
    final_day = datetime(2026, 9, 3, 16, 30, tzinfo=UTC)
    assert index_cluster_pct_at(final_day) == FINAL_DAY_DEFINED_LOSS_PCT == Decimal("0.24")
    assert total_defined_loss_pct_at(final_day) == Decimal("0.24")
    shared = {
        "now": final_day,
        "equity": Decimal("90000"),
        "start_of_day_equity": Decimal("100000"),
        "peak_equity": Decimal("110000"),
        "daily_loss_entry_halt_active": True,
        "open_underlyings": frozenset({underlying}),
        "index_cluster_defined_loss": Decimal("11104"),
        "total_open_defined_loss": Decimal("11104"),
    }

    allowed = evaluate_risk(
        decision(replay_candidate),
        replay_candidate,
        context(
            **shared,
            open_underlying_structure_counts={underlying: 1},
        ),
    )
    blocked = evaluate_risk(
        decision(replay_candidate),
        replay_candidate,
        context(
            **shared,
            open_underlying_structure_counts={underlying: 2},
        ),
    )

    assert allowed.approved
    assert check(allowed, "daily_loss").passed
    assert check(allowed, "daily_loss_entry_halt").passed
    assert check(allowed, "competition_drawdown").passed
    assert check(allowed, "cluster_defined_loss_headroom").limit == "21600.00"
    assert check(allowed, "total_defined_loss_headroom").limit == "21600.00"
    assert not blocked.approved
    assert RiskReason.EXISTING_STRUCTURE in blocked.reason_codes


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
    assert result.quantity == 7
    assert result.awarded_risk == Decimal("2786")
    diversification = check(result, "portfolio_underlying_diversification")
    assert "open_underlyings=IWM,QQQ" in diversification.actual
    assert "candidate_underlying=SPY" in diversification.actual


@pytest.mark.asyncio
async def test_live_style_spy_debit_spread_sizes_to_twenty_five_with_distinct_underlying(
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
    assert result.quantity == 25
    assert result.awarded_risk == Decimal("3000.00")
    assert check(result, "effective_per_structure_percent").actual == "0.03"
    assert check(result, "effective_per_structure_budget").actual == "3000.00"
    assert "applied=false" in check(result, "high_conviction_index_tier").actual


@pytest.mark.asyncio
async def test_correlated_index_cap_rounds_quantity_to_zero(replay_candidate) -> None:
    result = evaluate_risk(
        decision(replay_candidate),
        replay_candidate,
        context(index_cluster_defined_loss=Decimal("11900")),
    )
    assert not result.approved
    assert RiskReason.ZERO_QUANTITY in result.reason_codes


@pytest.mark.asyncio
async def test_total_defined_loss_cap_rejects(replay_candidate) -> None:
    result = evaluate_risk(
        decision(replay_candidate),
        replay_candidate,
        context(total_open_defined_loss=Decimal("14900")),
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
            index_cluster_defined_loss=Decimal("11500"),
            total_open_defined_loss=Decimal("14500"),
        ),
    )

    assert result.approved
    assert result.quantity == 1
    assert result.awarded_risk == Decimal("398")
    assert result.awarded_risk <= Decimal("6000")
    assert result.awarded_risk <= Decimal("500")
    assert result.awarded_risk <= Decimal("500")


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
