from __future__ import annotations

import json
from collections.abc import Callable

from app.agents.review_crew.reviewer import HIGH_COST_RATIO, TradeKey, trade_key
from app.engine.sim import Scenario, TradeResult

MIN_BUCKET_TRADES = 3
IV_BAND = 5
DTE_BAND = 7

Keyed = tuple[TradeKey, TradeResult]


def _net(trades: list[Keyed]) -> float:
    return sum(t.net for _, t in trades)


def _split_loser(
    trades: list[Keyed], in_bucket: Callable[[TradeResult], bool]
) -> tuple[list[Keyed], list[Keyed]] | None:
    bucket = [kt for kt in trades if in_bucket(kt[1])]
    rest = [kt for kt in trades if not in_bucket(kt[1])]
    if len(bucket) >= MIN_BUCKET_TRADES and rest and _net(bucket) < 0 < _net(rest):
        return bucket, rest
    return None


def _proposal(
    scenario_index: int,
    title: str,
    bucket_label: str,
    bucket: list[Keyed],
    rest: list[Keyed],
    proposed_change: dict,
) -> dict:
    in_bucket, in_rest = _net(bucket), _net(rest)
    return {
        "title": f"Scenario #{scenario_index + 1}: {title}",
        "rationale": (
            f"The {len(bucket)} trades with {bucket_label} lost a net Rs {abs(in_bucket):,.0f}, while the other "
            f"{len(rest)} trades in this scenario made Rs {in_rest:,.0f}."
        ),
        "proposed_change": proposed_change,
        "expected_impact": f"Skipping these trades would have added Rs {abs(in_bucket):,.0f} net on this sample.",
        "evidence": {
            "scenario_index": scenario_index,
            "bucket": bucket_label,
            "trade_keys": [list(k) for k, _ in bucket],
            "sample_size": len(bucket),
            "metric": "net_pnl",
            "value_in_bucket": round(in_bucket, 4),
            "value_rest": round(in_rest, 4),
            "value_overall": round(in_bucket + in_rest, 4),
        },
    }


def _snapshot_value(trade: TradeResult, key: str) -> float | None:
    return trade.market_snapshot.get(key)


def _iv_proposal(idx: int, scenario: Scenario, trades: list[Keyed]) -> dict | None:
    lo = scenario.iv_rank_min
    if lo is None:
        return None
    hi = lo + IV_BAND
    split = _split_loser(trades, lambda t: (_snapshot_value(t, "iv_rank") or 0) < hi)
    if not split:
        return None
    return _proposal(
        idx,
        f"raise iv_rank_min from {lo:g} to {hi:g}",
        f"IV rank {lo:g} to {hi:g}",
        *split,
        {"section": "Entry", "field": "iv_rank_min", "current": lo, "proposed": hi},
    )


def _dte_proposals(idx: int, scenario: Scenario, trades: list[Keyed]) -> list[dict]:
    out: list[dict] = []
    if scenario.dte_min is None or scenario.dte_max is None:
        return out
    trades = [kt for kt in trades if _snapshot_value(kt[1], "dte") is not None]
    low_end = scenario.dte_min + DTE_BAND - 1
    split = _split_loser(trades, lambda t: t.market_snapshot["dte"] <= low_end)
    if split and low_end < scenario.dte_max:
        out.append(
            _proposal(
                idx,
                f"raise dte_min from {scenario.dte_min} to {low_end + 1}",
                f"DTE {scenario.dte_min} to {low_end}",
                *split,
                {"section": "Entry", "field": "dte_min", "current": scenario.dte_min, "proposed": low_end + 1},
            )
        )
    high_start = scenario.dte_max - DTE_BAND + 1
    split = _split_loser(trades, lambda t: t.market_snapshot["dte"] >= high_start)
    if split and high_start > scenario.dte_min:
        out.append(
            _proposal(
                idx,
                f"lower dte_max from {scenario.dte_max} to {high_start - 1}",
                f"DTE {high_start} to {scenario.dte_max}",
                *split,
                {"section": "Entry", "field": "dte_max", "current": scenario.dte_max, "proposed": high_start - 1},
            )
        )
    return out


