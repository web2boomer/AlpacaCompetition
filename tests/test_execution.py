from datetime import UTC, datetime, timedelta
from decimal import Decimal

from money_machine.execution import stale_order_action


def test_fresh_order_waits() -> None:
    submitted = datetime(2026, 8, 28, 15, tzinfo=UTC)
    action = stale_order_action(
        submitted_at=submitted,
        now=submitted + timedelta(seconds=89),
        attempt=0,
        original_limit=Decimal("1.00"),
        is_credit=True,
    )
    assert action.action == "wait"


def test_repricing_is_bounded() -> None:
    submitted = datetime(2026, 8, 28, 15, tzinfo=UTC)
    first = stale_order_action(
        submitted_at=submitted,
        now=submitted + timedelta(seconds=90),
        attempt=0,
        original_limit=Decimal("1.00"),
        is_credit=True,
    )
    exhausted = stale_order_action(
        submitted_at=submitted,
        now=submitted + timedelta(minutes=5),
        attempt=2,
        original_limit=Decimal("1.00"),
        is_credit=True,
    )
    assert first.action == "cancel_and_replace"
    assert first.next_limit == Decimal("0.95")
    assert exhausted.action == "cancel"
