"""Run one deployment's Trade Crew in its own process, separate from the API."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.agents.trade_crew.graph import CrewContext, halt, run_tick
from app.audit import audit_context
from app.brokers.base import BrokerAdapter
from app.brokers.paper import PaperBroker
from app.config import Settings, get_settings
from app.db.session import get_live_sessionmaker, live_schema_ready
from app.db.types import utcnow
from app.engine.costs import resolve_cost_profile
from app.live.store import TERMINAL_STATUSES, LiveStore
from app.market.calendar import event_lookup
from app.market.feed import MarketFeed, ReplayFeed
from app.strategies.rules import StrategyRules
from app.strategies.schema import parse_strategy_markdown


def build_context(
    live: Session,
    deployment_id: str,
    settings: Settings,
    *,
    feed: MarketFeed | None = None,
    adapter: BrokerAdapter | None = None,
    clock: Callable[[], datetime] = utcnow,
    explain: bool = False,
) -> CrewContext:
    """Paper deployments get a PaperBroker and a ReplayFeed unless others are passed in."""
    store = LiveStore(live)
    deployment = store.deployment(deployment_id)
    snapshot = store.deployed_strategy(deployment.deployed_strategy_id)
    account = store.broker_account(deployment.broker_account_id)
    if deployment.mode != "paper":
        raise ValueError("live deployments need a live broker adapter, which is not built yet")
    if account.broker != "paper" and adapter is None:
        raise ValueError(f"no adapter for broker {account.broker!r}; only paper accounts can run")
    if adapter is not None and adapter.mode != deployment.mode:
        raise ValueError(f"adapter mode {adapter.mode!r} does not match deployment mode {deployment.mode!r}")
    underlying = StrategyRules.model_validate(snapshot.rules).instrument.underlying
    if adapter is None:
        profile = resolve_cost_profile(settings, parse_strategy_markdown(snapshot.markdown))
        adapter = PaperBroker(profile, clock=clock)
    if feed is None:
        feed = ReplayFeed(
            underlying, strike_step=settings.strike_step(underlying) or 50, clock=clock, events=event_lookup(settings)
        )
    live.rollback()
    return CrewContext(live, deployment_id, adapter, feed, settings, clock=clock, explain=explain)


def run(
    deployment_id: str,
    interval: float = 60,
    max_ticks: int | None = None,
    explain: bool | None = None,
    sleep: Callable[[float], None] = time.sleep,
    emit: Callable[[str], None] = print,
) -> list[dict[str, Any]]:
    settings = get_settings()
    live = get_live_sessionmaker()()
    results: list[dict[str, Any]] = []
    try:
        if not live_schema_ready(live):
            raise ValueError("live database is not migrated: run `uv run alembic -n live upgrade head`")
        ctx = build_context(
            live, deployment_id, settings, explain=settings.trade_crew_explain if explain is None else explain
        )
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
