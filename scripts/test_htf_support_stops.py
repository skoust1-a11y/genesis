"""Benchmark Comparison Script: Baseline vs HTF Structural Support Stops.

Evaluates trade metrics:
1. On 1-Minute Micro-Execution (5-Day High-Resolution 1M Dataset)
2. On 5-Minute Institutional Structure (60-Day Full Dataset)

Metrics Compared:
- Total Trades
- Win Rate (%)
- Target Hit Rate (%)
- Total R-Multiple (+R)
- Average R per Trade
- Profit Factor
- Average Risk (Index Points)
- Average Option Risk (Points & % of Option Premium)
"""

import sys
from pathlib import Path
from collections import defaultdict
from datetime import datetime

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(BACKEND_DIR))

from app.api.market import _chart, parse_yahoo_chart
from app.liquidity_signals import (
    INDEX,
    scan_retest,
    scan_trendline_reversal,
    staged_exit_of,
    option_plan,
    _at,
    MIN_RISK,
    R_MULT,
    FIRST_ENTRY,
    LAST_ENTRY,
)
from app.config import get_settings
from scripts.maintain_trades_excel import get_market_data


def scan_retest_custom(
    bars: list[dict],
    above_vwap: dict[str, bool | None],
    pdh: float,
    pdl: float,
    stop_mode: str = "baseline",  # "baseline", "htf_support", "trigger_candle"
    bar_minutes: int = 5,
    allow_reentry: bool = True,
) -> list[dict]:
    """Custom scan_retest with parameterized stop loss calculation."""
    if not bars:
        return []
    day_open = bars[0]["open"]
    out = []
    min_risk = 10.0 if bar_minutes <= 2 else MIN_RISK

    for side in ("Long", "Short"):
        long = side == "Long"
        level = pdh if long else pdl
        stage, retest_extreme, why = 0, 0.0, []
        entries_taken = 0
        active_stop_time = ""
        stop_extreme = 0.0
        stop_bar_idx = -1
        max_entries = 2 if allow_reentry else 1

        for i, b in enumerate(bars):
            if entries_taken >= max_entries:
                break
            c = b["close"]
            closes_at = _at(b["date"], bar_minutes)
            if closes_at[11:] > LAST_ENTRY:
                break

            cooldown_bars = max(1, 15 // bar_minutes)
            if active_stop_time:
                if b["date"] <= active_stop_time or (i - stop_bar_idx) < cooldown_bars:
                    continue
                if "13:00" <= closes_at[11:] <= "13:30":
                    continue

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

                is_reentry = entries_taken > 0
                if is_reentry:
                    if long and b["low"] <= stop_extreme:
                        continue
                    if not long and b["high"] >= stop_extreme:
                        continue

                if closes_at[11:] < FIRST_ENTRY:
                    continue
                if "11:45" <= closes_at[11:] <= "12:15":
                    continue

                # Stop calculation modes
                if stop_mode == "baseline":
                    stop = (retest_extreme - 5.0) if long else (retest_extreme + 5.0)
                    basis = f"deep retest extreme {'-' if long else '+'} 5"
                elif stop_mode == "htf_support":
                    # HTF Structural Support: Anchor directly to flipped level (PDH/PDL) or 5m window low
                    htf_bars = bars[max(0, i - 4) : i + 1]
                    htf_extreme = min(x["low"] for x in htf_bars) if long else max(x["high"] for x in htf_bars)
                    if long:
                        stop = max(level - 5.0, htf_extreme - 5.0)
                    else:
                        stop = min(level + 5.0, htf_extreme + 5.0)
                    basis = "HTF 5m structural support level"
                elif stop_mode == "trigger_candle":
                    # Trigger candle extreme - 5
                    stop = (b["low"] - 5.0) if long else (b["high"] + 5.0)
                    basis = "trigger candle extreme - 5"

                risk = abs(c - stop)
                if risk >= min_risk:
                    target = c + R_MULT * risk if long else c - R_MULT * risk
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
                        "stop_basis": basis,
                        "candle": b,
                        "day_open": day_open,
                        "is_reentry": is_reentry,
                        "attempt": entries_taken + 1,
                    }
                    out.append(sig)
                    entries_taken += 1
                    if allow_reentry and entries_taken < max_entries:
                        res = staged_exit_of(bars, sig, bar_minutes=bar_minutes)
                        if res["r_multiple"] <= 0:
                            active_stop_time = res["exit_time"]
                            stop_bar_idx = next((k for k, x in enumerate(bars) if x["date"] >= active_stop_time), i)
                            stop_extreme = min(x["low"] for x in bars[i : stop_bar_idx + 1]) if long else max(x["high"] for x in bars[i : stop_bar_idx + 1])
                            stage = 1
                        else:
                            entries_taken = max_entries
                            break
                    else:
                        entries_taken = max_entries
                        break
    return out


