"""Live Real-Time Strategy Scanner & Telegram Alert Daemon.

Features:
1. Multi-Timeframe Monitoring: Unconstrained scanning across multiple timeframes (1m, 2m, 5m, 15m, 30m).
2. Explicit Timeframe Identification: Every signal, Telegram alert, chart, log, and journal entry explicitly announces its timeframe.
3. Evaluates all institutional trading strategies across all active timeframes:
   - Breakout & Retest
   - Trendline Reversal
   - Liquidity Sweep
4. Condition-Matched Re-Entries: Watches for second attempts after initial trade invalidation.
5. High-Resolution Active Trade Tracking: Tracks +1R trailing SL to breakeven, targets, and stops on 1m bars.
6. Real-time Telegram broadcast with option contract selection, risk-to-reward calculation, and chart images.
7. Automatically updates strategy Excel journals.

Usage:
    python scripts/live_scanner.py                         # Run live multi-timeframe daemon (1m, 5m, 15m)
    python scripts/live_scanner.py --timeframes 1m,5m      # Monitor specific timeframes
    python scripts/live_scanner.py --once                  # Run single scan on all timeframes & exit
    python scripts/live_scanner.py --no-telegram           # Dry run (detect & log without sending)
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

# Paths
ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(BACKEND_DIR))

from app import alerts
from app.api.market import _chart, add_vwap, parse_yahoo_chart
from app.config import get_settings
from app.liquidity_signals import (
    INDEX,
    VWAP_PROXY,
    charts,
    intraday_levels,
    message,
    option_plan,
    scan,
    scan_retest,
    scan_trendline_reversal,
    session_data,
    trading_levels,
)
from app.trading import scaling_law
import openpyxl
from scripts.maintain_trades_excel import main as update_trades_excel


def load_real_money_state(
    starting_capital: float = 100_000.0,
    start_lots: int = 4,
    cfg: scaling_law.ScaleUpLawConfig | None = None,
) -> tuple[scaling_law.ScalingTierState, scaling_law.ScalingDecision, scaling_law.CircuitBreakerStatus]:
    """Reconstruct live real-money tier state, locked safety wall, and circuit breaker status from live trades."""
    config = cfg or scaling_law.ScaleUpLawConfig()
    state = scaling_law.initialize_tier_state(starting_capital=starting_capital, start_lots=start_lots)
    master_file = ROOT_DIR / "trades" / "live_and_forward_trades.xlsx"
    today_trades: list[scaling_law.TradeRecord] = []
    today_str = datetime.now().strftime("%Y-%m-%d")

    if master_file.exists():
        try:
            wb = openpyxl.load_workbook(master_file, data_only=True)
            if "📋 Live & Forward Trades" in wb.sheetnames:
                ws = wb["📋 Live & Forward Trades"]
                rows = list(ws.iter_rows(min_row=2, values_only=True))
                rows_chron = sorted(rows, key=lambda r: (str(r[0]), str(r[1])))
                for i, r in enumerate(rows_chron):
                    pnl = float(r[21]) if r[21] is not None else 0.0
                    r_mult = float(r[14]) if r[14] is not None else 0.0
                    lots = int(r[23]) if r[23] is not None else start_lots
                    date_val = str(r[0])
                    trade = scaling_law.TradeRecord(
                        trade_id=f"LF{i}",
                        date=date_val,
                        strat_name=str(r[2]),
                        lots=lots,
                        pnl_inr=pnl,
                        r_multiple=r_mult,
                        exit_reason=str(r[12]),
                    )
                    state, _ = scaling_law.record_trade(state, trade, config)
                    if date_val == today_str:
                        today_trades.append(trade)
        except Exception as e:
            print(f"⚠️ Error loading real-money state from Excel: {e}")

    decision = scaling_law.evaluate_scaling(state, config)
    cb_status = scaling_law.check_daily_circuit_breaker(today_trades, config)
    return state, decision, cb_status


MARKET_OPEN = "09:15"
MARKET_CLOSE = "15:30"
SUPPORTED_TIMEFRAMES = ["1m", "2m", "5m", "15m", "30m"]
DEFAULT_TIMEFRAMES = ["1m", "5m", "15m"]


def parse_timeframe_minutes(tf: str) -> int:
    tf = tf.strip().lower()
    if tf.endswith("m"):
        return int(tf[:-1])
    elif tf.endswith("h"):
        return int(tf[:-1]) * 60
    elif tf.endswith("d"):
        return int(tf[:-1]) * 375
    return int(tf)


def get_timeframe_range(tf: str) -> str:
    mins = parse_timeframe_minutes(tf)
    if mins <= 5:
        return "5d"
    elif mins <= 15:
        return "10d"
    return "1mo"


def get_timeframe_role(tf: str) -> str:
    mins = parse_timeframe_minutes(tf)
    if mins == 1:
        return "Micro-Execution Entry"
    elif mins <= 3:
        return "Scalp Execution"
    elif mins == 5:
        return "Primary Institutional Structure"
    elif mins == 15:
        return "Macro Swing Structure"
    elif mins >= 30:
        return "Major Trend Frame"
    return f"{tf.upper()} Frame"


def is_market_hours() -> bool:
    now_dt = datetime.now()
    if now_dt.weekday() >= 5:  # Saturday or Sunday
        return False
    current_time = now_dt.strftime("%H:%M")
    return MARKET_OPEN <= current_time <= MARKET_CLOSE


def fetch_timeframe_data(tf: str, today_str: str) -> tuple[list[dict], dict[str, bool | None]]:
    """Fetch candles and VWAP proxy for a specific timeframe."""
    range_ = get_timeframe_range(tf)
    try:
        raw_bars = [
            b for b in parse_yahoo_chart(_chart(INDEX, tf, range_))
            if None not in (b.get("open"), b.get("high"), b.get("low"))
        ]
        today_bars = [b for b in raw_bars if b["date"].startswith(today_str)]
    except Exception as e:
        print(f"⚠️ Could not fetch {tf} index bars: {e}")
        return [], {}

    above = {}
    try:
        raw_proxy = parse_yahoo_chart(_chart(VWAP_PROXY, tf, range_))
        proxy_vwap = add_vwap(raw_proxy)
        above = {b["date"]: b["close"] > b["vwap"] if b.get("vwap") else None for b in proxy_vwap}
    except Exception:
        pass

    return today_bars, above


def scan_latest_bars(
    strategy: str = "auto",
    allow_reentry: bool = True,
    timeframes: list[str] | None = None,
):
    """Fetch session data across all enabled timeframes and run strategy scanners."""
    if not timeframes:
        timeframes = DEFAULT_TIMEFRAMES

    today_str = datetime.now().strftime("%Y-%m-%d")

    # Fetch daily/5m baseline to extract previous day's high/low
    bars_5m, _, _ = session_data(range_="5d")
    prev_dates = sorted(set(b["date"][:10] for b in bars_5m if b["date"][:10] < today_str))
    if not prev_dates:
        return {}, [], None, None, None

    prev_day_bars = [b for b in bars_5m if b["date"].startswith(prev_dates[-1])]
    pdh = max(b["high"] for b in prev_day_bars)
    pdl = min(b["low"] for b in prev_day_bars)
    pd_close = prev_day_bars[-1]["close"]
    pd_open = prev_day_bars[0]["open"]
    prev_day_info = [{"date": prev_dates[-1], "open": pd_open, "high": pdh, "low": pdl, "close": pd_close}]

    tf_bars_map = {}
    signals = []

    for tf in timeframes:
        try:
            bars, above = fetch_timeframe_data(tf, today_str)
            if not bars:
                continue
            tf_bars_map[tf] = bars
            bar_mins = parse_timeframe_minutes(tf)
            # Precompute key Spot levels for dynamic targets
            spot_levels = [pdh, pdl]
            if len(bars) >= 6:
                spot_levels.extend([max(b["high"] for b in bars[:6]), min(b["low"] for b in bars[:6])])
            spot_levels = sorted(list(set(spot_levels)))

            # Strategy: Trendline Reversal (CHoCH Confirmed + Dynamic Spot Target)
            if strategy in ("auto", "trendline"):
                for s in scan_trendline_reversal(
                    bars,
                    skip_lunch="conditional",
                    allow_reentry=allow_reentry,
                    bar_minutes=bar_mins,
                    require_choch=True,
                    max_risk=55.0,
                    dynamic_targets=True,
                    spot_levels=spot_levels,
                ):
                    if not (10.0 <= s.get("risk", 0) <= 55.0):
                        continue
                    s_dict = dict(s)
                    s_dict["timeframe"] = tf
                    is_reentry = "Re-Entry" in s_dict.get("pool", "")
                    strat_label = f"Trendline Reversal{' (Re-Entry)' if is_reentry else ''}"
                    signals.append((strat_label, tf, s_dict, bars))

            # Strategy: Breakout & Retest (Grade A PDH/PDL + ORH/ORL + Dynamic Spot Target)
            if strategy in ("auto", "retest"):
                for s in scan_retest(
                    bars,
                    above,
                    pdh,
                    pdl,
                    skip_lunch="conditional",
                    allow_reentry=False,
                    bar_minutes=bar_mins,
                    include_orh_orl=True,
                    dynamic_targets=True,
                    spot_levels=spot_levels,
                ):
                    if not (10.0 <= s.get("risk", 0) <= 55.0):
                        continue
                    s_dict = dict(s)
                    s_dict["timeframe"] = tf
                    is_reentry = "Re-Entry" in s_dict.get("pool", "")
                    pool_desc = s_dict.get("pool", "")
                    is_orb = "OR" in pool_desc
                    strat_label = f"Breakout & Retest ({'OR-30' if is_orb else 'PD'}){' (Re-Entry)' if is_reentry else ''}"
                    signals.append((strat_label, tf, s_dict, bars))

            # Strategy: Liquidity Sweep (requires enough bars and timeframe >= 3m)
            if strategy in ("auto", "sweep") and len(bars) >= 12 and bar_mins >= 3:
                for s in scan(bars, above, pdh, pdl, skip_lunch="conditional", dynamic_targets=True, spot_levels=spot_levels):
                    if not (10.0 <= s.get("risk", 0) <= 55.0):
                        continue
                    s_dict = dict(s)
                    s_dict["timeframe"] = tf
                    signals.append(("Liquidity Sweep", tf, s_dict, bars))

        except Exception as e:
            print(f"⚠️ Error scanning timeframe {tf}: {e}")

    return tf_bars_map, signals, prev_day_info, pdh, pdl


def broadcast_signal(
    strat_name: str,
    tf: str,
    sig: dict,
    chart_bars: list[dict],
    prev_day_info: list[dict],
    settings,
    no_telegram: bool = False,
):
    """Generate options plan, charts, and send alert to Telegram with prominent timeframe identification."""
    plan = option_plan(sig, settings)
    if not plan:
        print(f"⚠️ Could not generate option plan for {strat_name} ({tf}) at {sig['time']}")
        return None

    tf_upper = tf.upper()
    tf_role = get_timeframe_role(tf)
    lvls = trading_levels(prev_day_info, session_over=False) + intraday_levels(chart_bars, 3)
    body = message(sig, settings, plan, lvls)
    pngs = charts(chart_bars, sig, lvls, plan, settings)
    stop_p, target_p = plan["exits"][0]["premium"], plan["exits"][1]["premium"]
    lot = settings.lot_size("NIFTY") or 65
    per_share_entry = plan["entry_premium"]

    # Option Risk Guardrail: Never risk more than 20-25% of premium or 18 pts on 1m/2m micro trades
    max_risk_pts = min(per_share_entry * 0.22, 18.0) if tf in ("1m", "2m") else min(per_share_entry * 0.30, 25.0)
    per_share_stop = round(max(stop_p, per_share_entry - max_risk_pts), 2)
    per_share_risk = abs(per_share_entry - per_share_stop)

    # Scale targets proportionally to actual option risk
    per_share_t1 = round(per_share_entry + per_share_risk, 2)
    per_share_t2 = round(per_share_entry + 1.5 * per_share_risk, 2)
    per_share_target = round(max(target_p, per_share_entry + 2.0 * per_share_risk), 2)
    per_share_reward = abs(per_share_target - per_share_entry)

    # Option Buyer Buy Range & Limits
    buy_range_low = round(max(0.5, per_share_entry - max(2.0, per_share_entry * 0.03)), 1)
    buy_range_high = round(per_share_entry + max(2.0, per_share_entry * 0.03), 1)
    max_chase_limit = round(per_share_entry + max(4.0, per_share_entry * 0.06), 1)

    # Load Live Real-Money Scaling State & Safety Wall
    real_state, real_decision, cb_status = load_real_money_state()
    active_lots = real_state.current_lots
    active_qty = active_lots * lot
    trade_risk_inr = round(per_share_risk * active_qty, 2)
    risk_pct = round((trade_risk_inr / real_state.current_capital) * 100, 2)

    cb_banner = ""
    if cb_status.is_locked_out:
        cb_banner = f"⛔ [REAL-MONEY CIRCUIT BREAKER ACTIVE]\nSession trading locked: {cb_status.reason}\n\n"

    sig_time_str = sig["time"][11:16] if len(sig.get("time", "")) >= 16 else sig.get("time", "")
    forward_test_card = (
        f"{cb_banner}"
        f"🚀 NEW REAL-MONEY TRADE ENTRY\n"
        f"Trade Name: {strat_name} ({sig['side'].upper()}) [{tf_upper}]\n"
        f"Contract: NIFTY {plan['option']} ({plan['expiry']})\n"
        f"• Sizing Tier: {active_lots} Lots ({active_qty} Qty) | Max Risk: Rs {trade_risk_inr:,.2f} ({risk_pct}% of capital)\n"
        f"• Safety Wall: Rs {real_state.safety_wall_locked:,.2f} Locked (Inviolable) | Active Capital: Rs {real_state.active_risk_capital:,.2f}\n"
        f"• 4-Gate Progress: Trades {real_state.tier_trades_count}/15 | Tier R: +{real_state.tier_accumulated_r:.2f}R/12.0R\n"
        f"• Entry Time: {sig_time_str} IST\n"
        f"• Supposed Entry (CMP): Rs {per_share_entry:,.2f}\n"
        f"• Buy Range: Rs {buy_range_low:,.2f} – Rs {buy_range_high:,.2f}\n"
        f"• Stop Loss: Rs {per_share_stop:,.2f} (Index SL: {sig['stop']:,.2f})\n"
        f"• Targets: T1 Rs {per_share_t1:,.2f} | T2 Rs {per_share_t2:,.2f} | T3 Rs {per_share_target:,.2f}\n"
        f"• Trail Rule: Move SL to Breakeven once T1 (+1.0R) is hit"
    )

    opt_caption = forward_test_card

    nifty_caption = (
        f"📊 [LIVE {strat_name.upper()}] NIFTY {tf_upper} Structure & Levels\n"
        f"• Timeframe: {tf_upper} ({tf_role})\n"
        f"• Strategy: {strat_name} ({sig['side'].upper()})\n"
        f"• Index Entry: {sig['entry']:,.2f} | Stop: {sig['stop']:,.2f} | Target: {sig['target']:,.2f}\n"
        f"• Option Contract: {plan['option']} (Buy Range: Rs {buy_range_low:,.2f} - Rs {buy_range_high:,.2f})\n"
        f"• Trail Rule: Once +1R is reached, move SL to breakeven immediately"
    )

    print(f"\n=======================================================")
    if cb_status.is_locked_out:
        print(f"⛔ CIRCUIT BREAKER ACTIVE: {cb_status.reason}")
    print(f"🚀 REAL-MONEY BOT TRIGGER: [{strat_name}] {sig['side'].upper()}")
    print(f"   ⏱️ TIMEFRAME: {tf_upper} ({tf_role})")
    print(f"   🛡️ LOT SIZING: {active_lots} Lots ({active_qty} Qty) | Risk: Rs {trade_risk_inr:,.2f} ({risk_pct}%)")
    print(f"   🏦 SAFETY WALL: Rs {real_state.safety_wall_locked:,.2f} Locked | Active Capital: Rs {real_state.active_risk_capital:,.2f}")
    print(f"   ⚖️ 4-GATE STATUS: {real_decision.action} ({real_decision.reason})")
    print(f"   Time: {sig['time']} IST")
    print(f"   Option to Buy: {plan['option']} ({plan['expiry']})")
    print(f"   Suggested Buy Range: Rs {buy_range_low:,.2f} – Rs {buy_range_high:,.2f} / share (CMP ~Rs {per_share_entry:,.2f})")
    print(f"   Max Chase Limit: Rs {max_chase_limit:,.2f} / share")
    print(f"   Stop Loss: Rs {per_share_stop:,.2f} / share | Target: Rs {per_share_target:,.2f} / share")
    print(f"   Spot Index: Entry {sig['entry']:,.2f} | Stop {sig['stop']:,.2f} | Target {sig['target']:,.2f}")
    print(f"=======================================================\n")

    telegram_msg_id = None
    if not no_telegram:
        print("Broadcasting trade alert and charts to Telegram group...")
        # Send Option chart with the clean ticket card as caption & capture its message id for threading
        if pngs and len(pngs) >= 1:
            telegram_msg_id = alerts.send_signal_photo(settings, forward_test_card[:1024], pngs[0])
            # Send NIFTY index structure chart
            if len(pngs) >= 2:
                alerts.send_signal_photo(settings, nifty_caption[:1024], pngs[1])

        # Fallback if no charts generated or photo dispatch failed: send text card
        if not telegram_msg_id:
            telegram_msg_id = alerts.send_telegram_signal(settings, forward_test_card)

        print(f"✅ Telegram trade alert & charts dispatched (msg_id: {telegram_msg_id}) for {tf_upper} timeframe.")
    else:
        print("ℹ️ Telegram delivery skipped (--no-telegram flag active).")

    # Sync Excel Journal
    try:
        update_trades_excel([])
        print("✅ Strategy Excel journal updated.")
    except Exception as e:
        print(f"⚠️ Could not update Excel journal: {e}")

    return {
        "strat_name": strat_name,
        "timeframe": tf,
        "sig": sig,
        "plan": plan,
        "buy_range_low": buy_range_low,
        "buy_range_high": buy_range_high,
        "max_chase_limit": max_chase_limit,
        "per_share_risk": per_share_risk,
        "opt_t1": per_share_t1,
        "opt_t2": per_share_t2,
        "target_p": per_share_target,
        "stop_p": per_share_stop,
        "t1_alerted": False,
        "t2_alerted": False,
        "entry_time": sig["time"],
        "telegram_msg_id": telegram_msg_id,
        "milestones_hit": set(),
    }


def track_active_trade(active_trade: dict, bars: list[dict] | dict, settings, no_telegram: bool = False):
    """Tracks active trade for discrete milestone prices (+0.5R, +1.0R, +1.5R, +2.0R, +2.5R, +3.0R) and exits."""
    if isinstance(bars, dict):
        bars = [bars]
    if not bars:
        return active_trade

    current_bar = bars[-1]
    sig = active_trade["sig"]
    plan = active_trade["plan"]
    tf = active_trade.get("timeframe", "5m").upper()
    entry_p = plan["entry_premium"]
    target_p = active_trade.get("target_p", plan["exits"][1]["premium"])
    stop_p = active_trade.get("stop_p", plan["exits"][0]["premium"])
    per_share_risk = active_trade.get("per_share_risk", abs(entry_p - stop_p))
    opt_t1 = active_trade.get("opt_t1", entry_p)
    opt_t2 = active_trade.get("opt_t2", entry_p)

    long = sig["side"] == "Long"
    entry_spot = sig["entry"]
    risk = sig["risk"]
    one_r = entry_spot + risk if long else entry_spot - risk
    two_r = entry_spot + 2 * risk if long else entry_spot - 2 * risk
    three_r = sig["target"]

    def notify_trade(subject: str, text: str) -> None:
        if no_telegram:
            return
        orig_msg_id = active_trade.get("telegram_msg_id")
        if orig_msg_id:
            alerts.send_telegram_signal(settings, text, reply_to_message_id=orig_msg_id)
        else:
            alerts.send_telegram_signal(settings, text)

    entry_time_str = str(active_trade.get("entry_time", sig.get("time", "")))
    time_stamp = entry_time_str[11:16] if len(entry_time_str) >= 16 else entry_time_str
    trade_line = f"Opt: NIFTY {plan['option']} ({plan['expiry']} {time_stamp} IST)"

    # Filter bars since trade entry
    entry_key = entry_time_str[:16]
    relevant_bars = [b for b in bars if b["date"] >= entry_key]
    if not relevant_bars:
        relevant_bars = [current_bar]

    extreme_high = max(b["high"] for b in relevant_bars)
    extreme_low = min(b["low"] for b in relevant_bars)

    # Helper for formatted exit notifications
    def send_exit_alert(exit_p: float, reason: str) -> None:
        exit_time_str = current_bar["date"][11:16] if len(current_bar.get("date", "")) >= 16 else current_bar.get("date", "")
        pnl_pts = round(exit_p - entry_p, 2)
        pnl_pct = round((pnl_pts / entry_p) * 100, 1) if entry_p > 0 else 0.0
        r_mult = round(pnl_pts / per_share_risk, 2) if per_share_risk > 0 else 0.0
        emoji = "🎉" if pnl_pts > 0 else "🛡️" if pnl_pts == 0 else "🛑"
        exit_card = (
            f"{emoji} TRADE EXITED\n"
            f"Trade Name: {active_trade['strat_name']} ({sig['side'].upper()}) [{tf}]\n"
            f"Contract: NIFTY {plan['option']} ({plan['expiry']})\n"
            f"• Entry Time: {time_stamp} IST | Exit Time: {exit_time_str} IST\n"
            f"• Entry Price: Rs {entry_p:,.2f} | Exit Price: Rs {exit_p:,.2f}\n"
            f"• Result: {pnl_pts:+.2f} pts / share ({pnl_pct:+.1f}%) | {r_mult:+.2f}R\n"
            f"• Reason: {reason}\n"
            f"Status: ⚪ FLAT (Position Closed)"
        )
        print(f"\n{emoji} TRADE EXITED: {active_trade['strat_name']} [{tf}] | Exit: Rs {exit_p:,.2f} | Result: {pnl_pts:+.2f} pts ({r_mult:+.2f}R)\n")
        notify_trade(f"[genesis] Trade Exited ({tf})", exit_card)

    # Track milestones achieved
    milestones_hit = active_trade.setdefault("milestones_hit", set())
    if active_trade.get("t1_alerted"):
        milestones_hit.add("1.0R")
    if active_trade.get("t2_alerted"):
        milestones_hit.add("2.0R")

    r_step = (target_p - entry_p) / 3.0

    # Discrete Milestones: ONLY send updates when a milestone price level is breached
    milestones = [
        ("0.5R", entry_spot + 0.5 * risk * (1 if long else -1), round(entry_p + 0.5 * r_step, 2), "price"),
        ("1.0R", one_r, opt_t1, "t1_trail"),
        ("1.5R", entry_spot + 1.5 * risk * (1 if long else -1), round(entry_p + 1.5 * r_step, 2), "price"),
        ("2.0R", two_r, opt_t2, "t2_exit"),
        ("2.5R", entry_spot + 2.5 * risk * (1 if long else -1), round(entry_p + 2.5 * r_step, 2), "price"),
        ("3.0R", three_r, target_p, "t3_exit"),
    ]

    for m_key, m_spot, m_opt_price, m_type in milestones:
        reached = extreme_high >= m_spot if long else extreme_low <= m_spot
        if reached and m_key not in milestones_hit:
            milestones_hit.add(m_key)
            if m_type == "price":
                current_sl = active_trade.get("stop_p", stop_p)
                msg = (
                    f"🎯 Trade Milestone (+{m_key})\n"
                    f"Trade Name: {active_trade['strat_name']} ({sig['side'].upper()}) [{tf}]\n"
                    f"Contract: NIFTY {plan['option']}\n"
                    f"• Entry Price: Rs {entry_p:,.2f} @ {time_stamp} IST\n"
                    f"• Current Price (CMP): Rs {m_opt_price:,.2f}\n"
                    f"• Current SL: Rs {current_sl:,.2f}"
                )
                print(f"🎯 Milestone {m_key} reached: {active_trade['strat_name']} [{tf}] CMP: Rs {m_opt_price:,.2f} | SL: Rs {current_sl:,.2f}")
                notify_trade(f"[genesis] Milestone {m_key} Alert ({tf})", msg)

            elif m_type == "t1_trail":
                active_trade["t1_alerted"] = True
                active_trade["stop_p"] = entry_p  # Trail to cost
                msg = (
                    f"🛡️ Target 1 Hit (+1.0R) - Move SL to Cost\n"
                    f"Trade Name: {active_trade['strat_name']} ({sig['side'].upper()}) [{tf}]\n"
                    f"Contract: NIFTY {plan['option']}\n"
                    f"• Entry Price: Rs {entry_p:,.2f} @ {time_stamp} IST\n"
                    f"• Current Price (CMP): Rs {opt_t1:,.2f}\n"
                    f"• Trailed SL: Rs {entry_p:,.2f} (Breakeven Locked)"
                )
                print(f"🛡️ Milestone {m_key} (+1R) reached! Move SL to Rs {entry_p:,.2f} for {active_trade['strat_name']} [{tf}]")
                notify_trade(f"[genesis] Trailing SL Alert (+1R) ({tf})", msg)

            elif m_type == "t2_exit":
                active_trade["t2_alerted"] = True
                active_trade["stop_p"] = opt_t1  # Trail SL to lock +1R
                msg = (
                    f"💰 Target 2 Hit (+2.0R) - Book 50% Profit\n"
                    f"Trade Name: {active_trade['strat_name']} ({sig['side'].upper()}) [{tf}]\n"
                    f"Contract: NIFTY {plan['option']}\n"
                    f"• Entry Price: Rs {entry_p:,.2f} @ {time_stamp} IST\n"
                    f"• Current Price (CMP): Rs {opt_t2:,.2f}\n"
                    f"• Trailed SL: Rs {opt_t1:,.2f} (Lock +1.0R Profit)"
                )
                print(f"💰 Milestone {m_key} (+2R) reached! Book 50% lots for {active_trade['strat_name']} [{tf}]")
                notify_trade(f"[genesis] Target 2 Hit (+2R) ({tf})", msg)

            elif m_type == "t3_exit":
                send_exit_alert(target_p, "+3.0R Full Target Achieved")
                return None  # Trade completed

    # Stop Loss Check
    spot_stop = sig["stop"]
    current_sl = active_trade.get("stop_p", stop_p)
    if "2.0R" in milestones_hit:
        spot_stop = one_r  # Trailed to +1R
    elif "1.0R" in milestones_hit:
        spot_stop = entry_spot  # Trailed to cost

    hit_stop = (current_bar["low"] <= spot_stop if long else current_bar["high"] >= spot_stop)
    if hit_stop:
        reason = "Trailed Stop Hit (Cost / Breakeven Protected)" if "1.0R" in milestones_hit else "Stop Loss Hit"
        send_exit_alert(current_sl, reason)
        return None  # Trade completed

    # 15:15 IST intraday market close square-off
    current_time_str = current_bar["date"][11:]
    if current_time_str >= "15:15":
        curr_spot = current_bar["close"]
        delta_pnl = (curr_spot - sig["entry"]) if long else (sig["entry"] - curr_spot)
        est_curr_prem = max(0.5, round(entry_p + delta_pnl * 0.45, 2))
        send_exit_alert(est_curr_prem, f"Intraday Close Square-Off ({current_time_str[:5]} IST)")
        return None  # Trade completed

    return active_trade


def get_poll_interval(active_trade: dict | None, timeframes: list[str], explicit_interval: int | None) -> int:
    """Calculates adaptive polling interval: 10s for 1m candle tracking, 30s for 5m candle tracking."""
    if explicit_interval is not None and explicit_interval > 0:
        return explicit_interval
    if active_trade:
        trade_tf = active_trade.get("timeframe", "5m").lower()
        if trade_tf in ("1m", "2m"):
            return 10  # 10s for 1-minute candle option trade tracking
        elif trade_tf == "5m":
            return 30  # 30s for 5-minute candle option trade tracking
        else:
            return 60  # 60s for 15-minute+
    if "1m" in timeframes or "2m" in timeframes:
        return 10      # 10s to capture 1-min candle completion immediately
    return 30          # 30s default for 5m / 15m


def _load_seen_signals(today_str: str) -> set[str]:
    seen_file = ROOT_DIR / "trades" / f".seen_{today_str}.txt"
    if seen_file.exists():
        return set(line.strip() for line in seen_file.read_text().splitlines() if line.strip())
    return set()


def _record_seen_signal(today_str: str, key: str) -> None:
    seen_file = ROOT_DIR / "trades" / f".seen_{today_str}.txt"
    seen_file.parent.mkdir(parents=True, exist_ok=True)
    with open(seen_file, "a") as f:
        f.write(f"{key}\n")


def main():
    parser = argparse.ArgumentParser(description="Live Multi-Timeframe Strategy Scanner & Telegram Alert Daemon")
    parser.add_argument("--once", action="store_true", help="Run once on the latest candle and exit")
    parser.add_argument("--no-telegram", action="store_true", help="Dry run without sending Telegram messages")
    parser.add_argument("--strategy", choices=["trendline", "retest", "sweep", "auto"], default="auto")
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help="Poll interval in seconds (default: adaptive, 10s for 1m, 30s for 5m)",
    )
    parser.add_argument("--reentry", action="store_true", default=True, help="Enable condition-matched re-entries (default: True)")
    parser.add_argument("--no-reentry", dest="reentry", action="store_false", help="Disable re-entries")
    parser.add_argument(
        "--timeframes",
        type=str,
        default=",".join(DEFAULT_TIMEFRAMES),
        help=f"Comma-separated list of timeframes to scan (e.g. 1m,5m,15m). Supported: {','.join(SUPPORTED_TIMEFRAMES)}",
    )
    args = parser.parse_args()

    active_timeframes = [tf.strip().lower() for tf in args.timeframes.split(",") if tf.strip()]
    if "all" in active_timeframes:
        active_timeframes = list(SUPPORTED_TIMEFRAMES)

    settings = get_settings()
    today_str = datetime.now().strftime("%Y-%m-%d")
    last_processed_candles: dict[str, str] = {}
    seen_signal_keys = _load_seen_signals(today_str)
    active_trades: dict[str, dict] = {}

    print(f"=======================================================")
    print(f"  Genesis Real-Time Multi-Timeframe Institutional Scanner")
    print(f"  Mode: {'Single Scan' if args.once else 'Live Market Daemon'}")
    print(f"  Telegram Alerts: {'DISABLED' if args.no_telegram else 'ENABLED'}")
    print(f"  Strategy Filter: {args.strategy.upper()}")
    print(f"  Condition Re-Entries: {'ENABLED' if args.reentry else 'DISABLED'}")
    print(f"  Monitored Timeframes: {', '.join(tf.upper() for tf in active_timeframes)}")
    print(f"  Adaptive Cadence: 10s for 1m, 30s for 5m, 60s for 15m")
    print(f"=======================================================")

    startup_lock = ROOT_DIR / "trades" / f".startup_{today_str}.lock"
    startup_announced = startup_lock.exists()

    while True:
        try:
            if not args.once and not is_market_hours():
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"[{now_str}] Outside market hours (09:15 - 15:30 IST weekdays). Waiting...")
                time.sleep(60)
                continue

            tf_bars_map, signals, prev_day_info, pdh, pdl = scan_latest_bars(
                args.strategy, allow_reentry=args.reentry, timeframes=active_timeframes
            )

            if not tf_bars_map:
                now_str = datetime.now().strftime("%H:%M:%S")
                print(f"[{now_str}] Waiting for market session bars to load across timeframes...")
                if args.once:
                    break
                time.sleep(args.interval)
                continue

            # Initial startup check-in alert to Telegram channel
            if not startup_announced and not args.once and not args.no_telegram:
                try:
                    startup_msg = (
                        f"🟢 *GENESIS LIVE SCANNER ONLINE & WATCHING*\n\n"
                        f"📅 *Session*: `{datetime.now().strftime('%Y-%m-%d %H:%M')} IST`\n"
                        f"⏱️ *Monitored Timeframes*: `{', '.join(tf.upper() for tf in active_timeframes)}`\n"
                        f"🎯 *Strategies Active*: Breakout & Retest, Trendline Reversal, Liquidity Sweep\n"
                        f"🔄 *Condition Re-Entries*: `{'ENABLED' if args.reentry else 'DISABLED'}`\n\n"
                        f"📊 *Key Session Levels*:\n"
                        f"• Previous Day High (PDH): `{pdh:,.2f}`\n"
                        f"• Previous Day Low (PDL): `{pdl:,.2f}`\n\n"
                        f"🧪 *Forward-Testing Note*:\n"
                        f"Per-share trade tickets will be dispatched to this group the moment strict setup conditions confirm on any active timeframe."
                    )
                    alerts.send_signal(settings, "[genesis] 🟢 Scanner Online", startup_msg)
                    startup_lock.write_text(datetime.now().isoformat())
                    startup_announced = True
                except Exception as e:
                    print(f"⚠️ Could not send startup Telegram alert: {e}")

            # Heartbeat logs for confirmed candles on each timeframe
            for tf in active_timeframes:
                bars = tf_bars_map.get(tf)
                if not bars:
                    continue
                latest_bar = bars[-1]
                latest_time = latest_bar["date"]
                if latest_time != last_processed_candles.get(tf):
                    now_str = datetime.now().strftime("%H:%M:%S")
                    emoji = "⏱️" if tf in ("1m", "2m") else "📈" if tf == "5m" else "🏛️"
                    print(
                        f"[{now_str}] {emoji} [{tf.upper()} Candle Confirmed] {latest_time} | "
                        f"Open: {latest_bar['open']:,.2f} | Close: {latest_bar['close']:,.2f}"
                    )
                    last_processed_candles[tf] = latest_time

            # Active trade monitoring across all open positions
            fastest_tf = next((tf for tf in ("1m", "2m", "5m") if tf in tf_bars_map), active_timeframes[0])
            eval_bars = tf_bars_map[fastest_tf]
            for trade_key, trade_data in list(active_trades.items()):
                updated = track_active_trade(trade_data, eval_bars, settings, args.no_telegram)
                if updated is None:
                    active_trades.pop(trade_key, None)
                else:
                    active_trades[trade_key] = updated

            # Signal dispatch
            if signals:
                for strat_name, tf, sig, sig_bars in signals:
                    sig_key = f"{tf}_{strat_name}_{sig['time']}_{sig['side']}"
                    if sig_key in seen_signal_keys:
                        continue

                    # STRICT SINGLE ACTIVE TRADE LOCK:
                    # Only ONE active trade allowed at a time across all strategies and timeframes!
                    # Prevents multiple overlapping CE/PE calls so the user can easily follow each trade.
                    if active_trades:
                        continue

                    # Reference time for this timeframe's latest candle
                    tf_latest = tf_bars_map.get(tf, [])
                    ref_time = tf_latest[-1]["date"] if tf_latest else ""

                    if sig["time"] == ref_time or sig["time"].startswith(ref_time[:15]) or args.once:
                        seen_signal_keys.add(sig_key)
                        _record_seen_signal(today_str, sig_key)
                        new_trade = broadcast_signal(
                            strat_name, tf, sig, sig_bars, prev_day_info, settings, args.no_telegram
                        )
                        if new_trade:
                            active_trades[sig_key] = new_trade

            if args.once:
                if not signals:
                    print("ℹ️ No active strategy signals found on any monitored timeframe.")
                else:
                    print(f"ℹ️ Total signals detected across timeframes: {len(signals)}")
                    for strat_name, tf, sig, _ in signals:
                        print(f"   • [{tf.upper()}] {strat_name} {sig['side'].upper()} @ {sig['time']}")
                break

            first_trade = next(iter(active_trades.values())) if active_trades else None
            poll_wait = get_poll_interval(first_trade, active_timeframes, args.interval)
            time.sleep(poll_wait)

        except KeyboardInterrupt:
            print("\n👋 Scanner stopped by user.")
            break
        except Exception as e:
            print(f"⚠️ Error during scan loop: {e}")
            if args.once:
                break
            first_trade = next(iter(active_trades.values())) if active_trades else None
            poll_wait = get_poll_interval(first_trade, active_timeframes, args.interval)
            time.sleep(poll_wait)


if __name__ == "__main__":
    main()
