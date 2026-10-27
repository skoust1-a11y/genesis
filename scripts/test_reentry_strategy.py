"""Backtest Script: Testing Ross Cameron Re-Entry Rule.

Rule:
- If a trade gets stopped out or cut early, but the setup level is still intact and a
  subsequent candle confirmation forms later in the session:
  -> TAKE THE RE-ENTRY (Attempt 2).
- Maximum 1 re-entry per setup per day (Max 2 trades).
"""

import sys
from pathlib import Path
from collections import defaultdict

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(BACKEND_DIR))

from app.liquidity_signals import (
    scan,
    scan_retest,
    scan_trendline_reversal,
    staged_exit_of,
    _at,
    MIN_RISK,
    R_MULT,
    LAST_ENTRY,
    FIRST_ENTRY,
    BLACKOUT_START,
    BLACKOUT_END,
)
from scripts.maintain_trades_excel import get_market_data


def scan_retest_with_reentry(bars, above_vwap, pdh, pdl, max_entries=2):
    """Scan Breakout & Retest with up to max_entries (Initial + Re-entry)."""
    day_open = bars[0]["open"]
    signals = []
    
    for side in ("Long", "Short"):
        long = side == "Long"
        level = pdh if long else pdl
        stage = 0
        retest_extreme = 0.0
        active_stop_time = ""
        entries_taken = 0
        
        for i, b in enumerate(bars):
            if entries_taken >= max_entries:
                break
            closes_at = _at(b["date"], 5)
            if closes_at[11:] > LAST_ENTRY:
                break
            if active_stop_time and b["date"] <= active_stop_time:
                continue

            c = b["close"]
            if stage == 0:
                if (long and b["high"] >= level + 2.0 and c > day_open) or (
                    not long and b["low"] <= level - 2.0 and c < day_open
                ):
                    stage = 1
                continue

            if stage == 1:
                is_pullback = (long and b["low"] <= level + 15.0 and b["low"] >= level - 25.0) or (
                    not long and b["high"] >= level - 15.0 and b["high"] <= level + 25.0
                )
                if is_pullback:
                    stage = 2
                    retest_extreme = b["low"] if long else b["high"]
                continue

            if stage == 2:
                retest_extreme = min(retest_extreme, b["low"]) if long else max(retest_extreme, b["high"])
                confirmed = (
                    long and c > level and c > b["open"] and above_vwap.get(b["date"]) is True and c > day_open
                ) or (
                    not long and c < level and c < b["open"] and above_vwap.get(b["date"]) is False and c < day_open
                )
                if not confirmed:
                    continue
                if closes_at[11:] < FIRST_ENTRY:
                    continue

                stop = (retest_extreme - 5.0) if long else (retest_extreme + 5.0)
                risk = abs(c - stop)
                if risk >= MIN_RISK:
                    target = c + R_MULT * risk if long else c - R_MULT * risk
                    is_reentry = entries_taken > 0
                    sig = {
                        "side": side,
                        "strategy": "retest",
                        "pool": f"PD{'H Support' if long else 'L Resistance'} Flip" + (" (Re-Entry)" if is_reentry else ""),
                        "time": closes_at,
                        "entry": c,
                        "stop": round(stop, 2),
                        "target": round(target, 2),
                        "risk": round(risk, 2),
                        "level": level,
                        "is_reentry": is_reentry,
                        "attempt": entries_taken + 1,
                    }
                    signals.append(sig)
                    entries_taken += 1
                    
                    # Evaluate when this trade exits to know when re-entry is eligible
                    res = staged_exit_of(bars, sig)
                    if res["r_multiple"] <= 0:  # Only re-enter if stopped out or flat
                        active_stop_time = res["exit_time"]
                        stage = 1  # Reset stage to look for fresh retest/bounce
                    else:
                        break  # Win achieved, no need to re-enter
    return signals


