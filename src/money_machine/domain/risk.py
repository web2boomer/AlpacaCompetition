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
from money_machine.execution import entry_holding_policy

INDEX_PER_STRUCTURE_PCT = Decimal("0.03")
HIGH_CONVICTION_INDEX_PER_STRUCTURE_PCT = Decimal("0.06")
EARNINGS_PER_STRUCTURE_PCT = Decimal("0.0035")
HIGH_CONVICTION_MIN_CONFIDENCE = Decimal("0.80")
HIGH_CONVICTION_MIN_RICHNESS_RATIO = Decimal("1.50")
HIGH_CONVICTION_MIN_CONDOR_REWARD_RISK_RATIO = Decimal("0.25")
HIGH_CONVICTION_MIN_DIRECTIONAL_TREND_STRENGTH = Decimal("0.005")
HIGH_CONVICTION_MIN_DIRECTIONAL_REWARD_RISK_RATIO = Decimal("2.00")
HIGH_CONVICTION_MAX_DEBIT_TO_WIDTH_RATIO = Decimal("1") / Decimal("3")
INDEX_CLUSTER_PCT = Decimal("0.12")
TOTAL_DEFINED_LOSS_PCT = Decimal("0.15")
DAILY_LOSS_PCT = Decimal("0.06")
COMPETITION_DRAWDOWN_PCT = Decimal("0.12")
MAX_DATA_AGE_SECONDS = 90
INDEX_UNDERLYINGS = frozenset({"SPY", "QQQ", "IWM"})
INDEX_ACTIONS = frozenset({Action.INDEX_CONDOR, Action.CALL_DEBIT_SPREAD, Action.PUT_DEBIT_SPREAD})
DIRECTIONAL_INDEX_ACTIONS = frozenset({Action.CALL_DEBIT_SPREAD, Action.PUT_DEBIT_SPREAD})


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
    add(
        "daily_loss_entry_halt",
        not context.daily_loss_entry_halt_active,
        context.daily_loss_entry_halt_active,
        False,
        RiskReason.DAILY_LOSS,
    )
    drawdown = max(Decimal("0"), context.peak_equity - context.equity)
    drawdown_limit = context.peak_equity * COMPETITION_DRAWDOWN_PCT
    add(
        "competition_drawdown",
        drawdown < drawdown_limit,
        drawdown,
        drawdown_limit,
        RiskReason.DRAWDOWN,
    )
    holding = entry_holding_policy(
        context.now,
        decision.maximum_holding_minutes,
        maximum_holding_minutes=candidate.maximum_holding_minutes or None,
        hard_deadline=candidate.holding_deadline,
    )
    add(
        "session_holding_window",
        holding.accepted,
        (
            f"model_hold_minutes={decision.maximum_holding_minutes}; "
            f"effective_hold_minutes={holding.effective_holding_minutes}; "
            f"effective_deadline={holding.effective_deadline.isoformat()}; "
            f"clamp_reason={holding.reason}"
        ),
        (
            "at least 30 tradable minutes remain before the directional event-safe, "
            "15:50 ET, or competition boundary"
        ),
        RiskReason.NOT_FULL_EXECUTION,
    )
    add(
        "portfolio_underlying_diversification",
        True,
        (
            f"open_alpha_structures={context.open_alpha_structures}; "
            f"open_underlyings={','.join(sorted(context.open_underlyings)) or 'none'}; "
            f"pending_underlyings={','.join(sorted(context.pending_underlyings)) or 'none'}; "
            f"candidate_underlying={candidate.structure.underlying}"
        ),
        "one managed-or-pending structure per underlying; cluster and total caps authoritative",
        RiskReason.EXISTING_STRUCTURE,
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
    cluster_remaining = context.equity * INDEX_CLUSTER_PCT - context.index_cluster_defined_loss
    total_remaining = context.equity * TOTAL_DEFINED_LOSS_PCT - context.total_open_defined_loss
    add(
        "cluster_defined_loss_headroom",
        cluster_remaining > 0,
        cluster_remaining,
        context.equity * INDEX_CLUSTER_PCT,
        RiskReason.CLUSTER_CAP,
    )
    add(
        "total_defined_loss_headroom",
        total_remaining > 0,
        total_remaining,
        context.equity * TOTAL_DEFINED_LOSS_PCT,
        RiskReason.TOTAL_CAP,
    )

    index_strategy = (
        candidate.action in INDEX_ACTIONS and candidate.structure.underlying in INDEX_UNDERLYINGS
    )
    confidence = Decimal(str(decision.confidence))
    reward_risk_ratio = (
        candidate.payoff_quality_ratio
        if candidate.action is Action.INDEX_CONDOR and candidate.payoff_quality_ratio is not None
        else candidate.structure.maximum_profit / candidate.structure.maximum_loss
    )
    spread_width = _spread_width(candidate)
    debit_to_width_ratio = (
        candidate.structure.net_price / spread_width
        if candidate.action in DIRECTIONAL_INDEX_ACTIONS and spread_width > 0
        else None
    )
    condor_tier_thresholds_met = (
        index_strategy
        and candidate.action is Action.INDEX_CONDOR
        and confidence >= HIGH_CONVICTION_MIN_CONFIDENCE
        and candidate.richness_ratio >= HIGH_CONVICTION_MIN_RICHNESS_RATIO
        and reward_risk_ratio >= HIGH_CONVICTION_MIN_CONDOR_REWARD_RISK_RATIO
    )
    directional_tier_thresholds_met = (
        index_strategy
        and candidate.action in DIRECTIONAL_INDEX_ACTIONS
        and confidence >= HIGH_CONVICTION_MIN_CONFIDENCE
        and candidate.trend_strength is not None
        and candidate.trend_strength >= HIGH_CONVICTION_MIN_DIRECTIONAL_TREND_STRENGTH
        and reward_risk_ratio >= HIGH_CONVICTION_MIN_DIRECTIONAL_REWARD_RISK_RATIO
        and debit_to_width_ratio is not None
        and debit_to_width_ratio <= HIGH_CONVICTION_MAX_DEBIT_TO_WIDTH_RATIO
    )
    tier_thresholds_met = condor_tier_thresholds_met or directional_tier_thresholds_met
    hard_gates_passed = all(check.passed for check in checks)
    high_conviction_applied = tier_thresholds_met and hard_gates_passed
    qualification_path = (
        "condor"
        if candidate.action is Action.INDEX_CONDOR
        else "directional"
        if candidate.action in DIRECTIONAL_INDEX_ACTIONS
        else "ineligible"
    )
    qualification_failures = _high_conviction_failures(
        candidate=candidate,
        confidence=confidence,
        index_strategy=index_strategy,
        reward_risk_ratio=reward_risk_ratio,
        debit_to_width_ratio=debit_to_width_ratio,
        hard_gate_failures=tuple(check.name for check in checks if not check.passed),
    )
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
            f"qualification_path={qualification_path}; confidence={confidence}; "
            f"richness_ratio={candidate.richness_ratio}; "
            f"trend_strength={candidate.trend_strength}; reward_risk={reward_risk_ratio}; "
            f"debit_to_width={debit_to_width_ratio}; "
            f"liquidity_passed={str(candidate.liquidity_passed).lower()}; "
            f"index_strategy={str(index_strategy).lower()}; "
            f"hard_gates_passed={str(hard_gates_passed).lower()}; "
            f"qualification_reason={','.join(qualification_failures) or 'qualified'}"
        ),
        (
            f"condor(confidence>={HIGH_CONVICTION_MIN_CONFIDENCE}, "
            f"richness_ratio>={HIGH_CONVICTION_MIN_RICHNESS_RATIO}, "
            f"reward_risk>={HIGH_CONVICTION_MIN_CONDOR_REWARD_RISK_RATIO}) OR "
            f"directional(confidence>={HIGH_CONVICTION_MIN_CONFIDENCE}, "
            f"trend_strength>={HIGH_CONVICTION_MIN_DIRECTIONAL_TREND_STRENGTH}, "
            f"reward_risk>={HIGH_CONVICTION_MIN_DIRECTIONAL_REWARD_RISK_RATIO}, "
            f"debit_to_width<={HIGH_CONVICTION_MAX_DEBIT_TO_WIDTH_RATIO}); "
            "index_strategy=true; liquidity and all hard gates pass"
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


def _spread_width(candidate: Candidate) -> Decimal:
    strikes = sorted({leg.strike for leg in candidate.structure.legs})
    if len(strikes) != 2:
        return Decimal("0")
    return strikes[1] - strikes[0]


def _high_conviction_failures(
    *,
    candidate: Candidate,
    confidence: Decimal,
    index_strategy: bool,
    reward_risk_ratio: Decimal,
    debit_to_width_ratio: Decimal | None,
    hard_gate_failures: tuple[str, ...],
) -> tuple[str, ...]:
    failures: list[str] = []
    if not index_strategy:
        failures.append("not_eligible_index_strategy")
    if confidence < HIGH_CONVICTION_MIN_CONFIDENCE:
        failures.append("confidence_below_threshold")
    if candidate.action is Action.INDEX_CONDOR:
        if candidate.richness_ratio < HIGH_CONVICTION_MIN_RICHNESS_RATIO:
            failures.append("richness_below_threshold")
        if reward_risk_ratio < HIGH_CONVICTION_MIN_CONDOR_REWARD_RISK_RATIO:
            failures.append("condor_reward_risk_below_threshold")
    elif candidate.action in DIRECTIONAL_INDEX_ACTIONS:
        if (
            candidate.trend_strength is None
            or candidate.trend_strength < HIGH_CONVICTION_MIN_DIRECTIONAL_TREND_STRENGTH
        ):
            failures.append("directional_trend_below_threshold")
        if reward_risk_ratio < HIGH_CONVICTION_MIN_DIRECTIONAL_REWARD_RISK_RATIO:
            failures.append("directional_reward_risk_below_threshold")
        if (
            debit_to_width_ratio is None
            or debit_to_width_ratio > HIGH_CONVICTION_MAX_DEBIT_TO_WIDTH_RATIO
        ):
            failures.append("directional_debit_to_width_above_threshold")
    for name in hard_gate_failures:
        failures.append(f"hard_gate_failed:{name}")
    return tuple(failures)
