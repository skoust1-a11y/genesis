"""Comparison Script: V0 Baseline vs V1 Ross Cameron Candle Entry & Exit Rules.

Compares trade metrics across all 3 strategies on historical market data:
- Total Trades
- Win Rate (%)
- Target Hit (3R) Rate (%)
- Total R Accumulated
- Average R per Trade
- Profit Factor
"""

import sys
from pathlib import Path
from collections import defaultdict
from datetime import date, datetime

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(BACKEND_DIR))

from app.config import get_settings
from app.liquidity_signals import (
    scan,
    scan_retest,
    scan_trendline_reversal,
    staged_exit_of,
    weekly_expiry,
    estimated_plan,
    to_15m,
    pivots,
    _at,
    _bucket,
    FIRST_ENTRY,
    LAST_ENTRY,
    BLACKOUT_START,
    BLACKOUT_END,
    MIN_RISK,
    R_MULT,
    EXIT,
)
from scripts.maintain_trades_excel import get_market_data


def rc_staged_exit_of(
    day: list[dict],
    s: dict,
    partial_r: float = 2.0,
    partial_pct: float = 0.5,
    be_r: float = 1.0,
    quick_cut_bars: int = 2,
) -> dict:
    """Ross Cameron Exit Engine:
    1. Quick Invalidation Cut: Within first 2 bars, if bar breaks below entry candle low with 0 progress, cut trade early.
    2. Breakeven at +1R: Once +1R is hit, move SL to entry price.
    3. Partial 50% Profit at +2R.
    4. Candle-by-Candle Trailing Stop: Once +1R or partial is hit, SL ratchets up bar-by-bar
       to the previous candle's low (Long) or previous candle's high (Short).
    5. Final Target at +3R (or trail until candle breakdown).
    """
    long, e, risk = s["side"] == "Long", s["entry"], s["risk"]
    sg = 1 if long else -1
    stop = s["stop"]
    be_level = e + sg * be_r * risk
    partial_level = e + sg * partial_r * risk
    final_target = s["target"]

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

    entry_candle = after[0]

    for idx, b in enumerate(after):
        at = _at(b["date"], 5)

        # 1. Quick Invalidation Cut (Bar 1 or 2): If momentum completely fails and breaks entry candle extreme
        if idx in (1, 2) and not be_triggered and not partial_hit:
            quick_cut = (b["low"] < entry_candle["low"] and b["close"] < e) if long else (b["high"] > entry_candle["high"] and b["close"] > e)
            if quick_cut:
                final_px = b["close"]
                final_at = at
                reason = "RC Quick Cut (No Follow-Through)"
                break

        # 2. Check Stop hit on this bar
        if (b["low"] <= stop) if long else (b["high"] >= stop):
            final_px = stop
            final_at = at
            if be_triggered or partial_hit:
                reason = "Partial + RC Candle Trail Stop" if partial_hit else "RC Candle Trail Stop"
            else:
                reason = "Stop"
            break

        # 3. Check Partial Target hit (+2R)
        if not partial_hit:
            if (b["high"] >= partial_level) if long else (b["low"] <= partial_level):
                partial_hit = True
                partial_px = partial_level
                partial_at = at
                stop = max(stop, be_level) if long else min(stop, be_level)
                be_triggered = True

        # 4. Check Final Target hit (+3R)
        if (b["high"] >= final_target) if long else (b["low"] <= final_target):
            final_px = final_target
            final_at = at
            reason = "Target"
            break

        # 5. Check Time exit (15:10)
        if at[11:] >= EXIT:
            final_px = b["close"]
            final_at = at
            reason = "Time (15:10)"
            break

        # 6. Check if +1R was reached to trigger Breakeven & activate Candle-by-Candle Trailing
        if not be_triggered:
            if (b["high"] >= be_level) if long else (b["low"] <= be_level):
                stop = max(stop, e) if long else min(stop, e)
                be_triggered = True

        # 7. Ross Cameron Candle-by-Candle Trailing Stop
        # Once BE is triggered, ratchet the stop to the low of previous candle (long) or high of previous candle (short)
        if be_triggered and idx > 0:
            prev_b = after[idx - 1]
            if long:
                candle_trail_stop = prev_b["low"] - 2.0
                if candle_trail_stop > stop:
                    stop = candle_trail_stop
            else:
                candle_trail_stop = prev_b["high"] + 2.0
                if candle_trail_stop < stop:
                    stop = candle_trail_stop

    # Calculate overall realized R
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
        "partial_hit": partial_hit,
    }



