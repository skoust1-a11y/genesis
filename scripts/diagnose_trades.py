import sys
import os
sys.path.append(os.path.abspath("."))
sys.path.append(os.path.abspath("backend"))
import json
import tempfile
from pathlib import Path
import pandas as pd
from scripts.resimulate_since_oct15 import _chart, parse_yahoo_chart
from app.trading.crew.manager import TradeManager
from app.trading.scanner_crew.detector_agent import DetectorAgent
from app.trading.scanner_crew.levels_agent import LevelsAgent, SessionLevels
from app.trading.crew.alert_agent import AlertAgent

chart_5m = _chart("^NSEI", "5m", "1mo")
bars_5m = parse_yahoo_chart(chart_5m)
df_5m = pd.DataFrame(bars_5m)
df_5m["date_dt"] = pd.to_datetime(df_5m["date"])
df_5m["session_date"] = df_5m["date_dt"].dt.strftime("%Y-%m-%d")
df_5m["time_str"] = df_5m["date_dt"].dt.strftime("%H:%M")

temp_dir = Path(tempfile.mkdtemp())
tm = TradeManager(
    base_capital=50000.0,
    is_paper_trading=True,
    trades_dir=temp_dir,
    db_path=temp_dir / "trades.db",
    alert_agent=AlertAgent(is_paper_trading=True),
)
tm.alert_agent._send = lambda m: True

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

target_sessions = sorted([d for d in df_5m["session_date"].unique() if d >= "2026-10-15"])
detector_agent = DetectorAgent(otm_steps=2)
levels_agent = LevelsAgent()

for s_date in target_sessions:
    s_bars = df_5m[df_5m["session_date"] == s_date].sort_values("date_dt").reset_index(drop=True)
    if len(s_bars) < 6:
        continue
    tm.risk_agent.daily_losses_count = 0
    tm.risk_agent.daily_cumulative_r = 0.0
    tm.risk_agent.is_safety_wall_locked = False
    tm.executed_order_signatures.clear()

    prev_l = daily_map.get(s_date, {"pdh": float(s_bars["high"].max()), "pdl": float(s_bars["low"].min()), "pdc": float(s_bars.iloc[0]["open"])})
    pdh, pdl, pdc = prev_l["pdh"], prev_l["pdl"], prev_l["pdc"]
    or30_bars = s_bars[s_bars["time_str"] <= "09:45"]
    or30_high = float(or30_bars["high"].max()) if not or30_bars.empty else pdh
    or30_low = float(or30_bars["low"].min()) if not or30_bars.empty else pdl

    for i in range(len(s_bars)):
        curr = s_bars.iloc[i].to_dict()
        time_str = curr["time_str"]
        cmp_p = float(curr["close"])
        sub_5m = s_bars.iloc[:i+1]
        if len(sub_5m) < 4:
            continue
        pivot = (pdh + pdl + pdc) / 3.0
        r1 = (2 * pivot) - pdl
        s1 = (2 * pivot) - pdh
        eqh, eql = levels_agent.detect_equal_highs_lows(sub_5m, tol_pts=8.0)
        key_levels = sorted(list(set([pdh, pdl, pdc, or30_high, or30_low, r1, s1] + eqh + eql)))
        lev_obj = SessionLevels(
            pdh=pdh, pdl=pdl, pdc=pdc, pivot=pivot, r1=r1, s1=s1,
            or30_high=or30_high, or30_low=or30_low,
            eqh_levels=eqh, eql_levels=eql,
            all_key_levels=key_levels,
        )
        
        df_15m = sub_5m.set_index("date_dt").resample("15min").agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum","date":"first"}).dropna().reset_index()
        cand = detector_agent.scan_multi_timeframe_strategies({"5m": sub_5m.to_dict(orient="records"), "15m": df_15m.to_dict(orient="records")}, levels=lev_obj, current_time_str=time_str)

        tm.on_market_tick(cmp=cmp_p, df_5m=sub_5m, pdh=pdh, pdl=pdl, key_levels=key_levels, morning_floor=or30_low, candidate_setups=cand, date_str=s_date, current_time_str=time_str, india_vix=14.0)

# Read executed trades
exec_file = temp_dir / "live_executed_trades.json"
with open(exec_file) as f:
    trades = json.load(f)

for idx, t in enumerate(trades):
    t_start = str(t.get("timestamp", ""))[:16]
    t_exit = str(t.get("exit_timestamp", ""))[:16]
    ep = float(t.get("entry_price", 0))
    sl = float(t.get("stop_loss", 0))
    tgt = float(t.get("target_price", 0))
    rp = float(t.get("risk_points", abs(ep - sl) or 25.0))
    dr = str(t.get("direction", "CALL"))
    st = str(t.get("strategy", ""))[:25]
    pnl = float(t.get("pnl_rupees", 0))
    rr = float(t.get("realized_r", 0))
    reason = str(t.get("exit_reason", ""))

    # Find bars in this window
    dur_bars = df_5m[(df_5m["date"] >= t_start) & (df_5m["date"] <= t_exit)]
    if not dur_bars.empty:
        if dr == "CALL":
            max_move = dur_bars["high"].max() - ep
            max_adv = ep - dur_bars["low"].min()
        else:
            max_move = ep - dur_bars["low"].min()
            max_adv = dur_bars["high"].max() - ep
        mfe_r = max_move / rp
        mae_r = max_adv / rp
    else:
        mfe_r, mae_r = 0.0, 0.0

    print(f"{idx:02d} | {t_start} -> {t_exit[-5:]} | {st:25s} | {dr:4s} | Entry: {ep:.1f} | MFE: {mfe_r:+.2f}R | MAE: {mae_r:.2f}R | Exit: {rr:+.2f}R | PnL: {pnl:>+8.1f} | {reason[:40]}")