def calc_stats(trades):
    if not trades:
        return {
            "total": 0, "wins": 0, "losses": 0, "targets": 0,
            "win_rate": 0.0, "target_rate": 0.0, "total_r": 0.0,
            "avg_r": 0.0, "pf": 0.0, "avg_risk_pts": 0.0,
        }
    total = len(trades)
    wins = [t for t in trades if t[1]["r_multiple"] > 0]
    targets = [t for t in trades if t[1]["r_multiple"] >= 3.0 or "Target" in t[1]["exit_reason"]]
    losses = [t for t in trades if t[1]["r_multiple"] <= -1.0 or "Stop" in t[1]["exit_reason"]]
    total_r = sum(t[1]["r_multiple"] for t in trades)
    gross_p = sum(t[1]["r_multiple"] for t in trades if t[1]["r_multiple"] > 0)
    gross_l = abs(sum(t[1]["r_multiple"] for t in trades if t[1]["r_multiple"] < 0))
    pf = (gross_p / gross_l) if gross_l > 0 else 999.0
    avg_risk = sum(t[0]["risk"] for t in trades) / total
    return {
        "total": total,
        "wins": len(wins),
        "losses": len(losses),
        "targets": len(targets),
        "win_rate": round(len(wins) / total * 100, 1),
        "target_rate": round(len(targets) / total * 100, 1),
        "total_r": round(total_r, 2),
        "avg_r": round(total_r / total, 2),
        "pf": round(pf, 2),
        "avg_risk_pts": round(avg_risk, 1),
    }


