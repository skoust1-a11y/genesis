"""Tables in the separate live-trading database.

They have no foreign keys into the research database. `deployed_strategies` holds a frozen copy
of the approved strategy version, and every deployment, intent and live trade points at it.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, false
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import AppendOnly, LiveBase
from app.db.models.core import new_id
from app.db.types import Money, UTCDateTime, utcnow


class DeployedStrategy(AppendOnly, LiveBase):
    __tablename__ = "deployed_strategies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    strategy_id: Mapped[str] = mapped_column(String(36), index=True)
    strategy_version_id: Mapped[int] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(200))
    markdown: Mapped[str] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(String(64))
    rules: Mapped[dict[str, Any]] = mapped_column(JSON)
    rules_sha256: Mapped[str] = mapped_column(String(64))
    report_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    verdict_label: Mapped[str | None] = mapped_column(String(32), nullable=True)
    approved_by: Mapped[str] = mapped_column(String(128))
    approved_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class BrokerAccount(AppendOnly, LiveBase):
    __tablename__ = "broker_accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    broker: Mapped[str] = mapped_column(String(32))
    account_ref_masked: Mapped[str] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(String(16))
    default_algo_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Deployment(AppendOnly, LiveBase):
    __tablename__ = "deployments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    deployed_strategy_id: Mapped[str] = mapped_column(ForeignKey("deployed_strategies.id"), index=True)
    strategy_id: Mapped[str] = mapped_column(String(36), index=True)
    strategy_version_id: Mapped[int] = mapped_column(Integer)
    broker_account_id: Mapped[str] = mapped_column(ForeignKey("broker_accounts.id"))
    mode: Mapped[str] = mapped_column(String(16))
    sebi_algo_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    risk_limits: Mapped[dict[str, Any]] = mapped_column(JSON)
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class DeploymentEvent(AppendOnly, LiveBase):
    __tablename__ = "deployment_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    deployment_id: Mapped[str] = mapped_column(ForeignKey("deployments.id"), index=True)
    event: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class TradeIntent(AppendOnly, LiveBase):
    __tablename__ = "trade_intents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    deployment_id: Mapped[str] = mapped_column(ForeignKey("deployments.id"), index=True)
    deployed_strategy_id: Mapped[str] = mapped_column(ForeignKey("deployed_strategies.id"))
    strategy_version_id: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(16))
    legs: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    # Why
    entry_rule: Mapped[str | None] = mapped_column(Text, nullable=True)
    entry_conditions: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    reason_text: Mapped[str] = mapped_column(Text)
    market_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    # Vision
    thesis_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    target_pnl: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    stop_loss_pnl: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    max_loss: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    risk_reward: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_checks: Mapped[dict[str, Any]] = mapped_column(JSON)
    # Decision
    decided_by: Mapped[str] = mapped_column(String(128))
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    approval: Mapped[str] = mapped_column(String(16))
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decided_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Order(AppendOnly, LiveBase):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    trade_intent_id: Mapped[str] = mapped_column(ForeignKey("trade_intents.id"), index=True)
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True)
    broker_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    exchange_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sebi_algo_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    exchange: Mapped[str] = mapped_column(String(16))
    tradingsymbol: Mapped[str] = mapped_column(String(64))
    expiry: Mapped[str | None] = mapped_column(String(10), nullable=True)
    strike: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    option_type: Mapped[str | None] = mapped_column(String(4), nullable=True)
    side: Mapped[str] = mapped_column(String(4))
    quantity: Mapped[int] = mapped_column(Integer)
    order_type: Mapped[str] = mapped_column(String(16))
    product: Mapped[str] = mapped_column(String(16))
    limit_price: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    trigger_price: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    placed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class OrderEvent(AppendOnly, LiveBase):
    __tablename__ = "order_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    status: Mapped[str] = mapped_column(String(32))
    filled_qty: Mapped[int] = mapped_column(Integer, default=0)
    pending_qty: Mapped[int] = mapped_column(Integer, default=0)
    avg_price: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw: Mapped[dict[str, Any]] = mapped_column(JSON)
    event_at: Mapped[datetime] = mapped_column(UTCDateTime())
    received_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Fill(AppendOnly, LiveBase):
    __tablename__ = "fills"
    __table_args__ = (UniqueConstraint("order_id", "broker_trade_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    broker_trade_id: Mapped[str] = mapped_column(String(64))
    quantity: Mapped[int] = mapped_column(Integer)
    price: Mapped[Decimal] = mapped_column(Money)
    est_cost_breakdown: Mapped[dict[str, Any]] = mapped_column(JSON)
    filled_at: Mapped[datetime] = mapped_column(UTCDateTime())


class LiveTrade(AppendOnly, LiveBase):
    __tablename__ = "live_trades"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    deployment_id: Mapped[str] = mapped_column(ForeignKey("deployments.id"), index=True)
    deployed_strategy_id: Mapped[str] = mapped_column(ForeignKey("deployed_strategies.id"))
    open_intent_id: Mapped[str] = mapped_column(ForeignKey("trade_intents.id"))
    close_intent_id: Mapped[str | None] = mapped_column(ForeignKey("trade_intents.id"), nullable=True)
    strategy_version_id: Mapped[int] = mapped_column(Integer)
    legs: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime())
    closed_at: Mapped[datetime] = mapped_column(UTCDateTime())
    holding_minutes: Mapped[int] = mapped_column(Integer)
    exit_rule: Mapped[str | None] = mapped_column(Text, nullable=True)
    exit_reason: Mapped[str] = mapped_column(String(64))
    gross: Mapped[Decimal] = mapped_column(Money)
    est_costs: Mapped[Decimal] = mapped_column(Money)
    actual_costs: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    net: Mapped[Decimal] = mapped_column(Money)
    slippage_vs_expected: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    pnl_vs_target: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    max_adverse_excursion: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    max_favorable_excursion: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    outcome_label: Mapped[str] = mapped_column(String(16))
    # True when only some lots were closed (a policy partial booking); the opening intent stays open.
    partial: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())


class BrokerCharge(AppendOnly, LiveBase):
    __tablename__ = "broker_charges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    broker_account_id: Mapped[str] = mapped_column(ForeignKey("broker_accounts.id"), index=True)
    trade_date: Mapped[str] = mapped_column(String(10))
    order_id: Mapped[str | None] = mapped_column(ForeignKey("orders.id"), nullable=True)
    source: Mapped[str] = mapped_column(String(32))
    breakdown: Mapped[dict[str, Any]] = mapped_column(JSON)
    total: Mapped[Decimal] = mapped_column(Money)
    raw: Mapped[dict[str, Any]] = mapped_column(JSON)


class PositionSnapshot(AppendOnly, LiveBase):
    __tablename__ = "position_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    broker_account_id: Mapped[str] = mapped_column(ForeignKey("broker_accounts.id"), index=True)
    # Set by the Trade Crew, which snapshots one deployment's open positions per tick. No foreign key:
    # SQLite cannot add one to an existing table without rebuilding it and its append-only triggers.
    deployment_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    taken_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    positions: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    mtm: Mapped[Decimal] = mapped_column(Money)


class CapitalEvent(AppendOnly, LiveBase):
    """Money added to or taken from a deployment's capital after approval."""

    __tablename__ = "capital_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    deployment_id: Mapped[str] = mapped_column(ForeignKey("deployments.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # deposit | withdrawal
    amount_inr: Mapped[Decimal] = mapped_column(Money)  # always positive
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class DrawdownEvent(AppendOnly, LiveBase):
    """A deployment moving to another level of its drawdown ladder (0 = normal)."""

    __tablename__ = "drawdown_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    deployment_id: Mapped[str] = mapped_column(ForeignKey("deployments.id"), index=True)
    level: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(24))  # normal | warn | reduce_size | stop_new_entries | pause
    at_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    drawdown_pct: Mapped[float] = mapped_column(Float)
    drawdown_inr: Mapped[Decimal] = mapped_column(Money)
    equity: Mapped[Decimal] = mapped_column(Money)
    peak: Mapped[Decimal] = mapped_column(Money)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class LiveTradeReview(AppendOnly, LiveBase):
    __tablename__ = "live_trade_reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    live_trade_id: Mapped[str] = mapped_column(ForeignKey("live_trades.id"), index=True)
    reviewer_type: Mapped[str] = mapped_column(String(16))
    reviewer: Mapped[str] = mapped_column(String(128))
    followed_plan: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    process_verdict: Mapped[str | None] = mapped_column(String(48), nullable=True)
    what_went_well: Mapped[str | None] = mapped_column(Text, nullable=True)
    what_went_wrong: Mapped[str | None] = mapped_column(Text, nullable=True)
    mistake_tags: Mapped[list[str]] = mapped_column(JSON)
    lesson: Mapped[str | None] = mapped_column(Text, nullable=True)
    improvement_suggestion: Mapped[str | None] = mapped_column(Text, nullable=True)
    suggested_rule_change: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    supersedes_review_id: Mapped[int | None] = mapped_column(ForeignKey("live_trade_reviews.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class LiveAuditEvent(AppendOnly, LiveBase):
    __tablename__ = "live_audit_events"
    __table_args__ = (
        Index("ix_live_audit_events_entity", "entity_type", "entity_id"),
        UniqueConstraint("prev_hash"),
        UniqueConstraint("hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime())
    actor: Mapped[str] = mapped_column(String(128))
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(JSON)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


class TradeOpinion(AppendOnly, LiveBase):
    """The LLM's opinion on an entry the rules already approved. It may only say take, reduce or skip."""

    __tablename__ = "trade_opinions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    deployment_id: Mapped[str] = mapped_column(ForeignKey("deployments.id"), index=True)
    trade_intent_id: Mapped[str | None] = mapped_column(ForeignKey("trade_intents.id"), nullable=True, index=True)
    mode: Mapped[str] = mapped_column(String(16))  # shadow | veto
    status: Mapped[str] = mapped_column(String(16))  # ok | error
    decision: Mapped[str | None] = mapped_column(String(8), nullable=True)  # take | reduce | skip
    lots: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rules_lots: Mapped[int] = mapped_column(Integer)
    final_lots: Mapped[int] = mapped_column(Integer)
    applied: Mapped[bool] = mapped_column(Boolean)  # True only when the opinion changed what was sent
    reasons: Mapped[list[str]] = mapped_column(JSON)
    risk_flags: Mapped[list[str]] = mapped_column(JSON)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    prompt_sha256: Mapped[str] = mapped_column(String(64))
    context: Mapped[dict[str, Any]] = mapped_column(JSON)
    lesson_review_ids: Mapped[list[int]] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class PhantomTrade(AppendOnly, LiveBase):
    """What the lots the LLM removed would have done, priced like the real position. Written once, at exit."""

    __tablename__ = "phantom_trades"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    deployment_id: Mapped[str] = mapped_column(ForeignKey("deployments.id"), index=True)
    trade_intent_id: Mapped[str] = mapped_column(ForeignKey("trade_intents.id"), unique=True)
    trade_opinion_id: Mapped[str] = mapped_column(ForeignKey("trade_opinions.id"))
    kind: Mapped[str] = mapped_column(String(16))  # vetoed | reduced
    lots: Mapped[int] = mapped_column(Integer)
    legs: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime())
    closed_at: Mapped[datetime] = mapped_column(UTCDateTime())
    exit_reason: Mapped[str] = mapped_column(String(64))
    gross: Mapped[Decimal] = mapped_column(Money)
    est_costs: Mapped[Decimal] = mapped_column(Money)
    net: Mapped[Decimal] = mapped_column(Money)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


LIVE_APPEND_ONLY_TABLES = [
    "deployed_strategies",
    "broker_accounts",
    "deployments",
    "deployment_events",
    "trade_intents",
    "orders",
    "order_events",
    "fills",
    "live_trades",
    "broker_charges",
    "position_snapshots",
    "live_trade_reviews",
    "live_audit_events",
    "trade_opinions",
    "phantom_trades",
    "capital_events",
    "drawdown_events",
]
