from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from money_machine.domain.enums import ExecutionState

HACKATHON_STARTS_AT = datetime(2026, 8, 28, 13, 30, tzinfo=UTC)
SCORING_STARTS_AT = datetime(2026, 8, 31, 13, 30, tzinfo=UTC)
# Backwards-compatible name for the start of trading authority.
STARTS_AT = SCORING_STARTS_AT
NEW_ENTRY_CUTOFF = datetime(2026, 9, 3, 19, 30, tzinfo=UTC)
SHORT_VOL_FLATTEN_BY = datetime(2026, 9, 3, 19, 40, tzinfo=UTC)
FINAL_FLATTEN_BY = datetime(2026, 9, 3, 19, 45, tzinfo=UTC)
ENDS_AT = datetime(2026, 9, 4, 13, 30, tzinfo=UTC)
BASELINE_EQUITY = Decimal("100000.00")


@dataclass(frozen=True, slots=True)
class CompetitionClockSnapshot:
    at: datetime
    state: ExecutionState
    allow_new_entries: bool
    must_flatten_short_vol: bool
    must_flatten_all: bool
    competition_complete: bool


def competition_clock(at: datetime, *, has_positions: bool) -> CompetitionClockSnapshot:
    if at.tzinfo is None:
        raise ValueError("competition clock requires a timezone-aware timestamp")
    now = at.astimezone(UTC)
    if now < STARTS_AT:
        state = ExecutionState.OBSERVE_ONLY
    elif now < NEW_ENTRY_CUTOFF:
        state = ExecutionState.FULL_EXECUTION
    elif now < ENDS_AT:
        state = ExecutionState.CLOSE_ONLY
    elif has_positions:
        state = ExecutionState.CLOSE_ONLY_UNTIL_FLAT
    else:
        state = ExecutionState.DISABLED
    return CompetitionClockSnapshot(
        at=now,
        state=state,
        allow_new_entries=state is ExecutionState.FULL_EXECUTION,
        must_flatten_short_vol=now >= SHORT_VOL_FLATTEN_BY,
        must_flatten_all=now >= FINAL_FLATTEN_BY,
        competition_complete=now >= ENDS_AT and not has_positions,
    )
