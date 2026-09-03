import asyncio
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from money_machine.adapters.replay import infer_atm_implied_move
from money_machine.domain.candidates import CandidateBuildReport, build_candidates
from money_machine.domain.clock import (
    BASELINE_EQUITY,
    FORCED_FLATTEN_STARTS_AT,
    CompetitionClockSnapshot,
    competition_clock,
    competition_entry_window_open,
    is_official_performance_observation,
)
from money_machine.domain.daily_loss import loss_is_plausible, validate_managed_book_marks
from money_machine.domain.enums import (
    AccountRole,
    Action,
    AppEnvironment,
    ExecutionState,
    RiskReason,
    RunMode,
)
from money_machine.domain.risk import (
    COMPETITION_DRAWDOWN_PCT,
    DAILY_LOSS_PCT,
    daily_loss_pct_at,
    evaluate_risk,
    is_final_competition_day,
)
from money_machine.domain.schemas import (
    AuctionResult,
    BrokerOrderRequest,
    Candidate,
    OptionQuote,
    RiskContext,
    UnderlyingSnapshot,
)
from money_machine.execution import (
    MAVERICK_DIRECTIONAL_DEBIT_TAKE_PROFIT_MULTIPLE,
    URGENT_MAX_REPRICE_ATTEMPTS,
    ManagedStructure,
    close_request,
    closing_quote_materially_changed,
    daily_hard_exit_deadline,
    entry_holding_policy,
    refreshed_close_terms,
    replacement_request,
    stale_order_action,
    structure_exit_signal,
    urgent_close_debit_cap,
)
from money_machine.model_provider import safe_model_decision
from money_machine.persistence.repository import (
    AuditRepository,
    PriorMarketObservation,
    deterministic_client_order_id,
)
from money_machine.ports import AlpacaPort, ModelProvider
from money_machine.safety import verify_account_identity, verify_competition_baseline
from money_machine.settings import Settings

UNIVERSE = ("SPY", "QQQ", "IWM")
DIRECTION_CONFIRMATION_MIN_GAP = timedelta(minutes=5)
DIRECTION_CONFIRMATION_MAX_GAP = timedelta(minutes=10)
STRATEGY_ROTATION_INTERVAL = timedelta(minutes=45)
MEANINGFUL_PROGRESS_MULTIPLE = Decimal("1.05")
MAVERICK_MIN_TREND_ACCELERATION = Decimal("0.001")


@dataclass(frozen=True, slots=True)
class CycleOutcome:
    run_id: str
    created: bool
    approved: bool
    order_submitted: bool
    passport: dict[str, Any]


