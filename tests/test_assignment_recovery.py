from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import SecretStr

from money_machine.assignment_recovery import guarded_assignment_recovery
from money_machine.domain.schemas import AccountSnapshot, BrokerOrderResult
from money_machine.settings import Settings


def _settings() -> Settings:
    return Settings(
        app_env="production",
        account_role="competition",
        run_mode="live",
        alpaca_api_key=SecretStr("present"),
        alpaca_secret_key=SecretStr("present"),
        alpaca_expected_account_id=SecretStr("PA3MX339UDPS"),
    )


class FakeRepository:
    def __init__(self) -> None:
        self.intents: list[dict[str, object]] = []
        self.updates: list[dict[str, object]] = []
        self.completed: list[dict[str, object]] = []

    def begin_run(self, *_args, **_kwargs):
        return "recovery-run", True

    def persist_assignment_recovery_intent(self, _run_id, **kwargs):
        self.intents.append(kwargs)

    def update_assignment_recovery_order(self, client_order_id, **kwargs):
        self.updates.append({"client_order_id": client_order_id, **kwargs})

    def complete_run(self, _run_id, **kwargs):
        self.completed.append(kwargs)


class FakeAdapter:
    def __init__(self) -> None:
        self.open = True
        self.open_orders: list[dict[str, object]] = []
        self.initial_positions = [
            {"symbol": "IWM", "qty": "200", "side": "long", "asset_class": "us_equity"},
            {"symbol": "QQQ", "qty": "100", "side": "long", "asset_class": "us_equity"},
        ]
        self.placed: list[dict[str, object]] = []

    async def account(self) -> AccountSnapshot:
        return AccountSnapshot(
            account_id="PA3MX339UDPS",
            is_paper=True,
            equity=Decimal("99300"),
            cash=Decimal("-29000" if len(self.placed) < 2 else "99300"),
            buying_power=Decimal("240000"),
            portfolio_value=Decimal("99300"),
        )

    async def market_clock(self):
        return {"is_open": self.open}

    async def orders(self, *, status="open"):
        assert status == "open"
        return self.open_orders

    async def positions(self):
        return self.initial_positions if len(self.placed) < 2 else []

    async def stock_snapshots(self, symbols, *, feed=None):
        assert symbols == ["IWM", "QQQ"]
        assert feed is None
        observed = datetime.now(UTC).isoformat()
        return {
            "snapshots": {
                "IWM": {"latestQuote": {"bp": "290.50", "t": observed}},
                "QQQ": {"latestQuote": {"bp": "707.10", "t": observed}},
            }
        }

    async def place_stock_order(self, **kwargs):
        self.placed.append(kwargs)
        return BrokerOrderResult(
            broker_order_id=f"broker-{kwargs['symbol'].lower()}",
            client_order_id=str(kwargs["client_order_id"]),
            status="accepted",
            submitted_at=datetime.now(UTC),
            raw={"status": "accepted"},
        )

    async def order_by_id(self, broker_order_id):
        symbol = broker_order_id.removeprefix("broker-").upper()
        quantity = "100" if symbol == "QQQ" else "200"
        return {
            "id": broker_order_id,
            "status": "filled",
            "filled_qty": quantity,
            "filled_avg_price": "700.00" if symbol == "QQQ" else "290.00",
        }


@pytest.mark.asyncio
async def test_guarded_assignment_recovery_fills_exact_inventory_and_audits() -> None:
    repository = FakeRepository()
    adapter = FakeAdapter()

    receipt = await guarded_assignment_recovery(  # type: ignore[arg-type]
        _settings(), repository, adapter=adapter
    )

    assert receipt["account_fingerprint"] == "2e10efeeb330"
    assert receipt["open_positions"] == 0
    assert receipt["working_orders"] == 0
    assert receipt["incident_cleared_by_command"] is False
    assert [order["symbol"] for order in adapter.placed] == ["QQQ", "IWM"]
    assert adapter.placed[0]["quantity"] == 100
    assert adapter.placed[0]["limit_price"] == Decimal("707.00")
    assert adapter.placed[1]["quantity"] == 200
    assert adapter.placed[1]["limit_price"] == Decimal("290.40")
    assert len(repository.intents) == 2
    assert len(repository.updates) == 4
    assert len(repository.completed) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("closed", "regular market session"),
        ("order", "zero broker working orders"),
        ("quantity", "exact authorized assignments"),
        ("short", "expected long stock assignment"),
        ("option", "expected long stock assignment"),
    ],
)
async def test_guarded_assignment_recovery_refuses_ambiguous_broker_truth(
    mutation: str, message: str
) -> None:
    repository = FakeRepository()
    adapter = FakeAdapter()
    if mutation == "closed":
        adapter.open = False
    elif mutation == "order":
        adapter.open_orders = [{"id": "unexpected"}]
    elif mutation == "quantity":
        adapter.initial_positions[0]["qty"] = "199"
    elif mutation == "short":
        adapter.initial_positions[0]["side"] = "short"
    else:
        adapter.initial_positions[0]["asset_class"] = "option"

    with pytest.raises(RuntimeError, match=message):
        await guarded_assignment_recovery(  # type: ignore[arg-type]
            _settings(), repository, adapter=adapter
        )

    assert adapter.placed == []
    assert repository.intents == []


@pytest.mark.asyncio
async def test_guarded_assignment_recovery_stops_after_partial_fill() -> None:
    repository = FakeRepository()
    adapter = FakeAdapter()

    async def partial_order(_broker_order_id):
        return {"status": "canceled", "filled_qty": "50", "filled_avg_price": "707.00"}

    adapter.order_by_id = partial_order  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="QQQ assignment recovery did not fill exactly"):
        await guarded_assignment_recovery(  # type: ignore[arg-type]
            _settings(), repository, adapter=adapter
        )

    assert len(adapter.placed) == 1
    assert adapter.placed[0]["symbol"] == "QQQ"


@pytest.mark.asyncio
async def test_guarded_assignment_recovery_requires_explicit_production_role(settings) -> None:
    with pytest.raises(RuntimeError, match="production competition live"):
        await guarded_assignment_recovery(  # type: ignore[arg-type]
            settings, FakeRepository(), adapter=FakeAdapter()
        )
