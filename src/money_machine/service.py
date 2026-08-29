import asyncio
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from money_machine.adapters.replay import infer_atm_implied_move
from money_machine.domain.candidates import CandidateBuildReport, build_candidates
from money_machine.domain.clock import (
    BASELINE_EQUITY,
    FORCED_FLATTEN_STARTS_AT,
    CompetitionClockSnapshot,
    competition_clock,
    is_official_performance_observation,
)
from money_machine.domain.enums import (
    AccountRole,
    AppEnvironment,
    ExecutionState,
    RiskReason,
    RunMode,
)
from money_machine.domain.risk import COMPETITION_DRAWDOWN_PCT, DAILY_LOSS_PCT, evaluate_risk
from money_machine.domain.schemas import (
    AuctionResult,
    BrokerOrderRequest,
    Candidate,
    OptionQuote,
    RiskContext,
    UnderlyingSnapshot,
)
from money_machine.execution import (
    close_request,
    replacement_request,
    stale_order_action,
    structure_exit_signal,
)
from money_machine.persistence.repository import AuditRepository, deterministic_client_order_id
from money_machine.ports import AlpacaPort, ModelProvider
from money_machine.safety import verify_account_identity, verify_competition_baseline
from money_machine.settings import Settings

UNIVERSE = ("SPY", "QQQ", "IWM")


@dataclass(frozen=True, slots=True)
class CycleOutcome:
    run_id: str
    created: bool
    approved: bool
    order_submitted: bool
    passport: dict[str, Any]


