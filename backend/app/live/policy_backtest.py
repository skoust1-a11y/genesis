"""Backtest a strategy version with and without the risk limits you are about to approve.

Both runs replay the version's own rules over the whole market history with the same capital and
max lots; the second also applies the trade policy and drawdown ladder (including the
MAX_DRAWDOWN_PCT pause), through the same code the Trade Crew uses.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.engine.costs import resolve_cost_profile
from app.engine.sim import ASSUMPTIONS, Management, Scenario, SimResult, run_window
from app.live.approvals import parse_risk_limits
from app.market.calendar import event_lookup
from app.market.source import open_market_source
from app.strategies.schema import parse_strategy_markdown
from app.strategies.store import StrategyStore

POLICY_ASSUMPTIONS = [
    "Policy exits are checked at the daily close like the strategy's exits; a time of day on a time exit "
    "is ignored, so it fires at that day's close.",
    "A drawdown pause stops new entries for the rest of the history: a backtest is never restarted.",
    "The ladder measures equity against the approved capital; entry costs count from the day of entry.",
]


def _summary(r: SimResult, capital: float) -> dict[str, Any]:
    exits: dict[str, int] = {}
    for t in r.trades:
        exits[t.exit_reason] = exits.get(t.exit_reason, 0) + 1
    return {
        "net_pnl": round(r.net_pnl, 2),
        "return_pct": round(r.net_pnl / capital * 100, 2),
        "gross_pnl": round(r.gross_pnl, 2),
        "total_costs": round(r.total_costs, 2),
        "trades": len(r.trades),
        "win_rate": round(r.win_rate, 4),
        "max_drawdown_mtm": round(r.max_drawdown_mtm, 2),
        "max_drawdown_pct_of_capital": round(r.max_drawdown_mtm / capital * 100, 2),
        "max_drawdown_days": r.max_drawdown_days,
        "exit_reasons": exits,
        "skipped_signals": r.skipped,
        "management": r.management,
        "equity_curve": [{"date": p["date"], "equity": round(capital + p["equity"], 2)} for p in r.equity_curve],
    }


def policy_backtest(research: Session, strategy_id: str, version: int, risk_limits: dict[str, Any] | None) -> dict:
    settings = get_settings()
    row = StrategyStore(research).version(strategy_id, version)
    doc = parse_strategy_markdown(row.markdown)
    if doc.rules is None:
        raise ValueError("this version's rules do not validate: " + "; ".join(doc.rules_errors))
    raw_rules = doc.frontmatter.get("rules")
    limits = parse_risk_limits(risk_limits, raw_rules)
    managed = Management.from_limits(limits, settings.max_drawdown_pct)
    baseline = Management(capital_inr=managed.capital_inr, max_lots=managed.max_lots)
    rules = doc.rules.model_dump(mode="json", exclude_none=True)
    scenario = Scenario(label="base", rules=rules, lots=doc.rules.risk.max_lots)
    profile = resolve_cost_profile(settings, doc)
    source = open_market_source(doc.rules.instrument.underlying, settings, event_lookup(settings))
    try:
        dates = source.dates()
        without = run_window(scenario, source, dates, profile, settings, "backtest", baseline)
        with_policy = run_window(scenario, source, dates, profile, settings, "backtest", managed)
        data_source = source.data_source
    finally:
        source.close()
    capital = limits["capital_inr"]
    return {
        "strategy_id": strategy_id,
        "version": version,
        "risk_limits": limits,
        "max_drawdown_pct": settings.max_drawdown_pct,
        "data_source": data_source,
        "window": [with_policy.window_start, with_policy.window_end],
        "rules_only": _summary(without, capital),
        "with_policy": _summary(with_policy, capital),
        "assumptions": ASSUMPTIONS + POLICY_ASSUMPTIONS,
    }
