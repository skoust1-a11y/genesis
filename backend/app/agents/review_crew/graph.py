from __future__ import annotations

import json
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from app.agents.review_crew.hard_stop import hard_stop_proposal, hard_stop_sweep
from app.agents.review_crew.improver import add_llm_rationale, propose_improvements
from app.agents.review_crew.reviewer import review_sim_runs
from app.config import get_settings
from app.engine.costs import resolve_cost_profile
from app.engine.sim import ASSUMPTIONS, Scenario, SimResult, build_scenarios, run_window, split_dates
from app.market.calendar import event_lookup
from app.market.source import open_market_source
from app.strategies.schema import StrategyDocument


class ReviewState(TypedDict, total=False):
    strategy: StrategyDocument
    audit: dict
    scenarios: list
    scenario_results: list
    cost_summary: dict
    verdict: dict
    narrative: str
    sim_runs: list
    trade_reviews: list
    improvements: list
    data_source: str
    hard_stop_sweep: dict


def _audit_node(state: ReviewState) -> ReviewState:
    s = state["strategy"]
    issues = []
    if not s.entry:
        issues.append("Missing Entry rules")
    if not s.exit:
        issues.append("Missing Exit rules")
    if not s.risk:
        issues.append("Missing Risk rules")
    issues += s.rules_errors
    audit = {
        "executable": len(issues) == 0,
        "simulatable": s.rules is not None,
        "issues": issues,
        "gaps_count": len(s.gaps),
    }
    return {"audit": audit}


def _scenario_node(state: ReviewState) -> ReviewState:
    scenarios = build_scenarios(state["strategy"], get_settings())
    return {"scenarios": [sc.__dict__ for sc in scenarios]}


def _metrics(r: SimResult) -> dict:
    return {
        "net_pnl": r.net_pnl,
        "gross_pnl": r.gross_pnl,
        "total_costs": r.total_costs,
        "win_rate": r.win_rate,
        "max_drawdown": r.max_drawdown,
        "max_drawdown_mtm": r.max_drawdown_mtm,
        "max_drawdown_days": r.max_drawdown_days,
        "trades": len(r.trades),
        "window": [r.window_start, r.window_end],
        "by_year": r.by_year,
        "skipped_signals": r.skipped,
        "stats": r.stats,
    }


def _run_sims_node(state: ReviewState) -> ReviewState:
    strategy = state["strategy"]
    settings = get_settings()
    profile = resolve_cost_profile(settings, strategy)
    cost_summary = {
        "brokerage_per_order": profile.brokerage_per_order,
        "slippage_bps": profile.slippage_bps,
        "slippage_min_ticks": profile.slippage_min_ticks,
        "note": "Charges on every simulated fill: brokerage, STT on sells, exchange, GST, SEBI, stamp duty on "
        "buys and slippage; STT on long in-the-money legs exercised at expiry.",
    }
    if not state["scenarios"]:
        return {"scenario_results": [], "cost_summary": cost_summary, "sim_runs": [], "data_source": "none"}

    underlying = strategy.rules.instrument.underlying
    source = open_market_source(underlying, settings, event_lookup(settings))
    try:
        backtest_dates, forward_dates = split_dates(source.dates())
        results, sim_runs = [], []
        for idx, raw in enumerate(state["scenarios"]):
            scenario = Scenario(**raw)
            bt = run_window(scenario, source, backtest_dates, profile, settings, "backtest")
            fw = run_window(scenario, source, forward_dates, profile, settings, "forward")
            sim_runs.append({"scenario_index": idx, "mode": "backtest", "result": bt})
            sim_runs.append({"scenario_index": idx, "mode": "forward", "result": fw})
            results.append({"scenario": raw, "backtest": _metrics(bt), "forward": _metrics(fw)})
        data_source = source.data_source
    finally:
        source.close()
    return {"scenario_results": results, "cost_summary": cost_summary, "sim_runs": sim_runs, "data_source": data_source}


def _hard_stop_node(state: ReviewState) -> ReviewState:
    """The strategy at each stop in HARD_STOP_SWEEP_PCTS, over the windows the scenarios use."""
    settings = get_settings()
    rules = state["strategy"].rules
    if not state["scenarios"] or rules is None or not settings.hard_stop_sweep:
        return {}
    profile = resolve_cost_profile(settings, state["strategy"])
    source = open_market_source(rules.instrument.underlying, settings, event_lookup(settings))
    try:
        backtest_dates, forward_dates = split_dates(source.dates())
        sweep = hard_stop_sweep(rules, source, backtest_dates, forward_dates, profile, settings)
    finally:
        source.close()
    return {"hard_stop_sweep": sweep}


def _trade_review_node(state: ReviewState) -> ReviewState:
    return {"trade_reviews": review_sim_runs(state["sim_runs"])}


