from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from money_machine.domain.enums import (
    Action,
    ExecutionState,
    OptionRight,
    PositionIntent,
    Regime,
    RiskReason,
    Side,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AccountSnapshot(StrictModel):
    account_id: str
    account_number: str | None = None
    status: str = "ACTIVE"
    is_paper: bool
    equity: Decimal
    cash: Decimal
    buying_power: Decimal
    portfolio_value: Decimal
    realized_pl: Decimal = Decimal("0")
    unrealized_pl: Decimal = Decimal("0")


class OptionQuote(StrictModel):
    symbol: str
    underlying: str
    expiration: datetime
    right: OptionRight
    strike: Decimal
    bid: Decimal = Field(ge=0)
    ask: Decimal = Field(ge=0)
    volume: int | None = Field(default=None, ge=0)
    open_interest: int | None = Field(default=None, ge=0)
    bid_size: int | None = Field(default=None, ge=0)
    ask_size: int | None = Field(default=None, ge=0)
    implied_volatility: Decimal = Field(ge=0)
    delta: Decimal | None = None
    observed_at: datetime

    @model_validator(mode="after")
    def ask_not_below_bid(self) -> "OptionQuote":
        if self.ask < self.bid:
            raise ValueError("ask must be greater than or equal to bid")
        return self

    @property
    def midpoint(self) -> Decimal:
        return (self.bid + self.ask) / Decimal("2")

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid


class UnderlyingSnapshot(StrictModel):
    symbol: str
    spot: Decimal = Field(gt=0)
    previous_close: Decimal = Field(gt=0)
    realized_move_pct: Decimal = Field(gt=0)
    implied_move_pct: Decimal = Field(gt=0)
    trend_return_pct: Decimal
    event_risk: bool = False
    observed_at: datetime

    @property
    def richness_ratio(self) -> Decimal:
        return self.implied_move_pct / self.realized_move_pct


class OptionLeg(StrictModel):
    symbol: str
    underlying: str
    expiration: datetime
    right: OptionRight
    strike: Decimal
    side: Side
    position_intent: PositionIntent
    ratio_qty: int = Field(default=1, ge=1, le=4)
    bid: Decimal = Field(ge=0)
    ask: Decimal = Field(ge=0)
    volume: int | None = Field(default=None, ge=0)
    open_interest: int | None = Field(default=None, ge=0)
    bid_size: int | None = Field(default=None, ge=0)
    ask_size: int | None = Field(default=None, ge=0)

    @property
    def midpoint(self) -> Decimal:
        return (self.bid + self.ask) / Decimal("2")


class OptionStructure(StrictModel):
    strategy: Action
    underlying: str
    expiration: datetime
    legs: tuple[OptionLeg, ...]
    net_price: Decimal = Field(gt=0)
    maximum_loss: Decimal = Field(gt=0)
    maximum_profit: Decimal = Field(gt=0)
    is_credit: bool


class Candidate(StrictModel):
    candidate_id: str
    action: Action
    structure: OptionStructure
    score: Decimal
    expected_credit_or_debit: Decimal = Field(gt=0)
    structure_spread: Decimal = Field(ge=0)
    richness_ratio: Decimal = Field(gt=0)
    data_age_seconds: int = Field(ge=0)
    event_risk: bool
    liquidity_passed: bool
    trend_strength: Decimal | None = Field(default=None, ge=0)
    direction_agrees: bool = True
    minimum_confidence: Decimal = Decimal("0")
    payoff_quality_ratio: Decimal | None = Field(default=None, ge=0)
    maximum_holding_minutes: int = Field(default=0, ge=0, le=60)
    holding_deadline: datetime | None = None
    gate_evidence: tuple[str, ...]


class ModelDecision(StrictModel):
    regime: Regime
    action: Action
    candidate_id: str | None
    confidence: float = Field(ge=0, le=1)
    thesis: str = Field(min_length=1, max_length=600)
    evidence: tuple[str, ...] = Field(max_length=8)
    invalidation: tuple[str, ...] = Field(max_length=6)
    maximum_holding_minutes: int = Field(ge=0, le=10080)

    @model_validator(mode="after")
    def candidate_matches_action(self) -> "ModelDecision":
        if self.action is Action.ABSTAIN and self.candidate_id is not None:
            raise ValueError("abstention cannot select a candidate")
        if self.action is not Action.ABSTAIN and not self.candidate_id:
            raise ValueError("a trading action must select a candidate")
        return self

    @classmethod
    def abstention(cls, reason: str) -> "ModelDecision":
        return cls(
            regime=Regime.DISLOCATED,
            action=Action.ABSTAIN,
            candidate_id=None,
            confidence=0,
            thesis=reason[:600],
            evidence=("Model output was unavailable or rejected by schema validation.",),
            invalidation=("A later cycle may proceed after valid data and output are available.",),
            maximum_holding_minutes=0,
        )


class ModelDecisionEnvelope(StrictModel):
    decision: ModelDecision
    raw_response_hash: str
    validation_error: str | None = None
    provider: Literal["deterministic", "replay", "openai"] = "deterministic"
    model: str | None = None
    provider_response_id_hash: str | None = None
    selection_attempts: int = Field(default=1, ge=1, le=2)
    candidate_id_retry_used: bool = False
    initial_raw_response_hash: str | None = None


class RiskCheck(StrictModel):
    name: str
    passed: bool
    actual: str
    limit: str
    reason: RiskReason


class RiskDecisionResult(StrictModel):
    approved: bool
    quantity: int = Field(ge=0)
    awarded_risk: Decimal = Field(ge=0)
    reason_codes: tuple[RiskReason, ...]
    checks: tuple[RiskCheck, ...]


class RiskContext(StrictModel):
    now: datetime
    execution_state: ExecutionState
    equity: Decimal = Field(gt=0)
    start_of_day_equity: Decimal = Field(gt=0)
    peak_equity: Decimal = Field(gt=0)
    total_open_defined_loss: Decimal = Field(ge=0)
    index_cluster_defined_loss: Decimal = Field(ge=0)
    open_alpha_structures: int = Field(ge=0)
    pending_underlyings: frozenset[str] = frozenset()
    open_underlyings: frozenset[str] = frozenset()
    kill_switch_active: bool = False
    reconciliation_clean: bool = True
    daily_loss_entry_halt_active: bool = False


class AuctionResult(StrictModel):
    ranked_candidate_ids: tuple[str, ...]
    selected_candidate_id: str | None
    cash_won: bool
    awarded_risk: Decimal = Decimal("0")


class BrokerOrderRequest(StrictModel):
    client_order_id: str
    candidate_id: str
    quantity: int = Field(ge=1)
    limit_price: Decimal = Field(gt=0)
    is_credit: bool
    legs: tuple[OptionLeg, ...]
    environment_role: str
    attempt: int = Field(default=0, ge=0, le=2)
    is_closing: bool = False
    parent_client_order_id: str | None = None
    exit_reason: str | None = None
    exit_urgency: Literal["soft", "urgent"] | None = None

    @model_validator(mode="after")
    def intents_match_authority(self) -> "BrokerOrderRequest":
        closing_intents = {PositionIntent.BUY_TO_CLOSE, PositionIntent.SELL_TO_CLOSE}
        opening_intents = {PositionIntent.BUY_TO_OPEN, PositionIntent.SELL_TO_OPEN}
        intents = {leg.position_intent for leg in self.legs}
        required = closing_intents if self.is_closing else opening_intents
        if not intents or not intents.issubset(required):
            raise ValueError("order leg intents do not match entry/close authority")
        if self.is_closing != bool(self.parent_client_order_id):
            raise ValueError("closing orders require exactly one managed opening parent")
        return self


class BrokerOrderResult(StrictModel):
    broker_order_id: str
    client_order_id: str
    status: str
    submitted_at: datetime
    raw: dict[str, Any] = Field(default_factory=dict)
