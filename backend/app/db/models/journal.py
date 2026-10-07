from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, CheckConstraint, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import AppendOnly, Base
from app.db.types import UTCDateTime, utcnow


class TradeReview(AppendOnly, Base):
    """Reviews of simulated trades. Live-trade reviews live in the live database."""

    __tablename__ = "trade_reviews"
    __table_args__ = (
        CheckConstraint("trade_kind = 'sim' AND sim_trade_id IS NOT NULL", name="sim_trade_only"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_kind: Mapped[str] = mapped_column(String(8))
    sim_trade_id: Mapped[int | None] = mapped_column(ForeignKey("sim_trades.id"), nullable=True, index=True)
    reviewer_type: Mapped[str] = mapped_column(String(16))
    reviewer: Mapped[str] = mapped_column(String(128))
    rules_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    followed_plan: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    process_verdict: Mapped[str | None] = mapped_column(String(48), nullable=True)
    what_went_well: Mapped[str | None] = mapped_column(Text, nullable=True)
    what_went_wrong: Mapped[str | None] = mapped_column(Text, nullable=True)
    mistake_tags: Mapped[list[str]] = mapped_column(JSON)
    lesson: Mapped[str | None] = mapped_column(Text, nullable=True)
    improvement_suggestion: Mapped[str | None] = mapped_column(Text, nullable=True)
    suggested_rule_change: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    supersedes_review_id: Mapped[int | None] = mapped_column(ForeignKey("trade_reviews.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class StrategyImprovement(AppendOnly, Base):
    __tablename__ = "strategy_improvements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_id: Mapped[str] = mapped_column(ForeignKey("strategies.id"), index=True)
    from_version_id: Mapped[int] = mapped_column(ForeignKey("strategy_versions.id"))
    origin: Mapped[str] = mapped_column(String(16))
    report_id: Mapped[str | None] = mapped_column(ForeignKey("reports.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(200))
    rationale: Mapped[str] = mapped_column(Text)
    proposed_change: Mapped[dict[str, Any]] = mapped_column(JSON)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON)
    expected_impact: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposed_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class ImprovementDecision(AppendOnly, Base):
    __tablename__ = "improvement_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    improvement_id: Mapped[int] = mapped_column(ForeignKey("strategy_improvements.id"), index=True)
    decision: Mapped[str] = mapped_column(String(16))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    resulting_version_id: Mapped[int | None] = mapped_column(ForeignKey("strategy_versions.id"), nullable=True)
    decided_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
