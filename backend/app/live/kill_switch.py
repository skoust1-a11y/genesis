"""Stopping a deployment: the stop event, then its working orders cancelled at the broker and,
with `flatten`, every open position closed at market-protected limit prices."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app import alerts
from app.agents.trade_crew import graph
from app.agents.trade_crew.runner import build_context
from app.brokers.base import QuoteSource
from app.brokers.registry import get_adapter
from app.config import Settings
from app.db.types import utcnow
from app.engine.costs import resolve_cost_profile
from app.live import approvals
from app.live.store import TERMINAL_STATUSES, LiveStore
from app.strategies.schema import parse_strategy_markdown


def context(live: Session, settings: Settings, deployment_id: str) -> graph.CrewContext:
    """A crew context for the kill switch: the account's adapter and, when it has quotes, the broker feed."""
    store = LiveStore(live)
    deployment = store.deployment(deployment_id)
    account = store.broker_account(deployment.broker_account_id)
    snapshot = store.deployed_strategy(deployment.deployed_strategy_id)
    profile = resolve_cost_profile(settings, parse_strategy_markdown(snapshot.markdown))
    adapter = get_adapter(store, account, settings, profile, utcnow)
    feed_kind = "broker" if isinstance(adapter, QuoteSource) else "replay"
    return build_context(live, deployment_id, settings, adapter=adapter, feed_kind=feed_kind, kill_switch=True)


def stop(live: Session, settings: Settings, deployment_id: str, reason: str | None, flatten: bool = False,
         ctx: graph.CrewContext | None = None) -> dict[str, Any]:  # fmt: skip
    """Record the stop first (so the crew sends nothing new), then cancel working orders, then flatten.
    A failure after the stop is reported in the result and alerted; the deployment stays stopped."""
    if not (flatten and LiveStore(live).deployment_status(deployment_id) in TERMINAL_STATUSES):
        approvals.transition(live, deployment_id, "stop", reason)  # a halted deployment can still be flattened
    out: dict[str, Any] = {"cancelled": [], "exits": [], "error": None}
    if not flatten and not LiveStore(live).working_orders(deployment_id):
        live.rollback()
        return out
    try:
        ctx = ctx or context(live, settings, deployment_id)
        # protective stops stay until each position is closed: if the flatten fails they still guard it
        out["cancelled"] = graph.cancel_working_orders(ctx, include_stops=False)
        live.commit()
        if flatten:
            out["exits"] = graph.flatten(ctx)
            out["cancelled"] += graph.cancel_working_orders(ctx, include_stops=True)
            live.commit()
    except Exception as exc:
        live.rollback()
        out["error"] = f"{type(exc).__name__}: {exc}"
        what = "flatten" if flatten else "cancelling working orders"
        alerts.notify(settings, "halted", deployment_id, f"stopped, but {what} failed: {out['error']}")
    return out
