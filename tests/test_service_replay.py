from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from money_machine.domain.clock import (
    EOD_EQUITY_SNAPSHOT_AT,
    FLAT_TARGET_AT,
    FORCED_FLATTEN_STARTS_AT,
    NEW_ENTRY_CUTOFF,
    SCORING_STARTS_AT,
)
from money_machine.domain.enums import RunMode
from money_machine.model_provider import ReplayModelProvider
from money_machine.persistence.models import (
    BrokerOrderORM,
    EquitySnapshotORM,
    FillORM,
    MarketSnapshotORM,
)
from money_machine.safety import configured_account_fingerprint
from money_machine.service import AgentService
from money_machine.settings import Settings


def production_settings(settings) -> Settings:
    return Settings(
        app_env="production",
        account_role="competition",
        run_mode="live",
        database_url=settings.database_url,
        alpaca_api_key=SecretStr("present"),
        alpaca_secret_key=SecretStr("present"),
        alpaca_expected_account_id=SecretStr("REPLAY-PAPER-ACCOUNT"),
        client_order_prefix="mm-comp",
    )


async def seed_managed_position(settings, repository, replay_adapter, *, quantity: str = "1"):
    first = await AgentService(settings, repository).run_cycle(
        adapter=replay_adapter,
        model=ReplayModelProvider(),
        now=replay_adapter.observed_at,
        mode=RunMode.REPLAY,
    )
    selected_id = first.passport["decision"]["candidate_id"]
    selected = next(
        candidate
        for candidate in first.passport["candidates"]
        if candidate["candidate_id"] == selected_id
    )
    replay_adapter.data["positions"] = [
        {"asset_id": leg["symbol"], "symbol": leg["symbol"], "qty": quantity}
        for leg in selected["structure"]["legs"]
    ]
    with repository.database.session() as session:
        managed_order = session.scalar(
            select(BrokerOrderORM).where(BrokerOrderORM.agent_run_id == first.run_id)
        )
        assert managed_order is not None
        managed_order.environment_role = "competition"
        managed_order.quantity = int(quantity)
    return first


@pytest.mark.asyncio
async def test_replay_end_to_end_generates_decision_passport(
    settings, repository, replay_adapter
) -> None:
    outcome = await AgentService(settings, repository).run_cycle(
        adapter=replay_adapter,
        model=ReplayModelProvider(),
        now=replay_adapter.observed_at,
        mode=RunMode.REPLAY,
    )
    assert outcome.created
    assert outcome.approved
    assert outcome.order_submitted
    assert outcome.passport["result_label"] == "REPLAY — NOT OFFICIAL P&L"
    assert outcome.passport["risk"]["approved"]
    assert outcome.passport["execution"]["order_type"] == "limit"
    assert outcome.passport["counterfactuals"]["label"].startswith("HYPOTHETICAL")
    assert len(outcome.passport["audit_hash"]) == 64
    with repository.database.session() as session:
        assert session.scalar(select(func.count()).select_from(FillORM)) == 4
        assert session.scalar(select(func.count()).select_from(MarketSnapshotORM)) == 3
    risk_summary = repository.portfolio_risk_summary(Decimal(outcome.passport["account"]["equity"]))
    assert risk_summary["open_alpha_structures"] == 1