def scan_retest_v1(
    bars: list[dict],
    above_vwap: dict[str, bool | None],
    pdh: float,
    pdl: float,
    min_risk: float = MIN_RISK,
    min_breakout: float = 2.0,
    max_retest_depth: float = 25.0,
    skip_lunch: bool = False,
) -> list[dict]:
    """V1 with Ross Cameron Candle Confirmation & Pullback Low Stop."""
    if not bars:
        return []
    day_open, out = bars[0]["open"], []
    for side in ("Long", "Short"):
        long = side == "Long"
        level = pdh if long else pdl
        stage, retest_extreme, why = 0, 0.0, []
        for i, b in enumerate(bars):
            c = b["close"]
            closes_at = _at(b["date"], 5)
            if closes_at[11:] > LAST_ENTRY:
                break

            if stage == 0:
                if (long and b["high"] >= level + min_breakout and c > day_open) or (
                    not long and b["low"] <= level - min_breakout and c < day_open
                ):
                    stage = 1
                    why = [
                        f"{closes_at[11:]} breakout: 5m high {b['high']:,.2f} cleared PDH {level:,.2f}"
                        if long
                        else f"{closes_at[11:]} breakdown: 5m low {b['low']:,.2f} breached PDL {level:,.2f}"
                    ]
                continue

            if stage == 1:
                is_pullback = (long and b["low"] <= level + 15.0 and b["low"] >= level - max_retest_depth) or (
                    not long and b["high"] >= level - 15.0 and b["high"] <= level + max_retest_depth
                )
                if is_pullback:
                    stage = 2
                    retest_extreme = b["low"] if long else b["high"]
                    why.append(
                        f"{closes_at[11:]} retest: 5m {'low' if long else 'high'} {retest_extreme:,.2f} tested {'PDH' if long else 'PDL'} {level:,.2f}"
                    )
                continue

            if stage == 2:
                retest_extreme = min(retest_extreme, b["low"]) if long else max(retest_extreme, b["high"])
                
                # Baseline condition
                confirmed = (
                    long and c > level and c > b["open"] and above_vwap.get(b["date"]) is True and c > day_open
                ) or (
                    not long and c < level and c < b["open"] and above_vwap.get(b["date"]) is False and c < day_open
                )
                if not confirmed:
                    continue

                # Ross Cameron Rule 1: Candle over Candle (First candle to break previous high/low)
                prev_b = bars[i - 1]
                candle_break = (long and b["high"] > prev_b["high"]) or (not long and b["low"] < prev_b["low"])
                if not candle_break:
                    continue

                # Ross Cameron Rule 2: Rejection Wick Filter (Avoid long opposing wicks)
                body = abs(c - b["open"])
                if long:
                    upper_wick = b["high"] - max(b["open"], c)
                    if upper_wick > max(body * 1.5, 4.0):  # strong rejection at top
                        continue
                else:
                    lower_wick = min(b["open"], c) - b["low"]
                    if lower_wick > max(body * 1.5, 4.0):  # strong rejection at bottom
                        continue

                if closes_at[11:] < FIRST_ENTRY:
                    continue
                if skip_lunch and BLACKOUT_START <= closes_at[11:] <= BLACKOUT_END:
                    continue

                # Ross Cameron Rule 3: Stop at Retest Extreme with 5 pt buffer (avoid getting clipped by noise)
                stop = (retest_extreme - 5.0) if long else (retest_extreme + 5.0)
                risk = abs(c - stop)
                if risk >= min_risk:
                    target = c + R_MULT * risk if long else c - R_MULT * risk
                    why.append(
                        f"{closes_at[11:]} RC confirmation: candle break {'high' if long else 'low'} of prev 5m bar with stop at {stop:,.2f}"
                    )
                    out.append(
                        {
                            "side": side,
                            "strategy": "retest",
                            "pool": f"PD{'H Support' if long else 'L Resistance'} Flip (RC)",
                            "time": closes_at,
                            "entry": c,
                            "stop": round(stop, 2),
                            "target": round(target, 2),
                            "risk": round(risk, 2),
                            "level": level,
                            "why": why,
                            "stop_basis": f"retest {'low' if long else 'high'} {retest_extreme:,.2f} {'-' if long else '+'} 5",
                            "candle": b,
                            "day_open": day_open,
                        }
                    )
                    break

    return out


