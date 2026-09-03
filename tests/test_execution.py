from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from money_machine.domain.enums import Action
from money_machine.domain.schemas import OptionQuote
from money_machine.execution import (
    COMPETITION_DIRECTIONAL_DEBIT_TAKE_PROFIT_MULTIPLE,
    DEBIT_STOP_VALUE_FRACTION,
    MAVERICK_DIRECTIONAL_DEBIT_TAKE_PROFIT_MULTIPLE,
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


def test_directional_debit_take_profit_and_stop_are_executable_quote_driven(
    directional_candidate,
) -> None:
    opened_at = datetime(2026, 8, 31, 14, tzinfo=UTC)
    managed = _managed(directional_candidate, opened_at=opened_at)
    long_leg = next(leg for leg in directional_candidate.structure.legs if leg.side.value == "buy")
    short_leg = next(
        leg for leg in directional_candidate.structure.legs if leg.side.value == "sell"
    )

    def quotes(long_bid: Decimal, short_ask: Decimal):
        return {
            long_leg.symbol: OptionQuote(
                symbol=long_leg.symbol,
                underlying=long_leg.underlying,
                expiration=long_leg.expiration,
                right=long_leg.right,
                strike=long_leg.strike,
                bid=long_bid,
                ask=long_bid + Decimal("0.10"),
                volume=100,
                implied_volatility=Decimal("0.20"),
                observed_at=opened_at,
            ),
            short_leg.symbol: OptionQuote(
                symbol=short_leg.symbol,
                underlying=short_leg.underlying,
                expiration=short_leg.expiration,
                right=short_leg.right,
                strike=short_leg.strike,
                bid=max(Decimal("0.01"), short_ask - Decimal("0.10")),
                ask=short_ask,
                volume=100,
                implied_volatility=Decimal("0.20"),
                observed_at=opened_at,
            ),
        }

    take_profit = structure_exit_signal(
        managed,
        quotes=quotes(Decimal("4.00"), Decimal("0.20")),
        now=opened_at + timedelta(minutes=30),
    )
    stop = structure_exit_signal(
        managed,
        quotes=quotes(Decimal("0.20"), Decimal("0.15")),
        now=opened_at + timedelta(minutes=30),
    )
    timed = structure_exit_signal(
        replace(managed, maximum_holding_minutes=60),
        quotes={},
        now=opened_at + timedelta(minutes=60),
    )
    assert (take_profit.should_close, take_profit.reason) == (True, "debit_take_profit")
    assert (stop.should_close, stop.reason) == (True, "debit_stop_loss")
    assert (timed.should_close, timed.reason) == (True, "maximum_holding_time")


@pytest.mark.parametrize(
    ("offset", "should_close"),
    [
        (Decimal("-0.01"), False),
        (Decimal("0"), True),
        (Decimal("0.01"), True),
    ],
)
def test_competition_directional_debit_take_profit_boundary(
    directional_candidate, offset: Decimal, should_close: bool
) -> None:
    assert Decimal("1.35") == COMPETITION_DIRECTIONAL_DEBIT_TAKE_PROFIT_MULTIPLE
    opened_at = datetime(2026, 9, 3, 14, tzinfo=UTC)
    managed = _managed(directional_candidate, opened_at=opened_at)
    long_leg = next(leg for leg in managed.structure.legs if leg.side.value == "buy")
    short_leg = next(leg for leg in managed.structure.legs if leg.side.value == "sell")
    target = (
        managed.structure.net_price * COMPETITION_DIRECTIONAL_DEBIT_TAKE_PROFIT_MULTIPLE + offset
    )
    quotes = {
        long_leg.symbol: OptionQuote(
            symbol=long_leg.symbol,
            underlying=long_leg.underlying,
            expiration=long_leg.expiration,
            right=long_leg.right,
            strike=long_leg.strike,
            bid=target + Decimal("0.10"),
            ask=target + Decimal("0.11"),
            volume=100,
            implied_volatility=Decimal("0.20"),
            observed_at=opened_at,
        ),
        short_leg.symbol: OptionQuote(
            symbol=short_leg.symbol,
            underlying=short_leg.underlying,
            expiration=short_leg.expiration,
            right=short_leg.right,
            strike=short_leg.strike,
            bid=Decimal("0.09"),
            ask=Decimal("0.10"),
            volume=100,
            implied_volatility=Decimal("0.20"),
            observed_at=opened_at,
        ),
    }

    signal = structure_exit_signal(managed, quotes=quotes, now=opened_at + timedelta(minutes=5))

    assert signal.should_close is should_close
    assert signal.executable_price == target
    assert signal.reason == ("debit_take_profit" if should_close else "debit_exit_not_reached")


def test_directional_debit_stop_remains_at_point_sixty_five(directional_candidate) -> None:
    opened_at = datetime(2026, 9, 3, 14, tzinfo=UTC)
    managed = _managed(directional_candidate, opened_at=opened_at)
    long_leg = next(leg for leg in managed.structure.legs if leg.side.value == "buy")
    short_leg = next(leg for leg in managed.structure.legs if leg.side.value == "sell")
    stop_value = managed.structure.net_price * DEBIT_STOP_VALUE_FRACTION
    quotes = {
        long_leg.symbol: OptionQuote(
            symbol=long_leg.symbol,
            underlying=long_leg.underlying,
            expiration=long_leg.expiration,
            right=long_leg.right,
            strike=long_leg.strike,
            bid=stop_value + Decimal("0.10"),
            ask=stop_value + Decimal("0.11"),
            volume=100,
            implied_volatility=Decimal("0.20"),
            observed_at=opened_at,
        ),
        short_leg.symbol: OptionQuote(
            symbol=short_leg.symbol,
            underlying=short_leg.underlying,
            expiration=short_leg.expiration,
            right=short_leg.right,
            strike=short_leg.strike,
            bid=Decimal("0.09"),
            ask=Decimal("0.10"),
            volume=100,
            implied_volatility=Decimal("0.20"),
            observed_at=opened_at,
        ),
    }

    signal = structure_exit_signal(managed, quotes=quotes, now=opened_at + timedelta(minutes=5))

    assert signal.should_close
    assert signal.reason == "debit_stop_loss"
    assert signal.executable_price == stop_value


def test_competition_directional_take_profit_does_not_apply_to_other_debits(
    directional_candidate,
) -> None:
    opened_at = datetime(2026, 9, 3, 14, tzinfo=UTC)
    structure = directional_candidate.structure.model_copy(update={"strategy": Action.HEDGE})
    managed = replace(_managed(directional_candidate, opened_at=opened_at), structure=structure)
    long_leg = next(leg for leg in structure.legs if leg.side.value == "buy")
    short_leg = next(leg for leg in structure.legs if leg.side.value == "sell")
    close_credit = structure.net_price * COMPETITION_DIRECTIONAL_DEBIT_TAKE_PROFIT_MULTIPLE
    quotes = {
        long_leg.symbol: OptionQuote(
            symbol=long_leg.symbol,
            underlying=long_leg.underlying,
            expiration=long_leg.expiration,
            right=long_leg.right,
            strike=long_leg.strike,
            bid=close_credit + Decimal("0.10"),
            ask=close_credit + Decimal("0.11"),
            volume=100,
            implied_volatility=Decimal("0.20"),
            observed_at=opened_at,
        ),
        short_leg.symbol: OptionQuote(
            symbol=short_leg.symbol,
            underlying=short_leg.underlying,
            expiration=short_leg.expiration,
            right=short_leg.right,
            strike=short_leg.strike,
            bid=Decimal("0.09"),
            ask=Decimal("0.10"),
            volume=100,
            implied_volatility=Decimal("0.20"),
            observed_at=opened_at,
        ),
    }

    signal = structure_exit_signal(managed, quotes=quotes, now=opened_at + timedelta(minutes=5))

    assert not signal.should_close


def test_maverick_directional_uses_persisted_two_point_ten_target(
    directional_candidate,
) -> None:
    opened_at = datetime(2026, 9, 3, 14, tzinfo=UTC)
    managed = replace(
        _managed(directional_candidate, opened_at=opened_at),
        take_profit_multiple=MAVERICK_DIRECTIONAL_DEBIT_TAKE_PROFIT_MULTIPLE,
    )
    long_leg = next(leg for leg in managed.structure.legs if leg.side.value == "buy")
    short_leg = next(leg for leg in managed.structure.legs if leg.side.value == "sell")
    target = managed.structure.net_price * Decimal("2.10")
    quotes = {
        long_leg.symbol: OptionQuote(
            **long_leg.model_dump(exclude={"side", "position_intent", "ratio_qty", "bid", "ask"}),
            bid=target + Decimal("0.10"),
            ask=target + Decimal("0.11"),
            implied_volatility=Decimal("0.20"),
            observed_at=opened_at,
        ),
        short_leg.symbol: OptionQuote(
            **short_leg.model_dump(exclude={"side", "position_intent", "ratio_qty", "bid", "ask"}),
            bid=Decimal("0.09"),
            ask=Decimal("0.10"),
            implied_volatility=Decimal("0.20"),
            observed_at=opened_at,
        ),
    }

    signal = structure_exit_signal(managed, quotes=quotes, now=opened_at + timedelta(minutes=5))

    assert signal.should_close
    assert signal.reason == "debit_take_profit"


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
    last_accepted_cycle = entry_holding_policy(datetime(2026, 9, 2, 19, 20, tzinfo=UTC), 60)
    rejected = entry_holding_policy(datetime(2026, 9, 1, 19, 21, tzinfo=UTC), 60)
    assert accepted.accepted
    assert accepted.effective_deadline == datetime(2026, 9, 1, 19, 50, tzinfo=UTC)
    assert accepted.reason == "daily_boundary"
    assert last_accepted_cycle.accepted
    assert last_accepted_cycle.effective_holding_minutes == 30
    assert last_accepted_cycle.effective_deadline == datetime(2026, 9, 2, 19, 50, tzinfo=UTC)
    assert not rejected.accepted
    assert rejected.reason == "insufficient_tradable_session_window"


def test_directional_holding_policy_clamps_model_and_event_deadline() -> None:
    opened_at = datetime(2026, 9, 2, 16, 50, tzinfo=UTC)
    event_deadline = datetime(2026, 9, 2, 17, 45, tzinfo=UTC)

    policy = entry_holding_policy(
        opened_at,
        360,
        maximum_holding_minutes=60,
        hard_deadline=event_deadline,
    )

    assert policy.accepted
    assert policy.effective_holding_minutes == 55
    assert policy.effective_deadline == event_deadline
    assert policy.reason == "directional_or_session_boundary"


def test_lifecycle_closes_at_persisted_event_safe_deadline(replay_candidate) -> None:
    opened_at = datetime(2026, 9, 2, 16, 50, tzinfo=UTC)
    managed = replace(
        _managed(replay_candidate, opened_at=opened_at),
        maximum_holding_minutes=55,
    )

    signal = structure_exit_signal(
        managed,
        quotes={},
        now=datetime(2026, 9, 2, 17, 45, tzinfo=UTC),
    )

    assert signal.should_close
    assert signal.reason == "maximum_holding_time"


def test_daily_deadline_is_dst_aware_and_final_hour_flatten_is_earlier() -> None:
    assert daily_hard_exit_deadline(datetime(2026, 7, 1, 14, tzinfo=UTC)).hour == 19
    assert daily_hard_exit_deadline(datetime(2026, 1, 5, 15, tzinfo=UTC)).hour == 20
    thursday = entry_holding_policy(datetime(2026, 9, 3, 19, 0, tzinfo=UTC), 120)
    assert thursday.effective_deadline == datetime(2026, 9, 3, 19, 35, tzinfo=UTC)
    assert thursday.accepted


def test_final_hour_exact_1520_entry_has_fifteen_minute_managed_window() -> None:
    exact = entry_holding_policy(datetime(2026, 9, 3, 19, 20, tzinfo=UTC), 45)
    late = entry_holding_policy(datetime(2026, 9, 3, 19, 20, 1, tzinfo=UTC), 45)

    assert exact.accepted
    assert exact.effective_holding_minutes == 15
    assert exact.effective_deadline == datetime(2026, 9, 3, 19, 35, tzinfo=UTC)
    assert not late.accepted


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
        original_limit=Decimal("5.00"),
        is_credit=False,
        urgent_close=True,
        fresh_executable_limit=Decimal("1.40"),
        fresh_is_credit=False,
        urgent_debit_cap=Decimal("5.00"),
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
        urgent_debit_cap=Decimal("5.00"),
    )
    assert resting.action == "wait"
    assert "rests at defined-risk hard cap" in resting.reason
    assert moved.action == "cancel_and_replace"
    assert moved.next_limit == Decimal("5.00")
