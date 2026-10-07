from __future__ import annotations

import json
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from app.config import get_settings
from app.engine.costs import resolve_cost_profile
from app.engine.sim import build_scenarios, run_backtest, run_forward
from app.strategies.schema import StrategyDocument


class ReviewState(TypedDict, total=False):
    strategy: StrategyDocument
    audit: dict
    scenarios: list
    scenario_results: list
    cost_summary: dict
    verdict: dict
    narrative: str


def _audit_node(state: ReviewState) -> ReviewState:
    s = state["strategy"]
    issues = []
    if not s.entry:
        issues.append("Missing Entry rules")
    if not s.exit:
        issues.append("Missing Exit rules")
    if not s.risk:
        issues.append("Missing Risk rules")
    audit = {
        "executable": len(issues) == 0,
        "issues": issues,
        "gaps_count": len(s.gaps),
    }
    return {"audit": audit}


def _scenario_node(state: ReviewState) -> ReviewState:
    settings = get_settings()
    scenarios = build_scenarios(state["strategy"], settings)
    return {"scenarios": [sc.__dict__ for sc in scenarios]}


def _run_sims_node(state: ReviewState) -> ReviewState:
    strategy = state["strategy"]
    settings = get_settings()
    results = []
    for raw in state["scenarios"]:
        from app.engine.sim import Scenario

        scenario = Scenario(**raw)
        bt = run_backtest(strategy, scenario)
        fw = run_forward(strategy, scenario)
        results.append(
            {
                "scenario": raw,
                "backtest": {
                    "net_pnl": bt.net_pnl,
                    "gross_pnl": bt.gross_pnl,
                    "total_costs": bt.total_costs,
                    "win_rate": bt.win_rate,
                    "max_drawdown": bt.max_drawdown,
                    "trades": len(bt.trades),
                },
                "forward": {
                    "net_pnl": fw.net_pnl,
                    "gross_pnl": fw.gross_pnl,
                    "total_costs": fw.total_costs,
                    "win_rate": fw.win_rate,
                    "max_drawdown": fw.max_drawdown,
                    "trades": len(fw.trades),
                },
            }
        )
    profile = resolve_cost_profile(settings, strategy)
    cost_summary = {
        "brokerage_per_order": profile.brokerage_per_order,
        "slippage_bps": profile.slippage_bps,
        "note": "Charges applied on every simulated fill (brokerage, STT, exchange, GST, SEBI, stamp, slippage).",
    }
    return {"scenario_results": results, "cost_summary": cost_summary}


def _verdict_node(state: ReviewState) -> ReviewState:
    results = state["scenario_results"]
    passing = []
    for r in results:
        bt_ok = r["backtest"]["net_pnl"] > 0 and r["backtest"]["trades"] >= 3
        fw_ok = r["forward"]["net_pnl"] > 0 and r["forward"]["trades"] >= 2
        r["pass_net"] = bt_ok and fw_ok
        if r["pass_net"]:
            passing.append(r)
    if len(passing) == len(results) and results:
        label = "Profitable"
    elif passing:
        label = "Conditional"
    else:
        label = "Not profitable"
    best = max(results, key=lambda x: x["backtest"]["net_pnl"] + x["forward"]["net_pnl"], default=None)
    worst = min(results, key=lambda x: x["backtest"]["net_pnl"] + x["forward"]["net_pnl"], default=None)
    verdict = {
        "label": label,
        "scenarios_tested": len(results),
        "scenarios_passing": len(passing),
        "assumptions": [
            "Sample NSE-style historical data (not live vendor feed).",
            "Net verdict requires positive backtest and forward windows with minimum trades.",
        ],
        "best_scenario": best,
        "worst_scenario": worst,
    }
    narrative = (
        f"Review Crew verdict: {label}. "
        f"{len(passing)}/{len(results)} scenarios passed net checks after costs."
    )
    return {"verdict": verdict, "narrative": narrative}


def build_review_graph():
    graph = StateGraph(ReviewState)
    graph.add_node("audit", _audit_node)
    graph.add_node("scenarios", _scenario_node)
    graph.add_node("simulate", _run_sims_node)
    graph.add_node("verdict", _verdict_node)
    graph.add_edge(START, "audit")
    graph.add_edge("audit", "scenarios")
    graph.add_edge("scenarios", "simulate")
    graph.add_edge("simulate", "verdict")
    graph.add_edge("verdict", END)
    return graph.compile()


def run_review_crew(strategy: StrategyDocument) -> dict:
    app = build_review_graph()
    out = app.invoke({"strategy": strategy})
    return json.loads(json.dumps(out, default=str))
