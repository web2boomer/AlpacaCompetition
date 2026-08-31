import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, desc, func, select
from sqlalchemy.exc import IntegrityError

from money_machine.domain.clock import (
    BASELINE_EQUITY,
    EOD_EQUITY_SNAPSHOT_AT,
    HACKATHON_STARTS_AT,
    SCORING_STARTS_AT,
    scoring_window_state,
)
from money_machine.domain.enums import ExecutionState, RunMode
from money_machine.domain.schemas import (
    AccountSnapshot,
    AuctionResult,
    BrokerOrderRequest,
    BrokerOrderResult,
    Candidate,
    ModelDecisionEnvelope,
    OptionLeg,
    OptionStructure,
    RiskDecisionResult,
    UnderlyingSnapshot,
)
from money_machine.execution import ManagedOrder, ManagedStructure
from money_machine.persistence.database import Database
from money_machine.persistence.models import (
    AgentRunORM,
    AuctionORM,
    BrokerOrderORM,
    CandidateORM,
    EquitySnapshotORM,
    FillORM,
    MarketSnapshotORM,
    ModelDecisionORM,
    OptionStructureORM,
    PositionSnapshotORM,
    RiskDecisionORM,
    SchedulerLeaseORM,
    SystemStateORM,
)


@dataclass(frozen=True, slots=True)
class PersistedEquitySnapshot:
    id: int
    observed_at: datetime
    equity: Decimal
    cash: Decimal
    portfolio_value: Decimal
    realized_pl: Decimal
    unrealized_pl: Decimal


class AuditRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def begin_run(self, cycle_key: str, mode: RunMode, started_at: datetime) -> tuple[str, bool]:
        run_id = str(uuid4())
        try:
            with self.database.session() as session:
                session.add(
                    AgentRunORM(
                        id=run_id,
                        cycle_key=cycle_key,
                        correlation_id=str(uuid4()),
                        mode=mode.value,
                        status="started",
                        started_at=started_at,
                    )
                )
            return run_id, True
        except IntegrityError:
            with self.database.session() as session:
                existing = session.scalar(
                    select(AgentRunORM).where(AgentRunORM.cycle_key == cycle_key)
                )
                if existing is None:
                    raise
                return existing.id, False

    def persist_account_checkpoint(
        self,
        run_id: str,
        *,
        account: AccountSnapshot,
        official: bool,
        peak_equity: Decimal,
        positions: list[dict[str, Any]],
        observed_at: datetime,
    ) -> None:
        with self.database.session() as session:
            drawdown = max(Decimal("0"), peak_equity - account.equity)
            session.add(
                EquitySnapshotORM(
                    agent_run_id=run_id,
                    observed_at=observed_at,
                    equity=account.equity,
                    cash=account.cash,
                    buying_power=account.buying_power,
                    portfolio_value=account.portfolio_value,
                    realized_pl=account.realized_pl,
                    unrealized_pl=account.unrealized_pl,
                    peak_equity=peak_equity,
                    drawdown=drawdown,
                    official=official,
                )
            )
            for position in positions:
                safe = _safe_position(position)
                session.add(
                    PositionSnapshotORM(
                        agent_run_id=run_id,
                        observed_at=observed_at,
                        broker_position_id=str(
                            safe.get("asset_id") or safe.get("symbol") or "unknown"
                        ),
                        symbol=str(safe.get("symbol") or "unknown"),
                        quantity=Decimal(str(safe.get("qty") or 0)),
                        market_value=Decimal(str(safe.get("market_value") or 0)),
                        unrealized_pl=Decimal(str(safe.get("unrealized_pl") or 0)),
                        raw_hash=_hash(safe),
                    )
                )

    def persist_market_observations(
        self,
        run_id: str,
        *,
        source: str,
        snapshots: list[UnderlyingSnapshot],
    ) -> None:
        with self.database.session() as session:
            for snapshot in snapshots:
                features = snapshot.model_dump(mode="json")
                features["richness_ratio"] = str(snapshot.richness_ratio)
                session.add(
                    MarketSnapshotORM(
                        agent_run_id=run_id,
                        observed_at=snapshot.observed_at,
                        source=source,
                        symbol=snapshot.symbol,
                        features_json=features,
                        raw_hash=_hash(features),
                    )
                )

    def persist_candidates(self, run_id: str, candidates: tuple[Candidate, ...]) -> None:
        with self.database.session() as session:
            for candidate in candidates:
                row = CandidateORM(
                    agent_run_id=run_id,
                    candidate_id=candidate.candidate_id,
                    action=candidate.action.value,
                    symbol=candidate.structure.underlying,
                    score=candidate.score,
                    maximum_loss=candidate.structure.maximum_loss,
                    expected_price=candidate.expected_credit_or_debit,
                    liquidity_json={
                        "passed": candidate.liquidity_passed,
                        "spread": str(candidate.structure_spread),
                        "age_seconds": candidate.data_age_seconds,
                    },
                    evidence_json=list(candidate.gate_evidence),
                )
                session.add(row)
                session.flush()
                structure = candidate.structure
                session.add(
                    OptionStructureORM(
                        candidate_row_id=row.id,
                        strategy=structure.strategy.value,
                        underlying=structure.underlying,
                        expiration=structure.expiration,
                        legs_json=[leg.model_dump(mode="json") for leg in structure.legs],
                        net_price=structure.net_price,
                        maximum_loss=structure.maximum_loss,
                        maximum_profit=structure.maximum_profit,
                        is_credit=structure.is_credit,
                    )
                )

    def persist_decisions(
        self,
        run_id: str,
        *,
        envelope: ModelDecisionEnvelope,
        risk: RiskDecisionResult,
        auction: AuctionResult,
    ) -> None:
        decision = envelope.decision
        with self.database.session() as session:
            session.add(
                ModelDecisionORM(
                    agent_run_id=run_id,
                    regime=decision.regime.value,
                    action=decision.action.value,
                    candidate_id=decision.candidate_id,
                    confidence=Decimal(str(decision.confidence)),
                    thesis=decision.thesis,
                    evidence_json=list(decision.evidence),
                    invalidation_json=list(decision.invalidation),
                    maximum_holding_minutes=decision.maximum_holding_minutes,
                    raw_response_hash=envelope.raw_response_hash,
                    validation_error=envelope.validation_error,
                )
            )
            session.add(
                RiskDecisionORM(
                    agent_run_id=run_id,
                    approved=risk.approved,
                    quantity=risk.quantity,
                    awarded_risk=risk.awarded_risk,
                    reason_codes_json=[reason.value for reason in risk.reason_codes],
                    checks_json=[check.model_dump(mode="json") for check in risk.checks],
                )
            )
            session.add(
                AuctionORM(
                    agent_run_id=run_id,
                    ranked_candidates_json=list(auction.ranked_candidate_ids),
                    selected_candidate_id=auction.selected_candidate_id,
                    cash_won=auction.cash_won,
                    awarded_risk=auction.awarded_risk,
                )
            )

    def order_exists(self, client_order_id: str) -> bool:
        with self.database.session() as session:
            count = session.scalar(
                select(func.count())
                .select_from(BrokerOrderORM)
                .where(BrokerOrderORM.client_order_id == client_order_id)
            )
            return bool(count and count > 0)

    def has_managed_orders(self, environment_role: str) -> bool:
        with self.database.session() as session:
            count = session.scalar(
                select(func.count())
                .select_from(BrokerOrderORM)
                .where(BrokerOrderORM.environment_role == environment_role)
            )
            return bool(count and count > 0)

    def persist_order(
        self,
        run_id: str,
        request: BrokerOrderRequest,
        result: BrokerOrderResult,
    ) -> None:
        with self.database.session() as session:
            session.add(
                BrokerOrderORM(
                    agent_run_id=run_id,
                    candidate_id=request.candidate_id,
                    client_order_id=request.client_order_id,
                    broker_order_id=result.broker_order_id,
                    status=result.status,
                    quantity=request.quantity,
                    limit_price=request.limit_price,
                    environment_role=request.environment_role,
                    attempt=request.attempt,
                    submitted_at=result.submitted_at,
                    last_seen_at=result.submitted_at,
                    raw_json={
                        "result": result.raw,
                        "request": {
                            "is_credit": request.is_credit,
                            "is_closing": request.is_closing,
                            "legs": [leg.model_dump(mode="json") for leg in request.legs],
                        },
                    },
                )
            )
            if result.raw.get("source") == "replay" and result.status == "filled":
                for index, leg in enumerate(request.legs):
                    session.add(
                        FillORM(
                            broker_order_id=result.broker_order_id,
                            activity_id=f"{result.broker_order_id}:{index}",
                            symbol=leg.symbol,
                            quantity=Decimal(request.quantity * leg.ratio_qty),
                            price=leg.midpoint,
                            filled_at=result.submitted_at,
                            source="replay",
                        )
                    )

    def persist_fills(self, activities: list[dict[str, Any]]) -> None:
        """Append broker fill activities idempotently and advance known order state."""
        with self.database.session() as session:
            for activity in activities:
                activity_id = str(activity.get("id") or activity.get("activity_id") or "")
                broker_order_id = str(activity.get("order_id") or "")
                if not activity_id or not broker_order_id:
                    raise ValueError("Alpaca fill activity is missing an immutable identifier")
                exists = session.scalar(
                    select(func.count())
                    .select_from(FillORM)
                    .where(
                        FillORM.broker_order_id == broker_order_id,
                        FillORM.activity_id == activity_id,
                    )
                )
                if not exists:
                    session.add(
                        FillORM(
                            broker_order_id=broker_order_id,
                            activity_id=activity_id,
                            symbol=str(activity.get("symbol") or ""),
                            quantity=Decimal(str(activity.get("qty") or 0)),
                            price=Decimal(str(activity.get("price") or 0)),
                            filled_at=_safe_datetime(
                                activity.get("transaction_time")
                                or activity.get("date")
                                or activity.get("created_at")
                            ),
                            source="alpaca_mcp_v2",
                        )
                    )
                order = session.scalar(
                    select(BrokerOrderORM).where(BrokerOrderORM.broker_order_id == broker_order_id)
                )
                if order is not None:
                    leaves = Decimal(str(activity.get("leaves_qty") or 0))
                    order.status = "filled" if leaves == 0 else "partially_filled"
                    raw_json = dict(order.raw_json)
                    raw_json["remaining_quantity"] = str(leaves)
                    order.raw_json = raw_json
                    order.last_seen_at = _safe_datetime(
                        activity.get("transaction_time") or activity.get("date")
                    )

    def pending_managed_orders(self) -> tuple[ManagedOrder, ...]:
        managed: list[ManagedOrder] = []
        with self.database.session() as session:
            rows = session.scalars(
                select(BrokerOrderORM).where(
                    BrokerOrderORM.status.in_(
                        (
                            "accepted",
                            "accepted_for_bidding",
                            "calculated",
                            "held",
                            "pending_new",
                            "pending_cancel",
                            "pending_replace",
                            "new",
                            "stopped",
                            "submitted",
                            "partially_filled",
                        )
                    )
                )
            )
            for row in rows:
                request = row.raw_json.get("request", {})
                raw_legs = request.get("legs", []) if isinstance(request, dict) else []
                if not row.broker_order_id or not isinstance(raw_legs, list) or not raw_legs:
                    continue
                if row.submitted_at is None:
                    continue
                remaining = int(Decimal(str(row.raw_json.get("remaining_quantity", row.quantity))))
                if remaining < 1:
                    continue
                managed.append(
                    ManagedOrder(
                        agent_run_id=row.agent_run_id,
                        candidate_id=row.candidate_id,
                        client_order_id=row.client_order_id,
                        broker_order_id=row.broker_order_id,
                        status=row.status,
                        quantity=row.quantity,
                        remaining_quantity=remaining,
                        original_limit=row.limit_price,
                        attempt=row.attempt,
                        submitted_at=_safe_datetime(row.submitted_at),
                        is_credit=bool(request.get("is_credit", False)),
                        is_closing=bool(request.get("is_closing", False)),
                        legs=tuple(OptionLeg.model_validate(leg) for leg in raw_legs),
                    )
                )
        return tuple(managed)

    def open_managed_structures(self) -> tuple[ManagedStructure, ...]:
        managed: list[ManagedStructure] = []
        with self.database.session() as session:
            rows = session.execute(
                select(BrokerOrderORM, CandidateORM, OptionStructureORM, ModelDecisionORM)
                .join(
                    CandidateORM,
                    and_(
                        CandidateORM.agent_run_id == BrokerOrderORM.agent_run_id,
                        CandidateORM.candidate_id == BrokerOrderORM.candidate_id,
                    ),
                )
                .join(
                    OptionStructureORM,
                    OptionStructureORM.candidate_row_id == CandidateORM.id,
                )
                .join(
                    ModelDecisionORM,
                    ModelDecisionORM.agent_run_id == BrokerOrderORM.agent_run_id,
                )
                .where(
                    BrokerOrderORM.status.in_(("filled", "closing", "partially_filled_canceled"))
                )
            )
            for order, candidate, structure, decision in rows:
                request = order.raw_json.get("request", {})
                if isinstance(request, dict) and request.get("is_closing"):
                    continue
                if not order.broker_order_id:
                    continue
                managed.append(
                    ManagedStructure(
                        agent_run_id=order.agent_run_id,
                        candidate_id=candidate.candidate_id,
                        client_order_id=order.client_order_id,
                        broker_order_id=order.broker_order_id,
                        status=order.status,
                        quantity=order.quantity,
                        opened_at=_safe_datetime(order.submitted_at),
                        maximum_holding_minutes=decision.maximum_holding_minutes,
                        structure=OptionStructure(
                            strategy=structure.strategy,
                            underlying=structure.underlying,
                            expiration=structure.expiration,
                            legs=tuple(
                                OptionLeg.model_validate(leg) for leg in structure.legs_json
                            ),
                            net_price=structure.net_price,
                            maximum_loss=structure.maximum_loss,
                            maximum_profit=structure.maximum_profit,
                            is_credit=structure.is_credit,
                        ),
                    )
                )
        return tuple(managed)

    def mark_order_status(self, client_order_id: str, *, status: str, now: datetime) -> None:
        with self.database.session() as session:
            order = session.scalar(
                select(BrokerOrderORM).where(BrokerOrderORM.client_order_id == client_order_id)
            )
            if order is None:
                raise KeyError(client_order_id)
            order.status = status
            order.last_seen_at = now

    def latest_operational_state(self) -> dict[str, Any]:
        with self.database.session() as session:
            state = session.scalar(
                select(SystemStateORM).order_by(desc(SystemStateORM.id)).limit(1)
            )
            if state is None:
                return {
                    "kill_switch_active": False,
                    "reconciliation_clean": True,
                    "scheduler_heartbeat_at": None,
                    "last_success_at": None,
                }
            heartbeat = session.scalar(select(func.max(SystemStateORM.scheduler_heartbeat_at)))
            last_success = session.scalar(select(func.max(SystemStateORM.last_success_at)))
            return {
                "kill_switch_active": state.kill_switch_active,
                "reconciliation_clean": state.reconciliation_clean,
                "execution_state": state.execution_state,
                "incident_code": state.incident_code,
                "scheduler_heartbeat_at": heartbeat,
                "last_success_at": last_success,
            }

    def development_round_trip_verified(self) -> bool:
        """Return only whether a completed paper execution round trip is audited."""
        with self.database.session() as session:
            count = session.scalar(
                select(func.count())
                .select_from(AgentRunORM)
                .where(
                    AgentRunORM.cycle_key.like("development-roundtrip:%"),
                    AgentRunORM.status == "completed",
                )
            )
            return bool(count and count > 0)

    def portfolio_risk_summary(
        self, fallback_equity: Decimal, *, now: datetime | None = None
    ) -> dict[str, Any]:
        observed_at = (now or datetime.now(UTC)).astimezone(UTC)
        day_start = observed_at.replace(hour=0, minute=0, second=0, microsecond=0)
        with self.database.session() as session:
            peak = session.scalar(select(func.max(EquitySnapshotORM.equity))) or fallback_equity
            latest_today = (
                session.scalar(
                    select(EquitySnapshotORM.equity)
                    .where(EquitySnapshotORM.observed_at >= day_start)
                    .order_by(EquitySnapshotORM.observed_at)
                    .limit(1)
                )
                or fallback_equity
            )
            open_statuses = (
                "accepted",
                "accepted_for_bidding",
                "calculated",
                "held",
                "pending_new",
                "pending_cancel",
                "pending_replace",
                "new",
                "stopped",
                "submitted",
                "partially_filled",
                "partially_filled_canceled",
                "filled",
                "closing",
            )
            open_loss = session.scalar(
                select(func.coalesce(func.sum(RiskDecisionORM.awarded_risk), 0))
                .join(
                    BrokerOrderORM,
                    BrokerOrderORM.agent_run_id == RiskDecisionORM.agent_run_id,
                )
                .where(RiskDecisionORM.approved, BrokerOrderORM.status.in_(open_statuses))
            ) or Decimal("0")
            open_count = (
                session.scalar(
                    select(func.count())
                    .select_from(RiskDecisionORM)
                    .join(
                        BrokerOrderORM,
                        BrokerOrderORM.agent_run_id == RiskDecisionORM.agent_run_id,
                    )
                    .where(RiskDecisionORM.approved, BrokerOrderORM.status.in_(open_statuses))
                )
                or 0
            )
            pending = set(
                session.scalars(
                    select(CandidateORM.symbol)
                    .join(
                        BrokerOrderORM,
                        and_(
                            BrokerOrderORM.candidate_id == CandidateORM.candidate_id,
                            BrokerOrderORM.agent_run_id == CandidateORM.agent_run_id,
                        ),
                    )
                    .where(
                        BrokerOrderORM.status.in_(
                            [
                                "proposed",
                                "accepted",
                                "accepted_for_bidding",
                                "calculated",
                                "held",
                                "pending_new",
                                "pending_cancel",
                                "pending_replace",
                                "new",
                                "stopped",
                                "submitted",
                                "partially_filled",
                            ]
                        )
                    )
                )
            )
            open_underlyings = set(
                session.scalars(
                    select(CandidateORM.symbol)
                    .join(
                        BrokerOrderORM,
                        and_(
                            BrokerOrderORM.candidate_id == CandidateORM.candidate_id,
                            BrokerOrderORM.agent_run_id == CandidateORM.agent_run_id,
                        ),
                    )
                    .where(
                        BrokerOrderORM.status.in_(
                            [
                                "partially_filled",
                                "partially_filled_canceled",
                                "filled",
                                "closing",
                            ]
                        )
                    )
                )
            )
            return {
                "peak_equity": Decimal(str(peak)),
                "start_of_day_equity": Decimal(str(latest_today)),
                "total_open_defined_loss": Decimal(str(open_loss)),
                "index_cluster_defined_loss": Decimal(str(open_loss)),
                "open_alpha_structures": int(open_count),
                "pending_underlyings": frozenset(pending),
                "open_underlyings": frozenset(open_underlyings),
            }

    def latest_official_equity_at_or_before(
        self, observed_at: datetime, *, account_fingerprint: str | None = None
    ) -> PersistedEquitySnapshot | None:
        with self.database.session() as session:
            query = (
                select(EquitySnapshotORM)
                .join(AgentRunORM, AgentRunORM.id == EquitySnapshotORM.agent_run_id)
                .where(
                    EquitySnapshotORM.official,
                    EquitySnapshotORM.observed_at >= SCORING_STARTS_AT,
                    EquitySnapshotORM.observed_at <= EOD_EQUITY_SNAPSHOT_AT,
                    EquitySnapshotORM.observed_at <= observed_at,
                )
                .order_by(desc(EquitySnapshotORM.observed_at), desc(EquitySnapshotORM.id))
                .limit(1)
            )
            if account_fingerprint:
                query = query.where(
                    AgentRunORM.passport_json["account"]["fingerprint"].as_string()
                    == account_fingerprint
                )
            row = session.scalar(query)
            return _persisted_equity(row)

    def latest_pre_scoring_equity_at_or_before(
        self, observed_at: datetime, *, account_fingerprint: str | None = None
    ) -> PersistedEquitySnapshot | None:
        """Return persisted live-account telemetry without classifying it as official P&L."""
        with self.database.session() as session:
            query = (
                select(EquitySnapshotORM)
                .join(AgentRunORM, AgentRunORM.id == EquitySnapshotORM.agent_run_id)
                .where(
                    AgentRunORM.mode == RunMode.LIVE.value,
                    AgentRunORM.status == "completed",
                    EquitySnapshotORM.official.is_(False),
                    EquitySnapshotORM.observed_at >= HACKATHON_STARTS_AT,
                    EquitySnapshotORM.observed_at < SCORING_STARTS_AT,
                    EquitySnapshotORM.observed_at <= observed_at,
                )
                .order_by(desc(EquitySnapshotORM.observed_at), desc(EquitySnapshotORM.id))
                .limit(1)
            )
            if account_fingerprint:
                query = query.where(
                    AgentRunORM.passport_json["account"]["fingerprint"].as_string()
                    == account_fingerprint
                )
            row = session.scalar(query)
            return _persisted_equity(row)

    def first_official_equity_at_or_after(
        self, observed_at: datetime, *, account_fingerprint: str | None = None
    ) -> PersistedEquitySnapshot | None:
        with self.database.session() as session:
            query = (
                select(EquitySnapshotORM)
                .join(AgentRunORM, AgentRunORM.id == EquitySnapshotORM.agent_run_id)
                .where(
                    EquitySnapshotORM.official,
                    EquitySnapshotORM.observed_at >= SCORING_STARTS_AT,
                    EquitySnapshotORM.observed_at <= EOD_EQUITY_SNAPSHOT_AT,
                    EquitySnapshotORM.observed_at >= observed_at,
                )
                .order_by(EquitySnapshotORM.observed_at, EquitySnapshotORM.id)
                .limit(1)
            )
            if account_fingerprint:
                query = query.where(
                    AgentRunORM.passport_json["account"]["fingerprint"].as_string()
                    == account_fingerprint
                )
            row = session.scalar(query)
            return _persisted_equity(row)

    def competition_performance_summary(
        self,
        *,
        account_fingerprint: str | None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        observed_at = (now or datetime.now(UTC)).astimezone(UTC)
        empty = {
            "available": False,
            "starting_equity": str(BASELINE_EQUITY),
            "latest_equity": None,
            "dollar_pnl": None,
            "percentage_return": None,
            "peak_equity": None,
            "maximum_drawdown": None,
            "maximum_drawdown_percent": None,
            "latest_snapshot_at": None,
            "open_position_count": None,
            "working_order_count": None,
            "broker_confirmed_flat": None,
            "scoring_window_state": scoring_window_state(observed_at),
            "result_status": "unavailable",
            "alpaca_authoritative_notice": (
                "Alpaca account equity remains authoritative for the official result."
            ),
        }
        if not account_fingerprint:
            return empty
        with self.database.session() as session:
            rows = list(
                session.execute(
                    select(EquitySnapshotORM, AgentRunORM.passport_json)
                    .join(AgentRunORM, AgentRunORM.id == EquitySnapshotORM.agent_run_id)
                    .where(
                        EquitySnapshotORM.official,
                        EquitySnapshotORM.observed_at >= SCORING_STARTS_AT,
                        EquitySnapshotORM.observed_at <= EOD_EQUITY_SNAPSHOT_AT,
                        AgentRunORM.passport_json["account"]["fingerprint"].as_string()
                        == account_fingerprint,
                    )
                    .order_by(EquitySnapshotORM.observed_at, EquitySnapshotORM.id)
                )
            )
        if not rows:
            return empty
        snapshots = [row for row, _passport in rows]
        latest, latest_passport = rows[-1]
        peak = BASELINE_EQUITY
        maximum_drawdown = Decimal("0")
        for snapshot in snapshots:
            peak = max(peak, snapshot.equity)
            maximum_drawdown = max(maximum_drawdown, peak - snapshot.equity)
        pnl = latest.equity - BASELINE_EQUITY
        percentage_return = (pnl / BASELINE_EQUITY * Decimal("100")).quantize(Decimal("0.0001"))
        maximum_drawdown_percent = (
            maximum_drawdown / peak * Decimal("100") if peak else Decimal("0")
        ).quantize(Decimal("0.0001"))
        account = latest_passport.get("account", {}) if isinstance(latest_passport, dict) else {}
        latest_at = _safe_datetime(latest.observed_at)
        return {
            **empty,
            "available": True,
            "latest_equity": str(latest.equity),
            "dollar_pnl": str(pnl),
            "percentage_return": str(percentage_return),
            "peak_equity": str(peak),
            "maximum_drawdown": str(maximum_drawdown),
            "maximum_drawdown_percent": str(maximum_drawdown_percent),
            "latest_snapshot_at": latest_at.isoformat(),
            "open_position_count": int(account.get("open_position_count", 0)),
            "working_order_count": int(account.get("working_order_count", 0)),
            "broker_confirmed_flat": bool(account.get("broker_confirmed_flat", False)),
            "result_status": (
                "final_eod_snapshot" if latest_at == EOD_EQUITY_SNAPSHOT_AT else "provisional"
            ),
        }

    def official_equity_curve(self, *, account_fingerprint: str | None) -> list[EquitySnapshotORM]:
        if not account_fingerprint:
            return []
        with self.database.session() as session:
            return list(
                session.scalars(
                    select(EquitySnapshotORM)
                    .join(AgentRunORM, AgentRunORM.id == EquitySnapshotORM.agent_run_id)
                    .where(
                        EquitySnapshotORM.official,
                        EquitySnapshotORM.observed_at >= SCORING_STARTS_AT,
                        EquitySnapshotORM.observed_at <= EOD_EQUITY_SNAPSHOT_AT,
                        AgentRunORM.passport_json["account"]["fingerprint"].as_string()
                        == account_fingerprint,
                    )
                    .order_by(EquitySnapshotORM.observed_at, EquitySnapshotORM.id)
                )
            )

    def reconcile_broker_state(
        self,
        orders: list[dict[str, Any]],
        positions: list[dict[str, Any]],
        *,
        now: datetime | None = None,
        authoritative_absence: bool = False,
    ) -> tuple[bool, tuple[str, ...]]:
        with self.database.session() as session:
            known_orders = {
                order.client_order_id: order for order in session.scalars(select(BrokerOrderORM))
            }
            known_symbols: set[str] = set()
            for legs in session.scalars(select(OptionStructureORM.legs_json)):
                known_symbols.update(str(leg.get("symbol")) for leg in legs if leg.get("symbol"))
            incidents: list[str] = []
            for broker_order in orders:
                client_id = str(broker_order.get("client_order_id") or "")
                local_order = known_orders.get(client_id)
                if client_id and local_order is None:
                    incidents.append("orphaned_broker_order")
                elif local_order is not None:
                    local_order.status = str(broker_order.get("status") or local_order.status)
                    local_order.broker_order_id = str(
                        broker_order.get("id") or local_order.broker_order_id or ""
                    )
                    local_order.last_seen_at = _safe_datetime(
                        broker_order.get("updated_at") or broker_order.get("submitted_at") or now
                    )
                    remaining = _remaining_order_quantity(broker_order, local_order.quantity)
                    raw_json = dict(local_order.raw_json)
                    raw_json["remaining_quantity"] = str(remaining)
                    local_order.raw_json = raw_json
            for position in positions:
                symbol = str(position.get("symbol") or "")
                if symbol and symbol not in known_symbols:
                    incidents.append("orphaned_broker_position")
            position_inventory = {
                str(position.get("symbol")): Decimal(str(position.get("qty") or 0))
                for position in positions
                if position.get("symbol")
            }
            exposure_rows = list(
                session.execute(
                    select(BrokerOrderORM, OptionStructureORM)
                    .join(
                        CandidateORM,
                        and_(
                            CandidateORM.agent_run_id == BrokerOrderORM.agent_run_id,
                            CandidateORM.candidate_id == BrokerOrderORM.candidate_id,
                        ),
                    )
                    .join(
                        OptionStructureORM,
                        OptionStructureORM.candidate_row_id == CandidateORM.id,
                    )
                    .where(
                        BrokerOrderORM.status.in_(
                            (
                                "accepted",
                                "accepted_for_bidding",
                                "calculated",
                                "held",
                                "pending_new",
                                "pending_cancel",
                                "pending_replace",
                                "new",
                                "stopped",
                                "submitted",
                                "partially_filled",
                                "partially_filled_canceled",
                                "filled",
                                "closing",
                            )
                        )
                    )
                    .order_by(BrokerOrderORM.submitted_at, BrokerOrderORM.id)
                )
            )
            established_statuses = {"filled", "closing", "partially_filled_canceled"}
            for local_order, structure in exposure_rows:
                if local_order.status in established_statuses:
                    _reserve_structure_positions(
                        position_inventory, structure.legs_json, local_order.quantity
                    )
            for local_order, structure in exposure_rows:
                if local_order.status in established_statuses:
                    continue
                if not _consume_complete_structure_positions(
                    position_inventory, structure.legs_json, local_order.quantity
                ):
                    continue
                local_order.status = "filled"
                raw_json = dict(local_order.raw_json)
                raw_json["remaining_quantity"] = "0"
                raw_json["status_normalized_from_positions"] = True
                local_order.raw_json = raw_json
                local_order.last_seen_at = now or local_order.last_seen_at
            if authoritative_absence:
                position_symbols = {
                    str(position.get("symbol")) for position in positions if position.get("symbol")
                }
                structure_rows = session.execute(
                    select(BrokerOrderORM, OptionStructureORM)
                    .join(
                        CandidateORM,
                        and_(
                            CandidateORM.agent_run_id == BrokerOrderORM.agent_run_id,
                            CandidateORM.candidate_id == BrokerOrderORM.candidate_id,
                        ),
                    )
                    .join(
                        OptionStructureORM,
                        OptionStructureORM.candidate_row_id == CandidateORM.id,
                    )
                    .where(BrokerOrderORM.status == "closing")
                )
                for local_order, structure in structure_rows:
                    leg_symbols = {
                        str(leg.get("symbol")) for leg in structure.legs_json if leg.get("symbol")
                    }
                    if leg_symbols.isdisjoint(position_symbols):
                        local_order.status = "closed"
                        local_order.last_seen_at = now or local_order.last_seen_at
        unique = tuple(dict.fromkeys(incidents))
        return not unique, unique

    def acquire_scheduler_lease(
        self, *, name: str, owner_id: str, now: datetime, ttl_seconds: int = 360
    ) -> bool:
        with self.database.session() as session:
            lease = session.get(SchedulerLeaseORM, name)
            if lease is not None and lease.owner_id != owner_id and lease.expires_at > now:
                return False
            if lease is None:
                lease = SchedulerLeaseORM(
                    name=name,
                    owner_id=owner_id,
                    acquired_at=now,
                    expires_at=now + timedelta(seconds=ttl_seconds),
                )
                session.add(lease)
            else:
                lease.owner_id = owner_id
                lease.acquired_at = now
                lease.expires_at = now + timedelta(seconds=ttl_seconds)
            return True

    def append_system_state(
        self,
        *,
        run_id: str | None,
        now: datetime,
        execution_state: ExecutionState,
        kill_switch_active: bool,
        reconciliation_clean: bool,
        success: bool,
        scheduler_event: bool = True,
        incident_code: str | None = None,
        incident_detail: str | None = None,
    ) -> None:
        with self.database.session() as session:
            session.add(
                SystemStateORM(
                    agent_run_id=run_id,
                    observed_at=now,
                    execution_state=execution_state.value,
                    kill_switch_active=kill_switch_active,
                    reconciliation_clean=reconciliation_clean,
                    scheduler_heartbeat_at=now if scheduler_event else None,
                    last_success_at=now if success and scheduler_event else None,
                    incident_code=incident_code,
                    incident_detail=incident_detail,
                )
            )

    def set_kill_switch(self, *, active: bool, now: datetime) -> None:
        latest = self.latest_operational_state()
        state_value = latest.get("execution_state", ExecutionState.HALTED.value)
        self.append_system_state(
            run_id=None,
            now=now,
            execution_state=ExecutionState(state_value),
            kill_switch_active=active,
            reconciliation_clean=bool(latest.get("reconciliation_clean", True)),
            success=True,
            scheduler_event=False,
            incident_code="manual_kill_switch" if active else "kill_switch_cleared",
        )

    def complete_run(
        self,
        run_id: str,
        *,
        completed_at: datetime,
        passport: dict[str, Any],
        incident: str | None = None,
    ) -> None:
        with self.database.session() as session:
            run = session.get(AgentRunORM, run_id)
            if run is None:
                raise KeyError(run_id)
            run.completed_at = completed_at
            run.status = "failed_closed" if incident else "completed"
            run.passport_json = passport
            run.incident = incident

    def latest_passport(self) -> dict[str, Any] | None:
        with self.database.session() as session:
            value = session.scalar(
                select(AgentRunORM.passport_json)
                .where(AgentRunORM.passport_json.is_not(None))
                .order_by(desc(AgentRunORM.completed_at))
                .limit(1)
            )
            return value

    def passport_for_run(self, run_id: str) -> dict[str, Any] | None:
        with self.database.session() as session:
            return session.scalar(select(AgentRunORM.passport_json).where(AgentRunORM.id == run_id))

    def recent_activity(self, *, limit: int = 12) -> list[dict[str, Any]]:
        """Return a redacted operator feed derived from completed audit records."""
        with self.database.session() as session:
            runs = list(
                session.scalars(
                    select(AgentRunORM)
                    .where(
                        AgentRunORM.completed_at.is_not(None),
                        AgentRunORM.passport_json.is_not(None),
                    )
                    .order_by(desc(AgentRunORM.completed_at))
                    .limit(limit)
                )
            )

        activity: list[dict[str, Any]] = []
        for run in runs:
            passport = run.passport_json or {}
            decision = passport.get("decision", {})
            risk = passport.get("risk", {})
            execution = passport.get("execution", {})
            operational = passport.get("operational_state", {})
            action = str(decision.get("action") or "abstain").replace("_", " ")
            reason = ""
            failed_checks = [
                str(check.get("name") or "policy").replace("_", " ")
                for check in risk.get("checks", [])
                if isinstance(check, dict) and not check.get("passed", False)
            ]
            if run.incident or passport.get("status") == "failed_closed":
                label = "Cycle failed closed"
                detail = "System halted the cycle before execution"
                status = "halted"
                tone = "danger"
            elif execution.get("submitted"):
                label = "Order activity"
                detail = f"{action} · {execution.get('status', 'submitted')}"
                status = "submitted"
                tone = "success"
            elif risk.get("approved"):
                label = "Risk approved"
                detail = f"{action} · no duplicate order submitted"
                status = "approved"
                tone = "success"
            else:
                label = "Cash retained"
                reasons = ", ".join(failed_checks[:2]) or "no eligible candidate"
                detail = f"{action} · {reasons}"
                reason = _cash_retained_reason(passport)
                status = "abstain"
                tone = "neutral"
            state = str(operational.get("execution_state") or "observe_only").replace("_", " ")
            completed_at = _safe_datetime(run.completed_at or run.started_at)
            activity.append(
                {
                    "timestamp": completed_at.isoformat(),
                    "display_time": completed_at.strftime("%H:%M:%S"),
                    "label": label,
                    "detail": detail,
                    "reason": reason,
                    "status": status,
                    "tone": tone,
                    "state": state,
                    "run_id": run.id,
                    "href": f"/runs/{run.id}",
                }
            )
        return activity

    def dashboard_summary(self) -> dict[str, Any]:
        latest_passport = self.latest_passport()
        official = bool(latest_passport and latest_passport.get("official"))
        account = latest_passport.get("account", {}) if latest_passport else {}
        fingerprint = account.get("fingerprint") if isinstance(account, dict) else None
        with self.database.session() as session:
            equity_query = (
                select(EquitySnapshotORM)
                .join(AgentRunORM, AgentRunORM.id == EquitySnapshotORM.agent_run_id)
                .where(EquitySnapshotORM.official == official)
            )
            if fingerprint:
                equity_query = equity_query.where(
                    AgentRunORM.passport_json["account"]["fingerprint"].as_string() == fingerprint
                )
            latest_equity = session.scalar(
                equity_query.order_by(desc(AgentRunORM.started_at)).limit(1)
            )
            equities = list(
                session.scalars(equity_query.order_by(desc(AgentRunORM.started_at)).limit(120))
            )
            equities.reverse()
            state = session.scalar(
                select(SystemStateORM).order_by(desc(SystemStateORM.id)).limit(1)
            )
            counts = {
                "approved": session.scalar(
                    select(func.count())
                    .select_from(RiskDecisionORM)
                    .where(RiskDecisionORM.approved)
                )
                or 0,
                "rejected": session.scalar(
                    select(func.count())
                    .select_from(RiskDecisionORM)
                    .where(~RiskDecisionORM.approved)
                )
                or 0,
            }
            return {
                "latest_equity": latest_equity,
                "equities": equities,
                "state": state,
                "counts": counts,
                "latest_passport": latest_passport,
            }


def deterministic_client_order_id(
    prefix: str, *, cycle_key: str, candidate_id: str, quantity: int, attempt: int = 0
) -> str:
    payload = f"{cycle_key}|{candidate_id}|{quantity}|{attempt}"
    digest = hashlib.sha256(payload.encode()).hexdigest()[:24]
    return f"{prefix}-{digest}"[:48]


def _hash(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _safe_position(position: dict[str, Any]) -> dict[str, Any]:
    allowed = {"asset_id", "symbol", "qty", "market_value", "unrealized_pl", "side"}
    return {key: value for key, value in position.items() if key in allowed}


def _safe_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("broker activity/order timestamp is missing")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _cash_retained_reason(passport: dict[str, Any]) -> str:
    """Explain an abstention using only evidence already sealed in its passport."""
    decision = passport.get("decision", {})
    thesis = str(decision.get("thesis") or "").strip()
    if thesis:
        return thesis

    rejections = passport.get("candidate_rejections", {})
    if isinstance(rejections, dict):
        summaries: list[str] = []
        for symbol, raw_reasons in rejections.items():
            reasons = raw_reasons if isinstance(raw_reasons, (list, tuple)) else [raw_reasons]
            first_reason = next((str(item).strip() for item in reasons if str(item).strip()), "")
            if first_reason:
                summaries.append(f"{symbol}: {first_reason}")
        if summaries:
            return "; ".join(summaries[:3])

    risk = passport.get("risk", {})
    reason_codes = risk.get("reason_codes", []) if isinstance(risk, dict) else []
    readable_codes = [str(code).replace("_", " ") for code in reason_codes if code]
    if readable_codes:
        return f"Policy kept the account in cash: {', '.join(readable_codes[:2])}."
    return "No candidate cleared every mandatory data, liquidity, model, and risk gate."


def _persisted_equity(row: EquitySnapshotORM | None) -> PersistedEquitySnapshot | None:
    if row is None:
        return None
    return PersistedEquitySnapshot(
        id=row.id,
        observed_at=_safe_datetime(row.observed_at),
        equity=row.equity,
        cash=row.cash,
        portfolio_value=row.portfolio_value,
        realized_pl=row.realized_pl,
        unrealized_pl=row.unrealized_pl,
    )


def _remaining_order_quantity(payload: dict[str, Any], fallback: int) -> Decimal:
    explicit = payload.get("remaining_qty") or payload.get("leaves_qty")
    if explicit is not None:
        return max(Decimal("0"), Decimal(str(explicit)))
    quantity = Decimal(str(payload.get("qty") or fallback))
    filled = Decimal(str(payload.get("filled_qty") or 0))
    return max(Decimal("0"), quantity - filled)


def _leg_direction(leg: dict[str, Any]) -> Decimal:
    return Decimal("1") if str(leg.get("side") or "").lower() == "buy" else Decimal("-1")


def _reserve_structure_positions(
    inventory: dict[str, Decimal], legs: list[dict[str, Any]], quantity: int
) -> None:
    for leg in legs:
        symbol = str(leg.get("symbol") or "")
        direction = _leg_direction(leg)
        available = inventory.get(symbol, Decimal("0")) * direction
        if available <= 0:
            continue
        required = Decimal(str(leg.get("ratio_qty") or 1)) * quantity
        reserved = min(available, required)
        inventory[symbol] = inventory.get(symbol, Decimal("0")) - direction * reserved


def _consume_complete_structure_positions(
    inventory: dict[str, Decimal], legs: list[dict[str, Any]], quantity: int
) -> bool:
    requirements: list[tuple[str, Decimal, Decimal]] = []
    for leg in legs:
        symbol = str(leg.get("symbol") or "")
        direction = _leg_direction(leg)
        required = Decimal(str(leg.get("ratio_qty") or 1)) * quantity
        if not symbol or inventory.get(symbol, Decimal("0")) * direction < required:
            return False
        requirements.append((symbol, direction, required))
    for symbol, direction, required in requirements:
        inventory[symbol] -= direction * required
    return True