def scan_trendline_with_reentry(bars, min_slope=0.5, max_entries=2):
    """Scan Trendline Reversal with up to max_entries (Initial + Re-entry)."""
    if len(bars) < 10:
        return []
    signals = []

    for side in ("Short", "Long"):
        long = side == "Long"
        key = "high" if long else "low"
        entries_taken = 0
        active_stop_time = ""

        for i in range(5, len(bars)):
            if entries_taken >= max_entries:
                break
            b = bars[i]
            closes_at = _at(b["date"], 5)
            time_str = closes_at[11:]
            if time_str > LAST_ENTRY:
                break
            if time_str < FIRST_ENTRY:
                continue
            if active_stop_time and b["date"] <= active_stop_time:
                continue

            c = b["close"]
            piv = []
            for j in range(2, i - 1):
                if long and bars[j]["high"] > bars[j - 1]["high"] and bars[j]["high"] > bars[j + 1]["high"]:
                    piv.append(j)
                elif not long and bars[j]["low"] < bars[j - 1]["low"] and bars[j]["low"] < bars[j + 1]["low"]:
                    piv.append(j)

            if len(piv) < 2:
                continue

            x1, x2 = piv[-2], piv[-1]
            if x2 - x1 < 4:
                continue
            y1, y2 = bars[x1][key], bars[x2][key]
            slope = (y2 - y1) / (x2 - x1)

            triggered = False
            if not long and slope >= min_slope:
                tl_val = y1 + slope * (i - x1)
                tl_prev = y1 + slope * (i - 1 - x1)
                if c < tl_val and bars[i - 1]["close"] >= tl_prev - 2.0 and c < b["open"]:
                    stop = max(b["high"], max(bars[k]["high"] for k in range(max(0, i - 2), i + 1))) + 5.0
                    risk = stop - c
                    if MIN_RISK <= risk <= 65.0:
                        target = c - R_MULT * risk
                        is_reentry = entries_taken > 0
                        sig = {
                            "side": "Short",
                            "strategy": "trendline_reversal",
                            "pool": "Trendline Breakdown" + (" (Re-Entry)" if is_reentry else ""),
                            "time": closes_at,
                            "entry": c,
                            "stop": round(stop, 2),
                            "target": round(target, 2),
                            "risk": round(risk, 2),
                            "level": round(tl_val, 2),
                            "is_reentry": is_reentry,
                            "attempt": entries_taken + 1,
                        }
                        signals.append(sig)
                        entries_taken += 1
                        res = staged_exit_of(bars, sig)
                        if res["r_multiple"] <= 0:
                            active_stop_time = res["exit_time"]
                        else:
                            break

            elif long and slope <= -min_slope:
                tl_val = y1 + slope * (i - x1)
                tl_prev = y1 + slope * (i - 1 - x1)
                if c > tl_val and bars[i - 1]["close"] <= tl_prev + 2.0 and c > b["open"]:
                    stop = min(b["low"], min(bars[k]["low"] for k in range(max(0, i - 2), i + 1))) - 5.0
                    risk = c - stop
                    if MIN_RISK <= risk <= 65.0:
                        target = c + R_MULT * risk
                        is_reentry = entries_taken > 0
                        sig = {
                            "side": "Long",
                            "strategy": "trendline_reversal",
                            "pool": "Trendline Breakout" + (" (Re-Entry)" if is_reentry else ""),
                            "time": closes_at,
                            "entry": c,
                            "stop": round(stop, 2),
                            "target": round(target, 2),
                            "risk": round(risk, 2),
                            "level": round(tl_val, 2),
                            "is_reentry": is_reentry,
                            "attempt": entries_taken + 1,
                        }
                        signals.append(sig)
                        entries_taken += 1
                        res = staged_exit_of(bars, sig)
                        if res["r_multiple"] <= 0:
                            active_stop_time = res["exit_time"]
                        else:
                            break
    return signals


