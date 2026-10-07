from __future__ import annotations

from app.engine.sim import SimResult, TradeResult

REVIEW_RULES_VERSION = "1"
REVIEWER = "review_crew"
HIGH_COST_RATIO = 0.30
MARGINAL_ENTRY_POINTS = 2.0
FORWARD_DECAY_RATIO = 0.5
PROCESS_TAGS = {"costs_ate_edge", "high_cost_ratio", "marginal_entry"}

TradeKey = tuple[int, str, int]


def trade_key(scenario_index: int, mode: str, seq: int) -> TradeKey:
    return (scenario_index, mode, seq)


def _iv_condition(trade: TradeResult):
    return next(
        (
            c for c in trade.entry_conditions
            if c.field == "iv_rank" and c.op in (">=", ">") and c.actual is not None
        ),
        None,
    )  # fmt: skip


def forward_decay(backtest: SimResult | None, forward: SimResult | None) -> bool:
    if not backtest or not forward or not backtest.trades or not forward.trades:
        return False
    bt_per_trade = backtest.net_pnl / len(backtest.trades)
    fw_per_trade = forward.net_pnl / len(forward.trades)
    return bt_per_trade > 0 and fw_per_trade < bt_per_trade * FORWARD_DECAY_RATIO


def mistake_tags(trade: TradeResult, run_tags: list[str] | None = None) -> list[str]:
    tags: list[str] = []
    if trade.gross > 0 and trade.net <= 0:
        tags.append("costs_ate_edge")
    if trade.gross > 0 and trade.costs / trade.gross > HIGH_COST_RATIO:
        tags.append("high_cost_ratio")
    iv = _iv_condition(trade)
    if iv is not None and iv.actual - iv.threshold < MARGINAL_ENTRY_POINTS:
        tags.append("marginal_entry")
    tags.extend(run_tags or [])
    return tags


def review_trade(trade: TradeResult, run_tags: list[str] | None = None) -> dict:
    tags = mistake_tags(trade, run_tags)
    followed_plan = all(c.passed for c in trade.entry_conditions)
    good_process = followed_plan and not (set(tags) & PROCESS_TAGS)
    good_outcome = trade.net > 0
    verdict = f"{'good' if good_process else 'bad'}_process_{'good' if good_outcome else 'bad'}_outcome"

    went_well: list[str] = []
    went_wrong: list[str] = []
    if followed_plan:
        went_well.append(f"Entry conditions met: {trade.reason_text}.")
    else:
        went_wrong.append("Entered without all entry conditions passing.")
    if good_outcome:
        went_well.append(f"Net {trade.net:,.2f} after {trade.costs:,.2f} in charges.")
    else:
        went_wrong.append(f"Net {trade.net:,.2f} after {trade.costs:,.2f} in charges.")

    iv = _iv_condition(trade)
    lesson: str
    suggestion: str | None = None
    rule_change: dict | None = None
    if "costs_ate_edge" in tags or "high_cost_ratio" in tags:
        pct = trade.costs / trade.gross if trade.gross else 0.0
        went_wrong.append(f"Charges took {pct:.0%} of gross profit.")
        lesson = "Charges must be small relative to the expected credit before entering."
        suggestion = f"Skip entries where estimated charges exceed {HIGH_COST_RATIO:.0%} of expected gross."
        rule_change = {
            "section": "Risk",
            "rule": f"Skip entries where estimated charges exceed {HIGH_COST_RATIO:.0%} of expected gross",
        }
    elif "marginal_entry" in tags and iv is not None:
        went_wrong.append(
            f"IV rank {iv.actual:g} was within {MARGINAL_ENTRY_POINTS:g} points of the {iv.threshold:g} threshold."
        )
        lesson = "Marginal signals carry little edge; demand a clearer IV-rank margin."
        proposed = iv.threshold + MARGINAL_ENTRY_POINTS
        suggestion = f"Raise iv_rank_min from {iv.threshold:g} to {proposed:g}."
        rule_change = {"section": "Entry", "field": "iv_rank_min", "current": iv.threshold, "proposed": proposed}
    elif "forward_decay" in tags:
        went_wrong.append("Forward net per trade is below half of the backtest; the edge may be decaying.")
        lesson = "Re-validate the setup on recent data before trusting backtest results."
    elif good_outcome:
        lesson = "Rules followed and the trade paid; repeat as planned."
    else:
        lesson = "Rules followed but the trade lost; check whether the thesis still holds."

    return {
        "reviewer_type": "agent",
        "reviewer": REVIEWER,
        "rules_version": REVIEW_RULES_VERSION,
        "followed_plan": followed_plan,
        "process_verdict": verdict,
        "what_went_well": " ".join(went_well) or None,
        "what_went_wrong": " ".join(went_wrong) or None,
        "mistake_tags": tags,
        "lesson": lesson,
        "improvement_suggestion": suggestion,
        "suggested_rule_change": rule_change,
        "confidence": 1.0,
    }


def review_sim_runs(sim_runs: list[dict]) -> list[dict]:
    """Review every simulated trade. Each sim_run is {scenario_index, mode, result}."""
    by_scenario: dict[int, dict[str, SimResult]] = {}
    for run in sim_runs:
        by_scenario.setdefault(run["scenario_index"], {})[run["mode"]] = run["result"]

    reviews: list[dict] = []
    for run in sim_runs:
        idx, mode, result = run["scenario_index"], run["mode"], run["result"]
        modes = by_scenario[idx]
        run_tags = ["forward_decay"] if mode == "forward" and forward_decay(modes.get("backtest"), result) else []
        for seq, trade in enumerate(result.trades, start=1):
            review = review_trade(trade, run_tags)
            review["key"] = trade_key(idx, mode, seq)
            reviews.append(review)
    return reviews
