"""Quantitative Validation: Dynamic Spot S/R Targets and Level Reaction Trades on NIFTY Spot.

Evaluates:
1. Baseline: Fixed 3.0R Target (with +1R Breakeven and 2.0R milestone).
2. Dynamic Spot S/R Targets:
   - Target is pegged to key NIFTY Spot levels: PDH, PDL, ORH, ORL, Pivot R1/S1, Equal Highs/Lows.
   - If an opposing level is between 1.8R and 3.0R, take profit 3 pts ahead of the level.
   - If no opposing level before 3.0R, allow runner to target the next macro level (up to 5.0R).
3. Level Reaction Trades (State Machine on Spot):
   - When Spot hits a key level target:
     - Case A (Rejection / Fade): Spot fails to close beyond the level within 2 candles and confirms a reversal -> Counter-trend trade.
     - Case B (Acceptance / Continuation): Spot closes beyond the level and holds on retest -> Continuation trade toward next level.
4. Combined Performance (Initial Trades with Dynamic Targets + Level Reaction Trades).
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(BACKEND_DIR))

from app.market.chart_analytics import (
    calculate_liquidity_levels,
    detect_swing_points,
    detect_bos_choch,
    detect_trendlines_and_channels,
    calculate_atr,
)
from app.liquidity_signals import (
    scan_trendline_reversal,
    scan_retest,
    scan,
    staged_exit_of,
)
from scripts.maintain_trades_excel import get_market_data


def simulate_dynamic_and_reactions():
    bars, above, daily = get_market_data(60)
    print(f"Loaded {len(bars)} 5m bars across 60 days.")

    by_date: dict[str, list[dict]] = defaultdict(list)
    for b in bars:
        by_date[b["date"][:10]].append(b)
    dates = sorted(by_date.keys())

    # Results buckets
    res_fixed = []
    res_dynamic = []
    res_reaction_fades = []
    res_reaction_conts = []

    for idx in range(1, len(dates)):
        prev_d, cur_d = dates[idx - 1], dates[idx]
        cur_bars = by_date[cur_d]
        prev_bars = by_date[prev_d]
        if len(cur_bars) < 15 or len(prev_bars) < 15:
            continue

        pdh = max(b["high"] for b in prev_bars)
        pdl = min(b["low"] for b in prev_bars)

        # Precompute Spot levels for today
        liq_levels = calculate_liquidity_levels(daily, cur_bars)
        # Pull level prices
        spot_levels = [lv.price for lv in liq_levels]
        # Also add PDH, PDL, ORH, ORL
        orh = max(b["high"] for b in cur_bars[:6]) if len(cur_bars) >= 6 else pdh
        orl = min(b["low"] for b in cur_bars[:6]) if len(cur_bars) >= 6 else pdl
        spot_levels.extend([pdh, pdl, orh, orl])
        spot_levels = sorted(list(set(spot_levels)))

        # Find initial trades (using CHoCH-confirmed trendlines + retest + sweep)
        day_signals = []
        for s in scan_trendline_reversal(cur_bars, skip_lunch="conditional", allow_reentry=True, bar_minutes=5, require_choch=True, max_risk=55.0):
            day_signals.append(("Trendline Reversal", s))
        for s in scan_retest(cur_bars, above, pdh, pdl, skip_lunch="conditional", allow_reentry=False, bar_minutes=5):
            if 10.0 <= s.get("risk", 0) <= 55.0:
                day_signals.append(("Breakout & Retest", s))
        for s in scan(cur_bars, above, pdh, pdl, skip_lunch="conditional"):
            if 10.0 <= s.get("risk", 0) <= 55.0:
                day_signals.append(("Liquidity Sweep", s))

        # Deduplicate signals at identical candle
        unique_sigs = []
        seen_times = set()
        for strat, sig in day_signals:
            if sig["time"] not in seen_times:
                seen_times.add(sig["time"])
                unique_sigs.append((strat, sig))

        for strat, sig in unique_sigs:
            entry_px = sig["entry"]
            stop_px = sig["stop"]
            risk = sig["risk"]
            is_long = sig["side"] == "Long"
            sig_time = sig["time"]
            entry_idx = next((i for i, b in enumerate(cur_bars) if b["date"] == sig_time), None)
            if entry_idx is None:
                continue

            # -------------------------------------------------------------
            # 1. FIXED 3.0R BASELINE SIMULATION
            # -------------------------------------------------------------
            fixed_exit = staged_exit_of(cur_bars, sig, bar_minutes=5)
            res_fixed.append({
                "strat": strat,
                "date": cur_d,
                "time": sig_time,
                "side": sig["side"],
                "r": fixed_exit["r_multiple"],
                "exit_reason": fixed_exit["exit_reason"],
            })

            # -------------------------------------------------------------
            # 2. DYNAMIC SPOT S/R TARGET SIMULATION
            # -------------------------------------------------------------
            # Find nearest opposing spot level
            opposing_levels = [lv for lv in spot_levels if (lv > entry_px if is_long else lv < entry_px)]
            if is_long:
                nearest_level = min(opposing_levels) if opposing_levels else entry_px + 3.0 * risk
                # If nearest level is between 1.8R and 3.0R, target level - 3.0 pts
                level_r = (nearest_level - entry_px) / risk
                if 1.8 <= level_r <= 3.2:
                    dynamic_target = nearest_level - 3.0
                    target_level_name = f"Spot Resistance ({nearest_level:,.1f})"
                elif level_r > 3.2:
                    # Clear skies! Let runner target next level up to 5R
                    dynamic_target = min(nearest_level - 3.0, entry_px + 4.5 * risk)
                    target_level_name = f"Extended Macro Spot Level ({dynamic_target:,.1f})"
                else:
                    # Opposing level is too close (<1.8R) -> default to 2.5R
                    dynamic_target = entry_px + 2.5 * risk
                    target_level_name = "2.5R Safety"
            else:
                nearest_level = max(opposing_levels) if opposing_levels else entry_px - 3.0 * risk
                level_r = (entry_px - nearest_level) / risk
                if 1.8 <= level_r <= 3.2:
                    dynamic_target = nearest_level + 3.0
                    target_level_name = f"Spot Support ({nearest_level:,.1f})"
                elif level_r > 3.2:
                    dynamic_target = max(nearest_level + 3.0, entry_px - 4.5 * risk)
                    target_level_name = f"Extended Macro Spot Level ({dynamic_target:,.1f})"
                else:
                    dynamic_target = entry_px - 2.5 * risk
                    target_level_name = "2.5R Safety"

            # Simulate execution with dynamic target on 5m spot bars
            be_moved = False
            curr_stop = stop_px
            target_hit = False
            target_hit_idx = None
            target_hit_px = None
            trade_r = 0.0
            trade_reason = "Square Off 15:15"

            for b_i in range(entry_idx + 1, len(cur_bars)):
                b = cur_bars[b_i]
                high, low, close = b["high"], b["low"], b["close"]

                # Breakeven milestone (+1.0R)
                if not be_moved:
                    if (is_long and high >= entry_px + 1.0 * risk) or (not is_long and low <= entry_px - 1.0 * risk):
                        be_moved = True
                        curr_stop = entry_px + (2.0 if is_long else -2.0)

                # Check Stop Hit
                if (is_long and low <= curr_stop) or (not is_long and high >= curr_stop):
                    if be_moved:
                        trade_r = 0.05
                        trade_reason = "Breakeven Hit"
                    else:
                        trade_r = -1.0
                        trade_reason = "Stop Loss Hit"
                    break

                # Check Dynamic Target Hit
                if (is_long and high >= dynamic_target) or (not is_long and low <= dynamic_target):
                    target_hit = True
                    target_hit_idx = b_i
                    target_hit_px = dynamic_target
                    achieved_r = abs(dynamic_target - entry_px) / risk
                    trade_r = achieved_r
                    trade_reason = f"Dynamic Target Hit ({target_level_name})"
                    break

            if not target_hit and trade_reason == "Square Off 15:15":
                last_b = cur_bars[-1]
                end_px = last_b["close"]
                trade_r = (end_px - entry_px) / risk if is_long else (entry_px - end_px) / risk

            res_dynamic.append({
                "strat": strat,
                "date": cur_d,
                "time": sig_time,
                "side": sig["side"],
                "r": trade_r,
                "exit_reason": trade_reason,
            })

            # -------------------------------------------------------------
            # 3. LEVEL REACTION TRADES (ON TARGET HIT)
            # -------------------------------------------------------------
            # If dynamic target was hit at a major spot level, monitor the next 3 candles
            # to see if it Rejects (Fade) or Accepts (Continuation)
            if target_hit and target_hit_idx is not None and target_hit_idx + 2 < len(cur_bars):
                # Inspect next 1-3 candles around the level
                test_bar = cur_bars[target_hit_idx]
                next_bar = cur_bars[target_hit_idx + 1]
                lvl_tested = nearest_level

                # Case A: Rejection / Wick Reversal (Counter-trend Fade)
                # Long hit resistance: next bar wicks above or touches, but closes below the level
                if is_long:
                    is_rejected = (next_bar["high"] >= lvl_tested - 2.0) and (next_bar["close"] < lvl_tested - 4.0) and (next_bar["close"] < next_bar["open"])
                    if is_rejected:
                        fade_entry = next_bar["close"]
                        fade_stop = max(test_bar["high"], next_bar["high"]) + 4.0
                        fade_risk = abs(fade_stop - fade_entry)
                        if 10.0 <= fade_risk <= 45.0:
                            fade_target = fade_entry - 2.5 * fade_risk
                            # Evaluate fade trade
                            fade_r = -1.0
                            fade_reason = "Stop Loss"
                            fade_be = False
                            for f_i in range(target_hit_idx + 2, len(cur_bars)):
                                fb = cur_bars[f_i]
                                if not fade_be and fb["low"] <= fade_entry - 1.0 * fade_risk:
                                    fade_be = True
                                if fb["high"] >= (fade_entry if fade_be else fade_stop):
                                    fade_r = 0.05 if fade_be else -1.0
                                    fade_reason = "Breakeven" if fade_be else "Stop Loss"
                                    break
                                if fb["low"] <= fade_target:
                                    fade_r = 2.5
                                    fade_reason = "Target Hit (+2.5R)"
                                    break
                            res_reaction_fades.append({
                                "type": "Fade Resistance Reversal (PE)",
                                "date": cur_d,
                                "time": next_bar["date"],
                                "r": fade_r,
                                "reason": fade_reason,
                            })
                    else:
                        # Case B: Acceptance / Continuation (CE)
                        # Long broke above resistance and closed decisively above
                        is_accepted = (next_bar["close"] > lvl_tested + 3.0) and (test_bar["close"] > lvl_tested)
                        if is_accepted:
                            cont_entry = next_bar["close"]
                            cont_stop = lvl_tested - 8.0
                            cont_risk = abs(cont_entry - cont_stop)
                            if 12.0 <= cont_risk <= 45.0:
                                cont_target = cont_entry + 2.5 * cont_risk
                                cont_r = -1.0
                                cont_reason = "Stop Loss"
                                cont_be = False
                                for c_i in range(target_hit_idx + 2, len(cur_bars)):
                                    cb = cur_bars[c_i]
                                    if not cont_be and cb["high"] >= cont_entry + 1.0 * cont_risk:
                                        cont_be = True
                                    if cb["low"] <= (cont_entry if cont_be else cont_stop):
                                        cont_r = 0.05 if cont_be else -1.0
                                        cont_reason = "Breakeven" if cont_be else "Stop Loss"
                                        break
                                    if cb["high"] >= cont_target:
                                        cont_r = 2.5
                                        cont_reason = "Target Hit (+2.5R)"
                                        break
                                res_reaction_conts.append({
                                    "type": "Breakout Continuation (CE)",
                                    "date": cur_d,
                                    "time": next_bar["date"],
                                    "r": cont_r,
                                    "reason": cont_reason,
                                })
                else:
                    # Short hit support
                    is_rejected = (next_bar["low"] <= lvl_tested + 2.0) and (next_bar["close"] > lvl_tested + 4.0) and (next_bar["close"] > next_bar["open"])
                    if is_rejected:
                        fade_entry = next_bar["close"]
                        fade_stop = min(test_bar["low"], next_bar["low"]) - 4.0
                        fade_risk = abs(fade_entry - fade_stop)
                        if 10.0 <= fade_risk <= 45.0:
                            fade_target = fade_entry + 2.5 * fade_risk
                            fade_r = -1.0
                            fade_reason = "Stop Loss"
                            fade_be = False
                            for f_i in range(target_hit_idx + 2, len(cur_bars)):
                                fb = cur_bars[f_i]
                                if not fade_be and fb["high"] >= fade_entry + 1.0 * fade_risk:
                                    fade_be = True
                                if fb["low"] <= (fade_entry if fade_be else fade_stop):
                                    fade_r = 0.05 if fade_be else -1.0
                                    fade_reason = "Breakeven" if fade_be else "Stop Loss"
                                    break
                                if fb["high"] >= fade_target:
                                    fade_r = 2.5
                                    fade_reason = "Target Hit (+2.5R)"
                                    break
                            res_reaction_fades.append({
                                "type": "Fade Support Reversal (CE)",
                                "date": cur_d,
                                "time": next_bar["date"],
                                "r": fade_r,
                                "reason": fade_reason,
                            })
                    else:
                        is_accepted = (next_bar["close"] < lvl_tested - 3.0) and (test_bar["close"] < lvl_tested)
                        if is_accepted:
                            cont_entry = next_bar["close"]
                            cont_stop = lvl_tested + 8.0
                            cont_risk = abs(cont_stop - cont_entry)
                            if 12.0 <= cont_risk <= 45.0:
                                cont_target = cont_entry - 2.5 * cont_risk
                                cont_r = -1.0
                                cont_reason = "Stop Loss"
                                cont_be = False
                                for c_i in range(target_hit_idx + 2, len(cur_bars)):
                                    cb = cur_bars[c_i]
                                    if not cont_be and cb["low"] <= cont_entry - 1.0 * cont_risk:
                                        cont_be = True
                                    if cb["high"] >= (cont_entry if cont_be else cont_stop):
                                        cont_r = 0.05 if cont_be else -1.0
                                        cont_reason = "Breakeven" if cont_be else "Stop Loss"
                                        break
                                    if cb["low"] <= cont_target:
                                        cont_r = 2.5
                                        cont_reason = "Target Hit (+2.5R)"
                                        break
                                res_reaction_conts.append({
                                    "type": "Breakdown Continuation (PE)",
                                    "date": cur_d,
                                    "time": next_bar["date"],
                                    "r": cont_r,
                                    "reason": cont_reason,
                                })

    # Helper function to print statistics
    def calc_stats(trades):
        if not trades:
            return {"trades": 0, "wr": 0.0, "tot_r": 0.0, "avg_r": 0.0, "pf": 0.0, "wins": 0, "losses": 0, "be": 0}
        n = len(trades)
        wins = [t for t in trades if t["r"] > 0.15]
        losses = [t for t in trades if t["r"] < -0.15]
        bes = [t for t in trades if -0.15 <= t["r"] <= 0.15]
        wr = len(wins) / n * 100.0
        tot_r = sum(t["r"] for t in trades)
        avg_r = tot_r / n
        gross_win = sum(t["r"] for t in wins)
        gross_loss = abs(sum(t["r"] for t in losses))
        pf = (gross_win / gross_loss) if gross_loss > 0 else 99.9
        return {
            "trades": n,
            "wr": wr,
            "tot_r": tot_r,
            "avg_r": avg_r,
            "pf": pf,
            "wins": len(wins),
            "losses": len(losses),
            "be": len(bes),
        }

    print("\n" + "=" * 90)
    print("📊 60-DAY OVERALL BACKTEST: FIXED TARGET vs DYNAMIC SPOT S/R & REACTION TRADES")
    print("=" * 90)
    
    systems = {
        "1. Baseline: Fixed 3.0R Target": res_fixed,
        "2. Dynamic Spot S/R Targets": res_dynamic,
        "3. Level Reaction Trades (Fades / Reversals)": res_reaction_fades,
        "4. Level Reaction Trades (Breakout Continuations)": res_reaction_conts,
        "5. Combined System (Dynamic Targets + All Reactions)": res_dynamic + res_reaction_fades + res_reaction_conts,
    }

    hdr = f"{'Strategy / System':<52} | {'Trades':>6} | {'Win %':>6} | {'Total R':>9} | {'Avg R':>7} | {'Profit Factor':>13}"
    print(hdr)
    print("-" * len(hdr))

    for name, tr_list in systems.items():
        st = calc_stats(tr_list)
        print(f"{name:<52} | {st['trades']:>6} | {st['wr']:>5.1f}% | {st['tot_r']:>+8.2f}R | {st['avg_r']:>+6.2f}R | {st['pf']:>13.2f}")

    # Now filter for Post-Oct 15 (recent regime)
    print("\n" + "=" * 90)
    print("📅 POST-OCT 15 PERFORMANCE COMPARISON (RECENT MARKET REGIME)")
    print("=" * 90)
    print(hdr)
    print("-" * len(hdr))
    for name, tr_list in systems.items():
        post15_list = [t for t in tr_list if t["date"] >= "2026-10-15"]
        st = calc_stats(post15_list)
        print(f"{name:<52} | {st['trades']:>6} | {st['wr']:>5.1f}% | {st['tot_r']:>+8.2f}R | {st['avg_r']:>+6.2f}R | {st['pf']:>13.2f}")
    print("=" * 90)


if __name__ == "__main__":
    simulate_dynamic_and_reactions()
