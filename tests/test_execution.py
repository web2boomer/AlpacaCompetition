from datetime import UTC, datetime, timedelta
from decimal import Decimal

from money_machine.domain.schemas import OptionQuote
from money_machine.execution import ManagedStructure, stale_order_action, structure_exit_signal


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
    signal = structure_exit_signal(
        _managed(replay_candidate, opened_at=opened_at),
        quotes={},
        now=opened_at + timedelta(minutes=360),
    )
    assert signal.should_close
    assert signal.reason == "maximum_holding_time"
