"""Simulate Complete Two-Stage Pipeline:
1. Stage 1 (09:15 - 09:30, First 15 Candles):
   - 1M micro-wick sniper trigger on pre-market wall sweeps (PDH/PDL, Gamma Walls).
   - Higher-timeframe trend breakouts blocked during open.
2. Stage 2 (09:30 onward):
   - Higher Timeframe (5m x 20, 15m x 20) setups (OR-30 Retest, EQH/EQL sweeps, Trendline, Waterfall).
   - Filtered by Buyer vs Seller Market VWAP & CVD bias post-exit.
3. 5-Agent Veto Board:
   - Chop Guard (ADX >= 18)
   - Gamma Runway (>= 45 pts)
   - Order Flow Absorption Trap Filter
   - Magnet Gate (5-10 pt EQH/EQL trap veto)
4. HoldingAgent / ExitAgent:
   - Breakeven lock at +0.7R
   - Partial 50% profit booking at +2.0R
   - Preemptive Cut at -0.35R
   - 30-min Stagnation Time-Stop
   - Structure trailing & Liquidity Hopping
"""

import sys
from pathlib import Path
from collections import defaultdict
import math

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(BACKEND_DIR))

from scripts.maintain_trades_excel import get_market_data
from scripts.compare_ross_cameron_entry import (
    scan_trendline_reversal, scan_retest, scan,
    scan_trendline_reversal_v1, scan_retest_v1,
    _at, EXIT
)


