from datetime import timedelta

from money_machine.domain.clock import (
    EOD_EQUITY_SNAPSHOT_AT,
    FINAL_HOUR_RECOVERY_STARTS_AT,
    FORCED_FLATTEN_STARTS_AT,
)
from money_machine.scheduler import scheduler_interval_seconds, scheduler_lease_ttl_seconds


def test_scheduler_accelerates_during_liquidation_until_broker_flat() -> None:
    assert (
        scheduler_interval_seconds(
            FORCED_FLATTEN_STARTS_AT - timedelta(seconds=1),
            broker_confirmed_flat=False,
        )
        == 60
    )
    assert (
        scheduler_interval_seconds(
            FORCED_FLATTEN_STARTS_AT,
            broker_confirmed_flat=False,
        )
        == 60
    )
    assert (
        scheduler_interval_seconds(
            FORCED_FLATTEN_STARTS_AT + timedelta(days=1),
            broker_confirmed_flat=False,
        )
        == 60
    )
    assert (
        scheduler_interval_seconds(
            FORCED_FLATTEN_STARTS_AT,
            broker_confirmed_flat=True,
        )
        == 60
    )


def test_scheduler_runs_every_minute_through_final_window_even_when_flat() -> None:
    assert (
        scheduler_interval_seconds(
            FINAL_HOUR_RECOVERY_STARTS_AT - timedelta(seconds=1), broker_confirmed_flat=True
        )
        == 300
    )
    assert (
        scheduler_interval_seconds(FINAL_HOUR_RECOVERY_STARTS_AT, broker_confirmed_flat=True) == 60
    )
    assert scheduler_interval_seconds(EOD_EQUITY_SNAPSHOT_AT, broker_confirmed_flat=True) == 60
    assert (
        scheduler_interval_seconds(
            EOD_EQUITY_SNAPSHOT_AT + timedelta(seconds=1), broker_confirmed_flat=True
        )
        == 300
    )


def test_scheduler_lease_expires_between_final_window_cycles() -> None:
    assert (
        scheduler_lease_ttl_seconds(FINAL_HOUR_RECOVERY_STARTS_AT, broker_confirmed_flat=True) == 90
    )
    assert (
        scheduler_lease_ttl_seconds(
            FINAL_HOUR_RECOVERY_STARTS_AT - timedelta(seconds=1), broker_confirmed_flat=True
        )
        == 360
    )
