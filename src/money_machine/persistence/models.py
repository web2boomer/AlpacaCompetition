from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class AgentRunORM(Base):
    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    cycle_key: Mapped[str] = mapped_column(String(80), nullable=False, unique=True, index=True)
    correlation_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    passport_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    incident: Mapped[str | None] = mapped_column(Text)

    snapshots: Mapped[list["MarketSnapshotORM"]] = relationship(back_populates="agent_run")
    candidates: Mapped[list["CandidateORM"]] = relationship(back_populates="agent_run")


class MarketSnapshotORM(Base):
    __tablename__ = "market_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id"), nullable=False, index=True
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(12), nullable=False)
    features_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    raw_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    agent_run: Mapped[AgentRunORM] = relationship(back_populates="snapshots")


class OptionStructureORM(Base):
    __tablename__ = "option_structures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_row_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"), unique=True)
    strategy: Mapped[str] = mapped_column(String(32), nullable=False)
    underlying: Mapped[str] = mapped_column(String(12), nullable=False)
    expiration: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    legs_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    net_price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    maximum_loss: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    maximum_profit: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    is_credit: Mapped[bool] = mapped_column(Boolean, nullable=False)


class CandidateORM(Base):
    __tablename__ = "candidates"
    __table_args__ = (UniqueConstraint("agent_run_id", "candidate_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id"), nullable=False, index=True
    )
    candidate_id: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(12), nullable=False)
    score: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    maximum_loss: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    expected_price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    liquidity_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    evidence_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)

    agent_run: Mapped[AgentRunORM] = relationship(back_populates="candidates")
    structure: Mapped[OptionStructureORM | None] = relationship(cascade="all, delete-orphan")


class AuctionORM(Base):
    __tablename__ = "auctions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id"), unique=True, nullable=False
    )
    ranked_candidates_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    selected_candidate_id: Mapped[str | None] = mapped_column(String(180))
    cash_won: Mapped[bool] = mapped_column(Boolean, nullable=False)
    awarded_risk: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)


class ModelDecisionORM(Base):
    __tablename__ = "model_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id"), unique=True, nullable=False
    )
    regime: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    candidate_id: Mapped[str | None] = mapped_column(String(180))
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    thesis: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    invalidation_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    maximum_holding_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_response_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    validation_error: Mapped[str | None] = mapped_column(String(80))


class RiskDecisionORM(Base):
    __tablename__ = "risk_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id"), unique=True, nullable=False
    )
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    awarded_risk: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    reason_codes_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    checks_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)


class BrokerOrderORM(Base):
    __tablename__ = "broker_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id"), nullable=False, index=True
    )
    candidate_id: Mapped[str] = mapped_column(String(180), nullable=False)
    client_order_id: Mapped[str] = mapped_column(
        String(48), nullable=False, unique=True, index=True
    )
    broker_order_id: Mapped[str | None] = mapped_column(String(80), unique=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    limit_price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    environment_role: Mapped[str] = mapped_column(String(20), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class FillORM(Base):
    __tablename__ = "fills"
    __table_args__ = (UniqueConstraint("broker_order_id", "activity_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    broker_order_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    activity_id: Mapped[str] = mapped_column(String(100), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)


class PositionSnapshotORM(Base):
    __tablename__ = "position_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id"), nullable=False, index=True
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    broker_position_id: Mapped[str] = mapped_column(String(100), nullable=False)
    symbol: Mapped[str] = mapped_column(String(40), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    market_value: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    unrealized_pl: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    raw_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class EquitySnapshotORM(Base):
    __tablename__ = "equity_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id"), nullable=False, index=True
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    equity: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    cash: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    buying_power: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    portfolio_value: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    realized_pl: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    unrealized_pl: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    peak_equity: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    drawdown: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    official: Mapped[bool] = mapped_column(Boolean, nullable=False)


class SystemStateORM(Base):
    __tablename__ = "system_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    execution_state: Mapped[str] = mapped_column(String(32), nullable=False)
    kill_switch_active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reconciliation_clean: Mapped[bool] = mapped_column(Boolean, nullable=False)
    scheduler_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    incident_code: Mapped[str | None] = mapped_column(String(80))
    incident_detail: Mapped[str | None] = mapped_column(Text)


class SchedulerLeaseORM(Base):
    __tablename__ = "scheduler_leases"

    name: Mapped[str] = mapped_column(String(40), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(80), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
