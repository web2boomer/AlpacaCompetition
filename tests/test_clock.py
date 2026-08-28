from datetime import timedelta

import pytest

from money_machine.domain.clock import (
    ENDS_AT,
    FINAL_FLATTEN_BY,
    NEW_ENTRY_CUTOFF,
    SHORT_VOL_FLATTEN_BY,
    STARTS_AT,
    competition_clock,
)
from money_machine.domain.enums import ExecutionState


@pytest.mark.parametrize(
    ("moment", "state", "entries", "short_flatten", "all_flatten", "complete"),
    [
        (STARTS_AT - timedelta(seconds=1), ExecutionState.OBSERVE_ONLY, False, False, False, False),
        (STARTS_AT, ExecutionState.FULL_EXECUTION, True, False, False, False),
        (
            STARTS_AT + timedelta(seconds=1),
            ExecutionState.FULL_EXECUTION,
            True,
            False,
            False,
            False,
        ),
        (
            NEW_ENTRY_CUTOFF - timedelta(seconds=1),
            ExecutionState.FULL_EXECUTION,
            True,
            False,
            False,
            False,
        ),
        (NEW_ENTRY_CUTOFF, ExecutionState.CLOSE_ONLY, False, False, False, False),
        (
            NEW_ENTRY_CUTOFF + timedelta(seconds=1),
            ExecutionState.CLOSE_ONLY,
            False,
            False,
            False,
            False,
        ),
        (
            SHORT_VOL_FLATTEN_BY - timedelta(seconds=1),
            ExecutionState.CLOSE_ONLY,
            False,
            False,
            False,
            False,
        ),
        (SHORT_VOL_FLATTEN_BY, ExecutionState.CLOSE_ONLY, False, True, False, False),
        (
            SHORT_VOL_FLATTEN_BY + timedelta(seconds=1),
            ExecutionState.CLOSE_ONLY,
            False,
            True,
            False,
            False,
        ),
        (
            FINAL_FLATTEN_BY - timedelta(seconds=1),
            ExecutionState.CLOSE_ONLY,
            False,
            True,
            False,
            False,
        ),
        (FINAL_FLATTEN_BY, ExecutionState.CLOSE_ONLY, False, True, True, False),
        (
            FINAL_FLATTEN_BY + timedelta(seconds=1),
            ExecutionState.CLOSE_ONLY,
            False,
            True,
            True,
            False,
        ),
        (ENDS_AT - timedelta(seconds=1), ExecutionState.CLOSE_ONLY, False, True, True, False),
        (ENDS_AT, ExecutionState.DISABLED, False, True, True, True),
        (ENDS_AT + timedelta(seconds=1), ExecutionState.DISABLED, False, True, True, True),
    ],
)
def test_every_competition_boundary(
    moment, state, entries, short_flatten, all_flatten, complete
) -> None:
    result = competition_clock(moment, has_positions=False)
    assert result.state is state
    assert result.allow_new_entries is entries
    assert result.must_flatten_short_vol is short_flatten
    assert result.must_flatten_all is all_flatten
    assert result.competition_complete is complete


def test_after_end_preserves_close_authority_with_positions() -> None:
    result = competition_clock(ENDS_AT + timedelta(days=1), has_positions=True)
    assert result.state is ExecutionState.CLOSE_ONLY_UNTIL_FLAT
    assert not result.allow_new_entries
    assert not result.competition_complete


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        competition_clock(STARTS_AT.replace(tzinfo=None), has_positions=False)
