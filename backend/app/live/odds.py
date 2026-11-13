"""The odds gate on a running deployment (see `app.trading.odds`).

The history behind it is a rules-only backtest of the deployed rules over the imported market data,
rebuilt once per market day and kept in memory per (deployment, rules hash, day). Only trades that
closed before the day count, as in backtests.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.config import Settings
from app.db.models import Deployment
from app.engine.costs import CostProfile, resolve_cost_profile
from app.live.store import LiveStore
from app.market.calendar import event_lookup
from app.market.source import open_market_source
from app.strategies.rules import StrategyRules
from app.strategies.schema import parse_strategy_markdown
from app.trading import odds as odds_gate
from app.trading import policy as trade_policy

_cache: dict[tuple[str, str, str], list[odds_gate.HistTrade]] = {}


def gate_for(deployment: Deployment) -> trade_policy.OddsGate | None:
    policy = trade_policy.policy_from_limits(deployment.risk_limits)
    return policy.odds_gate if policy else None


def _load(rules: StrategyRules, profile: CostProfile, settings: Settings, as_of: str) -> list[odds_gate.HistTrade]:
    source = open_market_source(rules.instrument.underlying, settings, event_lookup(settings))
    try:
        return odds_gate.strategy_history(rules, source, profile, settings, until=as_of)
    finally:
        source.close()


def history(
    deployment_id: str, rules_sha: str, rules: StrategyRules, profile: CostProfile, settings: Settings, as_of: str
) -> list[odds_gate.HistTrade]:
    """The rules-only trades for `as_of`, built on the first call of the day."""
    key = (deployment_id, rules_sha, as_of)
    if key not in _cache:
        for old in [k for k in _cache if k[0] == deployment_id]:
            del _cache[old]
        _cache[key] = _load(rules, profile, settings, as_of)
    return _cache[key]


def clear_cache() -> None:
    _cache.clear()


def _latest_vix(settings: Settings, underlying: str) -> tuple[str, float] | None:
    from app.market.models import IndexBar
    from app.market.store import open_market_store

    store = open_market_store(settings)
    try:
        row = store.session.execute(
            select(IndexBar.date, IndexBar.vix)
            .where(IndexBar.underlying == underlying.upper(), IndexBar.vix.is_not(None))
            .order_by(IndexBar.date.desc())
            .limit(1)
        ).first()
    finally:
        store.close()
    return (row[0], float(row[1])) if row else None


def odds_status(store: LiveStore, settings: Settings, deployment: Deployment, as_of: str) -> dict[str, Any] | None:
    """The gate, every band's past trades, and the band of the latest imported VIX. None without a gate."""
    gate = gate_for(deployment)
    if gate is None:
        return None
    snapshot = store.deployed_strategy(deployment.deployed_strategy_id)
    doc = parse_strategy_markdown(snapshot.markdown)
    rules = StrategyRules.model_validate(snapshot.rules)
    trades = history(deployment.id, snapshot.rules_sha256, rules, resolve_cost_profile(settings, doc), settings, as_of)
    latest = _latest_vix(settings, rules.instrument.underlying)
    vix = latest[1] if latest else None
    now = odds_gate.odds_for(trades, as_of, vix, gate)
    checks = odds_gate.odds_checks(now, gate)
    return {
        "gate": gate.model_dump(mode="json", exclude_none=True),
        "as_of": as_of,
        "vix_date": latest[0] if latest else None,
        "today": now.as_dict(),
        "entries_open": all(c.passed for c in checks),
        "summary": odds_gate.odds_text(now),
        "bands": [o.as_dict() for o in odds_gate.odds_table(trades, as_of, gate)],
        "history_trades": len(trades),
    }