@pytest.mark.asyncio
async def test_duplicate_cycle_and_order_are_suppressed(
    settings, repository, replay_adapter
) -> None:
    service = AgentService(settings, repository)
    first = await service.run_cycle(
        adapter=replay_adapter,
        model=ReplayModelProvider(),
        now=replay_adapter.observed_at,
        mode=RunMode.REPLAY,
    )
    second = await service.run_cycle(
        adapter=replay_adapter,
        model=ReplayModelProvider(),
        now=replay_adapter.observed_at,
        mode=RunMode.REPLAY,
    )
    assert first.run_id == second.run_id
    assert not second.created
    assert len(replay_adapter.submitted_orders) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "broker_status",
    [
        "accepted",
        "accepted_for_bidding",
        "calculated",
        "held",
        "pending_new",
        "pending_cancel",
        "pending_replace",
        "new",
        "stopped",
    ],
)
async def test_broker_pending_statuses_reserve_risk_and_underlying(
    settings, repository, replay_adapter, broker_status
) -> None:
    outcome = await AgentService(settings, repository).run_cycle(
        adapter=replay_adapter,
        model=ReplayModelProvider(),
        now=replay_adapter.observed_at,
        mode=RunMode.REPLAY,
    )
    with repository.database.session() as session:
        order = session.scalar(
            select(BrokerOrderORM).where(BrokerOrderORM.agent_run_id == outcome.run_id)
        )
        assert order is not None
        order.status = broker_status

    summary = repository.portfolio_risk_summary(Decimal("100000"))

    assert summary["open_alpha_structures"] == 1
    assert summary["total_open_defined_loss"] == Decimal(outcome.passport["risk"]["awarded_risk"])
    assert summary["pending_underlyings"] == frozenset({"QQQ"})


@pytest.mark.asyncio
async def test_kill_switch_blocks_entry(settings, repository, replay_adapter) -> None:
    repository.set_kill_switch(active=True, now=replay_adapter.observed_at)
    outcome = await AgentService(settings, repository).run_cycle(
        adapter=replay_adapter,
        model=ReplayModelProvider(),
        now=replay_adapter.observed_at,
        mode=RunMode.REPLAY,
    )
    assert not outcome.approved
    assert not outcome.order_submitted
    assert "kill_switch_active" in outcome.passport["risk"]["reason_codes"]


@pytest.mark.asyncio
async def test_restart_reconciliation_halts_on_orphaned_position(
    settings, repository, replay_adapter
) -> None:
    replay_adapter.data["positions"] = [
        {"asset_id": "orphan", "symbol": "SPY260904C00999999", "qty": "1"}
    ]
    outcome = await AgentService(settings, repository).run_cycle(
        adapter=replay_adapter,
        model=ReplayModelProvider(),
        now=replay_adapter.observed_at,
        mode=RunMode.REPLAY,
    )
    assert not outcome.approved
    assert "reconciliation_not_clean" in outcome.passport["risk"]["reason_codes"]
    assert outcome.passport["operational_state"]["incidents"] == ["orphaned_broker_position"]


@pytest.mark.asyncio
async def test_next_window_with_stale_data_abstains(settings, repository, replay_adapter) -> None:
    outcome = await AgentService(settings, repository).run_cycle(
        adapter=replay_adapter,
        model=ReplayModelProvider(),
        now=replay_adapter.observed_at + timedelta(minutes=5),
        mode=RunMode.REPLAY,
    )
    assert not outcome.approved
    assert outcome.passport["decision"]["action"] == "abstain"


@pytest.mark.asyncio
async def test_stale_entry_is_canceled_and_replaced_with_bounded_concession(
    settings, repository, replay_adapter
) -> None:
    service = AgentService(settings, repository)
    first = await service.run_cycle(
        adapter=replay_adapter,
        model=ReplayModelProvider(),
        now=replay_adapter.observed_at,
        mode=RunMode.REPLAY,
    )
    with repository.database.session() as session:
        order = session.scalar(
            select(BrokerOrderORM).where(BrokerOrderORM.agent_run_id == first.run_id)
        )
        assert order is not None
        order.status = "submitted"

    second = await service.run_cycle(
        adapter=replay_adapter,
        model=ReplayModelProvider(),
        now=replay_adapter.observed_at + timedelta(minutes=5),
        mode=RunMode.REPLAY,
    )
    assert replay_adapter.canceled_order_ids
    assert replay_adapter.submitted_requests[-1].attempt == 1
    assert any(
        event["event"] == "order_replaced_with_bounded_concession"
        for event in second.passport["operational_state"]["lifecycle_events"]
    )