def _improve_node(state: ReviewState) -> ReviewState:
    proposals = propose_improvements(state["sim_runs"])
    stop = hard_stop_proposal(state.get("hard_stop_sweep"), state["strategy"].rules) if state["strategy"].rules else None
    proposals += [stop] if stop else []
    add_llm_rationale(proposals, state["strategy"].markdown, get_settings())
    return {"improvements": proposals}


def _verdict_node(state: ReviewState) -> ReviewState:
    settings = get_settings()
    results = state["scenario_results"]
    passing = []
    for r in results:
        bt_ok = r["backtest"]["net_pnl"] > 0 and r["backtest"]["trades"] >= settings.verdict_min_backtest_trades
        fw_ok = r["forward"]["net_pnl"] > 0 and r["forward"]["trades"] >= settings.verdict_min_forward_trades
        r["pass_net"] = bt_ok and fw_ok
        r["edge_before_costs"] = r["backtest"]["gross_pnl"] > 0 and r["forward"]["gross_pnl"] > 0
        r["too_few_trades"] = (
            r["backtest"]["trades"] < settings.verdict_min_backtest_trades
            or r["forward"]["trades"] < settings.verdict_min_forward_trades
        )
        if r["pass_net"]:
            passing.append(r)
    no_edge = sum(1 for r in results if not r["edge_before_costs"])
    thin = sum(1 for r in results if r["too_few_trades"])
    if not state["audit"]["simulatable"]:
        label = "Not executable"
    elif len(passing) == len(results) and results:
        label = "Profitable"
    elif passing:
        label = "Conditional"
    elif results and thin == len(results):
        label = "Too few trades"
    else:
        label = "Not profitable"
    data_source = state.get("data_source", "none")
    if data_source != "nse_eod" and label in ("Profitable", "Conditional"):
        label = "Synthetic data only"  # synthetic prices prove nothing; approval then needs an acknowledgement
    best = max(results, key=lambda x: x["backtest"]["net_pnl"] + x["forward"]["net_pnl"], default=None)
    worst = min(results, key=lambda x: x["backtest"]["net_pnl"] + x["forward"]["net_pnl"], default=None)
    verdict = {
        "label": label,
        "scenarios_tested": len(results),
        "scenarios_passing": len(passing),
        "scenarios_without_edge_before_costs": no_edge,
        "scenarios_with_too_few_trades": thin,
        "data_source": data_source,
        "assumptions": [
            "Imported NSE end-of-day data." if data_source == "nse_eod"
            else "Bundled sample data with synthetic option prices (no NSE data imported).",
            *ASSUMPTIONS,
            f"Net verdict requires positive backtest and forward windows with at least "
            f"{settings.verdict_min_backtest_trades} and {settings.verdict_min_forward_trades} trades.",
        ],
        "best_scenario": best,
        "worst_scenario": worst,
    }
    if label == "Not executable":
        narrative = "Review Crew verdict: Not executable. " + "; ".join(state["audit"]["issues"])
    else:
        narrative = (
            f"Review Crew verdict: {label}. "
            f"{len(passing)}/{len(results)} scenarios passed net checks after costs."
        )
        if no_edge:
            narrative += f" {no_edge} lose money before costs: no edge to protect."
        if thin:
            narrative += f" {thin} have too few trades to judge."
    return {"verdict": verdict, "narrative": narrative}


def build_review_graph():
    graph = StateGraph(ReviewState)
    graph.add_node("audit", _audit_node)
    graph.add_node("scenarios", _scenario_node)
    graph.add_node("simulate", _run_sims_node)
    graph.add_node("hard_stop", _hard_stop_node)
    graph.add_node("trade_review", _trade_review_node)
    graph.add_node("verdict", _verdict_node)
    graph.add_node("improve", _improve_node)
    graph.add_edge(START, "audit")
    graph.add_edge("audit", "scenarios")
    graph.add_edge("scenarios", "simulate")
    graph.add_edge("simulate", "hard_stop")
    graph.add_edge("hard_stop", "trade_review")
    graph.add_edge("trade_review", "verdict")
    graph.add_edge("verdict", "improve")
    graph.add_edge("improve", END)
    return graph.compile()


def run_review_crew(strategy: StrategyDocument) -> tuple[dict, list, list, list]:
    """Returns (payload, sim_runs, trade_reviews, improvements)."""
    app = build_review_graph()
    out = dict(app.invoke({"strategy": strategy}))
    sim_runs = out.pop("sim_runs", [])
    reviews = out.pop("trade_reviews", [])
    improvements = out.pop("improvements", [])
    payload = json.loads(json.dumps(out, default=str))
    return payload, sim_runs, reviews, improvements
