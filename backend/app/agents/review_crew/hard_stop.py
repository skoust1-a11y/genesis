"""Hard stop-loss sweep: the strategy re-run with a stop at several % of capital, target R x stop."""

from __future__ import annotations

import copy
import math

from app.config import Settings
from app.engine.costs import CostProfile
from app.engine.sim import Scenario, SimResult, run_window
from app.strategies.rules import CAPITAL_EXITS, CREDIT_EXITS, DEBIT_EXITS, StrategyRules, risk_reward_ratio

SIZING_RULE = "risk.max_loss_pct_of_capital"
CAVEAT = "Picked from one stretch of history; it can differ on other data."


def sweep_rules(rules: StrategyRules, pct: float, r_multiple: float) -> dict:
    """`rules` as JSON with the target/stop pair swapped for a stop at `pct`% of capital and an R target."""
    raw = copy.deepcopy(rules.model_dump(mode="json", exclude_none=True))
    swapped = (*CREDIT_EXITS, *DEBIT_EXITS, *CAPITAL_EXITS)
    raw["exit"] = [e for e in raw["exit"] if e["kind"] not in swapped] + [
        {"kind": "stop_loss_pct_of_capital", "value": pct},
        {"kind": "profit_target_r_multiple", "value": round(r_multiple, 4)},
    ]
    return raw


def _window(r: SimResult) -> dict:
    return {"trades": len(r.trades), "win_rate": round(r.win_rate, 4), "net_pnl": round(r.net_pnl, 2)}


def sweep_row(pct: float, capital: float, r_multiple: float, bt: SimResult, fw: SimResult, settings: Settings) -> dict:
    losses = [t.net for t in bt.trades + fw.trades if t.net < 0]
    stop = capital * pct / 100
    return {
        "stop_pct": pct,
        "stop_inr": round(stop, 2),
        "target_inr": round(stop * r_multiple, 2),
        "backtest": _window(bt),
        "forward": _window(fw),
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "worst_loss": round(min(losses), 2) if losses else None,
        "max_drawdown": round(max(bt.max_drawdown, bt.max_drawdown_mtm, fw.max_drawdown, fw.max_drawdown_mtm), 2),
        "sizing_blocked": bt.skipped.get(SIZING_RULE, 0) + fw.skipped.get(SIZING_RULE, 0),
        "eligible": (
            bt.net_pnl > 0 and len(bt.trades) >= settings.verdict_min_backtest_trades
            and fw.net_pnl > 0 and len(fw.trades) >= settings.verdict_min_forward_trades
        ),
    }  # fmt: skip


def _score(row: dict) -> float:
    net = row["backtest"]["net_pnl"] + row["forward"]["net_pnl"]
    return net / row["max_drawdown"] if row["max_drawdown"] > 0 else math.inf


def pick_stop(rows: list[dict], settings: Settings) -> tuple[float | None, str]:
    """The eligible stop with the most net P&L per rupee of drawdown; the smaller stop on a tie."""
    eligible = [r for r in rows if r["eligible"]]
    if not eligible:
        return None, (
            f"No stop made money in both the backtest and the forward window with at least "
            f"{settings.verdict_min_backtest_trades} and {settings.verdict_min_forward_trades} trades."
        )
    best = min(eligible, key=lambda r: (-_score(r), r["stop_pct"]))
    net = best["backtest"]["net_pnl"] + best["forward"]["net_pnl"]
    reason = (
        f"{best['stop_pct']:g}% (Rs {best['stop_inr']:,.0f}) made the most net P&L per rupee of drawdown among "
        f"the {len(eligible)} stop(s) profitable in both windows: Rs {net:,.0f} net against a "
        f"Rs {best['max_drawdown']:,.0f} largest drawdown. {CAVEAT}"
    )
    return best["stop_pct"], reason


def hard_stop_sweep(
    rules: StrategyRules,
    source,
    backtest_dates: list[str],
    forward_dates: list[str],
    profile: CostProfile,
    settings: Settings,
) -> dict:
    """Backtest and forward-test each stop in `settings.hard_stop_sweep` and pick the ideal one."""
    capital = settings.default_capital_inr
    r_multiple = risk_reward_ratio(rules)
    out = {"capital_inr": capital, "risk_reward": r_multiple, "rows": [], "recommended_pct": None}
    if r_multiple is None:
        return out | {"reason": "The strategy has no profit target and stop pair, so it has no risk-to-reward ratio."}
    for pct in settings.hard_stop_sweep:
        raw = sweep_rules(rules, pct, r_multiple)
        try:
            StrategyRules.model_validate(raw)
        except ValueError:
            continue
        scenario = Scenario(label=f"hard stop {pct:g}% of capital", rules=raw, lots=raw["risk"]["max_lots"])
        bt = run_window(scenario, source, backtest_dates, profile, settings, "backtest")
        fw = run_window(scenario, source, forward_dates, profile, settings, "forward")
        out["rows"].append(sweep_row(pct, capital, r_multiple, bt, fw, settings))
    if not out["rows"]:
        return out | {"reason": "No stop in HARD_STOP_SWEEP_PCTS is a valid stop as % of capital."}
    recommended, reason = pick_stop(out["rows"], settings)
    return out | {"recommended_pct": recommended, "reason": reason}


def hard_stop_proposal(sweep: dict | None, rules: StrategyRules) -> dict | None:
    """An improvement suggestion for the recommended stop, unless the strategy already uses it."""
    if not sweep or sweep.get("recommended_pct") is None:
        return None
    proposed, current = sweep["recommended_pct"], rules.stop_loss_pct_of_capital
    if current == proposed:
        return None
    row = next(r for r in sweep["rows"] if r["stop_pct"] == proposed)
    r_multiple = sweep["risk_reward"]
    return {
        "title": f"Hard stop-loss at {proposed:g}% of capital (Rs {row['stop_inr']:,.0f}), target {r_multiple:g}R",
        "rationale": sweep["reason"],
        "proposed_change": {
            "section": "Exit",
            "field": "stop_loss_pct_of_capital",
            "current": current,
            "proposed": proposed,
        },
        "expected_impact": (
            f"Rs {row['backtest']['net_pnl']:,.0f} net in the backtest and Rs {row['forward']['net_pnl']:,.0f} "
            f"in the forward window on Rs {sweep['capital_inr']:,.0f} capital."
        ),
        "evidence": {
            "metric": "net_pnl_per_drawdown",
            "capital_inr": sweep["capital_inr"],
            "risk_reward": r_multiple,
            "rows": sweep["rows"],
        },
    }