class AgentService:
    def __init__(
        self,
        settings: Settings,
        repository: AuditRepository,
        *,
        daily_loss_confirmation_delay_seconds: float = 15,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.daily_loss_confirmation_delay_seconds = daily_loss_confirmation_delay_seconds

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
        account_checkpoint_persisted = False
        checkpoint_positions: list[dict[str, Any]] = []
        risk_summary: dict[str, Any] | None = None
        peak_equity = BASELINE_EQUITY
        previous_passport = self.repository.latest_passport() or {}
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
            checkpoint_positions = list(positions)
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
            raw_account = account
            peak_equity = max(account.equity, risk_summary["peak_equity"], BASELINE_EQUITY)
            daily_control = self.repository.daily_loss_control(
                now=now,
                defined_loss_envelope=risk_summary["total_open_defined_loss"],
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
            elif execution_state is ExecutionState.FULL_EXECUTION and (
                not market_open or (production_account and not competition_entry_window_open(now))
            ):
                execution_state = ExecutionState.OBSERVE_ONLY

            snapshots_raw, chains_raw = await asyncio.gather(
                asyncio.gather(*(adapter.underlying_snapshot(symbol) for symbol in UNIVERSE)),
                asyncio.gather(*(adapter.option_chain(symbol) for symbol in UNIVERSE)),
            )
            chains = {
                symbol: list(chain) for symbol, chain in zip(UNIVERSE, chains_raw, strict=True)
            }
            effective_daily_loss_pct = daily_loss_pct_at(now)
            daily_limit = risk_summary["start_of_day_equity"] * effective_daily_loss_pct
            raw_daily_loss = max(
                Decimal("0"), risk_summary["start_of_day_equity"] - raw_account.equity
            )
            mark_quality_reason = "not_required"
            mark_quality_passed = False
            plausible_loss = True
            authorized_final_day_latch_release = False
            if (
                daily_control.status == "latched"
                and effective_daily_loss_pct > DAILY_LOSS_PCT
                and daily_control.last_loss < daily_limit
                and raw_daily_loss < daily_limit
            ):
                daily_control = self.repository.update_daily_loss_control(
                    now=now,
                    status="clear",
                    confirmation_count=0,
                    last_loss=raw_daily_loss,
                    defined_loss_envelope=daily_control.defined_loss_envelope,
                    quote_quality_passed=False,
                    reason="recovered_under_authorized_final_day_limit",
                )
                authorized_final_day_latch_release = True
            if (
                daily_control.status != "latched"
                and raw_daily_loss >= daily_limit
                and not reconciliation_clean
            ):
                daily_control = self.repository.update_daily_loss_control(
                    now=now,
                    status="provisional",
                    confirmation_count=1,
                    first_breach_at=now,
                    last_loss=raw_daily_loss,
                    defined_loss_envelope=daily_control.defined_loss_envelope,
                    quote_quality_passed=False,
                    reason="immediate_reconciliation_safety_exit",
                )
                mark_quality_reason = "reconciliation_not_clean"
                mark_quality_passed = False
            elif daily_control.status != "latched" and raw_daily_loss >= daily_limit:
                daily_control = self.repository.update_daily_loss_control(
                    now=now,
                    status="provisional",
                    confirmation_count=1,
                    first_breach_at=now,
                    last_loss=raw_daily_loss,
                    defined_loss_envelope=daily_control.defined_loss_envelope,
                    quote_quality_passed=False,
                    reason="raw_breach_awaiting_confirmation",
                )
                if self.daily_loss_confirmation_delay_seconds > 0:
                    await asyncio.sleep(self.daily_loss_confirmation_delay_seconds)
                confirmed_account = await adapter.account()
                if mode is not RunMode.REPLAY:
                    verify_account_identity(self.settings, confirmed_account)
                account = confirmed_account
                confirmed_loss = max(
                    Decimal("0"), risk_summary["start_of_day_equity"] - account.equity
                )
                managed = self.repository.open_managed_structures()
                mark_quality = validate_managed_book_marks(
                    managed_structures=managed,
                    positions=checkpoint_positions,
                    chains=chains,
                    now=now,
                )
                mark_quality_reason = mark_quality.reason
                mark_quality_passed = mark_quality.passed
                plausible_loss = not positions or loss_is_plausible(
                    confirmed_loss, daily_control.defined_loss_envelope
                )
                if confirmed_loss < daily_limit:
                    daily_control = self.repository.update_daily_loss_control(
                        now=now,
                        status="clear",
                        confirmation_count=0,
                        last_loss=confirmed_loss,
                        defined_loss_envelope=daily_control.defined_loss_envelope,
                        quote_quality_passed=mark_quality_passed,
                        reason="second_observation_recovered",
                    )
                elif plausible_loss and mark_quality_passed:
                    daily_control = self.repository.update_daily_loss_control(
                        now=now,
                        status="latched",
                        confirmation_count=2,
                        last_loss=confirmed_loss,
                        defined_loss_envelope=daily_control.defined_loss_envelope,
                        quote_quality_passed=True,
                        reason="confirmed_credible_daily_loss_breach",
                    )
                else:
                    reason = (
                        "loss_exceeds_defined_risk_envelope"
                        if not plausible_loss
                        else f"unvalidated_marks:{mark_quality_reason}"
                    )
                    daily_control = self.repository.update_daily_loss_control(
                        now=now,
                        status="provisional",
                        confirmation_count=2,
                        last_loss=confirmed_loss,
                        defined_loss_envelope=daily_control.defined_loss_envelope,
                        quote_quality_passed=mark_quality_passed,
                        reason=reason,
                    )
            elif daily_control.status == "provisional" and raw_daily_loss < daily_limit:
                daily_control = self.repository.update_daily_loss_control(
                    now=now,
                    status="clear",
                    confirmation_count=0,
                    last_loss=raw_daily_loss,
                    defined_loss_envelope=daily_control.defined_loss_envelope,
                    quote_quality_passed=False,
                    reason="later_cycle_recovered_before_validation",
                )
            elif daily_control.status == "clear" and not authorized_final_day_latch_release:
                daily_control = self.repository.update_daily_loss_control(
                    now=now,
                    status="clear",
                    confirmation_count=0,
                    last_loss=raw_daily_loss,
                    defined_loss_envelope=daily_control.defined_loss_envelope,
                    quote_quality_passed=False,
                    reason="below_daily_loss_limit",
                )

            peak_equity = max(account.equity, risk_summary["peak_equity"], BASELINE_EQUITY)
            self.repository.persist_account_checkpoint(
                run_id,
                account=account,
                official=official,
                peak_equity=peak_equity,
                positions=list(positions),
                observed_at=now,
            )
            account_checkpoint_persisted = True
            safety_equity = min(raw_account.equity, account.equity)
            drawdown = max(Decimal("0"), peak_equity - safety_equity)
            final_day_portfolio_loss_override = is_final_competition_day(now)
            if final_day_portfolio_loss_override:
                portfolio_exit_reason = (
                    "reconciliation_safety_incident"
                    if not reconciliation_clean and raw_daily_loss >= daily_limit
                    else None
                )
            else:
                portfolio_exit_reason = (
                    "competition_drawdown_limit"
                    if drawdown >= peak_equity * COMPETITION_DRAWDOWN_PCT
                    else "reconciliation_safety_incident"
                    if not reconciliation_clean and raw_daily_loss >= daily_limit
                    else "daily_loss_limit"
                    if daily_control.status == "latched"
                    else None
                )
            daily_loss_evidence = {
                "status": daily_control.status,
                "entry_halt_active": daily_control.status in {"provisional", "latched"},
                "confirmation_count": daily_control.confirmation_count,
                "raw_equity": str(raw_account.equity),
                "confirmed_equity": str(account.equity),
                "daily_loss_limit": str(daily_limit),
                "observed_loss": str(daily_control.last_loss),
                "defined_loss_envelope": str(daily_control.defined_loss_envelope),
                "loss_plausible": plausible_loss,
                "quote_quality_passed": mark_quality_passed,
                "quote_quality_reason": mark_quality_reason,
                "reason": daily_control.reason,
                "portfolio_loss_exit_override_applied": final_day_portfolio_loss_override,
                "portfolio_exit_reason": portfolio_exit_reason,
            }
            lifecycle_events, lifecycle_incidents = await self._maintain_order_lifecycle(
                adapter=adapter,
                run_id=run_id,
                cycle_key=cycle_key,
                now=now,
                clock=clock,
                market_open=market_open,
                allow_new_entries=(
                    execution_state is ExecutionState.FULL_EXECUTION
                    and (daily_control.status == "clear" or is_final_competition_day(now))
                ),
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
            rotation = _strategy_rotation_before_selection(
                previous_passport=previous_passport,
                now=now,
                broker_flat=(
                    execution_state is ExecutionState.FULL_EXECUTION
                    and not positions
                    and not orders
                ),
                fallback_flat_started_at=self.repository.flat_no_entry_since(
                    mode=mode,
                    before_cycle_at=now,
                ),
                managed_structures=self.repository.open_managed_structures(),
                chains=chains,
            )
            portfolio_candidate_exclusions = _portfolio_candidate_exclusions(
                report.candidates,
                pending_underlyings=risk_summary["pending_underlyings"],
                open_underlyings=risk_summary["open_underlyings"],
                open_underlying_structure_counts=risk_summary["open_underlying_structure_counts"],
                now=now,
            )
            directional_exclusions, directional_confirmation = (
                _directional_policy_exclusions(
                    report.candidates,
                    snapshots=snapshots,
                    repository=self.repository,
                    run_id=run_id,
                    mode=mode,
                    now=now,
                )
                if production_account
                else ({}, {})
            )
            for candidate_id, reasons in directional_exclusions.items():
                portfolio_candidate_exclusions[candidate_id] = tuple(
                    dict.fromkeys((*portfolio_candidate_exclusions.get(candidate_id, ()), *reasons))
                )
            rotation_exclusions = _strategy_rotation_exclusions(
                report.candidates,
                rotation=rotation,
            )
            for candidate_id, reasons in rotation_exclusions.items():
                portfolio_candidate_exclusions[candidate_id] = tuple(
                    dict.fromkeys((*portfolio_candidate_exclusions.get(candidate_id, ()), *reasons))
                )
            auction_candidates = tuple(
                candidate
                for candidate in report.candidates
                if candidate.candidate_id not in portfolio_candidate_exclusions
            )
            market_context = {
                "observed_at": now.isoformat(),
                "features": [_snapshot_features(snapshot) for snapshot in snapshots],
                "portfolio_candidate_exclusions": portfolio_candidate_exclusions,
                "directional_confirmation": directional_confirmation,
            }
            portfolio_context = {
                "equity": str(account.equity),
                "open_defined_loss": str(risk_summary["total_open_defined_loss"]),
                "open_structures": risk_summary["open_alpha_structures"],
                "execution_state": execution_state.value,
                "reconciliation_clean": reconciliation_clean,
            }
            if auction_candidates:
                envelope = await model.decide(
                    candidates=auction_candidates,
                    market_context=market_context,
                    portfolio_context=portfolio_context,
                )
            else:
                rotation_due = rotation["decision"] == "rotate_playbook"
                envelope = safe_model_decision(
                    {
                        "regime": "dislocated",
                        "action": "abstain",
                        "candidate_id": None,
                        "confidence": 1.0,
                        "thesis": (
                            "The 45-minute strategy rotation fired, but cash won because no "
                            "alternative cleared every hard gate."
                            if rotation_due
                            else "Cash won because every generated candidate was excluded by "
                            "competition policy, confirmation, pending, or "
                            "existing-underlying state."
                        ),
                        "evidence": [
                            "The full candidate report was preserved for counterfactual audit."
                        ],
                        "invalidation": [
                            "A later cycle may proceed after managed or pending exposure clears."
                        ],
                        "maximum_holding_minutes": 0,
                    },
                    provider="deterministic",
                )
            selected = _candidate_by_id(auction_candidates, envelope.decision.candidate_id)
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
                open_underlyings=risk_summary["open_underlyings"],
                open_underlying_structure_counts=risk_summary["open_underlying_structure_counts"],
                kill_switch_active=bool(operational.get("kill_switch_active", False)),
                reconciliation_clean=reconciliation_clean,
                daily_loss_entry_halt_active=(
                    daily_control.status in {"provisional", "latched"}
                    and not is_final_competition_day(now)
                ),
                maverick_candidate_ids=frozenset(
                    candidate_id
                    for candidate_id, item in directional_confirmation.items()
                    if item.get("maverick_signal_confirmed") is True
                ),
                maverick_entry_already_used=self.repository.maverick_entry_used(now=now),
            )
            risk = evaluate_risk(envelope.decision, selected, context)
            holding_policy = entry_holding_policy(
                now,
                envelope.decision.maximum_holding_minutes,
                maximum_holding_minutes=(
                    selected.maximum_holding_minutes if selected is not None else None
                )
                or None,
                hard_deadline=selected.holding_deadline if selected is not None else None,
            )
            if risk.approved:
                envelope = envelope.model_copy(
                    update={
                        "decision": envelope.decision.model_copy(
                            update={
                                "maximum_holding_minutes": holding_policy.effective_holding_minutes
                            }
                        )
                    }
                )
            auction = AuctionResult(
                ranked_candidate_ids=tuple(c.candidate_id for c in auction_candidates),
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
                        take_profit_multiple=_entry_take_profit_multiple(
                            risk=risk,
                            selected=selected,
                            now=now,
                        ),
                    )
                    order_result = await adapter.place_option_order(order_request)
                    self.repository.persist_order(run_id, order_request, order_result)
                    self.repository.increase_daily_loss_envelope(now=now, amount=risk.awarded_risk)

            rotation = _strategy_rotation_after_selection(
                rotation,
                selected=selected,
                entry_submitted=order_result is not None,
            )

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
                portfolio_candidate_exclusions=portfolio_candidate_exclusions,
                directional_confirmation=directional_confirmation,
                daily_loss_control=daily_loss_evidence,
                strategy_rotation=rotation,
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
            if (
                account is not None
                and risk_summary is not None
                and not account_checkpoint_persisted
                and max(Decimal("0"), risk_summary["start_of_day_equity"] - account.equity)
                < risk_summary["start_of_day_equity"] * daily_loss_pct_at(now)
            ):
                self.repository.persist_account_checkpoint(
                    run_id,
                    account=account,
                    official=official,
                    peak_equity=peak_equity,
                    positions=checkpoint_positions,
                    observed_at=now,
                )
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
        freshly_filled_entry_candidates: set[str] = set()
        quote_map = {quote.symbol: quote for chain in chains.values() for quote in chain}
        signed_position_quantities = {
            str(position.get("symbol")): Decimal(str(position.get("qty") or 0))
            for position in positions
            if position.get("symbol") and Decimal(str(position.get("qty") or 0)) != 0
        }
        position_quantities = {
            symbol: abs(quantity) for symbol, quantity in signed_position_quantities.items()
        }
        for candidate_id in self.repository.reconcile_filled_closing_parents(now=now):
            events.append(
                {
                    "event": "closing_parent_terminal_reconciled",
                    "candidate_id": candidate_id,
                    "status": "closed",
                }
            )
        if clock.flat_target_reached and (positions or self.repository.pending_managed_orders()):
            incidents.append("flat_target_exposure_remaining")
        for order in self.repository.pending_managed_orders():
            broker_order = await adapter.order_by_id(order.broker_order_id)
            broker_status = str(broker_order.get("status") or "").lower()
            if broker_status in {"canceled", "expired", "filled", "rejected"}:
                terminal_status = broker_status
                reported_filled = broker_order.get("filled_qty")
                if (
                    not order.is_closing
                    and broker_status != "filled"
                    and reported_filled is not None
                    and Decimal(str(reported_filled)) > 0
                ):
                    terminal_status = "partially_filled_canceled"
                self.repository.mark_order_status(
                    order.client_order_id, status=terminal_status, now=now
                )
                if not order.is_closing and broker_status == "filled":
                    freshly_filled_entry_candidates.add(order.candidate_id)
                if order.is_closing and broker_status == "filled":
                    reported_quantity = broker_order.get("qty")
                    quantity_mismatch = (
                        reported_quantity is not None
                        and Decimal(str(reported_quantity)) != Decimal(order.quantity)
                    ) or (
                        reported_filled is not None
                        and Decimal(str(reported_filled)) != Decimal(order.quantity)
                    )
                    expected_after_close: dict[str, Decimal] = {}
                    for managed in self.repository.open_managed_structures():
                        if managed.client_order_id == order.parent_client_order_id:
                            continue
                        for leg in managed.structure.legs:
                            direction = Decimal("1") if leg.side.value == "buy" else Decimal("-1")
                            expected_after_close[leg.symbol] = expected_after_close.get(
                                leg.symbol, Decimal("0")
                            ) + (direction * managed.quantity * leg.ratio_qty)
                    expected_after_close = {
                        symbol: quantity
                        for symbol, quantity in expected_after_close.items()
                        if quantity != 0
                    }
                    residual_inventory_mismatch = signed_position_quantities != expected_after_close
                    if quantity_mismatch:
                        incidents.append("mismatched_filled_close_quantity")
                        active_closing_candidates.add(order.candidate_id.removesuffix(":close"))
                    elif residual_inventory_mismatch:
                        incidents.append("filled_close_residual_positions")
                        active_closing_candidates.add(order.candidate_id.removesuffix(":close"))
                    else:
                        parent_closed = self.repository.mark_managed_structure_closed(
                            order.candidate_id.removesuffix(":close"),
                            now=now,
                            parent_client_order_id=order.parent_client_order_id,
                            expected_quantity=order.quantity,
                        )
                        if not parent_closed:
                            incidents.append("ambiguous_filled_close_parent")
                events.append(
                    {
                        "event": (
                            "closing_order_terminal_reconciled"
                            if order.is_closing
                            else "entry_order_terminal_reconciled"
                        ),
                        "status": terminal_status,
                        "remaining_quantity": order.remaining_quantity,
                    }
                )
                continue
            if order.is_closing and order.parent_client_order_id is None:
                incidents.append("ambiguous_closing_order_parent")
                active_closing_candidates.add(order.candidate_id.removesuffix(":close"))
                continue
            cutoff_cancel = not order.is_closing and not allow_new_entries
            urgent_close = bool(
                order.is_closing
                and (
                    order.exit_urgency != "soft"
                    or force_close_reason is not None
                    or now >= min(daily_hard_exit_deadline(now), FORCED_FLATTEN_STARTS_AT)
                )
            )
            fresh_close_terms = refreshed_close_terms(order, quote_map) if urgent_close else None
            action = (
                None
                if cutoff_cancel
                else stale_order_action(
                    submitted_at=order.submitted_at,
                    now=now,
                    attempt=order.attempt,
                    original_limit=order.original_limit,
                    is_credit=order.is_credit,
                    soft_close=order.is_closing and not urgent_close,
                    quote_materially_changed=(
                        closing_quote_materially_changed(order, quote_map)
                        if order.is_closing
                        else True
                    ),
                    urgent_close=urgent_close,
                    fresh_executable_limit=(
                        fresh_close_terms[0] if fresh_close_terms is not None else None
                    ),
                    fresh_is_credit=(
                        fresh_close_terms[1] if fresh_close_terms is not None else None
                    ),
                    urgent_debit_cap=(urgent_close_debit_cap(order) if urgent_close else None),
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
                    "exit_reason": order.exit_reason,
                    "exit_urgency": "urgent" if urgent_close else order.exit_urgency,
                    "lifecycle_reason": action.reason if action is not None else "entry_cutoff",
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
            next_attempt = (
                min(order.attempt + 1, URGENT_MAX_REPRICE_ATTEMPTS)
                if urgent_close
                else order.attempt + 1
            )
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
                is_credit=action.next_is_credit,
                legs=fresh_close_terms[2] if fresh_close_terms is not None else None,
                attempt=next_attempt,
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
                    "limit_price": str(request.limit_price),
                    "is_credit": request.is_credit,
                    "exit_reason": request.exit_reason,
                    "exit_urgency": request.exit_urgency,
                    "lifecycle_reason": action.reason,
                }
            )

        for managed in self.repository.open_managed_structures():
            if managed.candidate_id in freshly_filled_entry_candidates:
                continue
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
                self.repository.mark_managed_structure_externally_reduced(
                    managed.client_order_id, now=now
                )
                incidents.append("rejected_close_incomplete_structure")
                events.append(
                    {
                        "event": "managed_structure_externally_reduced",
                        "client_order_id": managed.client_order_id,
                        "candidate_id": managed.candidate_id,
                        "present_leg_symbols": [
                            symbol
                            for symbol, is_present in zip(leg_symbols, present, strict=True)
                            if is_present
                        ],
                        "missing_leg_symbols": [
                            symbol
                            for symbol, is_present in zip(leg_symbols, present, strict=True)
                            if not is_present
                        ],
                        "risk_treatment": "original_defined_loss_retained",
                    }
                )
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
                    exit_reason=signal.reason,
                    exit_urgency=signal.urgency,
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
                    "exit_urgency": signal.urgency,
                }
            )
        return tuple(events), tuple(dict.fromkeys(incidents))