def run_reentry_test():
    bars, above, daily = get_market_data(days=60)
    by_date = defaultdict(list)
    for b in bars:
        by_date[b["date"][:10]].append(b)
    dates = sorted(by_date.keys())

    baseline_trades = []
    reentry_trades = []

    for idx in range(1, len(dates)):
        prev_d, cur_d = dates[idx - 1], dates[idx]
        cur_bars = by_date[cur_d]
        prev_bars = by_date[prev_d]
        pdh = max(b["high"] for b in prev_bars)
        pdl = min(b["low"] for b in prev_bars)

        is_today = cur_d == dates[-1]
        eval_bars = cur_bars
        eval_above = above
        bar_mins = 5
        if is_today:
            try:
                from scripts.live_scanner import fetch_timeframe_data
                bars_1m, above_1m = fetch_timeframe_data("1m", cur_d)
                if bars_1m:
                    eval_bars = bars_1m
                    eval_above = above_1m
                    bar_mins = 1
            except Exception:
                pass

        # Baseline (Max 1 entry per setup)
        for s in scan_trendline_reversal(eval_bars, skip_lunch=False, bar_minutes=bar_mins):
            res = staged_exit_of(eval_bars, s, bar_minutes=bar_mins)
            baseline_trades.append((s, res))
        for s in scan_retest(eval_bars, eval_above, pdh, pdl, skip_lunch=False, bar_minutes=bar_mins):
            res = staged_exit_of(eval_bars, s, bar_minutes=bar_mins)
            baseline_trades.append((s, res))

        # Re-entry Engine (Allows 1 Re-entry if stopped out)
        for s in scan_trendline_reversal(eval_bars, skip_lunch=False, allow_reentry=True, bar_minutes=bar_mins):
            res = staged_exit_of(eval_bars, s, bar_minutes=bar_mins)
            reentry_trades.append((s, res))
        for s in scan_retest(eval_bars, eval_above, pdh, pdl, skip_lunch=False, allow_reentry=True, bar_minutes=bar_mins):
            res = staged_exit_of(eval_bars, s, bar_minutes=bar_mins)
            reentry_trades.append((s, res))

    def stats(trades):
        total = len(trades)
        wins = [t for t in trades if t[1]["r_multiple"] > 0]
        targets = [t for t in trades if t[1]["r_multiple"] >= 3.0 or "Target" in t[1]["exit_reason"]]
        losses = [t for t in trades if t[1]["r_multiple"] <= -1.0 or "Stop" in t[1]["exit_reason"]]
        total_r = sum(t[1]["r_multiple"] for t in trades)
        gross_p = sum(t[1]["r_multiple"] for t in trades if t[1]["r_multiple"] > 0)
        gross_l = abs(sum(t[1]["r_multiple"] for t in trades if t[1]["r_multiple"] < 0))
        pf = (gross_p / gross_l) if gross_l > 0 else 999.0
        return {
            "total": total,
            "wins": len(wins),
            "losses": len(losses),
            "targets": len(targets),
            "win_rate": round(len(wins) / total * 100, 1) if total else 0,
            "target_rate": round(len(targets) / total * 100, 1) if total else 0,
            "total_r": round(total_r, 2),
            "avg_r": round(total_r / total, 2) if total else 0,
            "pf": round(pf, 2),
        }

    s_base = stats(baseline_trades)
    s_re = stats(reentry_trades)

    # Analyze Re-entry Trades specifically
    reentries_only = [t for t in reentry_trades if t[0].get("is_reentry")]
    s_only_re = stats(reentries_only)

    print("================== RE-ENTRY BACKTEST COMPARISON (60 DAYS) ==================")
    print(f"{'Metric':<25} | {'Single Entry Only':<22} | {'With Re-Entry Allowed':<22}")
    print("-" * 75)
    print(f"{'Total Trades':<25} | {s_base['total']:<22} | {s_re['total']:<22}")
    print(f"{'Win Rate (%)':<25} | {s_base['win_rate']}%{'':<17} | {s_re['win_rate']}%{'':<17}")
    print(f"{'Target Hit Rate (%)':<25} | {s_base['target_rate']}%{'':<17} | {s_re['target_rate']}%{'':<17}")
    print(f"{'Total R-Multiple':<25} | {s_base['total_r']:+0.2f}R{'':<15} | {s_re['total_r']:+0.2f}R{'':<15}")
    print(f"{'Avg R per Trade':<25} | {s_base['avg_r']:+0.2f}R{'':<15} | {s_re['avg_r']:+0.2f}R{'':<15}")
    print(f"{'Profit Factor':<25} | {s_base['pf']:<22} | {s_re['pf']:<22}")
    print("============================================================================")
    print("\n--- STANDALONE PERFORMANCE OF 2ND ATTEMPTS (RE-ENTRIES ONLY) ---")
    print(f"Total Re-Entry Trades Taken : {s_only_re['total']}")
    print(f"Re-Entry Win Rate           : {s_only_re['win_rate']}%")
    print(f"Re-Entry Total R Net Gain   : {s_only_re['total_r']:+0.2f}R")
    print(f"Re-Entry Profit Factor      : {s_only_re['pf']}")
    print("----------------------------------------------------------------")


if __name__ == "__main__":
    run_reentry_test()