def scan_trendline_reversal_v1(
    bars: list[dict],
    min_slope: float = 0.5,
    skip_lunch: bool = False,
    min_risk: float = MIN_RISK,
    max_risk: float = 65.0,
) -> list[dict]:
    """V1 Trendline Reversal with Ross Cameron Candle Trigger & Stop."""
    if len(bars) < 10:
        return []
    day_open, out = bars[0]["open"], []

    for side in ("Short", "Long"):
        long = side == "Long"
        key = "high" if long else "low"

        for i in range(5, len(bars)):
            b = bars[i]
            closes_at = _at(b["date"], 5)
            time_str = closes_at[11:]
            if time_str > LAST_ENTRY:
                break
            if time_str < FIRST_ENTRY:
                continue
            if skip_lunch and BLACKOUT_START <= time_str <= BLACKOUT_END:
                continue

            c = b["close"]
            piv = pivots(bars[: i + 1], key)
            if len(piv) < 2:
                continue

            candidate_pairs = [(piv[-2], piv[-1])]
            if len(piv) >= 3:
                candidate_pairs.append((piv[-3], piv[-1]))

            for x1, x2 in candidate_pairs:
                if x2 - x1 < 4:
                    continue
                y1, y2 = bars[x1][key], bars[x2][key]
                slope = (y2 - y1) / (x2 - x1)

                # Short: ascending trendline
                if not long and slope >= min_slope:
                    tl_val = y1 + slope * (i - x1)
                    tl_prev = y1 + slope * (i - 1 - x1)
                    if c < tl_val and bars[i - 1]["close"] >= tl_prev - 2.0 and c < b["open"]:
                        # Ross Cameron Rule 1: Break below prior candle low
                        prev_b = bars[i - 1]
                        if b["low"] >= prev_b["low"]:
                            continue
                        # Ross Cameron Rule 2: Lower wick not excessive
                        body = abs(c - b["open"])
                        if (min(b["open"], c) - b["low"]) > max(body * 1.5, 4.0):
                            continue

                        violations = sum(
                            1 for k in range(x1 + 1, x2) if bars[k]["close"] < y1 + slope * (k - x1) - 3.0
                        )
                        if violations > 1:
                            continue
                        
                        stop = max(b["high"], max(bars[k]["high"] for k in range(max(0, i - 2), i + 1))) + 5.0
                        risk = stop - c
                        if min_risk <= risk <= max_risk:
                            target = c - R_MULT * risk
                            why = [
                                f"{time_str} RC trendline breakdown: 5m close {c:,.2f} below ascending line {tl_val:,.2f} & broke prior low",
                            ]
                            out.append(
                                {
                                    "side": "Short",
                                    "strategy": "trendline_reversal",
                                    "pool": "Trendline Breakdown (RC)",
                                    "time": closes_at,
                                    "entry": c,
                                    "stop": round(stop, 2),
                                    "target": round(target, 2),
                                    "risk": round(risk, 2),
                                    "level": round(tl_val, 2),
                                    "why": why,
                                    "stop_basis": f"RC breakdown high {stop - 5:,.2f} + 5",
                                    "candle": b,
                                    "day_open": day_open,
                                    "trendline": (x1, y1, x2, y2),
                                }
                            )
                            break

                # Long: descending trendline
                elif long and slope <= -min_slope:
                    tl_val = y1 + slope * (i - x1)
                    tl_prev = y1 + slope * (i - 1 - x1)
                    if c > tl_val and bars[i - 1]["close"] <= tl_prev + 2.0 and c > b["open"]:
                        # Ross Cameron Rule 1: Break above prior candle high
                        prev_b = bars[i - 1]
                        if b["high"] <= prev_b["high"]:
                            continue
                        # Ross Cameron Rule 2: Upper wick not excessive
                        body = abs(c - b["open"])
                        if (b["high"] - max(b["open"], c)) > max(body * 1.5, 4.0):
                            continue

                        violations = sum(
                            1 for k in range(x1 + 1, x2) if bars[k]["close"] > y1 + slope * (k - x1) + 3.0
                        )
                        if violations > 1:
                            continue

                        stop = min(b["low"], min(bars[k]["low"] for k in range(max(0, i - 2), i + 1))) - 5.0
                        risk = c - stop
                        if min_risk <= risk <= max_risk:
                            target = c + R_MULT * risk
                            why = [
                                f"{time_str} RC trendline breakout: 5m close {c:,.2f} above descending line {tl_val:,.2f} & broke prior high",
                            ]
                            out.append(
                                {
                                    "side": "Long",
                                    "strategy": "trendline_reversal",
                                    "pool": "Trendline Breakout (RC)",
                                    "time": closes_at,
                                    "entry": c,
                                    "stop": round(stop, 2),
                                    "target": round(target, 2),
                                    "risk": round(risk, 2),
                                    "level": round(tl_val, 2),
                                    "why": why,
                                    "stop_basis": f"RC breakout low {stop + 5:,.2f} - 5",
                                    "candle": b,
                                    "day_open": day_open,
                                    "trendline": (x1, y1, x2, y2),
                                }
                            )
                            break

    return out


