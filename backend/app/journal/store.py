from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.audit import current_actor, record_event
from app.db.hashing import normalize_json
from app.db.models import (
    DeployedStrategy,
    Deployment,
    ImprovementDecision,
    LiveTrade,
    LiveTradeReview,
    SimTrade,
    StrategyImprovement,
    StrategyVersion,
    TradeIntent,
    TradeReview,
)
from app.db.serialize import row_to_dict
from app.db.session import live_schema_ready
from app.live.store import LiveStore
from app.strategies.store import StrategyStore

TRADE_KINDS = ("sim", "live")
DECISIONS = ("accepted", "rejected", "applied")
TERMINAL_STATUSES = ("rejected", "applied")
HUMAN_REVIEW_FIELDS = (
    "followed_plan", "process_verdict", "what_went_well", "what_went_wrong", "mistake_tags",
    "lesson", "improvement_suggestion", "suggested_rule_change", "confidence", "supersedes_review_id",
)  # fmt: skip


class JournalStore:
    """Sim trades come from the research database's trade_journal view; live trades from the live database."""

    def __init__(self, session: Session, live_session: Session | None = None) -> None:
        self.session = session
        self.live_session = live_session

    def _live_ready(self) -> bool:
        return self.live_session is not None and live_schema_ready(self.live_session)

    def _live_store(self) -> LiveStore:
        if not self._live_ready():
            raise ValueError("live database is not migrated: run `uv run alembic -n live upgrade head`")
        return LiveStore(self.live_session)

    # Journal

    def journal(
        self,
        kind: str | None = None,
        strategy_id: str | None = None,
        outcome: str | None = None,
        tag: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if kind not in (None, *TRADE_KINDS):
            return []
        filters = (strategy_id, outcome, tag, date_from, date_to)
        rows: list[dict[str, Any]] = []
        if kind in (None, "sim"):
            rows += self._sim_rows(*filters, limit=limit + offset)
        if kind in (None, "live") and self._live_ready():
            rows += self._live_rows(*filters)
        rows.sort(key=lambda r: (r["kind"], r["trade_id"]))
        rows.sort(key=lambda r: r["trade_date"] or "", reverse=True)
        return rows[offset : offset + limit]

    def _live_rows(
        self,
        strategy_id: str | None,
        outcome: str | None,
        tag: str | None,
        date_from: str | None,
        date_to: str | None,
    ) -> list[dict[str, Any]]:
        reviews: dict[str, LiveTradeReview] = LiveStore(self.live_session).latest_reviews()
        stmt = (
            select(LiveTrade, Deployment, TradeIntent, DeployedStrategy)
            .join(Deployment, Deployment.id == LiveTrade.deployment_id)
            .join(TradeIntent, TradeIntent.id == LiveTrade.open_intent_id)
            .join(DeployedStrategy, DeployedStrategy.id == LiveTrade.deployed_strategy_id)
        )
        rows = []
        for trade, deployment, intent, snapshot in self.live_session.execute(stmt).all():
            review = reviews.get(trade.id)
            row = {
                "kind": "live",
                "trade_id": trade.id,
                "strategy_id": deployment.strategy_id,
                "strategy_version_id": trade.strategy_version_id,
                "strategy_version": snapshot.version,
                "trade_date": trade.closed_at.strftime("%Y-%m-%d"),
                "mode": deployment.mode,
                "entry_rule": intent.entry_rule,
                "reason_text": intent.reason_text,
                "target_pnl": intent.target_pnl,
                "net": trade.net,
                "pnl_vs_target": trade.pnl_vs_target,
                "outcome_label": trade.outcome_label,
                "source_id": trade.deployment_id,
                "latest_verdict": review.process_verdict if review else None,
                "mistake_tags": review.mistake_tags if review else None,
            }
            if strategy_id and row["strategy_id"] != strategy_id:
                continue
            if outcome and row["outcome_label"] != outcome:
                continue
            if date_from and row["trade_date"] < date_from:
                continue
            if date_to and row["trade_date"] > date_to:
                continue
            if tag and tag not in (row["mistake_tags"] or []):
                continue
            rows.append(row)
        return rows

    def _sim_rows(
        self,
        strategy_id: str | None,
        outcome: str | None,
        tag: str | None,
        date_from: str | None,
        date_to: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: dict[str, Any] = {"limit": limit}
        if strategy_id:
            clauses.append("strategy_id = :strategy_id")
            params["strategy_id"] = strategy_id
        if outcome:
            clauses.append("outcome_label = :outcome")
            params["outcome"] = outcome
        if date_from:
            clauses.append("trade_date >= :date_from")
            params["date_from"] = date_from
        if date_to:
            clauses.append("trade_date <= :date_to")
            params["date_to"] = date_to
        if tag:
            if self.session.get_bind().dialect.name == "postgresql":
                clauses.append("jsonb_exists(CAST(mistake_tags AS jsonb), :tag)")
            else:
                clauses.append(
                    "EXISTS (SELECT 1 FROM json_each(trade_journal.mistake_tags) WHERE json_each.value = :tag)"
                )
            params["tag"] = tag
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM trade_journal {where} ORDER BY trade_date DESC, kind, trade_id LIMIT :limit"
        rows = []
        for row in self.session.execute(text(sql), params).mappings():
            item = dict(row)
            if isinstance(item.get("mistake_tags"), str):
                item["mistake_tags"] = json.loads(item["mistake_tags"])
            rows.append(item)
        return rows

    # Reviews

    def _sim_trade(self, trade_id: str | int) -> SimTrade:
        try:
            trade = self.session.get(SimTrade, int(trade_id))
        except (TypeError, ValueError):
            trade = None
        if trade is None:
            raise FileNotFoundError(f"sim trade {trade_id}")
        return trade

    def _check_kind(self, kind: str) -> None:
        if kind not in TRADE_KINDS:
            raise ValueError("kind must be 'sim' or 'live'")

    def reviews(self, kind: str, trade_id: str | int) -> list[TradeReview | LiveTradeReview]:
        self._check_kind(kind)
        if kind == "live":
            return self._live_store().reviews(str(trade_id))
        self._sim_trade(trade_id)
        return list(
            self.session.scalars(
                select(TradeReview).where(TradeReview.sim_trade_id == int(trade_id)).order_by(TradeReview.id)
            )
        )

    def add_review(self, kind: str, trade_id: str | int, data: dict[str, Any]) -> TradeReview | LiveTradeReview:
        self._check_kind(kind)
        if kind == "live":
            return self._live_store().add_review(str(trade_id), data)
        trade = self._sim_trade(trade_id)
        fields = {k: data.get(k) for k in HUMAN_REVIEW_FIELDS}
        supersedes = fields.get("supersedes_review_id")
        if supersedes is not None:
            previous = self.session.get(TradeReview, supersedes)
            if previous is None or previous not in self.reviews(kind, trade_id):
                raise ValueError("supersedes_review_id must reference a review of the same trade")
        fields["mistake_tags"] = normalize_json(fields.get("mistake_tags") or [])
        fields["suggested_rule_change"] = normalize_json(fields.get("suggested_rule_change"))
        review = TradeReview(
            trade_kind=kind,
            sim_trade_id=trade.id,
            reviewer_type="human",
            reviewer=current_actor(),
            rules_version=None,
            **fields,
        )
        self.session.add(review)
        self.session.flush()
        record_event(
            self.session,
            "trade.reviewed",
            "trade_review",
            review.id,
            row_to_dict(review, exclude=("created_at",)),
        )
        return review

    # Improvements

    def status(self, improvement_id: int) -> str:
        latest = self.session.scalar(
            select(ImprovementDecision.decision)
            .where(ImprovementDecision.improvement_id == improvement_id)
            .order_by(ImprovementDecision.id.desc())
            .limit(1)
        )
        return latest or "proposed"

    def get_improvement(self, improvement_id: int) -> StrategyImprovement:
        improvement = self.session.get(StrategyImprovement, improvement_id)
        if improvement is None:
            raise FileNotFoundError(f"improvement {improvement_id}")
        return improvement

    def improvements(self, strategy_id: str) -> list[dict[str, Any]]:
        StrategyStore(self.session).get_strategy(strategy_id)
        rows = self.session.scalars(
            select(StrategyImprovement)
            .where(StrategyImprovement.strategy_id == strategy_id)
            .order_by(StrategyImprovement.id.desc())
        )
        out = []
        for improvement in rows:
            item = row_to_dict(improvement)
            item["status"] = self.status(improvement.id)
            item["decisions"] = [row_to_dict(d) for d in self.decisions(improvement.id)]
            out.append(item)
        return out

    def decisions(self, improvement_id: int) -> list[ImprovementDecision]:
        return list(
            self.session.scalars(
                select(ImprovementDecision)
                .where(ImprovementDecision.improvement_id == improvement_id)
                .order_by(ImprovementDecision.id)
            )
        )

    def propose(
        self,
        *,
        strategy_id: str,
        title: str,
        rationale: str,
        proposed_change: dict,
        evidence: dict | None = None,
        expected_impact: str | None = None,
        origin: str = "human",
        report_id: str | None = None,
    ) -> StrategyImprovement:
        version = StrategyStore(self.session).latest_version(strategy_id)
        improvement = StrategyImprovement(
            strategy_id=strategy_id,
            from_version_id=version.id,
            origin=origin,
            report_id=report_id,
            title=title[:200],
            rationale=rationale,
            proposed_change=normalize_json(proposed_change),
            evidence=normalize_json(evidence or {}),
            expected_impact=expected_impact,
            proposed_by=current_actor(),
        )
        self.session.add(improvement)
        self.session.flush()
        record_event(
            self.session,
            "improvement.proposed",
            "strategy_improvement",
            improvement.id,
            row_to_dict(improvement, exclude=("created_at",)),
        )
        return improvement

    def decide(
        self, improvement_id: int, decision: str, note: str | None = None, markdown: str | None = None
    ) -> ImprovementDecision:
        if decision not in DECISIONS:
            raise ValueError(f"decision must be one of {', '.join(DECISIONS)}")
        improvement = self.get_improvement(improvement_id)
        current = self.status(improvement_id)
        if current in TERMINAL_STATUSES:
            raise ValueError(f"improvement is already {current}")

        resulting_version: StrategyVersion | None = None
        if decision == "applied":
            if not markdown:
                raise ValueError("markdown is required to apply an improvement")
            saved = StrategyStore(self.session).save(markdown, strategy_id=improvement.strategy_id)
            if not saved.created_version:
                raise ValueError("markdown is unchanged from the current version")
            resulting_version = saved.version

        row = ImprovementDecision(
            improvement_id=improvement_id,
            decision=decision,
            note=note,
            resulting_version_id=resulting_version.id if resulting_version else None,
            decided_by=current_actor(),
        )
        self.session.add(row)
        self.session.flush()
        record_event(
            self.session,
            "improvement.decided",
            "strategy_improvement",
            improvement_id,
            {
                "decision_id": row.id,
                "decision": decision,
                "previous_status": current,
                "note": note,
                "resulting_version_id": row.resulting_version_id,
            },
        )
        return row