class AgentService:
    def __init__(self, settings: Settings, repository: AuditRepository) -> None:
        self.settings = settings
        self.repository = repository

    async def run_cycle(
        self,
        *,
        adapter: AlpacaPort,
        model: ModelProvider,
        now: datetime,
        mode: RunMode,
    ) -> CycleOutcome:
        now = now.astimezone(UTC)
        cycle_key = _cycle_key(now, mode)
        run_id, created = self.repository.begin_run(cycle_key, mode, now)
        if not created:
            passport = self.repository.passport_for_run(run_id) or {
                "run_id": run_id,
                "status": "duplicate_cycle_suppressed",
                "reason_codes": [RiskReason.DUPLICATE_CYCLE.value],
            }
            return CycleOutcome(run_id, False, False, False, passport)

        account: Any = None
        fingerprint = "unverified"
        production_account = (
            mode is RunMode.LIVE and self.settings.app_env is AppEnvironment.PRODUCTION
        )
        official = production_account and is_official_performance_observation(now)
        broker_position_count = 0
        working_order_count = 0
        try:
            account = await adapter.account()
            if mode is RunMode.REPLAY:
                fingerprint = "replay"
            else:
                fingerprint = verify_account_identity(self.settings, account).account_fingerprint
            orders, positions, activities = await asyncio.gather(
                adapter.orders(status="open"),
                adapter.positions(),
                adapter.activities(),
            )
            broker_position_count = len(positions)
            working_order_count = len(orders)
            if (
                production_account
                and self.settings.account_role is AccountRole.COMPETITION
                and not self.repository.has_managed_orders(AccountRole.COMPETITION.value)
            ):
                all_orders = await adapter.orders(status="all")
                verify_competition_baseline(
                    account,
                    positions=len(positions),
                    orders=len(all_orders) + len(activities),
                )
            self.repository.persist_fills(list(activities))
            reconciliation_clean, reconciliation_incidents = self.repository.reconcile_broker_state(
                list(orders),
                list(positions),
                now=now,
                authoritative_absence=mode is RunMode.LIVE,
            )
            risk_summary = self.repository.portfolio_risk_summary(account.equity, now=now)
            peak_equity = max(account.equity, risk_summary["peak_equity"], BASELINE_EQUITY)
            self.repository.persist_account_checkpoint(
                run_id,
                account=account,
                official=official,
                peak_equity=peak_equity,
                positions=list(positions),
                observed_at=now,
            )
            market_clock_payload = await adapter.market_clock()
            market_open = bool(market_clock_payload.get("is_open", False))
            clock = competition_clock(
                now,
                has_exposure=bool(positions) or bool(orders),
            )
            execution_state = clock.state
            if self.settings.app_env is AppEnvironment.DEVELOPMENT:
                execution_state = (
                    ExecutionState.FULL_EXECUTION if market_open else ExecutionState.OBSERVE_ONLY
                )
            elif execution_state is ExecutionState.FULL_EXECUTION and not market_open:
                execution_state = ExecutionState.OBSERVE_ONLY

            snapshots_raw, chains_raw = await asyncio.gather(
                asyncio.gather(*(adapter.underlying_snapshot(symbol) for symbol in UNIVERSE)),
                asyncio.gather(*(adapter.option_chain(symbol) for symbol in UNIVERSE)),
            )
            chains = {
                symbol: list(chain) for symbol, chain in zip(UNIVERSE, chains_raw, strict=True)
            }
            portfolio_exit_reason = _portfolio_exit_reason(
                equity=account.equity,
                start_of_day_equity=risk_summary["start_of_day_equity"],
                peak_equity=peak_equity,
            )
            lifecycle_events, lifecycle_incidents = await self._maintain_order_lifecycle(
                adapter=adapter,
                run_id=run_id,
                cycle_key=cycle_key,
                now=now,
                clock=clock,
                market_open=market_open,
                allow_new_entries=execution_state is ExecutionState.FULL_EXECUTION,
                positions=list(positions),
                chains=chains,
                force_close_reason=portfolio_exit_reason,
            )
            if lifecycle_incidents:
                reconciliation_clean = False
                reconciliation_incidents = tuple(
                    dict.fromkeys((*reconciliation_incidents, *lifecycle_incidents))
                )
            snapshots = [
                infer_atm_implied_move(snapshot, chains[snapshot.symbol])
                for snapshot in snapshots_raw
            ]
            self.repository.persist_market_observations(
                run_id,
                source="replay" if mode is RunMode.REPLAY else "alpaca_mcp_v2",
                snapshots=snapshots,
            )
            report = build_candidates(snapshots, chains, now)
            self.repository.persist_candidates(run_id, report.candidates)
            envelope = await model.decide(
                candidates=report.candidates,
                market_context={
                    "observed_at": now.isoformat(),
                    "features": [_snapshot_features(snapshot) for snapshot in snapshots],
                    "candidate_rejections": report.rejections,
                },
                portfolio_context={
                    "equity": str(account.equity),
                    "open_defined_loss": str(risk_summary["total_open_defined_loss"]),
                    "open_structures": risk_summary["open_alpha_structures"],
                    "execution_state": execution_state.value,
                    "reconciliation_clean": reconciliation_clean,
                },
            )
            selected = _candidate_by_id(report, envelope.decision.candidate_id)
            operational = self.repository.latest_operational_state()
            context = RiskContext(
                now=now,
                execution_state=execution_state,
                equity=account.equity,
                start_of_day_equity=risk_summary["start_of_day_equity"],
                peak_equity=peak_equity,
                total_open_defined_loss=risk_summary["total_open_defined_loss"],
                index_cluster_defined_loss=risk_summary["index_cluster_defined_loss"],
                open_alpha_structures=risk_summary["open_alpha_structures"],
                pending_underlyings=risk_summary["pending_underlyings"],
                kill_switch_active=bool(operational.get("kill_switch_active", False)),
                reconciliation_clean=reconciliation_clean,
            )
            risk = evaluate_risk(envelope.decision, selected, context)
            auction = AuctionResult(
                ranked_candidate_ids=tuple(c.candidate_id for c in report.candidates),
                selected_candidate_id=envelope.decision.candidate_id,
                cash_won=not risk.approved,
                awarded_risk=risk.awarded_risk,
            )
            self.repository.persist_decisions(run_id, envelope=envelope, risk=risk, auction=auction)
            order_request: BrokerOrderRequest | None = None
            order_result = None
            if risk.approved and selected is not None:
                client_order_id = deterministic_client_order_id(
                    self.settings.client_order_prefix,
                    cycle_key=cycle_key,
                    candidate_id=selected.candidate_id,
                    quantity=risk.quantity,
                )
                if not self.repository.order_exists(client_order_id):
                    order_request = BrokerOrderRequest(
                        client_order_id=client_order_id,
                        candidate_id=selected.candidate_id,
                        quantity=risk.quantity,
                        limit_price=selected.structure.net_price,
                        is_credit=selected.structure.is_credit,
                        legs=selected.structure.legs,
                        environment_role=self.settings.account_role.value,
                    )
                    order_result = await adapter.place_option_order(order_request)
                    self.repository.persist_order(run_id, order_request, order_result)

            passport = _passport(
                run_id=run_id,
                mode=mode,
                official=official,
                production_account=production_account,
                now=now,
                fingerprint=fingerprint,
                clock=clock,
                broker_position_count=broker_position_count,
                working_order_count=working_order_count,
                execution_state=execution_state,
                account=account,
                snapshots=snapshots,
                report=report,
                envelope=envelope,
                risk=risk,
                auction=auction,
                order_request=order_request,
                order_result=order_result,
                reconciliation_incidents=reconciliation_incidents,
                lifecycle_events=lifecycle_events,
            )
            self.repository.complete_run(run_id, completed_at=now, passport=passport)
            self.repository.append_system_state(
                run_id=run_id,
                now=now,
                execution_state=execution_state,
                kill_switch_active=context.kill_switch_active,
                reconciliation_clean=reconciliation_clean,
                success=True,
                incident_code=reconciliation_incidents[0] if reconciliation_incidents else None,
            )
            return CycleOutcome(run_id, True, risk.approved, order_result is not None, passport)
        except Exception as exc:
            incident = type(exc).__name__
            passport = {
                "run_id": run_id,
                "mode": mode.value,
                "official": official,
                "production_account": production_account,
                "status": "failed_closed",
                "observed_at": now.isoformat(),
                "result_label": (
                    "OFFICIAL ALPACA PAPER" if official else "NOT AN OFFICIAL SCORING OBSERVATION"
                ),
                "account": {
                    "fingerprint": fingerprint,
                    "equity": str(account.equity) if account is not None else None,
                    "pnl": (str(account.equity - BASELINE_EQUITY) if account is not None else None),
                    "is_paper": account.is_paper if account is not None else None,
                    "open_position_count": broker_position_count,
                    "working_order_count": working_order_count,
                    "broker_confirmed_flat": (
                        broker_position_count == 0 and working_order_count == 0
                    ),
                },
                "incident": {"code": "cycle_exception", "type": incident},
                "decision": {"action": "abstain", "thesis": "Cycle failed closed."},
                "risk": {"approved": False, "reason_codes": ["system_failure"]},
                "execution": {"submitted": False},
            }
            self.repository.complete_run(
                run_id, completed_at=now, passport=passport, incident=incident
            )
            self.repository.append_system_state(
                run_id=run_id,
                now=now,
                execution_state=ExecutionState.HALTED,
                kill_switch_active=True,
                reconciliation_clean=False,
                success=False,
                incident_code="cycle_exception",
                incident_detail=incident,
            )
            return CycleOutcome(run_id, True, False, False, passport)

    async def _maintain_order_lifecycle(
        self,
        *,
        adapter: AlpacaPort,
        run_id: str,
        cycle_key: str,
        now: datetime,
        clock: CompetitionClockSnapshot,
        market_open: bool,
        allow_new_entries: bool,
        positions: list[dict[str, Any]],
        chains: dict[str, list[OptionQuote]],
        force_close_reason: str | None,
    ) -> tuple[tuple[dict[str, Any], ...], tuple[str, ...]]:
        events: list[dict[str, Any]] = []
        incidents: list[str] = []
        active_closing_candidates: set[str] = set()
        if clock.flat_target_reached and (positions or self.repository.pending_managed_orders()):
            incidents.append("flat_target_exposure_remaining")
        for order in self.repository.pending_managed_orders():
            if clock.force_flatten_all and order.is_closing:
                broker_order = await adapter.order_by_id(order.broker_order_id)
                broker_status = str(broker_order.get("status") or "").lower()
                if broker_status in {"canceled", "expired", "filled", "rejected"}:
                    self.repository.mark_order_status(
                        order.client_order_id, status=broker_status, now=now
                    )
                    events.append(
                        {
                            "event": "closing_order_terminal_reconciled",
                            "status": broker_status,
                            "remaining_quantity": order.remaining_quantity,
                        }
                    )
                    continue
            cutoff_cancel = not order.is_closing and not allow_new_entries
            action = (
                None
                if cutoff_cancel
                else stale_order_action(
                    submitted_at=order.submitted_at,
                    now=now,
                    attempt=order.attempt,
                    original_limit=order.original_limit,
                    is_credit=order.is_credit,
                )
            )
            closing_candidate = order.candidate_id.removesuffix(":close")
            if action is not None and action.action == "wait":
                if order.is_closing:
                    active_closing_candidates.add(closing_candidate)
                continue
            await adapter.cancel_order(order.broker_order_id)
            canceled_status = (
                "partially_filled_canceled"
                if not order.is_closing and order.status == "partially_filled"
                else "canceled"
            )
            self.repository.mark_order_status(
                order.client_order_id, status=canceled_status, now=now
            )
            events.append(
                {
                    "event": (
                        "entry_order_canceled_at_cutoff"
                        if cutoff_cancel
                        else "stale_order_canceled"
                    ),
                    "client_order_id": order.client_order_id,
                    "broker_order_id": order.broker_order_id,
                    "status": canceled_status,
                    "remaining_quantity": order.remaining_quantity,
                }
            )
            may_replace = market_open and (order.is_closing or allow_new_entries)
            if (
                action is None
                or action.action != "cancel_and_replace"
                or not may_replace
                or action.next_limit is None
            ):
                continue
            next_attempt = order.attempt + 1
            client_order_id = deterministic_client_order_id(
                self.settings.client_order_prefix,
                cycle_key=cycle_key,
                candidate_id=order.candidate_id,
                quantity=order.remaining_quantity,
                attempt=next_attempt,
            )
            if self.repository.order_exists(client_order_id):
                active_closing_candidates.add(closing_candidate)
                continue
            request = replacement_request(
                order,
                client_order_id=client_order_id,
                next_limit=action.next_limit,
                environment_role=self.settings.account_role.value,
            )
            result = await adapter.place_option_order(request)
            self.repository.persist_order(order.agent_run_id, request, result)
            active_closing_candidates.add(closing_candidate)
            events.append(
                {
                    "event": "order_replaced_with_bounded_concession",
                    "client_order_id": request.client_order_id,
                    "broker_order_id": result.broker_order_id,
                    "status": result.status,
                    "quantity": request.quantity,
                }
            )

        quote_map = {quote.symbol: quote for chain in chains.values() for quote in chain}
        position_quantities = {
            str(position.get("symbol")): abs(Decimal(str(position.get("qty") or 0)))
            for position in positions
            if position.get("symbol")
        }
        for managed in self.repository.open_managed_structures():
            if managed.candidate_id in active_closing_candidates:
                continue
            leg_symbols = [leg.symbol for leg in managed.structure.legs]
            present = [position_quantities.get(symbol, Decimal("0")) > 0 for symbol in leg_symbols]
            if not any(present):
                self.repository.mark_order_status(managed.client_order_id, status="closed", now=now)
                events.append(
                    {
                        "event": "flat_structure_reconciled",
                        "client_order_id": managed.client_order_id,
                        "broker_order_id": managed.broker_order_id,
                        "status": "closed",
                    }
                )
                continue
            if not all(present):
                incidents.append("rejected_close_incomplete_structure")
                continue
            if not market_open:
                continue
            deadline_reason = None
            if clock.force_flatten_all:
                deadline_reason = "forced_liquidation_window"
            elif managed.status == "closing":
                deadline_reason = "close_recovery"
            signal = structure_exit_signal(
                managed,
                quotes=quote_map,
                now=now,
                force_close_reason=deadline_reason or force_close_reason,
            )
            if not signal.should_close:
                continue
            closeable = min(
                int(position_quantities[leg.symbol] / Decimal(leg.ratio_qty))
                for leg in managed.structure.legs
            )
            quantity = min(managed.quantity, closeable)
            if quantity < 1:
                incidents.append("rejected_close_zero_quantity")
                continue
            client_order_id = deterministic_client_order_id(
                self.settings.client_order_prefix,
                cycle_key=cycle_key,
                candidate_id=f"{managed.candidate_id}:close",
                quantity=quantity,
            )
            if self.repository.order_exists(client_order_id):
                continue
            try:
                request = close_request(
                    managed,
                    quotes=quote_map,
                    client_order_id=client_order_id,
                    quantity=quantity,
                    environment_role=self.settings.account_role.value,
                )
            except ValueError:
                incidents.append("rejected_close_incomplete_quotes")
                continue
            result = await adapter.place_option_order(request)
            self.repository.persist_order(run_id, request, result)
            self.repository.mark_order_status(managed.client_order_id, status="closing", now=now)
            events.append(
                {
                    "event": "position_close_submitted",
                    "reason": signal.reason,
                    "client_order_id": request.client_order_id,
                    "broker_order_id": result.broker_order_id,
                    "status": result.status,
                    "quantity": request.quantity,
                }
            )
        return tuple(events), tuple(dict.fromkeys(incidents))


