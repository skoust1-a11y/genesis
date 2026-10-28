"""Validate Chart Analytics Enhancements against Baseline Genesis Strategies.

Tests:
1. Baseline V0: Production scan_trendline_reversal, scan_retest, scan
2. Variant 1 (3-Point Trendlines): Require >= 3 swing touches from chart_analytics
3. Variant 2 (Structure Break / CHoCH Confirmation): Require BOS / CHoCH confirmation on trendline breaks
4. Variant 3 (Expanded Liquidity Pools): Expand Liquidity Sweep to ORH/ORL and EQH/EQL pools
5. Variant 4 (Combined Institutional): 3-Point Trendlines + Enhanced Liquidity Pools + CHoCH Filter
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(BACKEND_DIR))

from app.api.market import parse_yahoo_chart, _chart
from app.config import get_settings
from app.market.chart_analytics import (
    calculate_liquidity_levels,
    detect_liquidity_sweeps,
    detect_swing_points,
    detect_bos_choch,
    detect_trendlines_and_channels,
    calculate_atr,
    aggregate_bars,
)
from app.liquidity_signals import (
    INDEX,
    VWAP_PROXY,
    scan_trendline_reversal,
    scan_retest,
    scan,
    staged_exit_of,
)
from scripts.maintain_trades_excel import get_market_data


def run_benchmark():
    print("=" * 80)
    print("🚀 RUNNING QUANTITATIVE VALIDATION: BASELINE vs CHART ANALYTICS")
    print("=" * 80)

    settings = get_settings()
    bars, above, daily = get_market_data(60)
    print(f"Loaded {len(bars)} 5m bars across 60 days.")

    by_date: dict[str, list[dict]] = defaultdict(list)
    for b in bars:
        by_date[b["date"][:10]].append(b)
    dates = sorted(by_date.keys())

    # Results containers
    # Keys: 'baseline', 'trendline_3pt', 'choch_filter', 'expanded_liquidity', 'combined'
    trade_buckets: dict[str, list[dict]] = {
        "V0: Baseline Production": [],
        "V1: 3-Point Trendline Validation": [],
        "V2: CHoCH / Structure Confirmed": [],
        "V3: Expanded Liquidity (ORH/ORL + EQH/EQL)": [],
        "V4: Combined Institutional (V1 + V2 + V3)": [],
    }

    for idx in range(1, len(dates)):
        prev_d, cur_d = dates[idx - 1], dates[idx]
        cur_bars = by_date[cur_d]
        prev_bars = by_date[prev_d]
        if len(cur_bars) < 15 or len(prev_bars) < 15:
            continue

        pdh = max(b["high"] for b in prev_bars)
        pdl = min(b["low"] for b in prev_bars)

        # -------------------------------------------------------------
        # 1. Baseline V0 Trades
        # -------------------------------------------------------------
        v0_sigs = []
        # TL
        for s in scan_trendline_reversal(cur_bars, skip_lunch="conditional", allow_reentry=True, bar_minutes=5):
            if 10.0 <= s.get("risk", 0) <= 55.0:
                v0_sigs.append(("Trendline Reversal", s))
        # Retest
        for s in scan_retest(cur_bars, above, pdh, pdl, skip_lunch="conditional", allow_reentry=False, bar_minutes=5):
            if 10.0 <= s.get("risk", 0) <= 55.0:
                v0_sigs.append(("Breakout & Retest", s))
        # Sweep
        for s in scan(cur_bars, above, pdh, pdl, skip_lunch="conditional"):
            if 10.0 <= s.get("risk", 0) <= 55.0:
                v0_sigs.append(("Liquidity Sweep", s))

        for strat, sig in v0_sigs:
            res = staged_exit_of(cur_bars, sig, bar_minutes=5)
            trade_buckets["V0: Baseline Production"].append({
                "strat": strat,
                "date": cur_d,
                "time": sig["time"],
                "side": sig["side"],
                "risk": sig["risk"],
                "r": res["r_multiple"],
                "reason": res["exit_reason"],
            })

        # -------------------------------------------------------------
        # Chart Analytics Pre-computation for cur_bars
        # -------------------------------------------------------------
        atr_vals = calculate_atr(cur_bars, 14)
        swings = detect_swing_points(cur_bars, sensitivity=2, atr_series=atr_vals)
        structure_events = detect_bos_choch(cur_bars, swings)
        tl_analytics = detect_trendlines_and_channels(cur_bars, swings, atr_val=atr_vals)
        liq_levels = calculate_liquidity_levels(daily, cur_bars)
        liq_sweeps = detect_liquidity_sweeps(cur_bars, liq_levels, reclaim_window=5)

        # -------------------------------------------------------------
        # 2. Variant 1: 3-Point Trendlines
        # Filter trendline reversals to those that coincide with a confirmed 3-point trendline
        # -------------------------------------------------------------
        v1_sigs = []
        three_pt_lines = [t for t in tl_analytics if t.touch_count >= 3]
        for strat, sig in v0_sigs:
            if strat == "Trendline Reversal":
                sig_idx = next((i for i, b in enumerate(cur_bars) if b["date"] == sig["time"]), None)
                if sig_idx is None:
                    continue
                matched = False
                for tl in three_pt_lines:
                    if (sig["side"] == "Long" and tl.side == "high") or (sig["side"] == "Short" and tl.side == "low"):
                        matched = True
                        break
                if matched:
                    v1_sigs.append((strat, sig))
            else:
                v1_sigs.append((strat, sig))

        for strat, sig in v1_sigs:
            res = staged_exit_of(cur_bars, sig, bar_minutes=5)
            trade_buckets["V1: 3-Point Trendline Validation"].append({
                "strat": strat,
                "date": cur_d,
                "time": sig["time"],
                "side": sig["side"],
                "risk": sig["risk"],
                "r": res["r_multiple"],
                "reason": res["exit_reason"],
            })

        # -------------------------------------------------------------
        # 3. Variant 2: CHoCH / Structure Confirmed
        # Only take trendlines if accompanied by a CHoCH / BOS event
        # -------------------------------------------------------------
        v2_sigs = []
        choch_times = {ev.break_date: ev for ev in structure_events}
        for strat, sig in v0_sigs:
            if strat == "Trendline Reversal":
                sig_time = sig["time"]
                sig_idx = next((i for i, b in enumerate(cur_bars) if b["date"] == sig_time), -1)
                has_choch = False
                if sig_idx >= 0:
                    for k in range(max(0, sig_idx - 3), sig_idx + 1):
                        b_date = cur_bars[k]["date"]
                        if b_date in choch_times:
                            ev = choch_times[b_date]
                            if (sig["side"] == "Long" and ev.direction == "bullish") or (
                                sig["side"] == "Short" and ev.direction == "bearish"
                            ):
                                has_choch = True
                                break
                if has_choch:
                    v2_sigs.append((strat, sig))
            else:
                v2_sigs.append((strat, sig))

        for strat, sig in v2_sigs:
            res = staged_exit_of(cur_bars, sig, bar_minutes=5)
            trade_buckets["V2: CHoCH / Structure Confirmed"].append({
                "strat": strat,
                "date": cur_d,
                "time": sig["time"],
                "side": sig["side"],
                "risk": sig["risk"],
                "r": res["r_multiple"],
                "reason": res["exit_reason"],
            })

        # -------------------------------------------------------------
        # 4. Variant 3: Expanded Liquidity Pools (ORH/ORL + EQH/EQL)
        # Add sweep trades from detect_liquidity_sweeps
        # -------------------------------------------------------------
        v3_sigs = list(v0_sigs)
        for sw in liq_sweeps:
            if not sw.reclaimed or sw.sweep_type != "wick_sweep":
                continue
            if not any(k in sw.level_label for k in ("ORH", "ORL", "EQH", "EQL")):
                continue
            rec_idx = sw.reclaim_index
            if rec_idx is None or rec_idx >= len(cur_bars):
                continue
            rec_bar = cur_bars[rec_idx]
            is_long = "ORL" in sw.level_label or "EQL" in sw.level_label
            side = "Long" if is_long else "Short"
            entry_px = rec_bar["close"]
            stop_px = sw.extreme_price - 5.0 if is_long else sw.extreme_price + 5.0
            risk_pts = abs(entry_px - stop_px)
            if not (12.0 <= risk_pts <= 45.0):
                continue
            target_px = entry_px + 2.5 * risk_pts if is_long else entry_px - 2.5 * risk_pts

            sig = {
                "time": rec_bar["date"],
                "side": side,
                "entry": entry_px,
                "stop": stop_px,
                "target": target_px,
                "risk": risk_pts,
                "pool": f"Expanded Sweep ({sw.level_label})",
                "why": [f"Wick sweep of {sw.level_label} ({sw.level_price:,.2f}) with reclaim at {entry_px:,.2f}"],
            }
            v3_sigs.append((f"Expanded Sweep ({sw.level_label})", sig))

        for strat, sig in v3_sigs:
            res = staged_exit_of(cur_bars, sig, bar_minutes=5)
            trade_buckets["V3: Expanded Liquidity (ORH/ORL + EQH/EQL)"].append({
                "strat": strat,
                "date": cur_d,
                "time": sig["time"],
                "side": sig["side"],
                "risk": sig["risk"],
                "r": res["r_multiple"],
                "reason": res["exit_reason"],
            })

        # -------------------------------------------------------------
        # 5. Variant 4: Combined Institutional (V1 + V2 + V3)
        # -------------------------------------------------------------
        v4_sigs = []
        for strat, sig in v2_sigs:
            if strat == "Trendline Reversal":
                if any(t["time"] == sig["time"] for t in trade_buckets["V1: 3-Point Trendline Validation"]):
                    v4_sigs.append((strat, sig))
            else:
                v4_sigs.append((strat, sig))
        for strat, sig in v3_sigs:
            if "Expanded Sweep" in strat:
                v4_sigs.append((strat, sig))

        for strat, sig in v4_sigs:
            res = staged_exit_of(cur_bars, sig, bar_minutes=5)
            trade_buckets["V4: Combined Institutional (V1 + V2 + V3)"].append({
                "strat": strat,
                "date": cur_d,
                "time": sig["time"],
                "side": sig["side"],
                "risk": sig["risk"],
                "r": res["r_multiple"],
                "reason": res["exit_reason"],
            })

    # -------------------------------------------------------------
    # Print Performance Metrics Matrix
    # -------------------------------------------------------------
    print("\n" + "=" * 105)
    print(f"{'Configuration':44} | {'Trades':6} | {'Win %':6} | {'Total R':8} | {'Avg R':7} | {'Profit Factor':13}")
    print("-" * 105)

    for name, trs in trade_buckets.items():
        if not trs:
            print(f"{name:44} | 0 trades")
            continue
        n_trades = len(trs)
        wins = [t for t in trs if t["r"] > 0]
        losses = [t for t in trs if t["r"] < 0]
        win_rate = (len(wins) / n_trades) * 100.0 if n_trades > 0 else 0.0
        total_r = sum(t["r"] for t in trs)
        avg_r = total_r / n_trades if n_trades > 0 else 0.0

        gross_win = sum(t["r"] for t in wins)
        gross_loss = abs(sum(t["r"] for t in losses))
        pf = (gross_win / gross_loss) if gross_loss > 0 else 99.9

        print(f"{name:44} | {n_trades:6d} | {win_rate:5.1f}% | {total_r:+7.2f}R | {avg_r:+6.2f}R | {pf:13.2f}")

    print("=" * 105)


if __name__ == "__main__":
    run_benchmark()
