from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from money_machine.domain.schemas import OptionQuote
from money_machine.execution import (
    ManagedStructure,
    daily_hard_exit_deadline,
    entry_holding_policy,
    stale_order_action,
    structure_exit_signal,
)


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


def _managed(replay_candidate, *, opened_at: datetime) -> ManagedStructure:
    return ManagedStructure(
        agent_run_id="run",
        candidate_id=replay_candidate.candidate_id,
        client_order_id="client",
        broker_order_id="broker",
        status="filled",
        quantity=1,
        opened_at=opened_at,
        maximum_holding_minutes=360,
        structure=replay_candidate.structure,
    )


def _quotes(replay_candidate, *, sold_ask: Decimal, bought_bid: Decimal):
    quotes = {}
    for leg in replay_candidate.structure.legs:
        is_sold = leg.side.value == "sell"
        bid = Decimal("0.05") if is_sold else bought_bid
        ask = sold_ask if is_sold else max(bought_bid, Decimal("0.10"))
        quotes[leg.symbol] = OptionQuote(
            symbol=leg.symbol,
            underlying=leg.underlying,
            expiration=leg.expiration,
            right=leg.right,
            strike=leg.strike,
            bid=bid,
            ask=ask,
            volume=leg.volume,
            open_interest=leg.open_interest,
            implied_volatility=Decimal("0.20"),
            observed_at=datetime(2026, 8, 31, 15, tzinfo=UTC),
        )
    return quotes


def test_credit_structure_takes_profit_from_executable_close_quotes(replay_candidate) -> None:
    assert replay_candidate.structure.is_credit
    opened_at = datetime(2026, 8, 31, 14, tzinfo=UTC)
    signal = structure_exit_signal(
        _managed(replay_candidate, opened_at=opened_at),
        quotes=_quotes(replay_candidate, sold_ask=Decimal("0.10"), bought_bid=Decimal("0.05")),
        now=opened_at + timedelta(minutes=30),
    )
    assert signal.should_close
    assert signal.reason == "credit_take_profit"


def test_structure_closes_at_maximum_holding_time_without_waiting_for_price(
    replay_candidate,
) -> None:
    opened_at = datetime(2026, 8, 31, 14, tzinfo=UTC)
    managed = replace(_managed(replay_candidate, opened_at=opened_at), maximum_holding_minutes=60)
    signal = structure_exit_signal(
        managed,
        quotes={},
        now=opened_at + timedelta(minutes=60),
    )
    assert signal.should_close
    assert signal.reason == "maximum_holding_time"
    assert signal.urgency == "soft"


def test_daily_hard_boundary_is_urgent_and_prevents_overnight_roll(replay_candidate) -> None:
    opened_at = datetime(2026, 8, 31, 14, tzinfo=UTC)
    signal = structure_exit_signal(
        _managed(replay_candidate, opened_at=opened_at),
        quotes={},
        now=datetime(2026, 8, 31, 19, 50, tzinfo=UTC),
    )
    assert signal.should_close
    assert signal.reason == "daily_hard_exit_boundary"
    assert signal.urgency == "urgent"


def test_entry_holding_policy_clamps_and_rejects_too_late() -> None:
    accepted = entry_holding_policy(datetime(2026, 9, 1, 18, 0, tzinfo=UTC), 360)
    rejected = entry_holding_policy(datetime(2026, 9, 1, 19, 21, tzinfo=UTC), 60)
    assert accepted.accepted
    assert accepted.effective_deadline == datetime(2026, 9, 1, 19, 50, tzinfo=UTC)
    assert accepted.reason == "daily_boundary"
    assert not rejected.accepted
    assert rejected.reason == "insufficient_tradable_session_window"


def test_daily_deadline_is_dst_aware_and_thursday_flatten_is_earlier() -> None:
    assert daily_hard_exit_deadline(datetime(2026, 7, 1, 14, tzinfo=UTC)).hour == 19
    assert daily_hard_exit_deadline(datetime(2026, 1, 5, 15, tzinfo=UTC)).hour == 20
    thursday = entry_holding_policy(datetime(2026, 9, 3, 19, 0, tzinfo=UTC), 120)
    assert thursday.effective_deadline == datetime(2026, 9, 3, 19, 15, tzinfo=UTC)
    assert not thursday.accepted


def test_soft_close_backs_off_without_quote_change_but_urgent_exit_cancels() -> None:
    submitted = datetime(2026, 9, 1, 14, tzinfo=UTC)
    soft = stale_order_action(
        submitted_at=submitted,
        now=submitted + timedelta(minutes=20),
        attempt=2,
        original_limit=Decimal("1.00"),
        is_credit=True,
        soft_close=True,
        quote_materially_changed=False,
    )
    urgent = stale_order_action(
        submitted_at=submitted,
        now=submitted + timedelta(minutes=20),
        attempt=2,
        original_limit=Decimal("1.00"),
        is_credit=True,
    )
    assert soft.action == "wait"
    assert "materially changed" in soft.reason
    assert urgent.action == "cancel"


def test_urgent_close_reprices_from_fresh_executable_nbbo_with_bounded_concession() -> None:
    submitted = datetime(2026, 9, 1, 14, tzinfo=UTC)
    debit = stale_order_action(
        submitted_at=submitted,
        now=submitted + timedelta(minutes=5),
        attempt=0,
        original_limit=Decimal("1.00"),
        is_credit=False,
        urgent_close=True,
        fresh_executable_limit=Decimal("1.40"),
        fresh_is_credit=False,
    )
    credit = stale_order_action(
        submitted_at=submitted,
        now=submitted + timedelta(minutes=5),
        attempt=0,
        original_limit=Decimal("1.00"),
        is_credit=True,
        urgent_close=True,
        fresh_executable_limit=Decimal("1.40"),
        fresh_is_credit=True,
    )
    assert debit.action == "cancel_and_replace"
    assert debit.next_limit == Decimal("1.50")
    assert debit.next_is_credit is False
    assert credit.action == "cancel_and_replace"
    assert credit.next_limit == Decimal("1.30")
    assert credit.next_is_credit is True


def test_exhausted_urgent_close_rests_at_cap_until_fresh_nbbo_moves() -> None:
    submitted = datetime(2026, 9, 1, 14, tzinfo=UTC)
    resting = stale_order_action(
        submitted_at=submitted,
        now=submitted + timedelta(minutes=5),
        attempt=2,
        original_limit=Decimal("1.70"),
        is_credit=False,
        urgent_close=True,
        fresh_executable_limit=Decimal("1.40"),
        fresh_is_credit=False,
    )
    moved = stale_order_action(
        submitted_at=submitted,
        now=submitted + timedelta(minutes=5),
        attempt=2,
        original_limit=Decimal("1.70"),
        is_credit=False,
        urgent_close=True,
        fresh_executable_limit=Decimal("1.60"),
        fresh_is_credit=False,
    )
    assert resting.action == "wait"
    assert "rests at bounded executable cap" in resting.reason
    assert moved.action == "cancel_and_replace"
    assert moved.next_limit == Decimal("1.90")
