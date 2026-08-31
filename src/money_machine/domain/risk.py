from decimal import ROUND_FLOOR, Decimal

from money_machine.domain.enums import Action, ExecutionState, RiskReason
from money_machine.domain.options import validate_defined_risk
from money_machine.domain.schemas import (
    Candidate,
    ModelDecision,
    RiskCheck,
    RiskContext,
    RiskDecisionResult,
)

INDEX_PER_STRUCTURE_PCT = Decimal("0.005")
HIGH_CONVICTION_INDEX_PER_STRUCTURE_PCT = Decimal("0.01")
EARNINGS_PER_STRUCTURE_PCT = Decimal("0.0035")
HIGH_CONVICTION_MIN_CONFIDENCE = Decimal("0.80")
HIGH_CONVICTION_MIN_RICHNESS_RATIO = Decimal("1.50")
INDEX_CLUSTER_PCT = Decimal("0.02")
TOTAL_DEFINED_LOSS_PCT = Decimal("0.03")
DAILY_LOSS_PCT = Decimal("0.01")
COMPETITION_DRAWDOWN_PCT = Decimal("0.02")
MAX_OPEN_ALPHA_STRUCTURES = 3
MAX_DATA_AGE_SECONDS = 90
INDEX_UNDERLYINGS = frozenset({"SPY", "QQQ", "IWM"})
INDEX_ACTIONS = frozenset({Action.INDEX_CONDOR, Action.CALL_DEBIT_SPREAD, Action.PUT_DEBIT_SPREAD})


