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
from money_machine.domain.daily_loss import loss_is_plausible, validate_managed_book_marks
from money_machine.domain.enums import RunMode
from money_machine.domain.schemas import AccountSnapshot
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
        {
            "asset_id": leg["symbol"],
            "symbol": leg["symbol"],
            "qty": quantity if leg["side"] == "buy" else f"-{quantity}",
        }
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


async def seed_pending_close(settings, repository, replay_adapter):
    await seed_managed_position(settings, repository, replay_adapter)
    service = AgentService(production_settings(settings), repository)
    await service.run_cycle(
        adapter=replay_adapter,
        model=ReplayModelProvider(),
        now=FORCED_FLATTEN_STARTS_AT,
        mode=RunMode.LIVE,
    )
    close_request = replay_adapter.submitted_requests[-1]
    assert close_request.is_closing
    with repository.database.session() as session:
        close_order = session.scalar(
            select(BrokerOrderORM).where(
                BrokerOrderORM.client_order_id == close_request.client_order_id
            )
        )
        assert close_order is not None
        close_order.status = "pending_new"
        close_order.submitted_at = replay_adapter.observed_at - timedelta(minutes=5)
    replay_adapter.canceled_order_ids.clear()
    return service, close_request, replay_adapter.submitted_orders[-1].broker_order_id


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
    checks = {item["name"]: item for item in outcome.passport["risk"]["checks"]}
    assert "applied=false" in checks["high_conviction_index_tier"]["actual"]
    assert checks["effective_per_structure_percent"]["actual"] == "0.01"
    assert Decimal(checks["effective_per_structure_budget"]["actual"]) == (
        Decimal(outcome.passport["account"]["equity"]) * Decimal("0.01")
    )
    assert outcome.passport["execution"]["order_type"] == "limit"
    assert outcome.passport["counterfactuals"]["label"].startswith("HYPOTHETICAL")
    assert len(outcome.passport["audit_hash"]) == 64
    with repository.database.session() as session:
        assert session.scalar(select(func.count()).select_from(FillORM)) == 4
        assert session.scalar(select(func.count()).select_from(MarketSnapshotORM)) == 3
    risk_summary = repository.portfolio_risk_summary(Decimal(outcome.passport["account"]["equity"]))
    assert risk_summary["open_alpha_structures"] == 1


class CapturingReplayModel:
    def __init__(self, preferred_underlying=None) -> None:
        self.candidates = ()
        self.preferred_underlying = preferred_underlying
        self.calls = 0

    async def decide(self, *, candidates, market_context, portfolio_context):
        del market_context, portfolio_context
        self.calls += 1
        self.candidates = tuple(candidates)
        ranked = self.candidates
        if self.preferred_underlying is not None:
            selected = next(
                candidate
                for candidate in ranked
                if candidate.structure.underlying == self.preferred_underlying
            )
            ranked = (selected, *(candidate for candidate in ranked if candidate is not selected))
        return await ReplayModelProvider().decide(
            candidates=ranked,
            market_context={},
            portfolio_context={},
        )


def live_style_risk_summary(*, open_underlyings, pending_underlyings=frozenset()):
    return {
        "peak_equity": Decimal("100000"),
        "start_of_day_equity": Decimal("100000"),
        "total_open_defined_loss": Decimal("1979"),
        "index_cluster_defined_loss": Decimal("1979"),
        "open_alpha_structures": 5,
        "pending_underlyings": pending_underlyings,
        "open_underlyings": open_underlyings,
    }


