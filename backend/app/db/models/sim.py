from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import AppendOnly, Base
from app.db.types import Money


class SimulationRun(AppendOnly, Base):
    __tablename__ = "simulation_runs"
    __table_args__ = (UniqueConstraint("report_id", "scenario_index", "mode"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_id: Mapped[str] = mapped_column(ForeignKey("reports.id"), index=True)
    scenario_index: Mapped[int] = mapped_column(Integer)
    scenario: Mapped[dict[str, Any]] = mapped_column(JSON)
    mode: Mapped[str] = mapped_column(String(16))
    window_start: Mapped[str | None] = mapped_column(String(10), nullable=True)
    window_end: Mapped[str | None] = mapped_column(String(10), nullable=True)
    bars_in_window: Mapped[int] = mapped_column(Integer)
    trade_count: Mapped[int] = mapped_column(Integer)
    gross_pnl: Mapped[Decimal] = mapped_column(Money)
    total_costs: Mapped[Decimal] = mapped_column(Money)
    net_pnl: Mapped[Decimal] = mapped_column(Money)
    win_rate: Mapped[float] = mapped_column(Float)
    max_drawdown: Mapped[Decimal] = mapped_column(Money)
    pass_net: Mapped[bool] = mapped_column(Boolean)
    trades_sha256: Mapped[str] = mapped_column(String(64))
    # Engine v3; NULL on runs written before migration 0005
    data_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    equity_curve: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    max_drawdown_mtm: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    max_drawdown_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    by_year: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    skipped: Mapped[dict[str, int] | None] = mapped_column(JSON, nullable=True)


class SimTrade(AppendOnly, Base):
    __tablename__ = "sim_trades"
    __table_args__ = (
        UniqueConstraint("simulation_run_id", "seq"),
        Index("ix_sim_trades_report_date", "report_id", "trade_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    simulation_run_id: Mapped[int] = mapped_column(ForeignKey("simulation_runs.id"))
    report_id: Mapped[str] = mapped_column(ForeignKey("reports.id"))
    strategy_version_id: Mapped[int] = mapped_column(ForeignKey("strategy_versions.id"))
    seq: Mapped[int] = mapped_column(Integer)
    trade_date: Mapped[str] = mapped_column(String(10))
    # Why
    entry_rule: Mapped[str] = mapped_column(Text)
    entry_conditions: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    reason_text: Mapped[str] = mapped_column(Text)
    edge: Mapped[float] = mapped_column(Float)
    market_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    # How
    lots: Mapped[int] = mapped_column(Integer)
    credit: Mapped[Decimal] = mapped_column(Money)
    max_loss: Mapped[Decimal] = mapped_column(Money)
    notional: Mapped[Decimal] = mapped_column(Money)
    orders: Mapped[int] = mapped_column(Integer)
    cost_breakdown: Mapped[dict[str, str]] = mapped_column(JSON)
    exit_rule: Mapped[str] = mapped_column(Text)
    exit_reason: Mapped[str] = mapped_column(String(64))
    holding_bars: Mapped[int] = mapped_column(Integer)
    # Vision
    target_pnl: Mapped[Decimal] = mapped_column(Money)
    stop_loss_pnl: Mapped[Decimal] = mapped_column(Money)
    risk_reward: Mapped[float] = mapped_column(Float)
    # Outcome
    gross: Mapped[Decimal] = mapped_column(Money)
    costs: Mapped[Decimal] = mapped_column(Money)
    net: Mapped[Decimal] = mapped_column(Money)
    pnl_vs_target: Mapped[Decimal] = mapped_column(Money)
    cost_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    outcome_label: Mapped[str] = mapped_column(String(16))
    equity_after: Mapped[Decimal] = mapped_column(Money)
    drawdown_after: Mapped[Decimal] = mapped_column(Money)
    # Engine v3; NULL on trades written before migration 0005
    legs: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    expiry: Mapped[str | None] = mapped_column(String(10), nullable=True)
    exit_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