def _cycle_key(now: datetime, mode: RunMode) -> str:
    bucket_minutes = 1 if now >= FORCED_FLATTEN_STARTS_AT else 5
    minute = now.minute - now.minute % bucket_minutes
    bucket = now.replace(minute=minute, second=0, microsecond=0)
    return f"{mode.value}:{bucket.isoformat()}"


def _candidate_by_id(
    candidates: tuple[Candidate, ...], candidate_id: str | None
) -> Candidate | None:
    return next(
        (candidate for candidate in candidates if candidate.candidate_id == candidate_id),
        None,
    )


def _risk_check_applied(risk: Any, name: str) -> bool:
    return any(check.name == name and "applied=true" in check.actual for check in risk.checks)


def _entry_take_profit_multiple(*, risk: Any, selected: Candidate, now: datetime) -> Decimal | None:
    final_competition_day = daily_loss_pct_at(now) > DAILY_LOSS_PCT
    directional = selected.action in {Action.CALL_DEBIT_SPREAD, Action.PUT_DEBIT_SPREAD}
    if final_competition_day and directional:
        return MAVERICK_DIRECTIONAL_DEBIT_TAKE_PROFIT_MULTIPLE
    if _risk_check_applied(risk, "maverick_final_day_tier"):
        return MAVERICK_DIRECTIONAL_DEBIT_TAKE_PROFIT_MULTIPLE
    return None


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
    portfolio_candidate_exclusions: dict[str, tuple[str, ...]],
    directional_confirmation: dict[str, dict[str, Any]],
    daily_loss_control: dict[str, Any],
    strategy_rotation: dict[str, Any],
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
        "daily_loss_control": daily_loss_control,
        "strategy_rotation": strategy_rotation,
        "evidence": [_snapshot_features(snapshot) for snapshot in snapshots],
        "candidate_rejections": report.rejections,
        "portfolio_candidate_exclusions": portfolio_candidate_exclusions,
        "directional_confirmation": directional_confirmation,
        "candidates": candidates,
        "auction": auction.model_dump(mode="json"),
        "decision": envelope.decision.model_dump(mode="json"),
        "model_validation": {
            "provider": envelope.provider,
            "model": envelope.model,
            "provider_response_id_hash": envelope.provider_response_id_hash,
            "raw_response_hash": envelope.raw_response_hash,
            "selection_attempts": envelope.selection_attempts,
            "candidate_id_retry_used": envelope.candidate_id_retry_used,
            "initial_raw_response_hash": envelope.initial_raw_response_hash,
            "retry_raw_response_hash": envelope.retry_raw_response_hash,
            "deterministic_fallback_used": envelope.deterministic_fallback_used,
            "selection_provenance": envelope.selection_provenance,
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
            ]
            + [
                {
                    "candidate_id": candidate.candidate_id,
                    "maximum_profit": str(candidate.structure.maximum_profit),
                    "maximum_loss": str(candidate.structure.maximum_loss),
                    "excluded_before_auction": True,
                    "reason": reason,
                }
                for candidate, reason in report.rejected_candidates
            ],
        },
        "audit_hash": hashlib.sha256(
            f"{run_id}|{now.isoformat()}|{envelope.raw_response_hash}".encode()
        ).hexdigest(),
    }