def run_benchmark():
    bars, above, daily = get_market_data(days=60)
    by_date = defaultdict(list)
    for b in bars:
        by_date[b["date"][:10]].append(b)
    dates = sorted(by_date.keys())

    v0_trades = []
    v1_trades = []
    v2_trades = []

    for idx in range(1, len(dates)):
        prev_d, cur_d = dates[idx - 1], dates[idx]
        cur_bars = by_date[cur_d]
        prev_bars = by_date[prev_d]
        pdh = max(b["high"] for b in prev_bars)
        pdl = min(b["low"] for b in prev_bars)

        # Baseline V0 (Current Genesis Entry + Current Genesis Staged Exit)
        for s in scan_trendline_reversal(cur_bars, skip_lunch=False):
            res = staged_exit_of(cur_bars, s)
            v0_trades.append(("Trendline Reversal", s, res))
        for s in scan_retest(cur_bars, above, pdh, pdl, skip_lunch=False):
            res = staged_exit_of(cur_bars, s)
            v0_trades.append(("Breakout & Retest", s, res))
        for s in scan(cur_bars, above, pdh, pdl, skip_lunch=False):
            res = staged_exit_of(cur_bars, s)
            v0_trades.append(("Liquidity Sweep", s, res))

        # V1: Pure Ross Cameron (RC Entry + Pure RC Quick Cut & Candle Trail Exit)
        for s in scan_trendline_reversal_v1(cur_bars, skip_lunch=False):
            res = rc_staged_exit_of(cur_bars, s)
            v1_trades.append(("Trendline Reversal", s, res))
        for s in scan_retest_v1(cur_bars, above, pdh, pdl, skip_lunch=False):
            res = rc_staged_exit_of(cur_bars, s)
            v1_trades.append(("Breakout & Retest", s, res))
        for s in scan(cur_bars, above, pdh, pdl, skip_lunch=False):
            res = rc_staged_exit_of(cur_bars, s)
            v1_trades.append(("Liquidity Sweep", s, res))

        # V2: Hybrid (RC Candle Confirmation Entry + Genesis Staged Exit with +1R Breakeven & +2R Partial)
        for s in scan_trendline_reversal_v1(cur_bars, skip_lunch=False):
            res = staged_exit_of(cur_bars, s)
            v2_trades.append(("Trendline Reversal", s, res))
        for s in scan_retest_v1(cur_bars, above, pdh, pdl, skip_lunch=False):
            res = staged_exit_of(cur_bars, s)
            v2_trades.append(("Breakout & Retest", s, res))
        for s in scan(cur_bars, above, pdh, pdl, skip_lunch=False):
            res = staged_exit_of(cur_bars, s)
            v2_trades.append(("Liquidity Sweep", s, res))



    def compute_stats(trades):
        total = len(trades)
        if total == 0:
            return {}
        wins = [t for t in trades if t[2]["r_multiple"] > 0]
        targets = [t for t in trades if t[2]["r_multiple"] >= 3.0 or "Target" in t[2]["exit_reason"]]
        losses = [t for t in trades if t[2]["r_multiple"] <= -1.0 or "Stop" in t[2]["exit_reason"]]
        total_r = sum(t[2]["r_multiple"] for t in trades)
        gross_profit_r = sum(t[2]["r_multiple"] for t in trades if t[2]["r_multiple"] > 0)
        gross_loss_r = abs(sum(t[2]["r_multiple"] for t in trades if t[2]["r_multiple"] < 0))
        pf = (gross_profit_r / gross_loss_r) if gross_loss_r > 0 else 999.0
        win_rate = (len(wins) / total) * 100
        target_rate = (len(targets) / total) * 100
        avg_r = total_r / total
        avg_risk = sum(t[1]["risk"] for t in trades) / total

        return {
            "total_trades": total,
            "wins": len(wins),
            "losses": len(losses),
            "target_hits": len(targets),
            "win_rate": round(win_rate, 1),
            "target_rate": round(target_rate, 1),
            "total_r": round(total_r, 2),
            "avg_r": round(avg_r, 2),
            "avg_risk_pts": round(avg_risk, 1),
            "profit_factor": round(pf, 2),
        }

    s0 = compute_stats(v0_trades)
    s1 = compute_stats(v1_trades)
    s2 = compute_stats(v2_trades)

    print("=========================== COMPARATIVE BENCHMARK (60 DAYS) ===========================")
    print(f"{'Metric':<24} | {'V0 (Current Baseline)':<22} | {'V1 (Pure RC Entry+Exit)':<22} | {'V2 (RC Entry + Gen Exit)':<22}")
    print("-" * 98)
    print(f"{'Total Trades':<24} | {s0.get('total_trades', 0):<22} | {s1.get('total_trades', 0):<22} | {s2.get('total_trades', 0):<22}")
    print(f"{'Win Rate (%)':<24} | {s0.get('win_rate', 0)}%{'':<17} | {s1.get('win_rate', 0)}%{'':<17} | {s2.get('win_rate', 0)}%{'':<17}")
    print(f"{'Target Hit Rate (%)':<24} | {s0.get('target_rate', 0)}%{'':<17} | {s1.get('target_rate', 0)}%{'':<17} | {s2.get('target_rate', 0)}%{'':<17}")
    print(f"{'Total R-Multiple':<24} | {s0.get('total_r', 0):+0.2f}R{'':<15} | {s1.get('total_r', 0):+0.2f}R{'':<15} | {s2.get('total_r', 0):+0.2f}R{'':<15}")
    print(f"{'Avg R per Trade':<24} | {s0.get('avg_r', 0):+0.2f}R{'':<15} | {s1.get('avg_r', 0):+0.2f}R{'':<15} | {s2.get('avg_r', 0):+0.2f}R{'':<15}")
    print(f"{'Avg Spot Risk (pts)':<24} | {s0.get('avg_risk_pts', 0)} pts{'':<14} | {s1.get('avg_risk_pts', 0)} pts{'':<14} | {s2.get('avg_risk_pts', 0)} pts{'':<14}")
    print(f"{'Profit Factor':<24} | {s0.get('profit_factor', 0):<22} | {s1.get('profit_factor', 0):<22} | {s2.get('profit_factor', 0):<22}")
    print("========================================================================================")


    def print_strategy_table(name, t0, t1, t2):
        s0 = compute_stats(t0)
        s1 = compute_stats(t1)
        s2 = compute_stats(t2)
        print(f"\n--- {name.upper()} ---")
        print(f"{'Metric':<24} | {'V0 Baseline':<20} | {'V1 Pure RC':<20} | {'V2 Hybrid':<20}")
        print("-" * 90)
        print(f"{'Total Trades':<24} | {s0.get('total_trades', 0):<20} | {s1.get('total_trades', 0):<20} | {s2.get('total_trades', 0):<20}")
        print(f"{'Win Rate (%)':<24} | {s0.get('win_rate', 0)}%{'':<15} | {s1.get('win_rate', 0)}%{'':<15} | {s2.get('win_rate', 0)}%{'':<15}")
        print(f"{'Target Hit Rate (%)':<24} | {s0.get('target_rate', 0)}%{'':<15} | {s1.get('target_rate', 0)}%{'':<15} | {s2.get('target_rate', 0)}%{'':<15}")
        print(f"{'Total R-Multiple':<24} | {s0.get('total_r', 0):+0.2f}R{'':<13} | {s1.get('total_r', 0):+0.2f}R{'':<13} | {s2.get('total_r', 0):+0.2f}R{'':<13}")
        print(f"{'Profit Factor':<24} | {s0.get('profit_factor', 0):<20} | {s1.get('profit_factor', 0):<20} | {s2.get('profit_factor', 0):<20}")

    for strat in ("Breakout & Retest", "Trendline Reversal", "Liquidity Sweep"):
        t0 = [t for t in v0_trades if t[0] == strat]
        t1 = [t for t in v1_trades if t[0] == strat]
        t2 = [t for t in v2_trades if t[0] == strat]
        print_strategy_table(strat, t0, t1, t2)




if __name__ == "__main__":
    run_benchmark()
