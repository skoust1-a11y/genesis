from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import current_actor, record_event
from app.db.base import LiveBase
from app.db.hashing import normalize_json
from app.db.models import (
    BrokerAccount,
    BrokerCharge,
    CapitalEvent,
    DeployedStrategy,
    Deployment,
    DeploymentEvent,
    DrawdownEvent,
    Fill,
    LiveAuditEvent,
    LiveTrade,
    LiveTradeReview,
    Order,
    OrderEvent,
    PhantomTrade,
    PositionSnapshot,
    TradeIntent,
    TradeOpinion,
)
from app.db.serialize import row_to_dict

JSON_FIELDS = {
    "risk_limits", "legs", "entry_conditions", "market_snapshot", "risk_checks", "rules",
    "raw", "est_cost_breakdown", "breakdown", "positions", "mistake_tags", "suggested_rule_change",
    "reasons", "risk_flags", "context", "lesson_review_ids",
}  # fmt: skip
TERMINAL_STATUSES = ("stopped", "halted")
VETO_EVENTS = ("llm_veto_enabled", "llm_veto_disabled")  # settings, not lifecycle: ignored by deployment_status
REVIEW_FIELDS = (
    "followed_plan", "process_verdict", "what_went_well", "what_went_wrong", "mistake_tags",
    "lesson", "improvement_suggestion", "suggested_rule_change", "confidence", "supersedes_review_id",
)  # fmt: skip