@pytest.mark.asyncio
async def test_final_flatten_submits_close_only_structure_and_reconciles_flat(
    settings, repository, replay_adapter
) -> None:
    first = await AgentService(settings, repository).run_cycle(
        adapter=replay_adapter,
        model=ReplayModelProvider(),
        now=replay_adapter.observed_at,
        mode=RunMode.REPLAY,
    )
    selected_id = first.passport["decision"]["candidate_id"]
    selected = next(
        candidate
        for candidate in first.passport["candidates"]
        if candidate["candidate_id"] == selected_id
    )
    replay_adapter.data["positions"] = [
        {"asset_id": leg["symbol"], "symbol": leg["symbol"], "qty": "1"}
        for leg in selected["structure"]["legs"]
    ]
    with repository.database.session() as session:
        managed_order = session.scalar(
            select(BrokerOrderORM).where(BrokerOrderORM.agent_run_id == first.run_id)
        )
        assert managed_order is not None
        managed_order.environment_role = "competition"
    production = Settings(
        app_env="production",
        account_role="competition",
        run_mode="live",
        database_url=settings.database_url,
        alpaca_api_key=SecretStr("present"),
        alpaca_secret_key=SecretStr("present"),
        alpaca_expected_account_id=SecretStr("REPLAY-PAPER-ACCOUNT"),
        client_order_prefix="mm-comp",
    )
    service = AgentService(production, repository)
    closing = await service.run_cycle(
        adapter=replay_adapter,
        model=ReplayModelProvider(),
        now=FORCED_FLATTEN_STARTS_AT,
        mode=RunMode.LIVE,
    )
    close_order = replay_adapter.submitted_requests[-1]
    assert close_order.is_closing
    assert all(leg.position_intent.value.endswith("_to_close") for leg in close_order.legs)
    assert closing.passport["execution"]["submitted"]
    assert not closing.passport["execution"]["entry_submitted"]
    assert any(
        event["event"] == "position_close_submitted"
        for event in closing.passport["operational_state"]["lifecycle_events"]
    )

    replay_adapter.data["positions"] = []
    await service.run_cycle(
        adapter=replay_adapter,
        model=ReplayModelProvider(),
        now=FORCED_FLATTEN_STARTS_AT + timedelta(minutes=5),
        mode=RunMode.LIVE,
    )
    summary = repository.portfolio_risk_summary(Decimal("100000"))
    assert summary["open_alpha_structures"] == 0


@pytest.mark.asyncio
async def test_first_competition_cycle_rejects_a_polluted_baseline(
    settings, repository, replay_adapter
) -> None:
    replay_adapter.data["account"]["equity"] = "99999"
    production = Settings(
        app_env="production",
        account_role="competition",
        run_mode="live",
        database_url=settings.database_url,
        alpaca_api_key=SecretStr("present"),
        alpaca_secret_key=SecretStr("present"),
        alpaca_expected_account_id=SecretStr("REPLAY-PAPER-ACCOUNT"),
        client_order_prefix="mm-comp",
    )
    outcome = await AgentService(production, repository).run_cycle(
        adapter=replay_adapter,
        model=ReplayModelProvider(),
        now=FORCED_FLATTEN_STARTS_AT,
        mode=RunMode.LIVE,
    )
    assert not outcome.approved
    assert not outcome.order_submitted
    assert outcome.passport["status"] == "failed_closed"


