from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import AppendOnly, Base, NoDelete
from app.db.types import UTCDateTime, utcnow


def new_id() -> str:
    return str(uuid.uuid4())


class Strategy(NoDelete, Base):
    __tablename__ = "strategies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    current_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class IngestRun(AppendOnly, Base):
    __tablename__ = "ingest_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_type: Mapped[str] = mapped_column(String(32))
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    hints: Mapped[str | None] = mapped_column(Text, nullable=True)
    extractor: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    raw_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str] = mapped_column(String(128))
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class StrategyVersion(AppendOnly, Base):
    __tablename__ = "strategy_versions"
    __table_args__ = (UniqueConstraint("strategy_id", "version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_id: Mapped[str] = mapped_column(ForeignKey("strategies.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    markdown: Mapped[str] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(String(64))
    frontmatter: Mapped[dict[str, Any]] = mapped_column(JSON)
    entry_rules: Mapped[list[str]] = mapped_column(JSON)
    exit_rules: Mapped[list[str]] = mapped_column(JSON)
    gaps: Mapped[list[str]] = mapped_column(JSON)
    ingest_run_id: Mapped[str | None] = mapped_column(ForeignKey("ingest_runs.id"), nullable=True)
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Report(AppendOnly, Base):
    __tablename__ = "reports"
    __table_args__ = (Index("ix_reports_strategy_started", "strategy_id", "started_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    strategy_id: Mapped[str] = mapped_column(ForeignKey("strategies.id"))
    strategy_version_id: Mapped[int] = mapped_column(ForeignKey("strategy_versions.id"))
    status: Mapped[str] = mapped_column(String(16))
    verdict_label: Mapped[str | None] = mapped_column(String(32), nullable=True)
    scenarios_tested: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scenarios_passing: Mapped[int | None] = mapped_column(Integer, nullable=True)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSON)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    payload_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str] = mapped_column(String(128))
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class AuditEvent(AppendOnly, Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_entity", "entity_type", "entity_id"),
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
