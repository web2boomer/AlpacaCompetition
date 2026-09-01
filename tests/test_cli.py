from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from money_machine.cli import _guarded_kill_switch_clear
from money_machine.domain.enums import Side
from money_machine.domain.schemas import AccountSnapshot
from money_machine.safety import configured_account_fingerprint
from money_machine.settings import Settings


def production_settings(database_url: str) -> Settings:
    return Settings(
        app_env="production",
        account_role="competition",
        run_mode="live",
        database_url=database_url,
        alpaca_api_key=SecretStr("present"),
        alpaca_secret_key=SecretStr("present"),
        alpaca_expected_account_id=SecretStr("REPLAY-PAPER-ACCOUNT"),
    )


class GuardRepository:
    def __init__(self, settings: Settings) -> None:
        self.state = {
            "kill_switch_active": True,
            "reconciliation_clean": True,
            "incident_code": None,
        }
        self.passport = {
            "account": {
                "fingerprint": configured_account_fingerprint(settings),
                "open_position_count": 1,
                "working_order_count": 1,
            },
            "operational_state": {"incidents": []},
        }
        self.pending = [SimpleNamespace(broker_order_id="redacted-order")]
        self.managed = [
            SimpleNamespace(
                quantity=1,
                structure=SimpleNamespace(
                    legs=(SimpleNamespace(symbol="QQQ-option", side=Side.BUY, ratio_qty=1),)
                ),
            )
        ]
        self.clear_calls: list[dict[str, object]] = []

    def latest_operational_state(self):
        return self.state

    def latest_passport(self):
        return self.passport

    def pending_managed_orders(self):
        return self.pending

    def open_managed_structures(self):
        return self.managed

    def set_kill_switch(self, **kwargs):
        self.clear_calls.append(kwargs)


class GuardAdapter:
    def __init__(self) -> None:
        self.open_orders = [{"id": "redacted-order"}]
        self.open_positions = [{"symbol": "QQQ-option", "qty": "1"}]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def account(self) -> AccountSnapshot:
        return AccountSnapshot(
            account_id="REPLAY-PAPER-ACCOUNT",
            is_paper=True,
            equity=Decimal("100000"),
            cash=Decimal("100000"),
            buying_power=Decimal("200000"),
            portfolio_value=Decimal("100000"),
        )

    async def orders(self, *, status: str):
        assert status == "open"
        return self.open_orders

    async def positions(self):
        return self.open_positions


@pytest.mark.asyncio
async def test_guarded_kill_clear_requires_exact_production_mode(settings, repository) -> None:
    with pytest.raises(RuntimeError, match="production competition live"):
        await _guarded_kill_switch_clear(settings, repository)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state_update", "message"),
    [
        ({"kill_switch_active": False}, "not active"),
        ({"reconciliation_clean": False}, "not clean"),
        ({"incident_code": "cycle_exception"}, "incident-free"),
    ],
)
async def test_guarded_kill_clear_rejects_unsafe_operational_state(
    settings, monkeypatch, state_update, message
) -> None:
    production = production_settings(settings.database_url)
    guarded = GuardRepository(production)
    guarded.state.update(state_update)
    adapter = GuardAdapter()
    monkeypatch.setattr("money_machine.cli.AlpacaMcpV2Adapter", lambda _settings: adapter)

    with pytest.raises(RuntimeError, match=message):
        await _guarded_kill_switch_clear(production, guarded)

    assert guarded.clear_calls == []


@pytest.mark.asyncio
async def test_guarded_kill_clear_rejects_broker_passport_count_mismatch(
    settings, monkeypatch
) -> None:
    production = production_settings(settings.database_url)
    guarded = GuardRepository(production)
    guarded.passport["account"]["working_order_count"] = 0
    adapter = GuardAdapter()
    monkeypatch.setattr("money_machine.cli.AlpacaMcpV2Adapter", lambda _settings: adapter)

    with pytest.raises(RuntimeError, match="working-order count"):
        await _guarded_kill_switch_clear(production, guarded)

    assert guarded.clear_calls == []


@pytest.mark.asyncio
async def test_guarded_kill_clear_rejects_exact_order_or_position_mismatch(
    settings, monkeypatch
) -> None:
    production = production_settings(settings.database_url)
    guarded = GuardRepository(production)
    adapter = GuardAdapter()
    adapter.open_orders[0]["id"] = "different-order"
    monkeypatch.setattr("money_machine.cli.AlpacaMcpV2Adapter", lambda _settings: adapter)

    with pytest.raises(RuntimeError, match="unexplained working order"):
        await _guarded_kill_switch_clear(production, guarded)

    adapter.open_orders[0]["id"] = "redacted-order"
    adapter.open_positions[0]["qty"] = "2"
    with pytest.raises(RuntimeError, match="position inventory"):
        await _guarded_kill_switch_clear(production, guarded)

    assert guarded.clear_calls == []


@pytest.mark.asyncio
async def test_guarded_kill_clear_succeeds_only_after_all_live_guards(
    settings, monkeypatch
) -> None:
    production = production_settings(settings.database_url)
    guarded = GuardRepository(production)
    adapter = GuardAdapter()
    monkeypatch.setattr("money_machine.cli.AlpacaMcpV2Adapter", lambda _settings: adapter)

    receipt = await _guarded_kill_switch_clear(production, guarded)

    assert receipt == {
        "kill_switch": "off",
        "guarded": True,
        "account_fingerprint": configured_account_fingerprint(production),
        "reconciliation": "clean",
        "open_positions": 1,
        "working_orders": 1,
        "observed_at": receipt["observed_at"],
    }
    assert len(guarded.clear_calls) == 1
    assert guarded.clear_calls[0]["active"] is False
    assert "user_authorized" in str(guarded.clear_calls[0]["incident_detail"])
