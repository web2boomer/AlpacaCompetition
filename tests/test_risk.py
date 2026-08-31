from decimal import Decimal

import pytest

from money_machine.domain.enums import Action, ExecutionState, RiskReason
from money_machine.domain.risk import (
    COMPETITION_DRAWDOWN_PCT,
    DAILY_LOSS_PCT,
    EARNINGS_PER_STRUCTURE_PCT,
    HIGH_CONVICTION_INDEX_PER_STRUCTURE_PCT,
    HIGH_CONVICTION_MIN_CONFIDENCE,
    HIGH_CONVICTION_MIN_RICHNESS_RATIO,
    INDEX_CLUSTER_PCT,
    INDEX_PER_STRUCTURE_PCT,
    MAX_OPEN_ALPHA_STRUCTURES,
    TOTAL_DEFINED_LOSS_PCT,
    evaluate_risk,
)
from money_machine.domain.schemas import ModelDecision, RiskContext


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


def check(result, name: str):
    return next(item for item in result.checks if item.name == name)


@pytest.mark.asyncio
async def test_quantity_rounds_down_to_per_structure_cap(replay_candidate) -> None:
    result = evaluate_risk(decision(replay_candidate), replay_candidate, context())
    assert result.approved
    assert result.quantity == 1
    assert result.awarded_risk == replay_candidate.structure.maximum_loss
    assert result.awarded_risk <= Decimal("500")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("confidence", "richness", "tier_applied", "expected_quantity"),
    [
        (0.79, Decimal("1.50"), False, 1),
        (0.80, Decimal("1.49"), False, 1),
        (0.80, Decimal("1.50"), True, 2),
    ],
)
async def test_high_conviction_index_tier_boundaries(
    replay_candidate, confidence, richness, tier_applied, expected_quantity
) -> None:
    candidate = candidate_with(replay_candidate, richness_ratio=richness)

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
    assert Decimal("0.005") == INDEX_PER_STRUCTURE_PCT
    assert Decimal("0.01") == HIGH_CONVICTION_INDEX_PER_STRUCTURE_PCT
    assert Decimal("0.0035") == EARNINGS_PER_STRUCTURE_PCT
    assert Decimal("0.80") == HIGH_CONVICTION_MIN_CONFIDENCE
    assert Decimal("1.50") == HIGH_CONVICTION_MIN_RICHNESS_RATIO
    assert Decimal("0.02") == INDEX_CLUSTER_PCT
    assert Decimal("0.03") == TOTAL_DEFINED_LOSS_PCT
    assert MAX_OPEN_ALPHA_STRUCTURES == 3
    assert Decimal("0.01") == DAILY_LOSS_PCT
    assert Decimal("0.02") == COMPETITION_DRAWDOWN_PCT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"execution_state": ExecutionState.CLOSE_ONLY}, RiskReason.NOT_FULL_EXECUTION),
        ({"kill_switch_active": True}, RiskReason.KILL_SWITCH),
        ({"reconciliation_clean": False}, RiskReason.RECONCILIATION),
        ({"equity": Decimal("98999")}, RiskReason.DAILY_LOSS),
        ({"equity": Decimal("97999")}, RiskReason.DRAWDOWN),
        ({"open_alpha_structures": 3}, RiskReason.OPEN_STRUCTURE_LIMIT),
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
async def test_five_open_structures_reject_before_high_conviction_sizing(
    replay_candidate,
) -> None:
    result = evaluate_risk(
        decision(replay_candidate, confidence=0.90),
        replay_candidate,
        context(open_alpha_structures=5),
    )

    assert not result.approved
    assert result.quantity == 0
    assert RiskReason.OPEN_STRUCTURE_LIMIT in result.reason_codes
    assert check(result, "open_structure_count").actual == "5"
    assert check(result, "open_structure_count").limit == "3"
    assert "applied=false" in check(result, "high_conviction_index_tier").actual
    assert check(result, "effective_per_structure_percent").actual == "0.005"


@pytest.mark.asyncio
async def test_correlated_index_cap_rounds_quantity_to_zero(replay_candidate) -> None:
    result = evaluate_risk(
        decision(replay_candidate),
        replay_candidate,
        context(index_cluster_defined_loss=Decimal("1950")),
    )
    assert not result.approved
    assert RiskReason.ZERO_QUANTITY in result.reason_codes


@pytest.mark.asyncio
async def test_total_defined_loss_cap_rejects(replay_candidate) -> None:
    result = evaluate_risk(
        decision(replay_candidate),
        replay_candidate,
        context(total_open_defined_loss=Decimal("2950")),
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
            index_cluster_defined_loss=Decimal("1250"),
            total_open_defined_loss=Decimal("2200"),
        ),
    )

    assert result.approved
    assert result.quantity == 1
    assert result.awarded_risk == Decimal("400")
    assert result.awarded_risk <= Decimal("1000")
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
