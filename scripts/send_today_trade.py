"""Send Today's Trade Signal to Telegram & Update the Strategy-Wise Excel Journal.

Features:
1. Scans today's price action for trade signals across all strategies (Breakout & Retest, Trendline Reversal, Liquidity Sweep).
2. Computes the optimal option contract, strike selection, entry premium, staged exits, and risk limits.
3. Generates institutional spot structure and option charts.
4. Broadcasts formatted alert and charts to the Telegram signals channel.
5. Automatically syncs and updates the strategy-wise Excel journals in the `trades/` folder.

Usage:
    python scripts/send_today_trade.py
    python scripts/send_today_trade.py --no-telegram   # dry-run / excel update only
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

# Paths
ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(BACKEND_DIR))

from app import alerts
from app.config import get_settings
from app.liquidity_signals import (
    EXIT,
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
from scripts.maintain_trades_excel import main as update_trades_excel


def main():
    parser = argparse.ArgumentParser(description="Send today's trade signal and update Excel journal")
    parser.add_argument("--no-telegram", action="store_true", help="Do not send to Telegram (dry run)")
    parser.add_argument("--strategy", choices=["trendline", "retest", "sweep", "auto"], default="auto")
    args = parser.parse_args()

    settings = get_settings()
    today_str = datetime.now().strftime("%Y-%m-%d")

    print(f"Fetching live session data for {today_str}...")
    bars, above, daily = session_data(range_="5d")
    today_bars = [b for b in bars if b["date"].startswith(today_str)]

    if not today_bars:
        print(f"No bars found for today ({today_str}). Markets may be closed.")
        return

    # Find previous trading day's high/low
    prev_dates = sorted(set(b["date"][:10] for b in bars if b["date"][:10] < today_str))
    if not prev_dates:
        print("Could not find previous day's bars.")
        return

    prev_day_bars = [b for b in bars if b["date"].startswith(prev_dates[-1])]
    pdh = max(b["high"] for b in prev_day_bars)
    pdl = min(b["low"] for b in prev_day_bars)
    pd_close = prev_day_bars[-1]["close"]
    pd_open = prev_day_bars[0]["open"]

    print(f"Previous Day ({prev_dates[-1]}): PDH {pdh:,.2f} | PDL {pdl:,.2f}")
    print(f"Today ({today_str}): {len(today_bars)} 5m bars. Open: {today_bars[0]['open']:,.2f}, Latest: {today_bars[-1]['close']:,.2f}")

    # Detect signals
    signals = []
    if args.strategy in ("auto", "trendline"):
        for s in scan_trendline_reversal(today_bars, skip_lunch=False):
            signals.append(("Trendline Reversal", s))
    if args.strategy in ("auto", "retest"):
        for s in scan_retest(today_bars, above, pdh, pdl, skip_lunch=False):
            signals.append(("Breakout & Retest", s))
    if args.strategy in ("auto", "sweep"):
        for s in scan(today_bars, above, pdh, pdl, skip_lunch=False):
            signals.append(("Liquidity Sweep", s))

    if not signals:
        print("No active trade signals detected for today.")
        return

    # Sort signals chronologically so signals[-1] is the latest trigger
    signals.sort(key=lambda item: item[1]["time"])

    # Prioritize the most recent signal
    strat_name, sig = signals[-1]
    print(f"\n🎯 Selected Signal: [{strat_name}] {sig['side'].upper()} at {sig['time'][11:]} IST")
    print(f"   Entry: {sig['entry']:,.2f} | Stop: {sig['stop']:,.2f} | Target: {sig['target']:,.2f} | Risk: {sig['risk']:.1f} pts")

    # Calculate Option Plan
    plan = option_plan(sig, settings)
    if not plan:
        print("Could not generate option plan.")
        return

    print(f"   Option Contract: {plan['option']} ({plan['expiry']}) @ ~Rs {plan['entry_premium']:,.2f}")

    # Generate Levels & Charts
    prev_day_info = [{"date": prev_dates[-1], "open": pd_open, "high": pdh, "low": pdl, "close": pd_close}]
    lvls = trading_levels(prev_day_info, session_over=True) + intraday_levels(today_bars, 3)

    body = message(sig, settings, plan, lvls)
    pngs = charts(today_bars, sig, lvls, plan, settings)
    print(f"   Generated {len(pngs)} chart visuals.")

    # Send to Telegram
    if not args.no_telegram:
        stop_p, target_p = plan["exits"][0]["premium"], plan["exits"][1]["premium"]
        opt_t1 = round(plan["entry_premium"] + (target_p - plan["entry_premium"]) / 3, 2)
        opt_t2 = round(plan["entry_premium"] + 2 * (target_p - plan["entry_premium"]) / 3, 2)

        opt_caption = (
            f"🎯 [{strat_name.upper()}] NIFTY {plan['option']} ({plan['expiry']})\n"
            f"Strategy: {strat_name} | Action: BUY\n"
            f"CMP: ~Rs {plan['entry_premium']:,.2f} | Stop Loss: Rs {stop_p:,.2f} (-20%)\n\n"
            f"🎯 Staged Targets (2-3 Lots):\n"
            f"• T1 (Rs {opt_t1:,.2f}): Move SL to Cost (Risk-Free)\n"
            f"• T2 (Rs {opt_t2:,.2f}): Book 50% Lots (Lock Profit)\n"
            f"• T3 (Rs {target_p:,.2f}): Book Remaining Runner (3R)\n\n"
            f"📊 Risk & Sizing (Rs 50k Capital):\n"
            f"• 2 Lots (130 qty): ~Rs {-plan['exits'][0]['pnl']:,.0f} total risk\n"
            f"• 3 Lots (195 qty): ~Rs {-plan['exits'][0]['pnl'] * 1.5:,.0f} total risk\n\n"
            f"💡 Setup: {sig['side']} {sig.get('pool', strat_name)} at {sig['time'][11:]} IST"
        )

        nifty_caption = (
            f"📊 [{strat_name.upper()}] NIFTY 5m Structure & Levels ({today_str})\n"
            f"• Strategy: {strat_name} ({sig['side'].upper()})\n"
            f"• Setup: {sig.get('pool', strat_name)} at {sig['time'][11:]} IST\n"
            f"• Index Entry: {sig['entry']:,.2f} | Stop: {sig['stop']:,.2f} | Target: {sig['target']:,.2f}\n"
            f"• Risk: {sig['risk']:.1f} pts | Reward: {abs(sig['target'] - sig['entry']):.1f} pts (1:3 R:R)\n"
            f"• Trail Rule: Once +1R is hit, move SL to breakeven immediately"
        )

        print("\nSending alerts to Telegram...")
        sent_channels = alerts.send_signal(settings, f"[genesis] NIFTY {sig['side'].upper()} signal", body)
        print(f"✅ Text alert sent via: {sent_channels}")

        res_opt = alerts.send_signal_photo(settings, opt_caption[:1024], pngs[0])
        print(f"✅ Option chart sent: {res_opt}")

        res_spot = alerts.send_signal_photo(settings, nifty_caption[:1024], pngs[1])
        print(f"✅ Spot chart sent: {res_spot}")

    # Synchronize Strategy-Wise Excel Journal
    print("\nUpdating Strategy-Wise Excel Journal...")
    update_trades_excel([])
    print("✅ Excel journal updated successfully!")


if __name__ == "__main__":
    main()