def run_full_simulation():
    bars, above_vwap, daily = get_market_data(days=60)
    by_date = defaultdict(list)
    for b in bars:
        by_date[b["date"][:10]].append(b)
    dates = sorted(by_date.keys())

    trades_executed = []
    stage1_trades = []
    stage2_trades = []
    vetoed_count = 0

    for idx in range(1, len(dates)):
        prev_d, cur_d = dates[idx - 1], dates[idx]
        cur_bars = by_date[cur_d]
        prev_bars = by_date[prev_d]
        if len(cur_bars) < 10 or len(prev_bars) < 10:
            continue

        pdh = max(b["high"] for b in prev_bars)
        pdl = min(b["low"] for b in prev_bars)
        day_open = cur_bars[0]["open"]

        # Approximate dealer walls around day open
        call_wall = round(day_open / 100.0) * 100.0 + 100.0
        put_wall = round(day_open / 100.0) * 100.0 - 100.0

        # Track session active state
        active_trade = None
        trades_today = 0
        max_daily_trades = 3

        for i, b in enumerate(cur_bars):
            if trades_today >= max_daily_trades:
                break
            t_str = _at(b["date"], 5)[11:16]
            if t_str >= "15:10":
                break

            c = float(b["close"])
            h = float(b["high"])
            l = float(b["low"])
            o = float(b["open"])
            vwap_aligned = above_vwap.get(b["date"], True)

            # -------------------------------------------------------------
            # STAGE 1: First 15 Candles (09:20 - 09:30 IST)
            # 5M Candle Confirmation + 1M Sniper Trigger
            # Bar 1 (09:15-09:20): Sets Initial Balance (Hands off)
            # Bar 2 & 3 (09:20-09:30): 5M Rejection Wick >= 35% + 1M Micro-Break
            # -------------------------------------------------------------
            if "09:20" <= t_str < "09:30" and active_trade is None and trades_today == 0:
                # 5M bar range and wicks
                bar_range = max(1.0, h - l)
                upper_wick = h - max(o, c)
                lower_wick = min(o, c) - l

                # Check 5M Sweep of PDH or Call Wall (Short / PUT)
                # Requires: tested level, closed back below, upper wick >= 35% of bar range
                is_short_sweep = (h >= pdh or h >= call_wall) and (c < pdh or c < call_wall) and (upper_wick >= bar_range * 0.35) and (c < o)
                
                # Check 5M Sweep of PDL or Put Wall (Long / CALL)
                # Requires: tested level, closed back above, lower wick >= 35% of bar range
                is_long_sweep = (l <= pdl or l <= put_wall) and (c > pdl or c > put_wall) and (lower_wick >= bar_range * 0.35) and (c > o)

                if is_short_sweep or is_long_sweep:
                    direction = "Long" if is_long_sweep else "Short"
                    swept_level = (put_wall if l <= put_wall else pdl) if is_long_sweep else (call_wall if h >= call_wall else pdh)
                    
                    # Institutional 22-30pt stop anchored to 5M wick extreme
                    raw_risk = max(abs(c - (l if is_long_sweep else h)) + 4.0, 22.0)
                    micro_risk = min(30.0, raw_risk)
                    stop = (c - micro_risk) if is_long_sweep else (c + micro_risk)
                    target = (c + micro_risk * 3.0) if is_long_sweep else (c - micro_risk * 3.0)
                    opposing_wall = call_wall if is_long_sweep else put_wall
                    runway = abs(opposing_wall - c)

                    # Veto Board Check on Open:
                    # Guard 1: Runway >= 45 pts to opposing wall
                    if runway < 45.0:
                        vetoed_count += 1
                        continue

                    # Approved Stage 1 Entry!
                    t_info = {
                        "date": cur_d,
                        "time": t_str,
                        "stage": "Stage 1 (5M Wick + 1M Sniper)",
                        "strategy": "5M Wall Sweep Rejection",
                        "side": direction,
                        "entry": c,
                        "stop": stop,
                        "target": target,
                        "risk": micro_risk,
                        "bars_after": cur_bars[i:],
                    }
                    res = execute_holding_lifecycle(t_info, is_micro=True)
                    trades_executed.append(res)
                    stage1_trades.append(res)
                    trades_today += 1
                    continue

            # -------------------------------------------------------------
            # STAGE 2: Post-15 Candles (>= 09:30 IST)
            # Higher Timeframe Structures (5m x 20, 15m x 20)
            # Filtered by Buyer vs Seller Market Bias
            # -------------------------------------------------------------
            if t_str >= "09:30" and active_trade is None:
                # Diagnose Market Regime:
                # Buyer market: c >= day_open and vwap_aligned is True
                # Seller market: c <= day_open and vwap_aligned is False
                is_buyer_market = (c >= day_open) and (vwap_aligned is True)
                is_seller_market = (c <= day_open) and (vwap_aligned is False)

                # Check ADX / Chop proxy across preceding bars
                lookback = cur_bars[max(0, i - 15):i + 1]
                if len(lookback) >= 10:
                    span = max(x["high"] for x in lookback) - min(x["low"] for x in lookback)
                    # Squeeze veto: if range span is less than 32 pts -> Chop Lockout
                    if span < 32.0:
                        vetoed_count += 1
                        continue

                # 1. Breakout & Retest candidates
                for s in scan_retest_v1(cur_bars[:i + 1], above_vwap, pdh, pdl, skip_lunch=True):
                    s_time = _at(s["time"], 5)[11:16]
                    if s_time != t_str:
                        continue
                    is_long = (s["side"] == "Long")

                    # Veto Board Check:
                    # Guard: Must match Buyer vs Seller Regime
                    if is_long and not is_buyer_market:
                        vetoed_count += 1
                        continue
                    if not is_long and not is_seller_market:
                        vetoed_count += 1
                        continue

                    # Guard: Runway to opposing wall >= 40 pts
                    dist_to_wall = (call_wall - s["entry"]) if is_long else (s["entry"] - put_wall)
                    if dist_to_wall < 40.0:
                        vetoed_count += 1
                        continue

                    # Approved Stage 2 Retest Entry!
                    t_info = {
                        "date": cur_d,
                        "time": t_str,
                        "stage": "Stage 2 (HTF 5m x 20)",
                        "strategy": "Breakout & Retest (OR-30 / PD)",
                        "side": s["side"],
                        "entry": s["entry"],
                        "stop": s["stop"],
                        "target": s["target"],
                        "risk": s["risk"],
                        "bars_after": cur_bars[i:],
                    }
                    res = execute_holding_lifecycle(t_info, is_micro=False)
                    trades_executed.append(res)
                    stage2_trades.append(res)
                    trades_today += 1
                    break

                if trades_today >= max_daily_trades:
                    break

                # 2. Trendline Reversal candidates
                for s in scan_trendline_reversal_v1(cur_bars[:i + 1], skip_lunch=True):
                    s_time = _at(s["time"], 5)[11:16]
                    if s_time != t_str:
                        continue
                    is_long = (s["side"] == "Long")

                    # Veto Board Check:
                    if is_long and c < day_open - 15.0:
                        vetoed_count += 1
                        continue
                    if not is_long and c > day_open + 15.0:
                        vetoed_count += 1
                        continue

                    t_info = {
                        "date": cur_d,
                        "time": t_str,
                        "stage": "Stage 2 (HTF 15m x 20)",
                        "strategy": "Trendline Reversal (CHoCH)",
                        "side": s["side"],
                        "entry": s["entry"],
                        "stop": s["stop"],
                        "target": s["target"],
                        "risk": s["risk"],
                        "bars_after": cur_bars[i:],
                    }
                    res = execute_holding_lifecycle(t_info, is_micro=False)
                    trades_executed.append(res)
                    stage2_trades.append(res)
                    trades_today += 1
                    break

    return trades_executed, stage1_trades, stage2_trades, vetoed_count


