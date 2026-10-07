"""Run one deployment's Trade Crew in its own process, separate from the API."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app import alerts
from app.agents.trade_crew.graph import CrewContext, halt, reconcile_positions, run_tick
from app.audit import audit_context
from app.brokers import registry
from app.brokers.base import BrokerAdapter, LiveBrokerAdapter
from app.config import Settings, get_settings
from app.db.session import get_live_sessionmaker, live_schema_ready
from app.db.types import utcnow
from app.engine.costs import resolve_cost_profile
from app.live import gates
from app.live.store import TERMINAL_STATUSES, LiveStore
from app.market.calendar import event_lookup
from app.market.feed import BrokerFeed, MarketFeed
from app.strategies.rules import StrategyRules
from app.strategies.schema import parse_strategy_markdown


def build_context(
    live: Session,
    deployment_id: str,
    settings: Settings,
    *,
    feed: MarketFeed | None = None,
    adapter: BrokerAdapter | None = None,
    feed_kind: str | None = None,
    clock: Callable[[], datetime] = utcnow,
    explain: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    kill_switch: bool = False,
) -> CrewContext:
    """The adapter comes from the account's broker in the registry and the feed from `feed_kind`
    (replay by default, broker for live), unless they are passed in. Live deployments must pass the
    live gates; adapters that keep positions are reconciled with the live database before the first tick.
    `kill_switch` skips both, so stopping and flattening still work when the gates no longer hold."""
    store = LiveStore(live)
    deployment = store.deployment(deployment_id)
    snapshot = store.deployed_strategy(deployment.deployed_strategy_id)
    account = store.broker_account(deployment.broker_account_id)
    rules = StrategyRules.model_validate(snapshot.rules)
    if feed_kind is None:
        feed_kind = "broker" if isinstance(feed, BrokerFeed) or (feed is None and deployment.mode == "live") else "replay"
    if feed_kind not in registry.FEEDS:
        raise ValueError(f"feed must be one of {', '.join(registry.FEEDS)}")
    if account.mode != deployment.mode:
        raise ValueError(f"broker account {account.id} is {account.mode}, deployment is {deployment.mode}")
    if deployment.mode == "live" and not kill_switch:
        refusals = gates.live_blockers(store, account, rules.instrument.structure, settings)
        if not deployment.sebi_algo_id:
            refusals.append("the deployment has no sebi_algo_id")
        if feed_kind != "broker":
            refusals.append("live trading needs --feed broker; replayed bars are refused")
        if refusals:
            raise ValueError("live trading refused: " + "; ".join(refusals))
    profile = resolve_cost_profile(settings, parse_strategy_markdown(snapshot.markdown))
    if adapter is None:
        adapter = registry.get_adapter(store, account, settings, profile, clock)
    if adapter.mode != deployment.mode:
        raise ValueError(f"adapter mode {adapter.mode!r} does not match deployment mode {deployment.mode!r}")
    if adapter.mode == "live" and not isinstance(adapter, LiveBrokerAdapter):
        raise ValueError(f"adapter {adapter.name!r} lacks get_order/margin/reconciliation, which live trading needs")
    if feed is None:
        feed = registry.get_feed(feed_kind, adapter, rules.instrument.underlying, settings, clock, event_lookup(settings))
    live.rollback()
    ctx = CrewContext(
        live, deployment_id, adapter, feed, settings, clock=clock, explain=explain,
        feed_kind=feed_kind, enforce_market_hours=feed_kind == "broker", sleep=sleep,
    )  # fmt: skip
    if kill_switch:
        return ctx
    with audit_context(f"trade_crew/{deployment_id}"):
        mismatch = reconcile_positions(ctx, deployment, profile)
        if mismatch:
            halt(ctx, f"reconciliation mismatch at startup: {mismatch}")
            alerts.notify(settings, "reconcile_mismatch", deployment_id, mismatch)
            raise ValueError(f"reconciliation mismatch at startup: {mismatch}; deployment halted")
    live.rollback()
    return ctx


def run(
    deployment_id: str,
    interval: float = 60,
    max_ticks: int | None = None,
    explain: bool | None = None,
    sleep: Callable[[float], None] = time.sleep,
    emit: Callable[[str], None] = print,
    feed: str | None = None,
) -> list[dict[str, Any]]:
    settings = get_settings()
    live = get_live_sessionmaker()()
    results: list[dict[str, Any]] = []
    try:
        if not live_schema_ready(live):
            raise ValueError("live database is not migrated: run `uv run alembic -n live upgrade head`")
        ctx = build_context(
            live, deployment_id, settings, feed_kind=feed,
            explain=settings.trade_crew_explain if explain is None else explain,
        )  # fmt: skip
        while max_ticks is None or len(results) < max_ticks:
            try:
                result = run_tick(ctx)
            except Exception as exc:
                live.rollback()
                with audit_context(f"trade_crew/{deployment_id}"):
                    halt(ctx, f"crew error: {type(exc).__name__}: {exc}")
                raise
            results.append(result)
            emit(json.dumps(result, default=str))
            status = LiveStore(live).deployment_status(deployment_id)
            live.rollback()
            if status in TERMINAL_STATUSES:
                emit(f"deployment is {status}; exiting")
                break
            if getattr(ctx.feed, "exhausted", False):
                emit("replay feed exhausted; exiting")
                break
            if max_ticks is None or len(results) < max_ticks:
                sleep(interval)
    finally:
        live.close()
    return results
