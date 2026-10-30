#!/usr/bin/env python3
"""Genesis Post-Market Refactoring Crew CLI Runner.

Triggers the post-market review pipeline after 15:30 IST:
1. Replays today's market session & trade tape (AutopsyAgent)
2. Compares actual exits vs peak R-multiples & flags missed trades
3. Tests walk-forward parameter calibrations (ParameterAgent)
4. Allocates 50% booked profit to the Safety Wall & sets tomorrow's lots (SafetyWallAgent)
5. Audits system logs, error frequencies & latency (CodeQualityAgent)
6. Generates reports/daily_audit_YYYY-MM-DD.md & dispatches Telegram summary (BriefingAgent)

Usage:
    python scripts/run_post_market_audit.py [--date YYYY-MM-DD] [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pandas as pd
from app.trading.post_market_crew.post_market_manager import PostMarketManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("genesis.run_post_market")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Genesis Post-Market Refactoring Crew")
    parser.add_argument("--date", type=str, default=None, help="Trading date in YYYY-MM-DD (defaults to today)")
    parser.add_argument("--capital", type=float, default=50_000.0, help="Base trading capital (default: 50000)")
    args = parser.parse_args()

    date_str = args.date or datetime.now().strftime("%Y-%m-%d")
    logger.info(f"Initiating Post-Market Refactoring & Audit Crew for {date_str}...")

    # Load today's candle data if available, or generate from session
    df_path = Path(f"data/nifty_intraday_{date_str}.csv")
    if df_path.exists():
        df_intraday = pd.read_csv(df_path)
    else:
        # Default placeholder dataframe for post-market tape review
        df_intraday = pd.DataFrame()

    # Load PDH/PDL levels from storage if available
    pdh, pdl = 23150.0, 22950.0

    manager = PostMarketManager(
        base_capital=args.capital,
        reports_dir=Path("reports"),
        trades_dir=Path("trades"),
    )

    result = manager.run_daily_review(
        df_intraday=df_intraday,
        pdh=pdh,
        pdl=pdl,
        date_str=date_str,
    )

    logger.info(f"✅ Post-Market Audit Complete! Report generated at: {result['report_path']}")
    print(f"\nAudit Report: {result['report_path']}")


if __name__ == "__main__":
    main()