def run_benchmark():
    print("=" * 80)
    print("       BENCHMARK: BASELINE DEEP RETEST STOP vs HTF SUPPORT STOP")
    print("=" * 80)

    # 1. TEST ON 1-MINUTE HIGH RESOLUTION DATASET (5 Days: Oct 21 - Oct 27)
    print("\n--- TEST 1: 1-MINUTE MICRO-EXECUTION DATASET (5 TRADING SESSIONS) ---")
    bars_1m = parse_yahoo_chart(_chart(INDEX, "1m", "5d"))
    by_date_1m = defaultdict(list)
    for b in bars_1m:
        by_date_1m[b["date"][:10]].append(b)
    dates_1m = sorted(by_date_1m.keys())

    # Get daily PDH/PDL from 5m data
    bars_5m, above_5m, _ = get_market_data(days=15)
    by_date_5m = defaultdict(list)
    for b in bars_5m:
        by_date_5m[b["date"][:10]].append(b)

    trades_1m_base = []
    trades_1m_htf = []
    trades_1m_trig = []

    for d in dates_1m:
        cur_bars = by_date_1m[d]
        if not cur_bars:
            continue
        # Find previous trading date in 5m data
        prev_dates = sorted(k for k in by_date_5m.keys() if k < d)
        if not prev_dates:
            continue
        prev_bars = by_date_5m[prev_dates[-1]]
        pdh = max(b["high"] for b in prev_bars)
        pdl = min(b["low"] for b in prev_bars)

        # Above VWAP proxy on 1m
        above_1m = {b["date"]: b["close"] > cur_bars[0]["open"] for b in cur_bars}

        # Baseline
        for s in scan_retest_custom(cur_bars, above_1m, pdh, pdl, stop_mode="baseline", bar_minutes=1):
            res = staged_exit_of(cur_bars, s, bar_minutes=1)
            trades_1m_base.append((s, res))

        # HTF Support
        for s in scan_retest_custom(cur_bars, above_1m, pdh, pdl, stop_mode="htf_support", bar_minutes=1):
            res = staged_exit_of(cur_bars, s, bar_minutes=1)
            trades_1m_htf.append((s, res))

        # Trigger Candle
        for s in scan_retest_custom(cur_bars, above_1m, pdh, pdl, stop_mode="trigger_candle", bar_minutes=1):
            res = staged_exit_of(cur_bars, s, bar_minutes=1)
            trades_1m_trig.append((s, res))

    s_1m_base = calc_stats(trades_1m_base)
    s_1m_htf = calc_stats(trades_1m_htf)
    s_1m_trig = calc_stats(trades_1m_trig)

    print(f"{'Metric':<24} | {'1M Baseline (Deep SL)':<22} | {'1M HTF Support Anchor':<22} | {'1M Trigger Candle SL':<20}")
    print("-" * 96)
    print(f"{'Total Trades':<24} | {s_1m_base['total']:<22} | {s_1m_htf['total']:<22} | {s_1m_trig['total']:<20}")
    print(f"{'Win Rate (%)':<24} | {s_1m_base['win_rate']}%{'':<17} | {s_1m_htf['win_rate']}%{'':<17} | {s_1m_trig['win_rate']}%{'':<15}")
    print(f"{'Target Hit Rate (%)':<24} | {s_1m_base['target_rate']}%{'':<17} | {s_1m_htf['target_rate']}%{'':<17} | {s_1m_trig['target_rate']}%{'':<15}")
    print(f"{'Total R Net Gain':<24} | {s_1m_base['total_r']:+0.2f}R{'':<15} | {s_1m_htf['total_r']:+0.2f}R{'':<15} | {s_1m_trig['total_r']:+0.2f}R{'':<13}")
    print(f"{'Avg R per Trade':<24} | {s_1m_base['avg_r']:+0.2f}R{'':<15} | {s_1m_htf['avg_r']:+0.2f}R{'':<15} | {s_1m_trig['avg_r']:+0.2f}R{'':<13}")
    print(f"{'Profit Factor':<24} | {s_1m_base['pf']:<22} | {s_1m_htf['pf']:<22} | {s_1m_trig['pf']:<20}")
    print(f"{'Avg Index Risk (pts)':<24} | {s_1m_base['avg_risk_pts']} pts{'':<14} | {s_1m_htf['avg_risk_pts']} pts{'':<14} | {s_1m_trig['avg_risk_pts']} pts{'':<12}")

    # 2. TEST ON 5-MINUTE DATASET (60 Days Full History)
    print("\n--- TEST 2: 5-MINUTE INSTITUTIONAL DATASET (60 DAYS FULL HISTORY) ---")
    all_5m, above_60d, _ = get_market_data(days=60)
    by_date_60d = defaultdict(list)
    for b in all_5m:
        by_date_60d[b["date"][:10]].append(b)
    dates_60d = sorted(by_date_60d.keys())

    trades_5m_base = []
    trades_5m_htf = []

    for idx in range(1, len(dates_60d)):
        prev_d, cur_d = dates_60d[idx - 1], dates_60d[idx]
        cur_bars = by_date_60d[cur_d]
        prev_bars = by_date_60d[prev_d]
        pdh = max(b["high"] for b in prev_bars)
        pdl = min(b["low"] for b in prev_bars)

        # Baseline (Deep Retest Extreme)
        for s in scan_retest_custom(cur_bars, above_60d, pdh, pdl, stop_mode="baseline", bar_minutes=5):
            res = staged_exit_of(cur_bars, s, bar_minutes=5)
            trades_5m_base.append((s, res))

        # HTF Support (Anchored directly to flipped PDH/PDL level)
        for s in scan_retest_custom(cur_bars, above_60d, pdh, pdl, stop_mode="htf_support", bar_minutes=5):
            res = staged_exit_of(cur_bars, s, bar_minutes=5)
            trades_5m_htf.append((s, res))

    s_5m_base = calc_stats(trades_5m_base)
    s_5m_htf = calc_stats(trades_5m_htf)

    print(f"{'Metric':<25} | {'5M Baseline (Deep SL)':<22} | {'5M HTF Structural Level':<22}")
    print("-" * 75)
    print(f"{'Total Trades':<25} | {s_5m_base['total']:<22} | {s_5m_htf['total']:<22}")
    print(f"{'Win Rate (%)':<25} | {s_5m_base['win_rate']}%{'':<17} | {s_5m_htf['win_rate']}%{'':<17}")
    print(f"{'Target Hit Rate (%)':<25} | {s_5m_base['target_rate']}%{'':<17} | {s_5m_htf['target_rate']}%{'':<17}")
    print(f"{'Total R Net Gain':<25} | {s_5m_base['total_r']:+0.2f}R{'':<15} | {s_5m_htf['total_r']:+0.2f}R{'':<15}")
    print(f"{'Avg R per Trade':<25} | {s_5m_base['avg_r']:+0.2f}R{'':<15} | {s_5m_htf['avg_r']:+0.2f}R{'':<15}")
    print(f"{'Profit Factor':<25} | {s_5m_base['pf']:<22} | {s_5m_htf['pf']:<22}")
    print(f"{'Avg Index Risk (pts)':<25} | {s_5m_base['avg_risk_pts']} pts{'':<14} | {s_5m_htf['avg_risk_pts']} pts{'':<14}")
    print("=" * 80)


if __name__ == "__main__":
    run_benchmark()
