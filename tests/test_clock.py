from datetime import timedelta

import pytest

from money_machine.domain.clock import (
    ENDS_AT,
    EOD_EQUITY_SNAPSHOT_AT,
    FLAT_TARGET_AT,
    FORCED_FLATTEN_STARTS_AT,
    NEW_ENTRY_CUTOFF,
    SCORING_STARTS_AT,
    competition_clock,
    is_official_performance_observation,
)
from money_machine.domain.enums import ExecutionState


@pytest.mark.parametrize(
    ("boundary", "before_state", "at_state"),
    [
        (SCORING_STARTS_AT, ExecutionState.OBSERVE_ONLY, ExecutionState.FULL_EXECUTION),
        (NEW_ENTRY_CUTOFF, ExecutionState.FULL_EXECUTION, ExecutionState.CLOSE_ONLY),
        (FORCED_FLATTEN_STARTS_AT, ExecutionState.CLOSE_ONLY, ExecutionState.CLOSE_ONLY),
        (FLAT_TARGET_AT, ExecutionState.CLOSE_ONLY, ExecutionState.CLOSE_ONLY),
        (EOD_EQUITY_SNAPSHOT_AT, ExecutionState.CLOSE_ONLY, ExecutionState.CLOSE_ONLY),
        (ENDS_AT, ExecutionState.CLOSE_ONLY, ExecutionState.DISABLED),
    ],
)
def test_state_one_second_before_at_and_after_each_transition(
    boundary, before_state, at_state
) -> None:
    before = competition_clock(boundary - timedelta(seconds=1), has_exposure=False)
    exact = competition_clock(boundary, has_exposure=False)
    after = competition_clock(boundary + timedelta(seconds=1), has_exposure=False)
    assert before.state is before_state
    assert exact.state is at_state
    assert after.state is at_state


@pytest.mark.parametrize(
    ("moment", "entries", "force_flatten", "flat_target", "eod", "window"),
    [
        (SCORING_STARTS_AT - timedelta(seconds=1), False, False, False, False, "pre_scoring"),
        (SCORING_STARTS_AT, True, False, False, False, "scoring"),
        (SCORING_STARTS_AT + timedelta(seconds=1), True, False, False, False, "scoring"),
        (NEW_ENTRY_CUTOFF - timedelta(seconds=1), True, False, False, False, "scoring"),
        (NEW_ENTRY_CUTOFF, False, False, False, False, "scoring"),
        (NEW_ENTRY_CUTOFF + timedelta(seconds=1), False, False, False, False, "scoring"),
        (FORCED_FLATTEN_STARTS_AT - timedelta(seconds=1), False, False, False, False, "scoring"),
        (FORCED_FLATTEN_STARTS_AT, False, True, False, False, "scoring"),
        (FORCED_FLATTEN_STARTS_AT + timedelta(seconds=1), False, True, False, False, "scoring"),
        (FLAT_TARGET_AT - timedelta(seconds=1), False, True, False, False, "scoring"),
        (FLAT_TARGET_AT, False, True, True, False, "scoring"),
        (FLAT_TARGET_AT + timedelta(seconds=1), False, True, True, False, "scoring"),
        (EOD_EQUITY_SNAPSHOT_AT - timedelta(seconds=1), False, True, True, False, "scoring"),
        (EOD_EQUITY_SNAPSHOT_AT, False, True, True, True, "eod_measurement"),
        (
            EOD_EQUITY_SNAPSHOT_AT + timedelta(seconds=1),
            False,
            True,
            True,
            True,
            "post_scoring",
        ),
    ],
)
def test_transition_flags(moment, entries, force_flatten, flat_target, eod, window) -> None:
    result = competition_clock(moment, has_exposure=False)
    assert result.allow_new_entries is entries
    assert result.force_flatten_all is force_flatten
    assert result.flat_target_reached is flat_target
    assert result.eod_equity_measurement_reached is eod
    assert result.scoring_window_state == window


def test_after_end_preserves_close_authority_with_any_broker_exposure() -> None:
    result = competition_clock(ENDS_AT + timedelta(days=1), has_exposure=True)
    assert result.state is ExecutionState.CLOSE_ONLY_UNTIL_FLAT
    assert not result.allow_new_entries
    assert not result.competition_complete


@pytest.mark.parametrize(
    ("moment", "official"),
    [
        (SCORING_STARTS_AT - timedelta(seconds=1), False),
        (SCORING_STARTS_AT, True),
        (SCORING_STARTS_AT + timedelta(seconds=1), True),
        (EOD_EQUITY_SNAPSHOT_AT - timedelta(seconds=1), True),
        (EOD_EQUITY_SNAPSHOT_AT, True),
        (EOD_EQUITY_SNAPSHOT_AT + timedelta(seconds=1), False),
    ],
)
def test_official_performance_classification_boundaries(moment, official) -> None:
    assert is_official_performance_observation(moment) is official


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        competition_clock(SCORING_STARTS_AT.replace(tzinfo=None), has_exposure=False)


def test_authoritative_equity_lock_precedes_formal_hackathon_end() -> None:
    assert EOD_EQUITY_SNAPSHOT_AT.isoformat() == "2026-09-03T20:00:00+00:00"
    assert ENDS_AT.isoformat() == "2026-09-04T13:30:00+00:00"
    assert EOD_EQUITY_SNAPSHOT_AT < ENDS_AT