def _cost_proposal(idx: int, trades: list[Keyed]) -> dict | None:
    def expensive(t: TradeResult) -> bool:
        return t.cost_ratio is None or t.gross <= 0 or t.cost_ratio > HIGH_COST_RATIO

    split = _split_loser(trades, expensive)
    if not split:
        return None
    return _proposal(
        idx,
        f"skip entries where charges exceed {HIGH_COST_RATIO:.0%} of gross",
        f"charges above {HIGH_COST_RATIO:.0%} of gross",
        *split,
        {"section": "Risk", "rule": f"Skip entries where estimated charges exceed {HIGH_COST_RATIO:.0%} of expected gross"},
    )


def _forward_proposal(idx: int, backtest: list[Keyed], forward: list[Keyed]) -> dict | None:
    if len(forward) < MIN_BUCKET_TRADES or not backtest:
        return None
    if not (_net(backtest) > 0 >= _net(forward)):
        return None
    return _proposal(
        idx,
        "re-validate: profitable in backtest, losing in forward test",
        "forward-window entries",
        forward,
        backtest,
        {
            "section": "Gaps",
            "add": "Forward window lost money while the backtest was profitable; re-validate on recent data",
        },
    )


def propose_improvements(sim_runs: list[dict]) -> list[dict]:
    """Deterministic proposals backed by trade evidence. Each sim_run is {scenario_index, mode, result}."""
    scenarios: dict[int, Scenario] = {}
    by_mode: dict[int, dict[str, list[Keyed]]] = {}
    for run in sim_runs:
        idx, mode, result = run["scenario_index"], run["mode"], run["result"]
        scenarios[idx] = result.scenario
        keyed = [(trade_key(idx, mode, seq), t) for seq, t in enumerate(result.trades, start=1)]
        by_mode.setdefault(idx, {})[mode] = keyed

    proposals: list[dict] = []
    for idx in sorted(by_mode):
        modes = by_mode[idx]
        trades = modes.get("backtest", []) + modes.get("forward", [])
        scenario = scenarios[idx]
        for proposal in (
            _iv_proposal(idx, scenario, trades),
            *_dte_proposals(idx, scenario, trades),
            _cost_proposal(idx, trades),
            _forward_proposal(idx, modes.get("backtest", []), modes.get("forward", [])),
        ):
            if proposal:
                proposals.append(proposal)
    return proposals


def add_llm_rationale(proposals: list[dict], strategy_markdown: str, settings) -> None:
    """Optionally append an LLM-written note to each proposal. Proposals never depend on it."""
    if not proposals or not settings.llm_enabled:
        return
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        from app.llm import chat_model

        llm = chat_model(settings)
        items = [{"title": p["title"], "rationale": p["rationale"]} for p in proposals]
        resp = llm.invoke(
            [
                SystemMessage(
                    content=(
                        "You review proposed trading-rule changes. For each item, write one or two sentences on "
                        "the trade-off of applying it. Reply with a JSON array of strings, one per item, same order."
                    )
                ),
                HumanMessage(content=f"Strategy:\n{strategy_markdown[:8000]}\n\nItems:\n{json.dumps(items)}"),
            ]
        )
        content = resp.content if isinstance(resp.content, str) else str(resp.content)
        start, end = content.find("["), content.rfind("]")
        notes = json.loads(content[start : end + 1])
    except Exception:
        return
    if not isinstance(notes, list) or len(notes) != len(proposals):
        return
    for proposal, note in zip(proposals, notes):
        if isinstance(note, str) and note.strip():
            proposal["rationale"] += f"\n\nLLM note ({settings.llm_model}): {note.strip()}"
            proposal["evidence"]["llm_model"] = settings.llm_model
