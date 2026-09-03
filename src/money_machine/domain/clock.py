from dataclasses import dataclass
from datetime import UTC, datetime, time
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from money_machine.domain.enums import ExecutionState

HACKATHON_STARTS_AT = datetime(2026, 8, 28, 13, 30, tzinfo=UTC)
SCORING_STARTS_AT = datetime(2026, 8, 31, 13, 30, tzinfo=UTC)
# Backwards-compatible name for the start of trading authority.
STARTS_AT = SCORING_STARTS_AT
FINAL_HOUR_RECOVERY_STARTS_AT = datetime(2026, 9, 3, 19, 0, tzinfo=UTC)
NEW_ENTRY_CUTOFF = datetime(2026, 9, 3, 19, 20, tzinfo=UTC)
FORCED_FLATTEN_STARTS_AT = datetime(2026, 9, 3, 19, 35, tzinfo=UTC)
FLAT_TARGET_AT = datetime(2026, 9, 3, 19, 50, tzinfo=UTC)
EOD_EQUITY_SNAPSHOT_AT = datetime(2026, 9, 3, 20, 0, tzinfo=UTC)
ENDS_AT = datetime(2026, 9, 4, 13, 30, tzinfo=UTC)
BASELINE_EQUITY = Decimal("100000.00")

ScoringWindowState = Literal["pre_scoring", "scoring", "eod_measurement", "post_scoring"]
MarketSessionPhase = Literal["market_hours", "extended_hours", "overnight"]
NEW_YORK = ZoneInfo("America/New_York")
DAILY_ENTRY_START_TIME = time(9, 45)
DAILY_ENTRY_CUTOFF_TIME = time(15, 20)


@dataclass(frozen=True, slots=True)
class CompetitionClockSnapshot:
    at: datetime
    state: ExecutionState
    allow_new_entries: bool
    force_flatten_all: bool
    flat_target_reached: bool
    eod_equity_measurement_reached: bool
    competition_complete: bool
    scoring_window_state: ScoringWindowState


def scoring_window_state(at: datetime) -> ScoringWindowState:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("competition clock requires a timezone-aware timestamp")
    now = at.astimezone(UTC)
    if now < SCORING_STARTS_AT:
        return "pre_scoring"
    if now < EOD_EQUITY_SNAPSHOT_AT:
        return "scoring"
    if now == EOD_EQUITY_SNAPSHOT_AT:
        return "eod_measurement"
    return "post_scoring"


def is_official_performance_observation(at: datetime) -> bool:
    return scoring_window_state(at) in {"scoring", "eod_measurement"}


def market_session_phase(at: datetime) -> MarketSessionPhase:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("market session requires a timezone-aware timestamp")
    local = at.astimezone(NEW_YORK)
    if local.weekday() >= 5:
        return "overnight"
    wall_time = local.time().replace(tzinfo=None)
    if time(9, 30) <= wall_time < time(16):
        return "market_hours"
    if time(4) <= wall_time < time(9, 30) or time(16) <= wall_time < time(20):
        return "extended_hours"
    return "overnight"


def is_regular_market_performance_observation(at: datetime) -> bool:
    """Return whether an observation can establish competition risk baselines."""
    return is_official_performance_observation(at) and market_session_phase(at) == "market_hours"


def competition_entry_window_open(at: datetime) -> bool:
    """Return whether a new competition entry may be considered at this wall-clock time."""
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("competition entry window requires a timezone-aware timestamp")
    local = at.astimezone(NEW_YORK)
    wall_time = local.time().replace(tzinfo=None)
    final_day = local.date() == EOD_EQUITY_SNAPSHOT_AT.astimezone(NEW_YORK).date()
    before_cutoff = (
        wall_time <= DAILY_ENTRY_CUTOFF_TIME if final_day else wall_time < DAILY_ENTRY_CUTOFF_TIME
    )
    return local.weekday() < 5 and wall_time >= DAILY_ENTRY_START_TIME and before_cutoff


def competition_clock(at: datetime, *, has_exposure: bool) -> CompetitionClockSnapshot:
    if at.tzinfo is None:
        raise ValueError("competition clock requires a timezone-aware timestamp")
    now = at.astimezone(UTC)
    if now < STARTS_AT:
        state = ExecutionState.OBSERVE_ONLY
    elif now <= NEW_ENTRY_CUTOFF:
        state = ExecutionState.FULL_EXECUTION
    elif now < ENDS_AT:
        state = ExecutionState.CLOSE_ONLY
    elif has_exposure:
        state = ExecutionState.CLOSE_ONLY_UNTIL_FLAT
    else:
        state = ExecutionState.DISABLED
    return CompetitionClockSnapshot(
        at=now,
        state=state,
        allow_new_entries=state is ExecutionState.FULL_EXECUTION,
        force_flatten_all=now >= FORCED_FLATTEN_STARTS_AT,
        flat_target_reached=now >= FLAT_TARGET_AT,
        eod_equity_measurement_reached=now >= EOD_EQUITY_SNAPSHOT_AT,
        competition_complete=now >= ENDS_AT and not has_exposure,
        scoring_window_state=scoring_window_state(now),
    )