@pytest.mark.asyncio
async def test_fresh_pending_entry_is_canceled_at_cutoff_without_replacement(
    settings, repository, replay_adapter
) -> None:
    first = await AgentService(settings, repository).run_cycle(
        adapter=replay_adapter,
        model=ReplayModelProvider(),
        now=replay_adapter.observed_at,
        mode=RunMode.REPLAY,
    )
    with repository.database.session() as session:
        order = session.scalar(
            select(BrokerOrderORM).where(BrokerOrderORM.agent_run_id == first.run_id)
        )
        assert order is not None
        order.status = "submitted"
        order.submitted_at = NEW_ENTRY_CUTOFF - timedelta(seconds=10)
        order.environment_role = "competition"
    request_count = len(replay_adapter.submitted_requests)

    outcome = await AgentService(production_settings(settings), repository).run_cycle(
        adapter=replay_adapter,
        model=ReplayModelProvider(),
        now=NEW_ENTRY_CUTOFF,
        mode=RunMode.LIVE,
    )

    assert replay_adapter.canceled_order_ids
    assert len(replay_adapter.submitted_requests) == request_count
    assert any(
        event["event"] == "entry_order_canceled_at_cutoff"
        for event in outcome.passport["operational_state"]["lifecycle_events"]
    )


@pytest.mark.asyncio
async def test_forced_close_exhaustion_reopens_emergency_close_eligibility(
    settings, repository, replay_adapter, monkeypatch
) -> None:
    await seed_managed_position(settings, repository, replay_adapter)
    service = AgentService(production_settings(settings), repository)
    await service.run_cycle(
        adapter=replay_adapter,
        model=ReplayModelProvider(),
        now=FORCED_FLATTEN_STARTS_AT,
        mode=RunMode.LIVE,
    )
    first_close = replay_adapter.submitted_requests[-1]
    with repository.database.session() as session:
        close_order = session.scalar(
            select(BrokerOrderORM).where(
                BrokerOrderORM.client_order_id == first_close.client_order_id
            )
        )
        assert close_order is not None
        close_order.status = "submitted"
        close_order.attempt = 2
        close_order.submitted_at = FORCED_FLATTEN_STARTS_AT - timedelta(minutes=5)

    async def still_working(_broker_order_id: str):
        return {"status": "submitted"}

    monkeypatch.setattr(replay_adapter, "order_by_id", still_working)
    await service.run_cycle(
        adapter=replay_adapter,
        model=ReplayModelProvider(),
        now=FORCED_FLATTEN_STARTS_AT + timedelta(minutes=1),
        mode=RunMode.LIVE,
    )

    closing_requests = [
        request for request in replay_adapter.submitted_requests if request.is_closing
    ]
    assert len(closing_requests) == 2
    assert closing_requests[0].client_order_id != closing_requests[1].client_order_id
    assert replay_adapter.canceled_order_ids


@pytest.mark.asyncio
async def test_partially_filled_close_reprices_only_remaining_quantity(
    settings, repository, replay_adapter, monkeypatch
) -> None:
    await seed_managed_position(settings, repository, replay_adapter, quantity="3")
    service = AgentService(production_settings(settings), repository)
    await service.run_cycle(
        adapter=replay_adapter,
        model=ReplayModelProvider(),
        now=FORCED_FLATTEN_STARTS_AT,
        mode=RunMode.LIVE,
    )
    first_close = replay_adapter.submitted_requests[-1]
    replay_adapter.data["positions"] = [
        {**position, "qty": "1"} for position in replay_adapter.data["positions"]
    ]
    broker_payload = {
        "id": replay_adapter.submitted_orders[-1].broker_order_id,
        "client_order_id": first_close.client_order_id,
        "status": "partially_filled",
        "qty": "3",
        "filled_qty": "2",
        "updated_at": FORCED_FLATTEN_STARTS_AT.isoformat(),
    }
    replay_adapter.data["orders"] = [broker_payload]

    async def partially_filled(_broker_order_id: str):
        return broker_payload

    monkeypatch.setattr(replay_adapter, "order_by_id", partially_filled)
    await service.run_cycle(
        adapter=replay_adapter,
        model=ReplayModelProvider(),
        now=FORCED_FLATTEN_STARTS_AT + timedelta(minutes=2),
        mode=RunMode.LIVE,
    )

    replacement = replay_adapter.submitted_requests[-1]
    assert replacement.is_closing
    assert replacement.quantity == 1
    assert replacement.attempt == 1


