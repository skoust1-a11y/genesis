"""Backtest Script: Comparing Hard Mathematical Exits vs Chart-Based Exits on NIFTY 5m.

Comparison:
1. Hard Exit (Current Baseline):
   - SL hit immediately if low/high touches stop level (wick stop).
   - Target hit immediately on price touch.
2. Chart-Based Exit:
   - Stop Loss is confirmed on CANDLE CLOSE (b['close'] <= stop for Long, b['close'] >= stop for Short).
   - Emergency Catastrophic SL at stop - 15 pts (in case of flash breakdown).
   - Momentum Reversal on Chart: Once in profit, exit if 5m candle closes against the trend with a strong reversal body.
   - Target Scaling: Book at resistance when a rejection candle appears.
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
    EXIT,
)
from scripts.maintain_trades_excel import get_market_data


def chart_based_exit_of(
    day: list[dict],
    s: dict,
    partial_r: float = 2.0,
    partial_pct: float = 0.5,
    be_r: float = 1.0,
    emergency_buffer: float = 15.0,  # Catastrophic wick buffer beyond stop
) -> dict:
    """Chart-Confirmed Exit Engine:
    1. Evaluates Stop on CANDLE CLOSE: Only exits if 5m candle actually closes beyond the stop level!
       (Filters out intraday fakeout wicks / stop hunts).
    2. Emergency Hard Stop: Only triggers intra-candle if price plunges emergency_buffer pts beyond stop.
    3. Chart-based Breakeven: Once price closes above +1R, SL moves to entry on chart.
    4. Chart-based Profit Taking: Partial 50% at +2R.
    5. Chart Reversal Trail: After +1.5R, exit remaining position if a confirmed 5m candle closes
       below the prior candle's low (Long) or above prior candle's high (Short).
    6. Time Exit: 15:10 IST.
    """
    long, e, risk = s["side"] == "Long", s["entry"], s["risk"]
    sg = 1 if long else -1
    stop = s["stop"]
    be_level = e + sg * be_r * risk
    partial_level = e + sg * partial_r * risk
    final_target = s["target"]
    catastrophic_stop = stop - sg * emergency_buffer

    partial_hit = False
    partial_at = None
    partial_px = None
    be_triggered = False

    after = [b for b in day if b["date"] >= s["time"]]
    if not after:
        after = [day[-1]]

    reason = "Last bar"
    final_px = day[-1]["close"]
    final_at = _at(day[-1]["date"], 5)

    for idx, b in enumerate(after):
        at = _at(b["date"], 5)
        c = b["close"]

        # 1. Emergency Catastrophic Stop (Flash crash protection)
        if (b["low"] <= catastrophic_stop) if long else (b["high"] >= catastrophic_stop):
            final_px = catastrophic_stop
            final_at = at
            reason = "Emergency Hard Stop"
            break

        # 2. Chart-Confirmed Stop on Candle CLOSE (Filters out stop-hunt wicks)
        candle_closed_beyond_stop = (c <= stop) if long else (c >= stop)
        if candle_closed_beyond_stop:
            final_px = c
            final_at = at
            if be_triggered or partial_hit:
                reason = "Chart Close Breakeven/Trail Stop"
            else:
                reason = "Chart Close Invalidation"
            break

        # 3. Partial Target Hit (+2R)
        if not partial_hit:
            if (b["high"] >= partial_level) if long else (b["low"] <= partial_level):
                partial_hit = True
                partial_px = partial_level
                partial_at = at
                stop = max(stop, be_level) if long else min(stop, be_level)
                be_triggered = True

        # 4. Final Target Hit (+3R)
        if (b["high"] >= final_target) if long else (b["low"] <= final_target):
            final_px = final_target
            final_at = at
            reason = "Target (3R)"
            break

        # 5. Time Exit (15:10 IST)
        if at[11:] >= EXIT:
            final_px = c
            final_at = at
            reason = "Time Exit (15:10)"
            break

        # 6. Chart Breakeven Trigger: Once candle touches +1R, move stop to entry for subsequent bars
        if not be_triggered:
            if (b["high"] >= be_level) if long else (b["low"] <= be_level):
                stop = max(stop, e) if long else min(stop, e)
                be_triggered = True

        # 7. Chart Structure Reversal: Once +1.5R has been reached, check if candle closes against trend
        reached_1_5r = (b["high"] >= e + sg * 1.5 * risk) if long else (b["low"] <= e - sg * 1.5 * risk)
        if (be_triggered or partial_hit) and reached_1_5r and idx > 0:
            prev_b = after[idx - 1]
            reversal = (c < prev_b["low"] and c < b["open"]) if long else (c > prev_b["high"] and c > b["open"])
            if reversal:
                final_px = c
                final_at = at
                reason = "Chart Structure Reversal Exit"
                break

    # Realized R calculation
    final_leg_r = (final_px - e) * sg / risk
    if partial_hit:
        total_r = partial_pct * partial_r + (1.0 - partial_pct) * final_leg_r
    else:
        total_r = final_leg_r

    return {
        "exit_reason": reason,
        "exit_price": round(final_px, 2),
        "exit_time": final_at,
        "r_multiple": round(total_r, 2),
    }


def run_comparison():
    bars, above, daily = get_market_data(days=60)
    by_date = defaultdict(list)
    for b in bars:
        by_date[b["date"][:10]].append(b)
    dates = sorted(by_date.keys())

    hard_trades = []
    chart_trades = []

    for idx in range(1, len(dates)):
        prev_d, cur_d = dates[idx - 1], dates[idx]
        cur_bars = by_date[cur_d]
        prev_bars = by_date[prev_d]
        pdh = max(b["high"] for b in prev_bars)
        pdl = min(b["low"] for b in prev_bars)

        # Collect all signals
        day_signals = []
        for s in scan_trendline_reversal(cur_bars, skip_lunch=True):
            day_signals.append(("Trendline Reversal", s))
        for s in scan_retest(cur_bars, above, pdh, pdl, skip_lunch=True):
            day_signals.append(("Breakout & Retest", s))
        for s in scan(cur_bars, above, pdh, pdl, skip_lunch=True):
            day_signals.append(("Liquidity Sweep", s))

        for strat, sig in day_signals:
            # 1. Hard Exit
            res_hard = staged_exit_of(cur_bars, sig)
            hard_trades.append((strat, sig, res_hard))

            # 2. Chart-Based Exit
            res_chart = chart_based_exit_of(cur_bars, sig)
            chart_trades.append((strat, sig, res_chart))

    def compute_stats(trades):
        total = len(trades)
        wins = [t for t in trades if t[2]["r_multiple"] > 0]
        targets = [t for t in trades if t[2]["r_multiple"] >= 3.0 or "Target" in t[2]["exit_reason"]]
        losses = [t for t in trades if t[2]["r_multiple"] <= -1.0 or "Stop" in t[2]["exit_reason"]]
        total_r = sum(t[2]["r_multiple"] for t in trades)
        gross_p = sum(t[2]["r_multiple"] for t in trades if t[2]["r_multiple"] > 0)
        gross_l = abs(sum(t[2]["r_multiple"] for t in trades if t[2]["r_multiple"] < 0))
        pf = (gross_p / gross_l) if gross_l > 0 else 999.0
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
        }

    s_hard = compute_stats(hard_trades)
    s_chart = compute_stats(chart_trades)

    print("================== HARD EXIT VS CHART-BASED EXIT (60 DAYS) ==================")
    print(f"{'Metric':<25} | {'Hard Wick Exit (Current)':<24} | {'Chart-Based Exit':<24}")
    print("-" * 78)
    print(f"{'Total Trades':<25} | {s_hard['total']:<24} | {s_chart['total']:<24}")
    print(f"{'Win Rate (%)':<25} | {s_hard['win_rate']}%{'':<19} | {s_chart['win_rate']}%{'':<19}")
    print(f"{'Target Hit Rate (%)':<25} | {s_hard['target_rate']}%{'':<19} | {s_chart['target_rate']}%{'':<19}")
    print(f"{'Total R-Multiple':<25} | {s_hard['total_r']:+0.2f}R{'':<17} | {s_chart['total_r']:+0.2f}R{'':<17}")
    print(f"{'Avg R per Trade':<25} | {s_hard['avg_r']:+0.2f}R{'':<17} | {s_chart['avg_r']:+0.2f}R{'':<17}")
    print(f"{'Profit Factor':<25} | {s_hard['pf']:<24} | {s_chart['pf']:<24}")
    print("=============================================================================")

    # Breakdown by strategy
    for strat in ("Liquidity Sweep", "Trendline Reversal", "Breakout & Retest"):
        th = [t for t in hard_trades if t[0] == strat]
        tc = [t for t in chart_trades if t[0] == strat]
        sh = compute_stats(th)
        sc = compute_stats(tc)
        print(f"\n--- {strat.upper()} ---")
        print(f"{'Metric':<25} | {'Hard Wick Exit':<20} | {'Chart-Based Exit':<20}")
        print("-" * 70)
        print(f"{'Win Rate (%)':<25} | {sh['win_rate']}%{'':<15} | {sc['win_rate']}%{'':<15}")
        print(f"{'Total R-Multiple':<25} | {sh['total_r']:+0.2f}R{'':<13} | {sc['total_r']:+0.2f}R{'':<13}")
        print(f"{'Profit Factor':<25} | {sh['pf']:<20} | {sc['pf']:<20}")


if __name__ == "__main__":
    run_comparison()