@pytest.mark.asyncio
async def test_portfolio_filter_excludes_qqq_but_preserves_full_report_and_selects_spy(
    settings,
    repository,
    replay_adapter,
    monkeypatch,
) -> None:
    spy = next(item for item in replay_adapter.data["underlyings"] if item["symbol"] == "SPY")
    spy.update(
        spot="760.00",
        previous_close="767.68",
        realized_move_pct="0.01",
        trend_return_pct="-0.01",
    )
    monkeypatch.setattr(
        repository,
        "portfolio_risk_summary",
        lambda *_args, **_kwargs: live_style_risk_summary(open_underlyings=frozenset({"QQQ"})),
    )
    model = CapturingReplayModel(preferred_underlying="SPY")

    outcome = await AgentService(settings, repository).run_cycle(
        adapter=replay_adapter,
        model=model,
        now=replay_adapter.observed_at,
        mode=RunMode.REPLAY,
    )

    assert model.candidates
    assert model.calls == 1
    assert {candidate.structure.underlying for candidate in model.candidates} <= {"SPY", "IWM"}
    assert any(candidate.structure.underlying == "SPY" for candidate in model.candidates)
    assert outcome.approved
    assert outcome.order_submitted
    assert outcome.passport["decision"]["candidate_id"].startswith("spy-")
    assert outcome.passport["auction"]["ranked_candidate_ids"] == [
        candidate.candidate_id for candidate in model.candidates
    ]
    full_underlyings = {
        candidate["structure"]["underlying"] for candidate in outcome.passport["candidates"]
    }
    assert "QQQ" in full_underlyings
    exclusions = outcome.passport["portfolio_candidate_exclusions"]
    assert exclusions
    assert all(
        "existing_managed_structure_for_underlying" in reasons
        for candidate_id, reasons in exclusions.items()
        if candidate_id.startswith("qqq-")
    )
    assert any(
        alternative["candidate_id"].startswith("qqq-")
        for alternative in outcome.passport["counterfactuals"]["alternatives"]
    )
    checks = {item["name"]: item for item in outcome.passport["risk"]["checks"]}
    assert "candidate_underlying=SPY" in checks["portfolio_underlying_diversification"]["actual"]
    assert checks["effective_per_structure_percent"]["actual"] == "0.03"


@pytest.mark.asyncio
async def test_portfolio_filter_records_pending_and_open_reasons_and_abstains_when_empty(
    settings,
    repository,
    replay_adapter,
    monkeypatch,
) -> None:
    spy = next(item for item in replay_adapter.data["underlyings"] if item["symbol"] == "SPY")
    spy.update(
        spot="760.00",
        previous_close="767.68",
        realized_move_pct="0.01",
        trend_return_pct="-0.01",
    )
    monkeypatch.setattr(
        repository,
        "portfolio_risk_summary",
        lambda *_args, **_kwargs: live_style_risk_summary(
            open_underlyings=frozenset({"QQQ", "IWM"}),
            pending_underlyings=frozenset({"SPY"}),
        ),
    )
    model = CapturingReplayModel()

    outcome = await AgentService(settings, repository).run_cycle(
        adapter=replay_adapter,
        model=model,
        now=replay_adapter.observed_at,
        mode=RunMode.REPLAY,
    )

    assert model.candidates == ()
    assert model.calls == 0
    assert not outcome.approved
    assert not outcome.order_submitted
    assert outcome.passport["decision"]["action"] == "abstain"
    assert "every generated candidate was excluded" in outcome.passport["decision"]["thesis"]
    assert outcome.passport["candidates"]
    exclusions = outcome.passport["portfolio_candidate_exclusions"]
    assert any(
        "pending_entry_for_underlying" in reasons
        for candidate_id, reasons in exclusions.items()
        if candidate_id.startswith("spy-")
    )
    assert any(
        "existing_managed_structure_for_underlying" in reasons
        for candidate_id, reasons in exclusions.items()
        if candidate_id.startswith("qqq-")
    )


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
    assert summary["open_underlyings"] == frozenset()