@pytest.mark.asyncio
async def test_liquidation_restart_is_idempotent_within_the_minute(
    settings, repository, replay_adapter
) -> None:
    await seed_managed_position(settings, repository, replay_adapter)
    service = AgentService(production_settings(settings), repository)
    first = await service.run_cycle(
        adapter=replay_adapter,
        model=ReplayModelProvider(),
        now=FORCED_FLATTEN_STARTS_AT,
        mode=RunMode.LIVE,
    )
    count = len(replay_adapter.submitted_requests)
    duplicate = await service.run_cycle(
        adapter=replay_adapter,
        model=ReplayModelProvider(),
        now=FORCED_FLATTEN_STARTS_AT + timedelta(seconds=30),
        mode=RunMode.LIVE,
    )
    assert duplicate.run_id == first.run_id
    assert not duplicate.created
    assert len(replay_adapter.submitted_requests) == count


@pytest.mark.asyncio
async def test_exposure_at_flat_target_emits_incident_and_keeps_close_authority(
    settings, repository, replay_adapter
) -> None:
    await seed_managed_position(settings, repository, replay_adapter)
    outcome = await AgentService(production_settings(settings), repository).run_cycle(
        adapter=replay_adapter,
        model=ReplayModelProvider(),
        now=FLAT_TARGET_AT,
        mode=RunMode.LIVE,
    )
    assert outcome.passport["operational_state"]["execution_state"] == "close_only"
    assert "flat_target_exposure_remaining" in outcome.passport["operational_state"]["incidents"]
    assert replay_adapter.submitted_requests[-1].is_closing


@pytest.mark.asyncio
async def test_pre_scoring_production_equity_is_not_official(
    settings, repository, replay_adapter
) -> None:
    replay_adapter.data["account"]["equity"] = "100000.00"
    replay_adapter.data["account"]["portfolio_value"] = "100000.00"
    production = production_settings(settings)
    outcome = await AgentService(production, repository).run_cycle(
        adapter=replay_adapter,
        model=ReplayModelProvider(),
        now=SCORING_STARTS_AT - timedelta(seconds=1),
        mode=RunMode.LIVE,
    )
    assert outcome.passport["production_account"] is True
    assert outcome.passport["official"] is False
    summary = repository.competition_performance_summary(
        account_fingerprint=configured_account_fingerprint(production),
        now=SCORING_STARTS_AT,
    )
    assert not summary["available"]


@pytest.mark.asyncio
async def test_equity_checkpoint_survives_market_data_failure(
    settings, repository, replay_adapter, monkeypatch
) -> None:
    replay_adapter.data["account"]["equity"] = "100000.00"
    replay_adapter.data["account"]["portfolio_value"] = "100000.00"

    async def market_failure(_symbol: str):
        raise RuntimeError("market data unavailable")

    monkeypatch.setattr(replay_adapter, "underlying_snapshot", market_failure)
    production = production_settings(settings)
    outcome = await AgentService(production, repository).run_cycle(
        adapter=replay_adapter,
        model=ReplayModelProvider(),
        now=SCORING_STARTS_AT + timedelta(minutes=5),
        mode=RunMode.LIVE,
    )
    assert outcome.passport["status"] == "failed_closed"
    with repository.database.session() as session:
        assert session.scalar(select(func.count()).select_from(EquitySnapshotORM)) == 1
        assert session.scalar(select(func.count()).select_from(MarketSnapshotORM)) == 0
    summary = repository.competition_performance_summary(
        account_fingerprint=configured_account_fingerprint(production),
        now=SCORING_STARTS_AT + timedelta(minutes=5),
    )
    assert summary["available"]


def test_eod_constant_is_thursday_close() -> None:
    assert EOD_EQUITY_SNAPSHOT_AT.isoformat() == "2026-09-03T20:00:00+00:00"