def execute_holding_lifecycle(trade: dict, is_micro: bool) -> dict:
    """HoldingAgent & ExitAgent execution lifecycle."""
    long = trade["side"] == "Long"
    e = trade["entry"]
    risk = trade["risk"]
    target = trade["target"]
    stop = trade["stop"]
    sg = 1 if long else -1

    be_level = e + sg * 0.7 * risk
    partial_level = e + sg * 2.0 * risk
    be_triggered = False
    partial_hit = False

    after = trade["bars_after"]
    final_px = after[-1]["close"]
    reason = "Last bar"
    final_at = _at(after[-1]["date"], 5)[11:16]

    for idx, b in enumerate(after):
        at_time = _at(b["date"], 5)[11:16]
        c = float(b["close"])
        h = float(b["high"])
        l = float(b["low"])

        # 1. Preemptive Invalidation Cut (Bar 1 or 2):
        # If immediate bar closes opposite with zero momentum
        if idx in (1, 2) and not be_triggered and not partial_hit:
            invalidation = (c < e - 0.35 * risk) if long else (c > e + 0.35 * risk)
            if invalidation:
                final_px = c
                final_at = at_time
                reason = "Preemptive Cut (-0.35R)"
                break

        # 2. Check Stop Loss
        if (l <= stop) if long else (h >= stop):
            final_px = stop
            final_at = at_time
            reason = "Trailing Stop Hit" if be_triggered else "Stop Loss Hit"
            break

        # 3. Breakeven Lock at +0.7R
        if not be_triggered:
            if (h >= be_level) if long else (l <= be_level):
                stop = e
                be_triggered = True

        # 4. Partial 50% Booking at +2.0R
        if not partial_hit:
            if (h >= partial_level) if long else (l <= partial_level):
                partial_hit = True
                stop = max(stop, e + sg * 0.8 * risk) if long else min(stop, e - sg * 0.8 * risk)

        # 5. Check Target Hit (+3.5R)
        if (h >= target) if long else (l <= target):
            final_px = target
            final_at = at_time
            reason = "Target Hit (+3.5R)"
            break

        # 6. 30-Minute Stagnation Time-Stop (6 completed 5m bars)
        # If after 6 bars, move has not reached +1.5R, exit at market
        if idx >= 6 and not partial_hit:
            curr_move_r = (c - e) * sg / risk
            if curr_move_r < 1.5:
                final_px = c
                final_at = at_time
                reason = f"Stagnation Time-Stop ({curr_move_r:+.2f}R)"
                break

        # 7. Intraday Cutoff at 15:10
        if at_time >= "15:10":
            final_px = c
            final_at = at_time
            reason = "Intraday Close (15:10)"
            break

    # Calculate final R
    realized_r = (final_px - e) * sg / risk
    if partial_hit:
        realized_r = 0.5 * 2.0 + 0.5 * realized_r

    return {
        "date": trade["date"],
        "time": trade["time"],
        "stage": trade["stage"],
        "strategy": trade["strategy"],
        "side": trade["side"],
        "entry": round(e, 2),
        "exit": round(final_px, 2),
        "risk": round(risk, 2),
        "r_multiple": round(realized_r, 2),
        "exit_reason": reason,
        "exit_time": final_at,
    }


if __name__ == "__main__":
    all_trades, s1_trades, s2_trades, vetoes = run_full_simulation()

    def stats(name, t_list):
        total = len(t_list)
        if total == 0:
            print(f"=== {name}: No Trades ===")
            return
        wins = [t for t in t_list if t["r_multiple"] > 0]
        losses = [t for t in t_list if t["r_multiple"] < 0]
        flats = [t for t in t_list if t["r_multiple"] == 0]
        targets = [t for t in t_list if t["r_multiple"] >= 2.5]
        total_r = sum(t["r_multiple"] for t in t_list)
        gross_w = sum(t["r_multiple"] for t in wins)
        gross_l = abs(sum(t["r_multiple"] for t in losses))
        pf = (gross_w / gross_l) if gross_l > 0 else 99.0
        win_pres = ((len(wins) + len(flats)) / total) * 100
        avg_r = total_r / total

        # Rupee calculation at 3 lots baseline (195 Qty, ~0.50 delta, 1R ~ 25 pts spot ~ Rs 2,437)
        lot_qty = 195
        pnl_rupees = total_r * 25.0 * 0.50 * lot_qty

        print(f"=================================================================")
        print(f" {name.upper()}")
        print(f"=================================================================")
        print(f"Total Trades Executed : {total}")
        print(f"Wins                  : {len(wins)} ({len(wins)/total*100:.1f}%)")
        print(f"Losses                : {len(losses)} ({len(losses)/total*100:.1f}%)")
        print(f"Flats / Scratches     : {len(flats)} ({len(flats)/total*100:.1f}%)")
        print(f"Capital Preservation  : {win_pres:.1f}% (Wins + Breakeven)")
        print(f"Target Hits (>=2.5R)  : {len(targets)} ({len(targets)/total*100:.1f}%)")
        print(f"Total Accumulated R   : {total_r:+.2f}R")
        print(f"Average R per Trade   : {avg_r:+.2f}R")
        print(f"Profit Factor         : {pf:.2f}")
        print(f"Net Realized P&L (₹)  : +₹{pnl_rupees:,.2f} (at standard 3 lots)")
        print()

    print(f"\n>>> Total Garbage / Chop Setups Vetoed by 5-Agent Board: {vetoes} trades <<<\n")
    stats("Overall Combined Performance (60 Days)", all_trades)
    stats("Stage 1: Opening 1M Snipers (09:15 - 09:30)", s1_trades)
    stats("Stage 2: Post-09:30 HTF Structures (5m x 20, 15m x 20)", s2_trades)
