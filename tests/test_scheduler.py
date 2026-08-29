from datetime import timedelta

from money_machine.domain.clock import FORCED_FLATTEN_STARTS_AT
from money_machine.scheduler import scheduler_interval_seconds


def test_scheduler_accelerates_during_liquidation_until_broker_flat() -> None:
    assert (
        scheduler_interval_seconds(
            FORCED_FLATTEN_STARTS_AT - timedelta(seconds=1),
            broker_confirmed_flat=False,
        )
        == 300
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
        == 300
    )