@pytest.mark.asyncio
async def test_complete_broker_positions_normalize_pending_parent_to_filled(
    settings, repository, replay_adapter
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
        order.status = "pending_new"
    selected = next(
        candidate
        for candidate in outcome.passport["candidates"]
        if candidate["candidate_id"] == outcome.passport["decision"]["candidate_id"]
    )
    positions = [
        {
            "symbol": leg["symbol"],
            "qty": str(
                leg["ratio_qty"] * outcome.passport["risk"]["quantity"]
                if leg["side"] == "buy"
                else -leg["ratio_qty"] * outcome.passport["risk"]["quantity"]
            ),
        }
        for leg in selected["structure"]["legs"]
    ]

    clean, incidents = repository.reconcile_broker_state([], positions)

    assert clean is True
    assert incidents == ()
    managed = repository.open_managed_structures()
    assert len(managed) == 1
    assert managed[0].status == "filled"
    summary = repository.portfolio_risk_summary(Decimal("100000"))
    assert summary["open_underlyings"] == frozenset({"QQQ"})


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
@pytest.mark.parametrize("broker_status", ["submitted", "pending_new", "new"])
async def test_stale_entry_is_canceled_and_replaced_with_bounded_concession(
    settings, repository, replay_adapter, broker_status
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
        order.status = broker_status

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
@pytest.mark.parametrize("broker_status", ["filled", "canceled", "expired", "rejected"])
async def test_terminal_pending_close_is_refreshed_without_cancel_or_replacement(
    settings, repository, replay_adapter, monkeypatch, broker_status
) -> None:
    service, close_request, _close_broker_id = await seed_pending_close(
        settings, repository, replay_adapter
    )
    replay_adapter.data["positions"] = []
    repository.set_kill_switch(active=True, now=replay_adapter.observed_at)

    async def terminal_order(_broker_order_id: str):
        return {"status": broker_status}

    monkeypatch.setattr(replay_adapter, "order_by_id", terminal_order)
    request_count = len(replay_adapter.submitted_requests)

    outcome = await service.run_cycle(
        adapter=replay_adapter,
        model=ReplayModelProvider(),
        now=replay_adapter.observed_at + timedelta(minutes=5),
        mode=RunMode.LIVE,
    )

    assert replay_adapter.canceled_order_ids == []
    assert len(replay_adapter.submitted_requests) == request_count
    events = outcome.passport["operational_state"]["lifecycle_events"]
    assert any(
        event["event"] == "closing_order_terminal_reconciled" and event["status"] == broker_status
        for event in events
    )
    assert not any(event["event"] == "order_replaced_with_bounded_concession" for event in events)
    with repository.database.session() as session:
        close_order = session.scalar(
            select(BrokerOrderORM).where(
                BrokerOrderORM.client_order_id == close_request.client_order_id
            )
        )
        assert close_order is not None
        assert close_order.status == broker_status


@pytest.mark.asyncio
async def test_filled_close_refresh_prevents_already_filled_cancel_regression(
    settings, repository, replay_adapter, monkeypatch
) -> None:
    service, _close_request, _close_broker_id = await seed_pending_close(
        settings, repository, replay_adapter
    )
    replay_adapter.data["positions"] = replay_adapter.data["positions"][:2]
    repository.set_kill_switch(active=True, now=replay_adapter.observed_at)

    async def filled_order(_broker_order_id: str):
        return {"status": "filled"}

    async def cancel_would_raise_422(_broker_order_id: str):
        raise AssertionError('cancel would reproduce Alpaca 422 "order is already filled"')

    monkeypatch.setattr(replay_adapter, "order_by_id", filled_order)
    monkeypatch.setattr(replay_adapter, "cancel_order", cancel_would_raise_422)

    outcome = await service.run_cycle(
        adapter=replay_adapter,
        model=ReplayModelProvider(),
        now=replay_adapter.observed_at + timedelta(minutes=5),
        mode=RunMode.LIVE,
    )

    assert outcome.passport.get("status") != "failed_closed"
    assert outcome.passport["operational_state"]["reconciliation_clean"] is True
    assert outcome.passport["operational_state"]["incidents"] == []
    assert any(
        event["event"] == "closing_order_terminal_reconciled" and event["status"] == "filled"
        for event in outcome.passport["operational_state"]["lifecycle_events"]
    )
    assert repository.open_managed_structures() == ()


@pytest.mark.asyncio
async def test_restart_reconciles_parent_for_already_normalized_filled_close(
    settings, repository, replay_adapter, monkeypatch
) -> None:
    service, close_request, _close_broker_id = await seed_pending_close(
        settings, repository, replay_adapter
    )
    replay_adapter.data["positions"] = replay_adapter.data["positions"][:2]
    repository.set_kill_switch(active=True, now=replay_adapter.observed_at)
    with repository.database.session() as session:
        close_order = session.scalar(
            select(BrokerOrderORM).where(
                BrokerOrderORM.client_order_id == close_request.client_order_id
            )
        )
        assert close_order is not None
        close_order.status = "filled"

    async def order_lookup_must_not_run(_broker_order_id: str):
        raise AssertionError("an already-terminal close must not re-enter pending maintenance")

    monkeypatch.setattr(replay_adapter, "order_by_id", order_lookup_must_not_run)

    outcome = await service.run_cycle(
        adapter=replay_adapter,
        model=ReplayModelProvider(),
        now=replay_adapter.observed_at + timedelta(minutes=5),
        mode=RunMode.LIVE,
    )

    assert outcome.passport["operational_state"]["reconciliation_clean"] is True
    assert outcome.passport["operational_state"]["incidents"] == []
    assert any(
        event["event"] == "closing_parent_terminal_reconciled"
        and event["candidate_id"] == close_request.candidate_id.removesuffix(":close")
        for event in outcome.passport["operational_state"]["lifecycle_events"]
    )
    assert replay_adapter.canceled_order_ids == []
    assert repository.open_managed_structures() == ()


@pytest.mark.asyncio
async def test_nonterminal_pending_close_keeps_existing_stale_replacement_path(
    settings, repository, replay_adapter, monkeypatch
) -> None:
    service, _close_request, close_broker_id = await seed_pending_close(
        settings, repository, replay_adapter
    )
    with repository.database.session() as session:
        close_order = session.scalar(
            select(BrokerOrderORM).where(BrokerOrderORM.broker_order_id == close_broker_id)
        )
        assert close_order is not None
        request = dict(close_order.raw_json["request"])
        request.pop("exit_urgency", None)
        close_order.raw_json = {**close_order.raw_json, "request": request}
    repository.set_kill_switch(active=True, now=replay_adapter.observed_at)

    async def pending_order(_broker_order_id: str):
        return {"status": "pending_new"}

    monkeypatch.setattr(replay_adapter, "order_by_id", pending_order)
    request_count = len(replay_adapter.submitted_requests)

    outcome = await service.run_cycle(
        adapter=replay_adapter,
        model=ReplayModelProvider(),
        now=replay_adapter.observed_at + timedelta(minutes=5),
        mode=RunMode.LIVE,
    )

    assert replay_adapter.canceled_order_ids == [close_broker_id]
    assert len(replay_adapter.submitted_requests) == request_count + 1
    replacement = replay_adapter.submitted_requests[-1]
    assert replacement.is_closing
    assert replacement.attempt == 1
    assert any(
        event["event"] == "order_replaced_with_bounded_concession"
        for event in outcome.passport["operational_state"]["lifecycle_events"]
    )


@pytest.mark.asyncio
async def test_exhausted_urgent_close_reprices_from_fresh_quotes_without_attempt_reset(
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
    for leg in first_close.legs:
        raw_quote = next(
            raw
            for chain in replay_adapter.data["chains"].values()
            for raw in chain
            if raw["symbol"] == leg.symbol
        )
        if leg.side.value == "buy":
            raw_quote["bid"] = "0.90"
            raw_quote["ask"] = "1.00"
        else:
            raw_quote["bid"] = "0.10"
            raw_quote["ask"] = "0.20"

    async def still_working(_broker_order_id: str):
        return {"status": "submitted"}

    monkeypatch.setattr(replay_adapter, "order_by_id", still_working)
    replay_adapter.data["orders"] = [
        {
            "id": replay_adapter.submitted_orders[-1].broker_order_id,
            "client_order_id": first_close.client_order_id,
            "status": "submitted",
            "qty": "1",
            "filled_qty": "0",
        }
    ]
    outcome = await service.run_cycle(
        adapter=replay_adapter,
        model=ReplayModelProvider(),
        now=FORCED_FLATTEN_STARTS_AT + timedelta(minutes=1),
        mode=RunMode.LIVE,
    )

    closing_requests = [
        request for request in replay_adapter.submitted_requests if request.is_closing
    ]
    assert len(closing_requests) == 2, outcome.passport.get("incident")
    assert closing_requests[0].client_order_id != closing_requests[1].client_order_id
    replacement = closing_requests[-1]
    assert replacement.attempt == 2
    assert replacement.limit_price == Decimal("2.10")
    assert replacement.is_credit is False
    assert replacement.quantity == 1
    assert replay_adapter.canceled_order_ids
    assert any(
        "fresh executable NBBO with bounded urgent concession 0.30" in event["lifecycle_reason"]
        for event in outcome.passport["operational_state"]["lifecycle_events"]
        if event["event"] == "stale_order_canceled"
    )

    cancel_count = len(replay_adapter.canceled_order_ids)
    request_count = len(replay_adapter.submitted_requests)
    with repository.database.session() as session:
        replacement_row = session.scalar(
            select(BrokerOrderORM).where(
                BrokerOrderORM.client_order_id == replacement.client_order_id
            )
        )
        assert replacement_row is not None
        replacement_row.status = "submitted"
        replacement_row.submitted_at = FORCED_FLATTEN_STARTS_AT - timedelta(minutes=5)
    replay_adapter.data["orders"] = [
        {
            "id": replay_adapter.submitted_orders[-1].broker_order_id,
            "client_order_id": replacement.client_order_id,
            "status": "submitted",
            "qty": "1",
            "filled_qty": "0",
        }
    ]
    await service.run_cycle(
        adapter=replay_adapter,
        model=ReplayModelProvider(),
        now=FORCED_FLATTEN_STARTS_AT + timedelta(minutes=2),
        mode=RunMode.LIVE,
    )
    assert len(replay_adapter.canceled_order_ids) == cancel_count
    assert len(replay_adapter.submitted_requests) == request_count


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


def account_at(equity: str) -> AccountSnapshot:
    return AccountSnapshot(
        account_id="REPLAY-PAPER-ACCOUNT",
        is_paper=True,
        equity=Decimal(equity),
        cash=Decimal(equity),
        buying_power=Decimal(equity) * 2,
        portfolio_value=Decimal(equity),
    )


@pytest.mark.asyncio
async def test_impossible_opening_mark_recovers_without_liquidation(
    settings, repository, replay_adapter, monkeypatch
) -> None:
    await seed_managed_position(settings, repository, replay_adapter)
    before = len(replay_adapter.submitted_requests)
    accounts = iter((account_at("95849.52"), account_at("99597.00")))

    async def sequenced_account() -> AccountSnapshot:
        return next(accounts)

    monkeypatch.setattr(replay_adapter, "account", sequenced_account)
    outcome = await AgentService(
        production_settings(settings),
        repository,
        daily_loss_confirmation_delay_seconds=0,
    ).run_cycle(
        adapter=replay_adapter,
        model=ReplayModelProvider(),
        now=replay_adapter.observed_at + timedelta(minutes=5),
        mode=RunMode.LIVE,
    )

    assert outcome.passport["daily_loss_control"]["status"] == "clear"
    assert outcome.passport["daily_loss_control"]["reason"] == "second_observation_recovered"
    assert len(replay_adapter.submitted_requests) == before
    with repository.database.session() as session:
        checkpoint = session.scalar(
            select(EquitySnapshotORM)
            .where(EquitySnapshotORM.agent_run_id == outcome.run_id)
            .limit(1)
        )
        assert checkpoint is not None
        assert checkpoint.equity == Decimal("99597.00")


@pytest.mark.asyncio
async def test_two_credible_breaches_with_fresh_complete_quotes_latch_and_close(
    settings, repository, replay_adapter, monkeypatch
) -> None:
    qqq_short_put = next(
        quote
        for quote in replay_adapter.data["chains"]["QQQ"]
        if quote["symbol"] == "QQQ260904P00700000"
    )
    qqq_short_put.update(bid="1.90", ask="2.00")
    await seed_managed_position(settings, repository, replay_adapter)
    cycle_at = replay_adapter.observed_at + timedelta(minutes=5)
    for chain in replay_adapter.data["chains"].values():
        for quote in chain:
            quote["observed_at"] = cycle_at.isoformat()
    accounts = iter((account_at("97390.00"), account_at("97400.00")))

    async def sequenced_account() -> AccountSnapshot:
        return next(accounts)

    monkeypatch.setattr(replay_adapter, "account", sequenced_account)
    outcome = await AgentService(
        production_settings(settings),
        repository,
        daily_loss_confirmation_delay_seconds=0,
    ).run_cycle(
        adapter=replay_adapter,
        model=ReplayModelProvider(),
        now=cycle_at,
        mode=RunMode.LIVE,
    )

    control = outcome.passport["daily_loss_control"]
    assert control["status"] == "latched", repr(control)
    assert control["confirmation_count"] == 2
    assert control["quote_quality_passed"] is True
    assert replay_adapter.submitted_requests[-1].is_closing
    assert any(
        event.get("reason") == "daily_loss_limit"
        for event in outcome.passport["operational_state"]["lifecycle_events"]
    )


@pytest.mark.asyncio
async def test_stale_quotes_freeze_entries_without_forced_liquidation(
    settings, repository, replay_adapter, monkeypatch
) -> None:
    await seed_managed_position(settings, repository, replay_adapter)
    before = len(replay_adapter.submitted_requests)
    accounts = iter((account_at("96900.00"), account_at("96850.00")))

    async def sequenced_account() -> AccountSnapshot:
        return next(accounts)

    monkeypatch.setattr(replay_adapter, "account", sequenced_account)
    outcome = await AgentService(
        production_settings(settings),
        repository,
        daily_loss_confirmation_delay_seconds=0,
    ).run_cycle(
        adapter=replay_adapter,
        model=ReplayModelProvider(),
        now=replay_adapter.observed_at + timedelta(minutes=5),
        mode=RunMode.LIVE,
    )

    control = outcome.passport["daily_loss_control"]
    assert control["status"] == "provisional"
    assert control["entry_halt_active"] is True
    assert control["quote_quality_reason"].startswith("stale_quote:")
    assert len(replay_adapter.submitted_requests) == before


@pytest.mark.asyncio
async def test_confirmed_impossible_loss_is_quarantined_without_liquidation(
    settings, repository, replay_adapter, monkeypatch
) -> None:
    await seed_managed_position(settings, repository, replay_adapter)
    before = len(replay_adapter.submitted_requests)
    cycle_at = replay_adapter.observed_at + timedelta(minutes=5)
    for chain in replay_adapter.data["chains"].values():
        for quote in chain:
            quote["observed_at"] = cycle_at.isoformat()
    accounts = iter((account_at("95849.52"), account_at("95849.52")))

    async def sequenced_account() -> AccountSnapshot:
        return next(accounts)

    monkeypatch.setattr(replay_adapter, "account", sequenced_account)
    outcome = await AgentService(
        production_settings(settings),
        repository,
        daily_loss_confirmation_delay_seconds=0,
    ).run_cycle(
        adapter=replay_adapter,
        model=ReplayModelProvider(),
        now=cycle_at,
        mode=RunMode.LIVE,
    )

    control = outcome.passport["daily_loss_control"]
    assert control["status"] == "provisional"
    assert control["loss_plausible"] is False
    assert control["quote_quality_passed"] is True
    assert control["reason"] == "loss_exceeds_defined_risk_envelope"
    assert len(replay_adapter.submitted_requests) == before


def test_daily_loss_latch_is_session_scoped(repository, replay_adapter) -> None:
    now = replay_adapter.observed_at
    repository.daily_loss_control(now=now, defined_loss_envelope=Decimal("2000"))
    repository.update_daily_loss_control(
        now=now,
        status="latched",
        confirmation_count=2,
        first_breach_at=now,
        last_loss=Decimal("3100"),
        defined_loss_envelope=Decimal("2000"),
        quote_quality_passed=True,
        reason="confirmed_credible_daily_loss_breach",
    )
    assert (
        repository.daily_loss_control(now=now, defined_loss_envelope=Decimal("0")).status
        == "latched"
    )
    assert (
        repository.daily_loss_control(
            now=now + timedelta(days=1), defined_loss_envelope=Decimal("0")
        ).status
        == "clear"
    )


def test_opening_mark_regression_exceeds_defined_loss_envelope() -> None:
    assert not loss_is_plausible(Decimal("4150.48"), Decimal("2125"))
    assert loss_is_plausible(Decimal("3100"), Decimal("2800"))


@pytest.mark.asyncio
async def test_missing_managed_leg_quote_fails_mark_validation(
    settings, repository, replay_adapter
) -> None:
    await seed_managed_position(settings, repository, replay_adapter)
    managed = repository.open_managed_structures()
    missing_symbol = managed[0].structure.legs[0].symbol
    chains = {
        symbol: [
            quote
            for quote in await replay_adapter.option_chain(symbol)
            if quote.symbol != missing_symbol
        ]
        for symbol in ("SPY", "QQQ", "IWM")
    }
    quality = validate_managed_book_marks(
        managed_structures=managed,
        positions=replay_adapter.data["positions"],
        chains=chains,
        now=replay_adapter.observed_at,
    )
    assert not quality.passed
    assert quality.reason == f"missing_quote:{missing_symbol}"
