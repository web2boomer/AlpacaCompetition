from decimal import Decimal

import pytest

from money_machine.domain.enums import Action, ExecutionState, RiskReason
from money_machine.domain.risk import evaluate_risk
from money_machine.domain.schemas import ModelDecision, RiskContext


def decision(candidate, **updates) -> ModelDecision:
    values = {
        "regime": "calm",
        "action": candidate.action,
        "candidate_id": candidate.candidate_id,
        "confidence": 0.90,
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
        "kill_switch_active": False,
        "reconciliation_clean": True,
    }
    values.update(updates)
    return RiskContext(**values)


@pytest.mark.asyncio
async def test_quantity_rounds_down_to_per_structure_cap(replay_candidate) -> None:
    result = evaluate_risk(decision(replay_candidate), replay_candidate, context())
    assert result.approved
    assert result.quantity == 1
    assert result.awarded_risk == replay_candidate.structure.maximum_loss
    assert result.awarded_risk <= Decimal("500")


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
async def test_correlated_index_cap_rounds_quantity_to_zero(replay_candidate) -> None:
    result = evaluate_risk(
        decision(replay_candidate),
        replay_candidate,
        context(index_cluster_defined_loss=Decimal("900")),
    )
    assert not result.approved
    assert RiskReason.ZERO_QUANTITY in result.reason_codes


@pytest.mark.asyncio
async def test_total_defined_loss_cap_rejects(replay_candidate) -> None:
    result = evaluate_risk(
        decision(replay_candidate),
        replay_candidate,
        context(total_open_defined_loss=Decimal("1800")),
    )
    assert not result.approved
    assert RiskReason.ZERO_QUANTITY in result.reason_codes


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