def _portfolio_candidate_exclusions(
    candidates: tuple[Candidate, ...],
    *,
    pending_underlyings: frozenset[str],
    open_underlyings: frozenset[str],
    open_underlying_structure_counts: dict[str, int],
    now: datetime,
) -> dict[str, tuple[str, ...]]:
    exclusions: dict[str, tuple[str, ...]] = {}
    for candidate in candidates:
        underlying = candidate.structure.underlying
        reasons: list[str] = []
        if underlying in pending_underlyings:
            reasons.append("pending_entry_for_underlying")
        final_day_additional_index_structure = (
            is_final_competition_day(now)
            and underlying in UNIVERSE
            and open_underlying_structure_counts.get(underlying, 1) == 1
        )
        if underlying in open_underlyings and not final_day_additional_index_structure:
            reasons.append("existing_managed_structure_for_underlying")
        if reasons:
            exclusions[candidate.candidate_id] = tuple(reasons)
    return exclusions


def _strategy_rotation_before_selection(
    *,
    previous_passport: dict[str, Any],
    now: datetime,
    broker_flat: bool,
    fallback_flat_started_at: datetime | None,
    managed_structures: tuple[ManagedStructure, ...],
    chains: dict[str, list[OptionQuote]],
) -> dict[str, Any]:
    previous = previous_passport.get("strategy_rotation", {})
    previous_flat = previous.get("flat_no_entry", {}) if isinstance(previous, dict) else {}
    start_at = now
    if broker_flat and previous_flat.get("active"):
        raw_start = previous_flat.get("started_at")
        if isinstance(raw_start, str):
            start_at = datetime.fromisoformat(raw_start.replace("Z", "+00:00")).astimezone(UTC)
    elif broker_flat and fallback_flat_started_at is not None:
        start_at = fallback_flat_started_at.astimezone(UTC)
    elapsed = max(0, int((now - start_at).total_seconds() // 60)) if broker_flat else 0
    flat_due = broker_flat and elapsed >= int(STRATEGY_ROTATION_INTERVAL.total_seconds() // 60)
    last_setup = previous.get("last_setup") if isinstance(previous, dict) else None
    if not isinstance(last_setup, dict):
        last_setup = _decision_setup(previous_passport)

    quote_map = {quote.symbol: quote for chain in chains.values() for quote in chain}
    previous_open = (
        {
            str(item.get("candidate_id")): item
            for item in previous.get("open_structures", [])
            if isinstance(item, dict) and item.get("candidate_id")
        }
        if isinstance(previous, dict)
        else {}
    )
    prior_open_rotation_due = any(bool(item.get("rotation_due")) for item in previous_open.values())
    open_states: list[dict[str, Any]] = []
    for managed in managed_structures:
        current_multiple = _directional_mark_multiple(managed, quote_map)
        prior_multiple = Decimal(
            str(previous_open.get(managed.candidate_id, {}).get("maximum_progress_multiple", "0"))
        )
        maximum_progress = max(prior_multiple, current_multiple or Decimal("0"))
        open_elapsed = max(0, int((now - managed.opened_at).total_seconds() // 60))
        meaningful = maximum_progress >= MEANINGFUL_PROGRESS_MULTIPLE
        open_states.append(
            {
                "candidate_id": managed.candidate_id,
                "underlying": managed.structure.underlying,
                "strategy": managed.structure.strategy.value,
                "started_at": managed.opened_at.isoformat(),
                "elapsed_minutes": open_elapsed,
                "current_progress_multiple": (
                    str(current_multiple) if current_multiple is not None else None
                ),
                "maximum_progress_multiple": str(maximum_progress),
                "take_profit_multiple": (
                    str(managed.take_profit_multiple)
                    if managed.take_profit_multiple is not None
                    else None
                ),
                "meaningful_progress_threshold": str(MEANINGFUL_PROGRESS_MULTIPLE),
                "meaningful_progress": meaningful,
                "rotation_due": open_elapsed >= 45 and not meaningful,
            }
        )

    return {
        "interval_minutes": 45,
        "flat_no_entry": {
            "active": broker_flat,
            "started_at": start_at.isoformat() if broker_flat else None,
            "elapsed_minutes": elapsed,
            "rotation_due": flat_due,
        },
        "open_structures": open_states,
        "last_setup": last_setup,
        "decision": (
            "rotate_playbook"
            if flat_due
            or prior_open_rotation_due
            or any(item["rotation_due"] for item in open_states)
            else "continue_current_playbook"
        ),
        "reason": (
            "flat_without_entry_for_45_minutes"
            if flat_due
            else "open_structure_without_meaningful_progress_for_45_minutes"
            if prior_open_rotation_due or any(item["rotation_due"] for item in open_states)
            else "rotation_interval_not_reached"
        ),
    }


def _strategy_rotation_exclusions(
    candidates: tuple[Candidate, ...], *, rotation: dict[str, Any]
) -> dict[str, tuple[str, ...]]:
    last_setup = rotation.get("last_setup")
    if rotation.get("decision") != "rotate_playbook" or not isinstance(last_setup, dict):
        return {}
    underlying = last_setup.get("underlying")
    action = last_setup.get("action")
    return {
        candidate.candidate_id: ("strategy_rotation_same_setup_excluded",)
        for candidate in candidates
        if candidate.structure.underlying == underlying and candidate.action.value == action
    }


def _strategy_rotation_after_selection(
    rotation: dict[str, Any], *, selected: Candidate | None, entry_submitted: bool
) -> dict[str, Any]:
    updated = dict(rotation)
    if selected is not None:
        updated["last_setup"] = {
            "underlying": selected.structure.underlying,
            "action": selected.action.value,
            "candidate_id": selected.candidate_id,
        }
    if entry_submitted:
        updated["flat_no_entry"] = {
            "active": False,
            "started_at": None,
            "elapsed_minutes": 0,
            "rotation_due": False,
        }
        updated["decision"] = "entry_submitted"
        updated["reason"] = "strategy_clock_reset_after_entry"
    return updated


def _decision_setup(passport: dict[str, Any]) -> dict[str, Any] | None:
    decision = passport.get("decision", {})
    candidate_id = decision.get("candidate_id") if isinstance(decision, dict) else None
    if not candidate_id:
        return None
    for candidate in passport.get("candidates", []):
        if not isinstance(candidate, dict) or candidate.get("candidate_id") != candidate_id:
            continue
        structure = candidate.get("structure", {})
        return {
            "underlying": structure.get("underlying"),
            "action": candidate.get("action"),
            "candidate_id": candidate_id,
        }
    return None


def _directional_mark_multiple(
    managed: ManagedStructure, quotes: dict[str, OptionQuote]
) -> Decimal | None:
    if managed.structure.is_credit or managed.structure.net_price <= 0:
        return None
    close_credit = Decimal("0")
    for leg in managed.structure.legs:
        quote = quotes.get(leg.symbol)
        if quote is None:
            return None
        close_credit += (
            quote.bid * leg.ratio_qty if leg.side.value == "buy" else -quote.ask * leg.ratio_qty
        )
    return max(Decimal("0"), close_credit) / managed.structure.net_price


def _directional_policy_exclusions(
    candidates: tuple[Candidate, ...],
    *,
    snapshots: list[UnderlyingSnapshot],
    repository: AuditRepository,
    run_id: str,
    mode: RunMode,
    now: datetime,
) -> tuple[dict[str, tuple[str, ...]], dict[str, dict[str, Any]]]:
    exclusions: dict[str, tuple[str, ...]] = {}
    evidence: dict[str, dict[str, Any]] = {}
    snapshots_by_symbol = {snapshot.symbol: snapshot for snapshot in snapshots}
    for candidate in candidates:
        if candidate.action is Action.INDEX_CONDOR:
            exclusions[candidate.candidate_id] = ("competition_directional_only_policy",)
            evidence[candidate.candidate_id] = {
                "passed": False,
                "reason": "competition_directional_only_policy",
                "current_cycle_at": now.isoformat(),
            }
            continue
        if candidate.action not in {Action.CALL_DEBIT_SPREAD, Action.PUT_DEBIT_SPREAD}:
            continue
        symbol = candidate.structure.underlying
        current = snapshots_by_symbol.get(symbol)
        prior = repository.prior_market_observation(
            symbol=symbol,
            mode=mode,
            exclude_run_id=run_id,
            before_cycle_at=now,
        )
        confirmation_reason = _directional_confirmation_reason(
            current=current, prior=prior, now=now
        )
        stop = repository.latest_directional_stop(
            underlying=symbol,
            action=candidate.action.value,
            now=now,
        )
        reset_at = (
            repository.directional_signal_reset_at(
                stop=stop,
                before_cycle_at=now,
                mode=mode,
            )
            if stop is not None
            else None
        )
        post_stop_reason = None
        if stop is not None and (reset_at is None or prior is None or reset_at > prior.cycle_at):
            post_stop_reason = "post_stop_signal_reset_and_reconfirmation_required"
        previous_trend = prior.snapshot.trend_return_pct if prior and prior.snapshot else None
        trend_acceleration = (
            abs(current.trend_return_pct) - abs(previous_trend)
            if current is not None and previous_trend is not None
            else None
        )
        opposite_action = (
            Action.PUT_DEBIT_SPREAD.value
            if candidate.action is Action.CALL_DEBIT_SPREAD
            else Action.CALL_DEBIT_SPREAD.value
        )
        opposite_stop = repository.latest_directional_stop(
            underlying=symbol,
            action=opposite_action,
            now=now,
        )
        opposite_reset_at = (
            repository.directional_signal_reset_at(
                stop=opposite_stop,
                before_cycle_at=now,
                mode=mode,
            )
            if opposite_stop is not None
            else None
        )
        reset_reversal_confirmed = bool(
            opposite_reset_at is not None
            and prior is not None
            and opposite_reset_at <= prior.cycle_at
        )
        maverick_signal_confirmed = bool(
            confirmation_reason is None
            and (
                (
                    trend_acceleration is not None
                    and trend_acceleration >= MAVERICK_MIN_TREND_ACCELERATION
                )
                or reset_reversal_confirmed
            )
        )
        reasons = tuple(
            reason for reason in (confirmation_reason, post_stop_reason) if reason is not None
        )
        item: dict[str, Any] = {
            "passed": not reasons,
            "reason": reasons[0] if reasons else "two_cycle_direction_confirmed",
            "current_cycle_at": now.isoformat(),
            "current_observed_at": current.observed_at.isoformat() if current else None,
            "current_trend_return_pct": str(current.trend_return_pct) if current else None,
            "previous_cycle_at": prior.cycle_at.isoformat() if prior else None,
            "previous_observed_at": prior.observed_at.isoformat() if prior else None,
            "previous_trend_return_pct": (
                str(prior.snapshot.trend_return_pct) if prior and prior.snapshot else None
            ),
            "previous_validation_error": prior.validation_error if prior else None,
            "latest_identical_setup_stop_at": stop.stopped_at.isoformat() if stop else None,
            "post_stop_signal_reset_at": reset_at.isoformat() if reset_at else None,
            "post_stop_reset_and_reconfirmation_passed": post_stop_reason is None,
            "trend_acceleration": (
                str(trend_acceleration) if trend_acceleration is not None else None
            ),
            "maverick_min_trend_acceleration": str(MAVERICK_MIN_TREND_ACCELERATION),
            "opposite_setup_stop_at": opposite_stop.stopped_at.isoformat()
            if opposite_stop
            else None,
            "opposite_setup_reset_at": (
                opposite_reset_at.isoformat() if opposite_reset_at else None
            ),
            "reset_reversal_confirmed": reset_reversal_confirmed,
            "maverick_signal_confirmed": maverick_signal_confirmed,
        }
        evidence[candidate.candidate_id] = item
        if reasons:
            exclusions[candidate.candidate_id] = reasons
    return exclusions, evidence


def _directional_confirmation_reason(
    *,
    current: UnderlyingSnapshot | None,
    prior: PriorMarketObservation | None,
    now: datetime,
) -> str | None:
    if current is None or prior is None:
        return "directional_confirmation_missing_history"
    if prior.snapshot is None:
        return "directional_confirmation_malformed_history"
    if abs(current.trend_return_pct) < Decimal("0.004"):
        return "directional_confirmation_current_below_threshold"
    current_bucket = _normal_cycle_bucket(now)
    prior_bucket = _normal_cycle_bucket(prior.cycle_at)
    gap = current_bucket - prior_bucket
    if gap < DIRECTION_CONFIRMATION_MIN_GAP or gap > DIRECTION_CONFIRMATION_MAX_GAP:
        return "directional_confirmation_stale_or_nonconsecutive"
    previous_trend = prior.snapshot.trend_return_pct
    current_trend = current.trend_return_pct
    if abs(previous_trend) < Decimal("0.004"):
        return "directional_confirmation_previous_below_threshold"
    if (previous_trend > 0) != (current_trend > 0):
        return "directional_confirmation_direction_reversed"
    return None


def _normal_cycle_bucket(at: datetime) -> datetime:
    minute = at.minute - at.minute % 5
    return at.astimezone(UTC).replace(minute=minute, second=0, microsecond=0)


def _snapshot_features(snapshot: UnderlyingSnapshot) -> dict[str, Any]:
    features = snapshot.model_dump(mode="json")
    features["richness_ratio"] = str(snapshot.richness_ratio)
    return features