def evaluate_risk(
    decision: ModelDecision,
    candidate: Candidate | None,
    context: RiskContext,
) -> RiskDecisionResult:
    checks: list[RiskCheck] = []

    def add(name: str, passed: bool, actual: object, limit: object, reason: RiskReason) -> None:
        checks.append(
            RiskCheck(
                name=name,
                passed=passed,
                actual=str(actual),
                limit=str(limit),
                reason=reason,
            )
        )

    if decision.action is Action.ABSTAIN:
        add("model_selection", False, "abstain", "candidate required", RiskReason.ABSTAIN)
        return _rejected(checks)
    if candidate is None:
        add(
            "candidate_membership",
            False,
            decision.candidate_id,
            "auction candidate",
            RiskReason.UNKNOWN_CANDIDATE,
        )
        return _rejected(checks)
    add(
        "action_matches_candidate",
        decision.action is candidate.action,
        decision.action,
        candidate.action,
        RiskReason.ACTION_MISMATCH,
    )
    add(
        "execution_state",
        context.execution_state is ExecutionState.FULL_EXECUTION,
        context.execution_state,
        ExecutionState.FULL_EXECUTION,
        RiskReason.NOT_FULL_EXECUTION,
    )
    add(
        "kill_switch",
        not context.kill_switch_active,
        context.kill_switch_active,
        False,
        RiskReason.KILL_SWITCH,
    )
    add(
        "reconciliation",
        context.reconciliation_clean,
        context.reconciliation_clean,
        True,
        RiskReason.RECONCILIATION,
    )
    add(
        "data_freshness",
        candidate.data_age_seconds <= MAX_DATA_AGE_SECONDS,
        candidate.data_age_seconds,
        MAX_DATA_AGE_SECONDS,
        RiskReason.STALE_DATA,
    )
    add(
        "liquidity",
        candidate.liquidity_passed,
        candidate.liquidity_passed,
        True,
        RiskReason.LIQUIDITY,
    )
    add("event_veto", not candidate.event_risk, candidate.event_risk, False, RiskReason.EVENT_RISK)
    if candidate.action in {Action.CALL_DEBIT_SPREAD, Action.PUT_DEBIT_SPREAD}:
        add(
            "directional_confidence",
            Decimal(str(decision.confidence)) >= candidate.minimum_confidence,
            decision.confidence,
            candidate.minimum_confidence,
            RiskReason.ACTION_MISMATCH,
        )
        add(
            "deterministic_direction",
            candidate.direction_agrees,
            candidate.direction_agrees,
            True,
            RiskReason.ACTION_MISMATCH,
        )
    try:
        validate_defined_risk(candidate.structure)
        structure_valid = True
    except ValueError:
        structure_valid = False
    add(
        "defined_risk_structure",
        structure_valid,
        structure_valid,
        True,
        RiskReason.INVALID_STRUCTURE,
    )

    daily_loss = max(Decimal("0"), context.start_of_day_equity - context.equity)
    daily_limit = context.start_of_day_equity * DAILY_LOSS_PCT
    add("daily_loss", daily_loss < daily_limit, daily_loss, daily_limit, RiskReason.DAILY_LOSS)
    drawdown = max(Decimal("0"), context.peak_equity - context.equity)
    drawdown_limit = context.peak_equity * COMPETITION_DRAWDOWN_PCT
    add(
        "competition_drawdown",
        drawdown < drawdown_limit,
        drawdown,
        drawdown_limit,
        RiskReason.DRAWDOWN,
    )
    add(
        "open_structure_count",
        context.open_alpha_structures < MAX_OPEN_ALPHA_STRUCTURES,
        context.open_alpha_structures,
        MAX_OPEN_ALPHA_STRUCTURES,
        RiskReason.OPEN_STRUCTURE_LIMIT,
    )
    add(
        "pending_underlying",
        candidate.structure.underlying not in context.pending_underlyings,
        candidate.structure.underlying in context.pending_underlyings,
        False,
        RiskReason.PENDING_UNDERLYING,
    )
    add(
        "existing_managed_structure",
        candidate.structure.underlying not in context.open_underlyings,
        candidate.structure.underlying in context.open_underlyings,
        False,
        RiskReason.EXISTING_STRUCTURE,
    )

    index_strategy = (
        candidate.action in INDEX_ACTIONS and candidate.structure.underlying in INDEX_UNDERLYINGS
    )
    confidence = Decimal(str(decision.confidence))
    tier_thresholds_met = (
        index_strategy
        and confidence >= HIGH_CONVICTION_MIN_CONFIDENCE
        and candidate.richness_ratio >= HIGH_CONVICTION_MIN_RICHNESS_RATIO
    )
    hard_gates_passed = all(check.passed for check in checks)
    high_conviction_applied = tier_thresholds_met and hard_gates_passed
    if candidate.action is Action.EARNINGS_CONDOR:
        per_pct = EARNINGS_PER_STRUCTURE_PCT
        tier_name = "earnings"
    elif high_conviction_applied:
        per_pct = HIGH_CONVICTION_INDEX_PER_STRUCTURE_PCT
        tier_name = "high_conviction_index"
    else:
        per_pct = INDEX_PER_STRUCTURE_PCT
        tier_name = "standard_index"
    per_budget = context.equity * per_pct
    add(
        "high_conviction_index_tier",
        True,
        (
            f"applied={str(high_conviction_applied).lower()}; tier={tier_name}; "
            f"confidence={confidence}; richness_ratio={candidate.richness_ratio}; "
            f"index_strategy={str(index_strategy).lower()}; "
            f"hard_gates_passed={str(hard_gates_passed).lower()}"
        ),
        (
            f"confidence>={HIGH_CONVICTION_MIN_CONFIDENCE}; "
            f"richness_ratio>={HIGH_CONVICTION_MIN_RICHNESS_RATIO}; "
            "index_strategy=true; hard_gates_passed=true"
        ),
        RiskReason.PER_STRUCTURE_CAP,
    )
    add(
        "effective_per_structure_percent",
        True,
        per_pct,
        (
            f"standard_index={INDEX_PER_STRUCTURE_PCT}; "
            f"high_conviction_index={HIGH_CONVICTION_INDEX_PER_STRUCTURE_PCT}; "
            f"earnings={EARNINGS_PER_STRUCTURE_PCT}"
        ),
        RiskReason.PER_STRUCTURE_CAP,
    )
    add(
        "effective_per_structure_budget",
        True,
        per_budget,
        f"current_equity={context.equity} * effective_percent={per_pct}",
        RiskReason.PER_STRUCTURE_CAP,
    )
    if not hard_gates_passed:
        return _rejected(checks)

    cluster_remaining = context.equity * INDEX_CLUSTER_PCT - context.index_cluster_defined_loss
    total_remaining = context.equity * TOTAL_DEFINED_LOSS_PCT - context.total_open_defined_loss
    available = max(Decimal("0"), min(per_budget, cluster_remaining, total_remaining))
    quantity = int(
        (available / candidate.structure.maximum_loss).to_integral_value(rounding=ROUND_FLOOR)
    )
    awarded = candidate.structure.maximum_loss * quantity
    add(
        "per_structure_cap",
        awarded <= per_budget,
        awarded,
        per_budget,
        RiskReason.PER_STRUCTURE_CAP,
    )
    add(
        "cluster_cap",
        awarded <= cluster_remaining,
        awarded,
        cluster_remaining,
        RiskReason.CLUSTER_CAP,
    )
    add(
        "total_defined_loss_cap",
        awarded <= total_remaining,
        awarded,
        total_remaining,
        RiskReason.TOTAL_CAP,
    )
    add("quantity_round_down", quantity >= 1, quantity, ">=1", RiskReason.ZERO_QUANTITY)
    if quantity < 1 or any(not check.passed for check in checks):
        return _rejected(checks)
    return RiskDecisionResult(
        approved=True,
        quantity=quantity,
        awarded_risk=awarded,
        reason_codes=(RiskReason.APPROVED,),
        checks=tuple(checks),
    )


def _rejected(checks: list[RiskCheck]) -> RiskDecisionResult:
    reasons = tuple(dict.fromkeys(check.reason for check in checks if not check.passed))
    return RiskDecisionResult(
        approved=False,
        quantity=0,
        awarded_risk=Decimal("0"),
        reason_codes=reasons or (RiskReason.INVALID_STRUCTURE,),
        checks=tuple(checks),
    )
