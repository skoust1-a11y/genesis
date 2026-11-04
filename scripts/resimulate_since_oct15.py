"""Resimulate Since Oct 15: Full Engine Replay with Architecture C & Latest SMC Gates.

Replays NIFTY 5M candles from 2026-10-15 to 2026-11-02 through the full 5-Crew engine:
- Architecture C Protective Gates (VIX Corridor, Gamma Runway, Lunch Lull, Terminal Expiry)
- EQH / EQL Smart Money Concepts & Liquidity Magnet Gate (Gate G)
- Dynamic micro-wick 25-35pt stops & 2-OTM delta floor (Delta >= 0.35)
- Fixed 3 to 4 lot sizing baseline (195-260 Qty)
- Corrected 30-minute Charm Stagnation Time-Stop & Structure Trailing
- 50% Inviolable Safety Wall Ratchet
"""

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
import pandas as pd

# Add backend to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from app.api.market import _chart, parse_yahoo_chart
from app.trading.crew.alert_agent import AlertAgent
from app.trading.crew.manager import TradeManager
from app.trading.scanner_crew.detector_agent import DetectorAgent
from app.trading.scanner_crew.levels_agent import LevelsAgent, SessionLevels


def run_resimulation():
    print("=" * 70)
    print("  GENESIS REPLAY ENGINE: RESIMULATING SINCE OCTOBER 15, 2026")
    print("  Stack: Architecture C + SMC (EQH/EQL) + Live Delta Banding")
    print("=" * 70)

    # 1. Fetch 5M candles & 1D candles from Yahoo Finance
    print("\n[1/4] Fetching historical candle data...")
    chart_5m = _chart("^NSEI", "5m", "1mo")
    bars_5m = parse_yahoo_chart(chart_5m)
    df_5m = pd.DataFrame(bars_5m)
    df_5m["date_dt"] = pd.to_datetime(df_5m["date"])
    df_5m["session_date"] = df_5m["date_dt"].dt.strftime("%Y-%m-%d")
    df_5m["time_str"] = df_5m["date_dt"].dt.strftime("%H:%M")

    chart_1d = _chart("^NSEI", "1d", "3mo")
    bars_1d = parse_yahoo_chart(chart_1d)
    df_1d = pd.DataFrame(bars_1d)
    df_1d["date_dt"] = pd.to_datetime(df_1d["date"])
    df_1d["session_date"] = df_1d["date_dt"].dt.strftime("%Y-%m-%d")

    # Map previous day levels from regular market session 5M bars (matches institutional execution & maintain_trades_excel)
    daily_map = {}
    all_dates = sorted(df_5m["session_date"].unique().tolist())
    for i in range(1, len(all_dates)):
        prev_d = all_dates[i - 1]
        curr_d = all_dates[i]
        prev_bars = df_5m[df_5m["session_date"] == prev_d]
        if not prev_bars.empty:
            daily_map[curr_d] = {
                "pdh": float(prev_bars["high"].max()),
                "pdl": float(prev_bars["low"].min()),
                "pdc": float(prev_bars.iloc[-1]["close"]),
            }

    # Filter target sessions from 2026-10-15
    target_sessions = sorted([
        d for d in df_5m["session_date"].unique()
        if d >= "2026-10-15"
    ])
    print(f"Target Sessions ({len(target_sessions)}): {target_sessions}")

    # 2. Setup isolated scratch environment
    temp_dir = Path(tempfile.mkdtemp(prefix="genesis_sim_"))
    trades_dir = temp_dir / "trades"
    trades_dir.mkdir(parents=True, exist_ok=True)
    db_path = temp_dir / "trades.db"

    silent_alert_agent = AlertAgent(is_paper_trading=True)
    # Ensure silent dispatch during simulation
    silent_alert_agent._send = lambda msg: True

    levels_agent = LevelsAgent()
    detector_agent = DetectorAgent(otm_steps=2)

    tm = TradeManager(
        base_capital=50_000.0,
        is_paper_trading=True,
        trades_dir=trades_dir,
        db_path=db_path,
        alert_agent=silent_alert_agent,
    )

    all_closed_trades = []
    daily_summaries = []

    print("\n[2/4] Executing tick-by-tick simulation with full crew gates...")

    for session_date in target_sessions:
        session_bars = df_5m[df_5m["session_date"] == session_date].sort_values("date_dt").reset_index(drop=True)
        if len(session_bars) < 6:
            continue

        # Reset daily counters at start of every market session
        tm.risk_agent.daily_losses_count = 0
        tm.risk_agent.daily_cumulative_r = 0.0
        tm.risk_agent.is_safety_wall_locked = False
        tm.executed_order_signatures.clear()

        prev_levels = daily_map.get(session_date, {
            "pdh": float(session_bars["high"].max()),
            "pdl": float(session_bars["low"].min()),
            "pdc": float(session_bars.iloc[0]["open"]),
        })
        pdh = prev_levels["pdh"]
        pdl = prev_levels["pdl"]
        pdc = prev_levels["pdc"]

        # Calculate morning opening range OR-30
        or30_bars = session_bars[session_bars["time_str"] <= "09:45"]
        or30_high = float(or30_bars["high"].max()) if not or30_bars.empty else pdh
        or30_low = float(or30_bars["low"].min()) if not or30_bars.empty else pdl

        # Compute dealer gamma walls and round levels
        pivot = (pdh + pdl + pdc) / 3.0
        r1 = (2.0 * pivot) - pdl
        s1 = (2.0 * pivot) - pdh

        start_vault = tm.risk_agent.safety_wall_reserve
        session_trades_before = len(all_closed_trades)

        for bar_idx in range(len(session_bars)):
            curr_bar = session_bars.iloc[bar_idx]
            time_str = curr_bar["time_str"]
            cmp_price = float(curr_bar["close"])

            # Cumulative intraday DataFrame up to current bar
            intraday_so_far = session_bars.iloc[: bar_idx + 1]

            # Dynamic SMC EQH / EQL detection
            eqh_levels, eql_levels = levels_agent.detect_equal_highs_lows(intraday_so_far)

            key_levels = sorted(list(set([pdh, pdl, pdc, or30_high, or30_low, r1, s1] + eqh_levels + eql_levels)))

            levels_obj = SessionLevels(
                pdh=pdh, pdl=pdl, pdc=pdc, pivot=pivot, r1=r1, s1=s1,
                or30_high=or30_high, or30_low=or30_low,
                eqh_levels=eqh_levels, eql_levels=eql_levels,
                all_key_levels=key_levels,
            )

            # Multi-timeframe: Resample 5M bars to 15M bars
            df_15m = intraday_so_far.set_index("date_dt").resample("15min").agg({
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
                "date": "first",
            }).dropna().reset_index()

            # Detect setups across multi-timeframe scanner strategies (5M + 15M)
            candidate_setups = detector_agent.scan_multi_timeframe_strategies(
                tf_bars_map={
                    "5m": intraday_so_far.to_dict(orient="records"),
                    "15m": df_15m.to_dict(orient="records"),
                },
                levels=levels_obj,
                current_time_str=time_str,
            )

            # If active trade is open, evaluate realistic intra-candle progression (Low/High extremes)
            active_now = tm.journal_agent.load_active_trades(session_date)
            if active_now:
                first_tr = next(iter(active_now.values()))
                tr_dir = first_tr.get("direction", "CALL")
                curr_low = float(curr_bar["low"])
                curr_high = float(curr_bar["high"])
                extreme_adverse = curr_low if tr_dir == "CALL" else curr_high
                extreme_favorable = curr_high if tr_dir == "CALL" else curr_low

                # 1. Adverse tick (Conservative Stop Loss check)
                res_adv = tm.on_market_tick(
                    cmp=extreme_adverse,
                    df_5m=intraday_so_far,
                    pdh=pdh,
                    pdl=pdl,
                    key_levels=key_levels,
                    morning_floor=or30_low,
                    candidate_setups=[],
                    date_str=session_date,
                    current_time_str=time_str,
                    india_vix=14.0,
                )
                for act in res_adv.get("actions", []):
                    if act.get("action") in ("TRADE_CLOSED", "TARGET_HIT", "STAGNATION_TIME_STOP", "STOP_LOSS_HIT", "REVERSAL_EXIT", "TRAILING_STOP_HIT", "STRUCTURE_TRAIL_EXIT", "INTRADAY_CLOSE"):
                        all_closed_trades.append({
                            "session_date": session_date,
                            "time": time_str,
                            "action": act.get("action"),
                            "trade_id": act.get("trade_id"),
                            "pnl_rupees": act.get("pnl_rupees", 0.0),
                            "realized_r": act.get("realized_r", 0.0),
                            "realized_points": act.get("realized_points", 0.0),
                            "exit_reason": act.get("exit_reason", ""),
                        })

                # 2. Favorable tick (Milestones / Target check if trade survived adverse tick)
                if tm.journal_agent.load_active_trades(session_date):
                    res_fav = tm.on_market_tick(
                        cmp=extreme_favorable,
                        df_5m=intraday_so_far,
                        pdh=pdh,
                        pdl=pdl,
                        key_levels=key_levels,
                        morning_floor=or30_low,
                        candidate_setups=[],
                        date_str=session_date,
                        current_time_str=time_str,
                        india_vix=14.0,
                    )
                    for act in res_fav.get("actions", []):
                        if act.get("action") in ("TRADE_CLOSED", "TARGET_HIT", "STAGNATION_TIME_STOP", "STOP_LOSS_HIT", "REVERSAL_EXIT", "TRAILING_STOP_HIT", "STRUCTURE_TRAIL_EXIT", "INTRADAY_CLOSE"):
                            all_closed_trades.append({
                                "session_date": session_date,
                                "time": time_str,
                                "action": act.get("action"),
                                "trade_id": act.get("trade_id"),
                                "pnl_rupees": act.get("pnl_rupees", 0.0),
                                "realized_r": act.get("realized_r", 0.0),
                                "realized_points": act.get("realized_points", 0.0),
                                "exit_reason": act.get("exit_reason", ""),
                            })

            # 3. Bar Close tick with candidate setups
            res = tm.on_market_tick(
                cmp=cmp_price,
                df_5m=intraday_so_far,
                pdh=pdh,
                pdl=pdl,
                key_levels=key_levels,
                morning_floor=or30_low,
                candidate_setups=candidate_setups,
                date_str=session_date,
                current_time_str=time_str,
                df_15m=df_15m,
                india_vix=14.0, # Standard optimal corridor
            )

            for act in res.get("actions", []):
                if act.get("action") in ("TRADE_CLOSED", "TARGET_HIT", "STAGNATION_TIME_STOP", "STOP_LOSS_HIT", "REVERSAL_EXIT", "TRAILING_STOP_HIT", "STRUCTURE_TRAIL_EXIT", "INTRADAY_CLOSE"):
                    all_closed_trades.append({
                        "session_date": session_date,
                        "time": time_str,
                        "action": act.get("action"),
                        "trade_id": act.get("trade_id"),
                        "pnl_rupees": act.get("pnl_rupees", 0.0),
                        "realized_r": act.get("realized_r", 0.0),
                        "realized_points": act.get("realized_points", 0.0),
                        "exit_reason": act.get("exit_reason", ""),
                    })

        # Load closed trades for today from live_executed_trades.json
        exec_file = trades_dir / "live_executed_trades.json"
        today_closed = []
        if exec_file.exists():
            try:
                import json
                with open(exec_file, "r", encoding="utf-8") as f:
                    all_json_trades = json.load(f)
                today_closed = [
                    t for t in all_json_trades
                    if str(t.get("timestamp", "") or t.get("entry_time", "") or t.get("Date", ""))[:10] == session_date
                    or str(t.get("exit_timestamp", ""))[:10] == session_date
                ]
            except Exception as e:
                pass

        for t in today_closed:
            if not any(x.get("trade_id") == t.get("trade_id") for x in all_closed_trades):
                all_closed_trades.append({
                    "session_date": session_date,
                    "time": t.get("exit_timestamp", "")[-8:-3] if t.get("exit_timestamp") else "15:10",
                    "action": t.get("status", "CLOSED"),
                    "trade_id": t.get("trade_id"),
                    "pnl_rupees": t.get("pnl_rupees", 0.0),
                    "realized_r": t.get("realized_r", 0.0),
                    "realized_points": t.get("realized_points", 0.0),
                    "exit_reason": t.get("exit_reason", ""),
                    "strategy": t.get("strategy", ""),
                    "direction": t.get("direction", ""),
                    "strike": t.get("suggested_strike", ""),
                })

        new_trades_count = len(all_closed_trades) - session_trades_before
        day_pnl = sum(t["pnl_rupees"] for t in all_closed_trades[-new_trades_count:]) if new_trades_count > 0 else 0.0
        day_r = sum(t["realized_r"] for t in all_closed_trades[-new_trades_count:]) if new_trades_count > 0 else 0.0
        end_vault = tm.risk_agent.safety_wall_reserve

        daily_summaries.append({
            "date": session_date,
            "trades": new_trades_count,
            "pnl": day_pnl,
            "r": day_r,
            "vault": end_vault,
        })

    # 3. Analyze Results from trades_dir / live_executed_trades.json
    print("\n[3/4] Compiling simulation analytics...")
    exec_file = trades_dir / "live_executed_trades.json"
    if exec_file.exists():
        import json
        with open(exec_file, "r", encoding="utf-8") as f:
            all_closed_trades = json.load(f)

    df_trades = pd.DataFrame(all_closed_trades)
    df_daily = pd.DataFrame(daily_summaries)

    total_trades = len(df_trades)
    wins = df_trades[df_trades["pnl_rupees"] > 0] if not df_trades.empty else pd.DataFrame()
    losses = df_trades[df_trades["realized_r"] <= -0.5] if not df_trades.empty else pd.DataFrame()
    breakevens = df_trades[(df_trades["pnl_rupees"] <= 0) & (df_trades["realized_r"] > -0.5)] if not df_trades.empty else pd.DataFrame()

    total_pnl = df_trades["pnl_rupees"].sum() if not df_trades.empty else 0.0
    total_r = df_trades["realized_r"].sum() if not df_trades.empty else 0.0
    pure_win_rate = (len(wins) / total_trades * 100) if total_trades > 0 else 0.0
    win_capital_preserved_rate = ((len(wins) + len(breakevens)) / total_trades * 100) if total_trades > 0 else 0.0

    gross_profit = wins["pnl_rupees"].sum() if not wins.empty else 0.0
    gross_loss = abs(df_trades[df_trades["pnl_rupees"] < 0]["pnl_rupees"].sum()) if not df_trades.empty else 0.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else 99.9

    final_vault = tm.risk_agent.safety_wall_reserve
    active_capital = tm.risk_agent.base_capital

    print("\n" + "=" * 70)
    print("  SIMULATION AUDIT RESULTS: 2026-10-15 TO 2026-11-02")
    print("=" * 70)
    print(f"Total Trading Sessions Replayed: {len(target_sessions)}")
    print(f"Total Completed Trades:         {total_trades}")
    print(f"Total Realized Net Profit:      Rs {total_pnl:,.2f}")
    print(f"Total Realized R:               +{total_r:.2f} R")
    print(f"Pure Win Rate:                  {pure_win_rate:.1f}% ({len(wins)} Wins, {len(losses)} Losses, {len(breakevens)} BE Scratches)")
    print(f"Win/Capital-Preserved Rate:     {win_capital_preserved_rate:.1f}% (Capital Preservation Edge)")
    print(f"Profit Factor:                  {profit_factor:.2f}")
    print(f"Gross Profit:                   Rs {gross_profit:,.2f}")
    print(f"Gross Loss:                     Rs {gross_loss:,.2f}")
    print(f"Avg Win:                        Rs {wins['pnl_rupees'].mean():,.2f}" if not wins.empty else "Avg Win: N/A")
    print(f"Avg Loss:                       Rs {losses['pnl_rupees'].mean():,.2f}" if not losses.empty else "Avg Loss: N/A")
    print(f"Safety Wall Vault Locked:       Rs {final_vault:,.2f} (Inviolable)")
    print(f"Active Trading Capital:         Rs {active_capital:,.2f}")
    print(f"Total Account Equity:           Rs {final_vault + active_capital:,.2f}")
    print("=" * 70)

    print("\n=== SESSION BY SESSION PERFORMANCE ===")
    for row in daily_summaries:
        status_sym = "🟢" if row["pnl"] > 0 else ("🔴" if row["pnl"] < 0 else "⚪")
        print(f"{status_sym} {row['date']} | Trades: {row['trades']:2d} | PnL: Rs {row['pnl']:10.2f} | R: {row['r']:+6.2f}R | Safety Wall: Rs {row['vault']:10.2f}")

    if not df_trades.empty:
        print("\n=== TRADE DETAILS ===")
        cols_to_print = [c for c in ["timestamp", "strategy", "direction", "entry_price", "exit_price", "realized_r", "pnl_rupees", "exit_reason"] if c in df_trades.columns]
        print(df_trades[cols_to_print].to_string())
        Path("reports").mkdir(exist_ok=True)
        df_trades.to_json("reports/last_sim_trades.json", orient="records", indent=2)

    return {
        "total_trades": total_trades,
        "total_pnl": total_pnl,
        "total_r": total_r,
        "pure_win_rate": pure_win_rate,
        "win_capital_preserved_rate": win_capital_preserved_rate,
        "profit_factor": profit_factor,
        "final_vault": final_vault,
        "active_capital": active_capital,
    }


if __name__ == "__main__":
    run_resimulation()
