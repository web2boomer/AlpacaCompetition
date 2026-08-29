from enum import StrEnum


class AppEnvironment(StrEnum):
    DEVELOPMENT = "development"
    PRODUCTION = "production"


class AccountRole(StrEnum):
    DEVELOPMENT = "development"
    COMPETITION = "competition"


class RunMode(StrEnum):
    REPLAY = "replay"
    LIVE = "live"


class ExecutionState(StrEnum):
    OBSERVE_ONLY = "observe_only"
    FULL_EXECUTION = "full_execution"
    CLOSE_ONLY = "close_only"
    CLOSE_ONLY_UNTIL_FLAT = "close_only_until_flat"
    DISABLED = "disabled"
    HALTED = "halted"


class Regime(StrEnum):
    CALM = "calm"
    DIRECTIONAL_UP = "directional_up"
    DIRECTIONAL_DOWN = "directional_down"
    EVENT_RISK = "event_risk"
    DISLOCATED = "dislocated"


class Action(StrEnum):
    INDEX_CONDOR = "index_condor"
    CALL_DEBIT_SPREAD = "call_debit_spread"
    PUT_DEBIT_SPREAD = "put_debit_spread"
    EARNINGS_CONDOR = "earnings_condor"
    HEDGE = "hedge"
    ABSTAIN = "abstain"


class OptionRight(StrEnum):
    CALL = "call"
    PUT = "put"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class PositionIntent(StrEnum):
    BUY_TO_OPEN = "buy_to_open"
    BUY_TO_CLOSE = "buy_to_close"
    SELL_TO_OPEN = "sell_to_open"
    SELL_TO_CLOSE = "sell_to_close"


class RiskReason(StrEnum):
    APPROVED = "approved"
    ABSTAIN = "model_abstained"
    INVALID_MODEL_OUTPUT = "invalid_model_output"
    UNKNOWN_CANDIDATE = "unknown_candidate"
    ACTION_MISMATCH = "action_candidate_mismatch"
    NOT_FULL_EXECUTION = "not_full_execution"
    KILL_SWITCH = "kill_switch_active"
    RECONCILIATION = "reconciliation_not_clean"
    STALE_DATA = "stale_market_data"
    INCOMPLETE_CHAIN = "incomplete_option_chain"
    INVALID_STRUCTURE = "invalid_option_structure"
    LIQUIDITY = "liquidity_gate_failed"
    EVENT_RISK = "event_risk_veto"
    DAILY_LOSS = "daily_loss_stop"
    DRAWDOWN = "competition_drawdown_stop"
    OPEN_STRUCTURE_LIMIT = "open_structure_limit"
    PENDING_UNDERLYING = "pending_entry_for_underlying"
    PER_STRUCTURE_CAP = "per_structure_risk_cap"
    CLUSTER_CAP = "correlated_index_cluster_cap"
    TOTAL_CAP = "total_defined_loss_cap"
    ZERO_QUANTITY = "calculated_quantity_zero"
    DUPLICATE_CYCLE = "duplicate_cycle"
    DUPLICATE_ORDER = "duplicate_order"


class OrderStatus(StrEnum):
    PROPOSED = "proposed"
    RISK_APPROVED = "risk_approved"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"
