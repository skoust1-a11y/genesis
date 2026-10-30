"""ScannerManager: Master Orchestrator for the Scanner Crew.

Unites:
- FeedAgent (Delta-only polling & VWAP)
- LevelsAgent (PDH/PDL, OR-30, strike levels)
- ConsolidationAgent (Midday compression & ATR deadbands)
- NewsAgent (Macro calendar, event risk guard, sentiment)
- DetectorAgent (Multi-strategy pattern detection)

Emits real-time ScanSnapshots directly consumable by TradeManager and live daemons.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
import pandas as pd

from app.trading.scanner_crew.consolidation_agent import ConsolidationAgent, ConsolidationStatus
from app.trading.scanner_crew.detector_agent import CandidateSetup, DetectorAgent
from app.trading.scanner_crew.feed_agent import FeedAgent
from app.trading.scanner_crew.levels_agent import LevelsAgent, SessionLevels
from app.trading.scanner_crew.news_agent import NewsAgent, NewsSentiment

logger = logging.getLogger("genesis.scanner_manager")


@dataclass
class ScanSnapshot:
    timestamp: str
    cmp: float
    levels: SessionLevels
    consolidation: ConsolidationStatus
    news_sentiment: NewsSentiment
    candidate_setups: list[CandidateSetup] = field(default_factory=list)
    actionable_setups: list[CandidateSetup] = field(default_factory=list)


class ScannerManager:
    """Master Orchestration Engine for Market Feeds, Levels & Pattern Detection."""

    def __init__(
        self,
        feed_agent: FeedAgent | None = None,
        levels_agent: LevelsAgent | None = None,
        consolidation_agent: ConsolidationAgent | None = None,
        news_agent: NewsAgent | None = None,
        detector_agent: DetectorAgent | None = None,
    ) -> None:
        self.feed_agent = feed_agent or FeedAgent()
        self.levels_agent = levels_agent or LevelsAgent()
        self.consolidation_agent = consolidation_agent or ConsolidationAgent()
        self.news_agent = news_agent or NewsAgent()
        self.detector_agent = detector_agent or DetectorAgent(
            consolidation_agent=self.consolidation_agent,
            news_agent=self.news_agent,
        )

    def produce_scan_snapshot(
        self,
        df_5m: pd.DataFrame | None = None,
        df_daily: pd.DataFrame | None = None,
        current_time_str: str | None = None,
        timeframes: list[str] | None = None,
        strategy_filter: str = "auto",
    ) -> ScanSnapshot:
        """Run the end-to-end scanner pipeline across 1M, 5M, 15M and return market state."""
        t_str = current_time_str or datetime.now().strftime("%H:%M")
        now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 1. Fetch candles across timeframes
        active_tfs = timeframes or ["1m", "5m", "15m"]
        tf_bars_map: dict[str, pd.DataFrame] = {}
        for tf in active_tfs:
            if tf == "5m" and df_5m is not None:
                tf_bars_map[tf] = df_5m
            else:
                tf_bars_map[tf] = self.feed_agent.fetch_timeframe_candles(tf)

        candles_5m = tf_bars_map.get("5m", pd.DataFrame())
        daily_bars = df_daily if df_daily is not None else self.feed_agent.fetch_timeframe_candles("1d")
        
        cmp = float(candles_5m["close"].iloc[-1]) if not candles_5m.empty else 23000.0

        # 2. Extract Session Levels
        levels = self.levels_agent.extract_session_levels(daily_bars, candles_5m)

        # 3. Check Volatility Compression & Chop
        compression = self.consolidation_agent.evaluate_compression(candles_5m, current_time_str=t_str)

        # 4. Evaluate News Sentiment & Event Risk
        news_sentiment = self.news_agent.evaluate_news_sentiment(current_time_str=t_str)

        # 5. Detect Candidate Setups across all strategies & timeframes
        candidates = self.detector_agent.scan_multi_timeframe_strategies(
            tf_bars_map=tf_bars_map,
            levels=levels,
            news_sentiment=news_sentiment,
            current_time_str=t_str,
            strategy_filter=strategy_filter,
        )

        actionable = [c for c in candidates if c.is_valid_for_trade_crew]

        return ScanSnapshot(
            timestamp=now_ts,
            cmp=cmp,
            levels=levels,
            consolidation=compression,
            news_sentiment=news_sentiment,
            candidate_setups=candidates,
            actionable_setups=actionable,
        )