class LiveStore:
    """Insert-only access to the live database. Every write is audited, with the full row, in its own chain."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def _insert(
        self, model: type[LiveBase], action: str, entity_type: str, fields: dict[str, Any], extra: dict | None = None
    ):
        clean = {k: normalize_json(v) if k in JSON_FIELDS else v for k, v in fields.items()}
        obj = model(**clean)
        self.session.add(obj)
        self.session.flush()
        details = row_to_dict(obj) | ({"extra": normalize_json(extra)} if extra else {})
        record_event(self.session, action, entity_type, obj.id, details, model=LiveAuditEvent)
        return obj

    # Writes

    def create_deployed_strategy(self, **fields) -> DeployedStrategy:
        return self._insert(DeployedStrategy, "deployed_strategy.created", "deployed_strategy", fields)

    def create_broker_account(self, **fields) -> BrokerAccount:
        return self._insert(BrokerAccount, "broker_account.created", "broker_account", fields)

    def create_deployment(self, **fields) -> Deployment:
        snapshot = self.session.get(DeployedStrategy, fields.get("deployed_strategy_id"))
        if snapshot is None:
            raise ValueError("deployed_strategy_id does not exist")
        for key in ("strategy_id", "strategy_version_id"):
            if fields.setdefault(key, getattr(snapshot, key)) != getattr(snapshot, key):
                raise ValueError(f"{key} does not match the deployed strategy")
        fields.setdefault("risk_limits", {})
        return self._insert(Deployment, "deployment.created", "deployment", fields)

    def add_deployment_event(
        self, deployment_id: str, event: str, reason: str | None = None, evidence: dict | None = None
    ) -> DeploymentEvent:
        """`evidence` is kept in the hash-chained audit event, next to the row."""
        fields = {"deployment_id": deployment_id, "event": event, "reason": reason, "actor": current_actor()}
        return self._insert(DeploymentEvent, f"deployment.{event}", "deployment_event", fields, evidence)

    def _from_deployment(self, fields: dict[str, Any]) -> dict[str, Any]:
        deployment = self.deployment(fields["deployment_id"])
        fields.setdefault("deployed_strategy_id", deployment.deployed_strategy_id)
        fields.setdefault("strategy_version_id", deployment.strategy_version_id)
        return fields

    def create_trade_intent(self, **fields) -> TradeIntent:
        fields.setdefault("entry_conditions", [])
        fields.setdefault("market_snapshot", {})
        fields.setdefault("risk_checks", {})
        fields.setdefault("approval", "auto")
        return self._insert(TradeIntent, "trade_intent.created", "trade_intent", self._from_deployment(fields))

    def place_order(self, **fields) -> Order:
        return self._insert(Order, "order.placed", "order", fields)

    def add_order_event(self, **fields) -> OrderEvent:
        fields.setdefault("raw", {})
        return self._insert(OrderEvent, "order.event", "order_event", fields)

    def record_fill(self, **fields) -> Fill:
        fields.setdefault("est_cost_breakdown", {})
        return self._insert(Fill, "fill.recorded", "fill", fields)

    def close_live_trade(self, **fields) -> LiveTrade:
        return self._insert(LiveTrade, "live_trade.closed", "live_trade", self._from_deployment(fields))

    def record_broker_charge(self, **fields) -> BrokerCharge:
        fields.setdefault("raw", {})
        return self._insert(BrokerCharge, "broker_charge.recorded", "broker_charge", fields)

    def snapshot_positions(self, **fields) -> PositionSnapshot:
        return self._insert(PositionSnapshot, "position.snapshot", "position_snapshot", fields)

    def add_capital_event(self, **fields) -> CapitalEvent:
        self.deployment(fields["deployment_id"])
        fields.setdefault("actor", current_actor())
        return self._insert(CapitalEvent, f"capital.{fields['kind']}", "capital_event", fields)

    def record_drawdown_event(self, **fields) -> DrawdownEvent:
        return self._insert(DrawdownEvent, "drawdown.level_changed", "drawdown_event", fields)

    def add_review(self, live_trade_id: str, data: dict[str, Any]) -> LiveTradeReview:
        self.live_trade(live_trade_id)
        fields = {k: data.get(k) for k in REVIEW_FIELDS}
        supersedes = fields.get("supersedes_review_id")
        if supersedes is not None and supersedes not in {r.id for r in self.reviews(live_trade_id)}:
            raise ValueError("supersedes_review_id must reference a review of the same trade")
        fields["mistake_tags"] = fields.get("mistake_tags") or []
        return self._insert(
            LiveTradeReview,
            "trade.reviewed",
            "live_trade_review",
            {"live_trade_id": live_trade_id, "reviewer_type": "human", "reviewer": current_actor(), **fields},
        )

    def add_agent_review(self, live_trade_id: str, data: dict[str, Any], reviewer: str) -> LiveTradeReview:
        """A review written by an agent (`reviewer` names it, e.g. `llm:gpt-4o-mini`), never superseding one."""
        self.live_trade(live_trade_id)
        fields = {k: data.get(k) for k in REVIEW_FIELDS if k != "supersedes_review_id"}
        fields["mistake_tags"] = fields.get("mistake_tags") or []
        return self._insert(
            LiveTradeReview,
            "trade.reviewed",
            "live_trade_review",
            {"live_trade_id": live_trade_id, "reviewer_type": "agent", "reviewer": reviewer, **fields},
        )

    def add_trade_opinion(self, **fields) -> TradeOpinion:
        for key in ("reasons", "risk_flags", "lesson_review_ids"):
            fields.setdefault(key, [])
        fields.setdefault("context", {})
        return self._insert(TradeOpinion, "trade_opinion.recorded", "trade_opinion", fields)

    def record_phantom_trade(self, **fields) -> PhantomTrade:
        return self._insert(PhantomTrade, "phantom_trade.closed", "phantom_trade", fields)

    # Reads

    def deployed_strategy(self, deployed_strategy_id: str) -> DeployedStrategy:
        snapshot = self.session.get(DeployedStrategy, deployed_strategy_id)
        if snapshot is None:
            raise FileNotFoundError(deployed_strategy_id)
        return snapshot

    def broker_account(self, broker_account_id: str) -> BrokerAccount:
        account = self.session.get(BrokerAccount, broker_account_id)
        if account is None:
            raise FileNotFoundError(broker_account_id)
        return account

    def broker_accounts(self) -> list[BrokerAccount]:
        return list(self.session.scalars(select(BrokerAccount).order_by(BrokerAccount.created_at)))

    def deployments(self) -> list[Deployment]:
        return list(self.session.scalars(select(Deployment).order_by(Deployment.created_at.desc())))

    def deployment(self, deployment_id: str) -> Deployment:
        deployment = self.session.get(Deployment, deployment_id)
        if deployment is None:
            raise FileNotFoundError(deployment_id)
        return deployment

    def deployment_events(self, deployment_id: str) -> list[DeploymentEvent]:
        return list(
            self.session.scalars(
                select(DeploymentEvent)
                .where(DeploymentEvent.deployment_id == deployment_id)
                .order_by(DeploymentEvent.id)
            )
        )

    def deployment_status(self, deployment_id: str) -> str:
        latest = self.session.scalar(
            select(DeploymentEvent.event)
            .where(DeploymentEvent.deployment_id == deployment_id, DeploymentEvent.event.not_in(VETO_EVENTS))
            .order_by(DeploymentEvent.id.desc())
            .limit(1)
        )
        return latest or "created"

    def veto_enabled(self, deployment_id: str) -> bool:
        latest = self.session.scalar(
            select(DeploymentEvent.event)
            .where(DeploymentEvent.deployment_id == deployment_id, DeploymentEvent.event.in_(VETO_EVENTS))
            .order_by(DeploymentEvent.id.desc())
            .limit(1)
        )
        return latest == "llm_veto_enabled"

    def active_deployments(self) -> list[Deployment]:
        return [d for d in self.deployments() if self.deployment_status(d.id) not in TERMINAL_STATUSES]

    def intents(self, deployment_id: str) -> list[TradeIntent]:
        self.deployment(deployment_id)
        return list(
            self.session.scalars(
                select(TradeIntent)
                .where(TradeIntent.deployment_id == deployment_id)
                .order_by(TradeIntent.decided_at)
            )
        )

    def orders_for_intent(self, trade_intent_id: str) -> list[Order]:
        return list(
            self.session.scalars(
                select(Order)
                .where(Order.trade_intent_id == trade_intent_id)
                .order_by(Order.placed_at, Order.client_order_id)
            )
        )

    def fills_for_order(self, order_id: str) -> list[Fill]:
        return list(self.session.scalars(select(Fill).where(Fill.order_id == order_id).order_by(Fill.id)))

    def order_by_broker_id(self, broker_order_id: str) -> Order | None:
        return self.session.scalar(select(Order).where(Order.broker_order_id == broker_order_id))

    def order_events(self, order_id: str) -> list[OrderEvent]:
        return list(
            self.session.scalars(select(OrderEvent).where(OrderEvent.order_id == order_id).order_by(OrderEvent.id))
        )

    def order_status(self, order_id: str) -> str | None:
        events = self.order_events(order_id)
        return events[-1].status if events else None

    def order_detail(self, order_id: str) -> dict[str, Any]:
        order = self.session.get(Order, order_id)
        if order is None:
            raise FileNotFoundError(order_id)
        events = self.order_events(order_id)
        intent = self.session.get(TradeIntent, order.trade_intent_id)
        return {
            "order": row_to_dict(order),
            "status": events[-1].status if events else None,
            "events": [row_to_dict(e) for e in events],
            "fills": [row_to_dict(f) for f in self.fills_for_order(order_id)],
            "intent": row_to_dict(intent) if intent else None,
        }

    def live_trade(self, live_trade_id: str) -> LiveTrade:
        trade = self.session.get(LiveTrade, live_trade_id)
        if trade is None:
            raise FileNotFoundError(f"live trade {live_trade_id}")
        return trade

    def live_trades(self, deployment_id: str | None = None, limit: int = 100, offset: int = 0) -> list[LiveTrade]:
        stmt = select(LiveTrade)
        if deployment_id:
            stmt = stmt.where(LiveTrade.deployment_id == deployment_id)
        stmt = stmt.order_by(LiveTrade.closed_at.desc()).limit(limit).offset(offset)
        return list(self.session.scalars(stmt))

    def trades_closed_on(self, day: date, deployment_id: str | None = None) -> list[LiveTrade]:
        start = datetime.combine(day, time.min, tzinfo=timezone.utc)
        stmt = select(LiveTrade).where(LiveTrade.closed_at >= start, LiveTrade.closed_at < start + timedelta(days=1))
        if deployment_id:
            stmt = stmt.where(LiveTrade.deployment_id == deployment_id)
        return list(self.session.scalars(stmt))

    def closed_trades(self, deployment_ids: list[str]) -> list[LiveTrade]:
        stmt = select(LiveTrade).where(LiveTrade.deployment_id.in_(deployment_ids))
        return list(self.session.scalars(stmt.order_by(LiveTrade.closed_at, LiveTrade.id)))

    def position_snapshots(self, deployment_ids: list[str]) -> list[PositionSnapshot]:
        stmt = select(PositionSnapshot).where(PositionSnapshot.deployment_id.in_(deployment_ids))
        return list(self.session.scalars(stmt.order_by(PositionSnapshot.taken_at, PositionSnapshot.id)))

    def latest_position_snapshot(self, deployment_id: str) -> PositionSnapshot | None:
        return self.session.scalar(
            select(PositionSnapshot)
            .where(PositionSnapshot.deployment_id == deployment_id)
            .order_by(PositionSnapshot.taken_at.desc(), PositionSnapshot.id.desc())
            .limit(1)
        )

    def capital_events(self, deployment_ids: list[str]) -> list[CapitalEvent]:
        stmt = select(CapitalEvent).where(CapitalEvent.deployment_id.in_(deployment_ids))
        return list(self.session.scalars(stmt.order_by(CapitalEvent.created_at, CapitalEvent.id)))

    def drawdown_events(self, deployment_id: str) -> list[DrawdownEvent]:
        stmt = select(DrawdownEvent).where(DrawdownEvent.deployment_id == deployment_id)
        return list(self.session.scalars(stmt.order_by(DrawdownEvent.id)))

    def drawdown_level(self, deployment_id: str) -> int:
        latest = self.session.scalar(
            select(DrawdownEvent.level)
            .where(DrawdownEvent.deployment_id == deployment_id)
            .order_by(DrawdownEvent.id.desc())
            .limit(1)
        )
        return latest or 0

    def closed_intent_ids(self, deployment_id: str | None = None) -> set[str]:
        stmt = select(LiveTrade.open_intent_id).where(LiveTrade.partial.is_(False))
        if deployment_id:
            stmt = stmt.where(LiveTrade.deployment_id == deployment_id)
        return set(self.session.scalars(stmt))

    def partial_trades(self, open_intent_id: str) -> list[LiveTrade]:
        stmt = select(LiveTrade).where(LiveTrade.open_intent_id == open_intent_id, LiveTrade.partial.is_(True))
        return list(self.session.scalars(stmt.order_by(LiveTrade.closed_at, LiveTrade.id)))

    def open_intents(self, deployment_id: str) -> list[TradeIntent]:
        """Opening intents that were sent to the broker and have no closed live trade yet."""
        closed = self.closed_intent_ids(deployment_id)
        return [
            i for i in self.intents(deployment_id)
            if i.action == "open" and i.approval == "auto" and i.id not in closed
        ]  # fmt: skip

    def reviews(self, live_trade_id: str) -> list[LiveTradeReview]:
        self.live_trade(live_trade_id)
        return list(
            self.session.scalars(
                select(LiveTradeReview)
                .where(LiveTradeReview.live_trade_id == live_trade_id)
                .order_by(LiveTradeReview.id)
            )
        )

    def latest_reviews(self) -> dict[str, LiveTradeReview]:
        rows = self.session.scalars(select(LiveTradeReview).order_by(LiveTradeReview.id))
        return {r.live_trade_id: r for r in rows}

    def recent_lessons(self, deployment_id: str, limit: int) -> list[LiveTradeReview]:
        """Newest reviews with a lesson on this deployment's closed trades."""
        stmt = (
            select(LiveTradeReview)
            .join(LiveTrade, LiveTrade.id == LiveTradeReview.live_trade_id)
            .where(LiveTrade.deployment_id == deployment_id, LiveTradeReview.lesson.is_not(None))
            .order_by(LiveTradeReview.id.desc())
            .limit(limit)
        )
        return list(self.session.scalars(stmt))

    def opinions(self, deployment_id: str | None = None) -> list[TradeOpinion]:
        stmt = select(TradeOpinion)
        if deployment_id:
            stmt = stmt.where(TradeOpinion.deployment_id == deployment_id)
        return list(self.session.scalars(stmt.order_by(TradeOpinion.created_at, TradeOpinion.id)))

    def opinion_for_intent(self, trade_intent_id: str) -> TradeOpinion | None:
        return self.session.scalar(select(TradeOpinion).where(TradeOpinion.trade_intent_id == trade_intent_id))

    def phantom_trades(self, deployment_id: str | None = None) -> list[PhantomTrade]:
        stmt = select(PhantomTrade)
        if deployment_id:
            stmt = stmt.where(PhantomTrade.deployment_id == deployment_id)
        return list(self.session.scalars(stmt.order_by(PhantomTrade.closed_at)))

    def open_phantoms(self, deployment_id: str) -> list[tuple[TradeOpinion, TradeIntent]]:
        """Veto opinions that removed lots whose phantom position has not hit an exit yet."""
        tracked = {p.trade_intent_id for p in self.phantom_trades(deployment_id)}
        return [
            (o, self.session.get(TradeIntent, o.trade_intent_id))
            for o in self.opinions(deployment_id)
            if o.mode == "veto" and o.final_lots < o.rules_lots
            and o.trade_intent_id and o.trade_intent_id not in tracked
        ]  # fmt: skip
