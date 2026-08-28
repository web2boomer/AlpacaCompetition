from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from money_machine.domain.clock import FINAL_FLATTEN_BY
from money_machine.domain.enums import RunMode
from money_machine.model_provider import ReplayModelProvider
from money_machine.persistence.models import BrokerOrderORM, FillORM
from money_machine.service import AgentService
from money_machine.settings import Settings


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
        now=FINAL_FLATTEN_BY,
        mode=RunMode.LIVE,
    )
    close_order = replay_adapter.submitted_requests[-1]
    assert close_order.is_closing
    assert all(leg.position_intent.value.endswith("_to_close") for leg in close_order.legs)
    assert closing.passport["execution"]["submitted"]
    assert not closing.passport["execution"]["entry_submitted"]
    assert any(
        event["event"] == "deadline_close_submitted"
        for event in closing.passport["operational_state"]["lifecycle_events"]
    )

    replay_adapter.data["positions"] = []
    await service.run_cycle(
        adapter=replay_adapter,
        model=ReplayModelProvider(),
        now=FINAL_FLATTEN_BY + timedelta(minutes=5),
        mode=RunMode.LIVE,
    )
    summary = repository.portfolio_risk_summary(Decimal("100000"))
    assert summary["open_alpha_structures"] == 0