def _cycle_key(now: datetime, mode: RunMode) -> str:
    bucket_minutes = 1 if now >= FORCED_FLATTEN_STARTS_AT else 5
    minute = now.minute - now.minute % bucket_minutes
    bucket = now.replace(minute=minute, second=0, microsecond=0)
    return f"{mode.value}:{bucket.isoformat()}"


def _candidate_by_id(report: CandidateBuildReport, candidate_id: str | None) -> Candidate | None:
    return next(
        (candidate for candidate in report.candidates if candidate.candidate_id == candidate_id),
        None,
    )


def _passport(
    *,
    run_id: str,
    mode: RunMode,
    official: bool,
    production_account: bool,
    now: datetime,
    fingerprint: str,
    clock: CompetitionClockSnapshot,
    broker_position_count: int,
    working_order_count: int,
    execution_state: ExecutionState,
    account: Any,
    snapshots: list[Any],
    report: CandidateBuildReport,
    envelope: Any,
    risk: Any,
    auction: AuctionResult,
    order_request: BrokerOrderRequest | None,
    order_result: Any,
    reconciliation_incidents: tuple[str, ...],
    lifecycle_events: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    candidates = [candidate.model_dump(mode="json") for candidate in report.candidates]
    submitted_lifecycle = [
        event
        for event in lifecycle_events
        if event["event"] in {"order_replaced_with_bounded_concession", "position_close_submitted"}
    ]
    latest_lifecycle = submitted_lifecycle[-1] if submitted_lifecycle else {}
    return {
        "run_id": run_id,
        "observed_at": now.isoformat(),
        "mode": mode.value,
        "official": official,
        "production_account": production_account,
        "scoring_window_state": clock.scoring_window_state,
        "result_label": (
            "OFFICIAL ALPACA PAPER"
            if official
            else (
                "COMPETITION ACCOUNT — OUTSIDE SCORING WINDOW"
                if production_account
                else "REPLAY — NOT OFFICIAL P&L"
            )
        ),
        "account": {
            "fingerprint": fingerprint,
            "equity": str(account.equity),
            "pnl": str(account.equity - BASELINE_EQUITY),
            "is_paper": account.is_paper,
            "open_position_count": broker_position_count,
            "working_order_count": working_order_count,
            "broker_confirmed_flat": broker_position_count == 0 and working_order_count == 0,
        },
        "operational_state": {
            "execution_state": execution_state.value,
            "reconciliation_clean": not reconciliation_incidents,
            "incidents": list(reconciliation_incidents),
            "lifecycle_events": list(lifecycle_events),
        },
        "evidence": [_snapshot_features(snapshot) for snapshot in snapshots],
        "candidate_rejections": report.rejections,
        "candidates": candidates,
        "auction": auction.model_dump(mode="json"),
        "decision": envelope.decision.model_dump(mode="json"),
        "model_validation": {
            "provider": envelope.provider,
            "model": envelope.model,
            "provider_response_id_hash": envelope.provider_response_id_hash,
            "raw_response_hash": envelope.raw_response_hash,
            "fallback_used": envelope.validation_error is not None,
            "error_type": envelope.validation_error,
        },
        "risk": risk.model_dump(mode="json"),
        "execution": {
            "submitted": order_result is not None or bool(submitted_lifecycle),
            "entry_submitted": order_result is not None,
            "client_order_id": (
                order_request.client_order_id
                if order_request
                else latest_lifecycle.get("client_order_id")
            ),
            "broker_order_id": (
                order_result.broker_order_id
                if order_result
                else latest_lifecycle.get("broker_order_id")
            ),
            "status": (
                order_result.status
                if order_result
                else latest_lifecycle.get("status", "not_submitted")
            ),
            "order_type": "limit" if order_request or submitted_lifecycle else None,
            "quantity": (
                order_request.quantity if order_request else latest_lifecycle.get("quantity", 0)
            ),
            "maintenance_orders": list(lifecycle_events),
        },
        "outcome": {
            "equity": str(account.equity),
            "realized_pl": str(account.realized_pl),
            "unrealized_pl": str(account.unrealized_pl),
            "official": official,
        },
        "counterfactuals": {
            "label": "HYPOTHETICAL — EXCLUDED FROM OFFICIAL P&L",
            "alternatives": [
                {
                    "candidate_id": candidate.candidate_id,
                    "maximum_profit": str(candidate.structure.maximum_profit),
                    "maximum_loss": str(candidate.structure.maximum_loss),
                }
                for candidate in report.candidates
                if candidate.candidate_id != auction.selected_candidate_id
            ],
        },
        "audit_hash": hashlib.sha256(
            f"{run_id}|{now.isoformat()}|{envelope.raw_response_hash}".encode()
        ).hexdigest(),
    }


def _snapshot_features(snapshot: UnderlyingSnapshot) -> dict[str, Any]:
    features = snapshot.model_dump(mode="json")
    features["richness_ratio"] = str(snapshot.richness_ratio)
    return features


def _portfolio_exit_reason(
    *, equity: Decimal, start_of_day_equity: Decimal, peak_equity: Decimal
) -> str | None:
    daily_loss = max(Decimal("0"), start_of_day_equity - equity)
    if daily_loss >= start_of_day_equity * DAILY_LOSS_PCT:
        return "daily_loss_limit"
    drawdown = max(Decimal("0"), peak_equity - equity)
    if drawdown >= peak_equity * COMPETITION_DRAWDOWN_PCT:
        return "competition_drawdown_limit"
    return None
