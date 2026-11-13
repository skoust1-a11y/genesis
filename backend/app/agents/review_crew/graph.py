from __future__ import annotations

import json
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from app.agents.review_crew.hard_stop import hard_stop_proposal, hard_stop_sweep
from app.agents.review_crew.improver import add_llm_rationale, propose_improvements
from app.agents.review_crew.reviewer import review_sim_runs
from app.config import get_settings
from app.engine.costs import resolve_cost_profile
from app.engine.sim import ASSUMPTIONS, ENGINE_VERSION, Scenario, SimResult, build_scenarios, run_window, split_dates
from app.market.calendar import event_lookup
from app.market.source import open_market_source
from app.strategies.capabilities import EOD_ADAPTER_VERSION, eod_rejection_reasons
from app.strategies.normalizer import normalize_strategy_document
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
    executed_baseline: bool


def _audit_node(state: ReviewState) -> ReviewState:
    s = state["strategy"]
    norm = normalize_strategy_document(s)
    rejections = eod_rejection_reasons(norm)
    if s.source_verification_required:
        rejections.append({"code": "UNVERIFIED_SOURCE_RULE_COVERAGE", "path": "source",
                           "detail": "AI extraction has no independently verified source-to-rule mapping"})
    if (norm.rules_v2 is not None
            and norm.rules_v2.position_sizing.capital_inr != get_settings().default_capital_inr):
        rejections.append({"code": "EXECUTION_ASSUMPTION_MISMATCH", "path": "position_sizing.capital_inr",
                           "detail": "EOD account capital differs from declared strategy capital"})
    if s.rules != norm.legacy_rules:
        rejections.append({"code": "EXECUTED_RULES_MISMATCH", "path": "rules",
                           "detail": "Review execution rules differ from normalized rules"})
    issues = []
    if not s.entry:
        issues.append("Missing Entry rules")
    if not s.exit:
        issues.append("Missing Exit rules")
    if not s.risk:
        issues.append("Missing Risk rules")
    issues += s.rules_errors
    for reason in norm.quarantine_reasons:
        if reason not in issues:
            issues.append(reason)
    issues += [f"{r['code']}: {r['path']}: {r['detail']}" for r in rejections]
    simulatable = bool(norm.simulatable and s.rules is not None and not rejections)
    audit = {
        "executable": len(issues) == 0 and not norm.quarantined and simulatable,
        "execution_status": "NOT_EXECUTABLE_EXACTLY" if rejections else "SUPPORTED_STRUCTURED_RULES",
        "rejection_reasons": rejections,
        "adapter_version": EOD_ADAPTER_VERSION,
        "engine_version": ENGINE_VERSION,
        "submitted_rules": {key: s.frontmatter[key] for key in ("rules", "rules_v2") if key in s.frontmatter},
        "normalized_specification": norm.to_dict(),
        "data_requirements": list(norm.rules_v2.data_dependencies) if norm.rules_v2 else [],
        "execution_assumptions": list(ASSUMPTIONS),
        "simulatable": simulatable,
        "structurally_complete": norm.structurally_complete,
        "backtest_eligible": norm.backtest_eligible,
        "independently_validated": norm.independently_validated,
        "paper_approved": norm.paper_approved,
        "strategy_state": norm.strategy_state,
        "quarantined": norm.quarantined,
        "quarantine_reasons": list(norm.quarantine_reasons),
        "promotion_state": norm.promotion_state,
        "category_class": norm.category_class,
        "paper_eligible": norm.paper_eligible,
        "promotable": norm.promotable,
        "normalizer_version": norm.normalizer_version,
        "source_provenance": dict(norm.provenance),
        "provenance_sha256": norm.provenance_sha256,
        "rules_sha256": norm.rules_sha256,
        "spec_sha256": norm.spec_sha256,
        "rules_v2_sha256": norm.rules_v2_sha256,
        "parameter_hash": norm.parameter_hash,
        "validation_binding": norm.validation_binding(),
        "issues": issues,
        "gaps_count": len(s.gaps),
    }
    return {"audit": audit}


def _scenario_node(state: ReviewState) -> ReviewState:
    if not state["audit"].get("simulatable") or state["audit"].get("quarantined"):
        return {"scenarios": []}
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
        return {"scenario_results": [], "cost_summary": cost_summary, "sim_runs": [],
                "data_source": "none", "executed_baseline": False}

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
    executed_baseline = any(
        run["mode"] == "backtest" and run["result"].scenario.label == "base"
        and run["result"].scenario.parsed == strategy.rules for run in sim_runs
    ) and any(
        run["mode"] == "forward" and run["result"].scenario.label == "base"
        and run["result"].scenario.parsed == strategy.rules for run in sim_runs
    )
    return {"scenario_results": results, "cost_summary": cost_summary, "sim_runs": sim_runs,
            "data_source": data_source, "executed_baseline": executed_baseline}


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
    if not state["audit"].get("simulatable"):
        return {"improvements": []}
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
    baseline = next((r for r in results if r.get("scenario", {}).get("label") == "base"), None)
    if not state["audit"]["simulatable"] or not state.get("executed_baseline") or baseline is None:
        label = "Not executable"
    elif baseline["pass_net"] and len(passing) == len(results):
        label = "Profitable"
    elif baseline["pass_net"]:
        label = "Conditional"
    elif baseline["too_few_trades"]:
        label = "Too few trades"
    else:
        label = "Not profitable"
    data_source = state.get("data_source", "none")
    if data_source != "nse_eod" and label in ("Profitable", "Conditional"):
        label = "Synthetic data only"  # synthetic prices prove nothing; approval then needs an acknowledgement
    best = max(results, key=lambda x: x["backtest"]["net_pnl"], default=None)
    worst = min(results, key=lambda x: x["backtest"]["net_pnl"], default=None)
    verdict = {
        "label": label,
        "executed_baseline": bool(state.get("executed_baseline")),
        "execution_status": state["audit"].get("execution_status"),
        "rejection_reasons": state["audit"].get("rejection_reasons", []),
        "quarantined": bool(state["audit"].get("quarantined", False)),
        "quarantine_reasons": list(state["audit"].get("quarantine_reasons", [])),
        "promotion_state": state["audit"].get("promotion_state", "QUARANTINED"),
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
        q_prefix = "QUARANTINED: " if state["audit"].get("quarantined") else ""
        narrative = f"Review Crew verdict: Not executable. {q_prefix}" + "; ".join(state["audit"]["issues"])
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
    out.pop("executed_baseline", None)  # Execution evidence is exposed in the verdict.
    payload = json.loads(json.dumps(out, default=str))
    return payload, sim_runs, reviews, improvements
