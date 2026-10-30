#!/usr/bin/env python3
"""Genesis Multi-Timeframe Institutional Scanner & Trading Daemon.

Orchestrated by the 3 Specialized Crews:
1. ScannerCrew (app.trading.scanner_crew):
   - FeedAgent: Delta-only multi-timeframe polling & VWAP proxy
   - LevelsAgent: PDH/PDL, OR-30 & strike levels
   - ConsolidationAgent: Midday compression box & ATR deadband detector
   - NewsAgent: Macro sentiment, heavyweight catalysts & event risk guard
   - DetectorAgent: Multi-strategy pattern scanner (Sweeps, Trendline, Waterfall)
2. TradeCrew (app.trading.crew):
   - TradeManager: Master live trade orchestrator
   - TrendAgent: Macro trend slope & dynamic R:R (+1.8R to +4.5R)
   - RiskAgent & OrderFlowAgent: Institutional sizing, absorption trap veto & whale tranches
   - ExitAgent: Unconditional Target 3 exit & 5M structure trailing
   - AlertAgent: Resilient Telegram tickets & CMP heartbeats
   - JournalAgent: SQLite SSOT, active cache & Excel synchronization
3. PostMarketCrew (app.trading.post_market_crew):
   - PostMarketManager: Daily autopsy, walk-forward optimization, 50% Safety Wall sweep
     and Telegram executive digest automatically dispatched at 15:31 IST.

Usage:
    python scripts/live_scanner.py                         # Run live multi-timeframe daemon (1m, 5m, 15m)
    python scripts/live_scanner.py --timeframes 1m,5m      # Monitor specific timeframes
    python scripts/live_scanner.py --once                  # Run single scan across timeframes & exit
    python scripts/live_scanner.py --no-telegram           # Dry run (detect & log without sending)
    python scripts/live_scanner.py --capital 100000        # Custom starting capital
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

# Paths
ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(BACKEND_DIR))

import pandas as pd
from app.trading.crew.manager import TradeManager
from app.trading.execution_crew.execution_manager import ExecutionManager
from app.trading.post_market_crew.post_market_manager import PostMarketManager
from app.trading.pre_market_crew.pre_market_manager import PreMarketManager
from app.trading.scanner_crew.scanner_manager import ScannerManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("genesis.live_scanner")

MARKET_OPEN = "09:15"
MARKET_CLOSE = "15:30"
SUPPORTED_TIMEFRAMES = ["1m", "2m", "5m", "15m", "30m"]
DEFAULT_TIMEFRAMES = ["1m", "5m", "15m"]


def is_market_hours() -> bool:
    """Check if current time is within Indian equity market hours (09:15 - 15:30 IST weekdays)."""
    now_dt = datetime.now()
    if now_dt.weekday() >= 5:  # Saturday or Sunday
        return False
    current_time = now_dt.strftime("%H:%M")
    return MARKET_OPEN <= current_time <= MARKET_CLOSE


def main() -> None:
    parser = argparse.ArgumentParser(description="Genesis Crew-Powered Multi-Timeframe Institutional Scanner")
    parser.add_argument("--strategy", type=str, default="auto", help="Strategy filter (auto, sweep, trendline, retest)")
    parser.add_argument("--reentry", action="store_true", default=True, help="Enable condition re-entries")
    parser.add_argument("--interval", type=int, default=10, help="Adaptive polling interval in seconds (default 10s)")
    parser.add_argument("--once", action="store_true", help="Run once across all timeframes and exit")
    parser.add_argument("--no-telegram", action="store_true", help="Suppress Telegram broadcasts (dry-run mode)")
    parser.add_argument("--capital", type=float, default=50_000.0, help="Base trading capital (default: 50000)")
    parser.add_argument(
        "--timeframes",
        type=str,
        default=",".join(DEFAULT_TIMEFRAMES),
        help=f"Comma-separated list of timeframes to scan. Supported: {','.join(SUPPORTED_TIMEFRAMES)}",
    )
    args = parser.parse_args()

    active_timeframes = [tf.strip().lower() for tf in args.timeframes.split(",") if tf.strip()]
    today_str = datetime.now().strftime("%Y-%m-%d")

    print("=======================================================")
    print("  Genesis Multi-Timeframe Institutional Scanner Daemon")
    print(f"  Architecture: 5 Specialized Autonomous Crews")
    print(f"  (PreMarketCrew + ScannerCrew + TradeCrew + ExecutionCrew + PostMarketCrew)")
    print(f"  Mode: {'Single Scan' if args.once else 'Live Daemon'}")
    print(f"  Telegram Alerts: {'DISABLED' if args.no_telegram else 'ENABLED'}")
    print(f"  Monitored Timeframes: {', '.join(tf.upper() for tf in active_timeframes)}")
    print(f"  Base Capital: ₹{args.capital:,.2f}")
    print("=======================================================")

    # 1. Initialize the 5 Specialized Autonomous Crews
    scanner_manager = ScannerManager()
    trade_manager = TradeManager(
        base_capital=args.capital,
        is_paper_trading=True,
        trades_dir=ROOT_DIR / "trades",
        db_path=ROOT_DIR / "data" / "trades.db",
    )
    execution_manager = ExecutionManager(
        is_live_broker=False,
    )
    pre_market_manager = PreMarketManager(
        trades_dir=ROOT_DIR / "trades",
    )
    post_market_manager = PostMarketManager(
        base_capital=args.capital,
        reports_dir=ROOT_DIR / "reports",
        trades_dir=ROOT_DIR / "trades",
    )

    if args.no_telegram:
        trade_manager.alert_agent.bot_token = None
        post_market_manager.briefing_agent.bot_token = None
        pre_market_manager.bot_token = None

    # Load dynamic calibrated strategy parameters (Self-Improvement Loop)
    params_file = ROOT_DIR / "trades" / "active_strategy_params.json"
    if params_file.exists():
        try:
            with open(params_file, "r", encoding="utf-8") as pf:
                active_params = json.load(pf)
                if "otm_steps" in active_params:
                    scanner_manager.detector_agent.otm_steps = int(active_params["otm_steps"])
                    trade_manager.entry_agent.otm_steps = int(active_params["otm_steps"])
                if "micro_wick_stop_buffer" in active_params:
                    trade_manager.entry_agent.default_stop_buffer = float(active_params["micro_wick_stop_buffer"])
                print(f"  Calibrated Strategy Parameters: Loaded from {params_file.name} (OTM: {scanner_manager.detector_agent.otm_steps})")
        except Exception as pe:
            logger.warning(f"Could not load dynamic strategy parameters: {pe}")

    last_candle_times: dict[str, str] = {}
    last_flat_heartbeat = time.time()
    startup_lock = ROOT_DIR / "trades" / f".startup_{today_str}.lock"
    pre_market_lock = ROOT_DIR / "trades" / f".pre_market_{today_str}.lock"
    post_market_lock = ROOT_DIR / "reports" / f".post_audit_{today_str}.lock"

    # Pre-Market Setup Run (Runs once per day before open)
    if not pre_market_lock.exists() and not args.once and not args.no_telegram:
        try:
            snapshot = scanner_manager.scan_feed(timeframes=["1d"])
            pdc_val = float(snapshot.levels.pdc) if snapshot.levels.pdc > 0 else 23000.0
            pre_market_manager.run_pre_market_setup(pdc=pdc_val, date_str=today_str)
            pre_market_lock.write_text(datetime.now().isoformat())
            print("  Pre-Market Setup: Battleplan compiled & dispatched to Telegram")
        except Exception as pme:
            logger.warning(f"Could not run pre-market setup: {pme}")

    # Startup Announcement
    if not startup_lock.exists() and not args.once and not args.no_telegram:
        try:
            startup_msg = (
                f"🟢 *GENESIS 3-CREW SCANNER ONLINE*\n\n"
                f"📅 *Session*: `{datetime.now().strftime('%Y-%m-%d %H:%M')} IST`\n"
                f"⏱️ *Monitored Frames*: `{', '.join(tf.upper() for tf in active_timeframes)}`\n"
                f"⚙️ *Engines*: `ScannerCrew` + `TradeCrew` + `PostMarketCrew`\n"
                f"🛡️ *Risk Management*: Order Flow Sizing & 55pt Risk Cap Active\n"
                f"📊 *Execution*: Micro-Wick Sniper Entries & Structure Trailing"
            )
            trade_manager.alert_agent._send(startup_msg)
            startup_lock.write_text(datetime.now().isoformat())
        except Exception as e:
            logger.warning(f"Could not send startup Telegram alert: {e}")

    # Main Execution Loop
    while True:
        try:
            cur_time_str = datetime.now().strftime("%H:%M")
            now_full_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # -------------------------------------------------------------
            # Outside Market Hours: Automated Post-Market Review Handoff
            # -------------------------------------------------------------
            if not args.once and not is_market_hours():
                if not post_market_lock.exists() and cur_time_str >= "15:31":
                    print(f"[{now_full_str}] 📋 Market Closed. Triggering Automated Post-Market Review...")
                    try:
                        res = post_market_manager.run_daily_review(
                            df_intraday=pd.DataFrame(),
                            pdh=23150.0,
                            pdl=22950.0,
                            date_str=today_str,
                        )
                        post_market_lock.write_text(datetime.now().isoformat())
                        print(f"[{now_full_str}] ✅ Post-Market Review Completed: {res['report_path']}")
                    except Exception as pme:
                        logger.error(f"Error executing post-market review: {pme}")

                print(f"[{now_full_str}] Outside market hours (09:15 - 15:30 IST weekdays). Waiting...")
                time.sleep(60)
                continue

            # -------------------------------------------------------------
            # Step 1: Scanner Crew Market Snapshot (1M, 5M, 15M + News + Levels)
            # -------------------------------------------------------------
            snapshot = scanner_manager.produce_scan_snapshot(
                current_time_str=cur_time_str,
                timeframes=active_timeframes,
                strategy_filter=args.strategy,
            )
            candles_5m = scanner_manager.feed_agent.fetch_timeframe_candles("5m")

            # Candle confirmation logs
            if not candles_5m.empty:
                latest_bar = candles_5m.iloc[-1]
                b_time = str(latest_bar.get("timestamp", ""))
                if b_time != last_candle_times.get("5m"):
                    last_candle_times["5m"] = b_time
                    c_close = float(latest_bar.get("close", 0.0))
                    print(f"[{cur_time_str}:00] 📈 [5M Candle Confirmed] {b_time} | Spot Close: {c_close:,.2f} | News: {snapshot.news_sentiment.sentiment_label}")

            # -------------------------------------------------------------
            # Step 2: Trade Crew Real-Time Execution & Position Management
            # -------------------------------------------------------------
            tick_result = trade_manager.on_market_tick(
                cmp=snapshot.cmp,
                df_5m=candles_5m,
                pdh=snapshot.levels.pdh,
                pdl=snapshot.levels.pdl,
                key_levels=snapshot.levels.all_key_levels,
                morning_floor=snapshot.levels.or30_low,
                candidate_setups=snapshot.actionable_setups,
                date_str=today_str,
            )

            for act in tick_result.get("actions", []):
                print(f"⚡ Trade Action Executed: {act['action']} on {act.get('trade_id')} ({act.get('reason', '')})")

            # -------------------------------------------------------------
            # Periodic Flat Heartbeat (Every 60 mins when no trades open)
            # -------------------------------------------------------------
            if tick_result.get("active_count", 0) == 0 and not args.no_telegram and is_market_hours():
                now_ts = time.time()
                if now_ts - last_flat_heartbeat >= 60 * 60:
                    last_flat_heartbeat = now_ts
                    hb_msg = (
                        f"🟢 *SCANNER CREW HEARTBEAT* `[{cur_time_str} IST]`\n\n"
                        f"• NIFTY Spot: `{snapshot.cmp:,.2f}`\n"
                        f"• Market State: `{snapshot.consolidation.state}`\n"
                        f"• Macro Sentiment: `{snapshot.news_sentiment.sentiment_label}` ({snapshot.news_sentiment.sentiment_score:+.2f})\n"
                        f"• Status: ⚪ *FLAT* (Capital protected on sidelines)\n"
                        f"• Monitored Levels: PDH `{snapshot.levels.pdh:,.2f}` | PDL `{snapshot.levels.pdl:,.2f}`"
                    )
                    trade_manager.alert_agent._send(hb_msg)

            if args.once:
                print(f"[{now_full_str}] Single scan complete across timeframes.")
                break

            time.sleep(args.interval)

        except KeyboardInterrupt:
            print("\nShutting down Genesis Scanner Daemon cleanly.")
            break
        except Exception as e:
            logger.error(f"Unexpected error in live scanner loop: {e}", exc_info=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
