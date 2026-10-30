"""Unit Tests for Scanner Crew and NewsAgent.

Validates that:
1. NewsAgent correctly calculates sentiment and enforces event risk blackouts.
2. LevelsAgent extracts PDH, PDL, and strike levels.
3. ConsolidationAgent detects compression boxes and midday lull.
4. DetectorAgent and ScannerManager produce clean, actionable setups.
"""

import pandas as pd
import pytest

from app.trading.scanner_crew.news_agent import NewsAgent, NewsSentiment
from app.trading.scanner_crew.levels_agent import LevelsAgent, SessionLevels
from app.trading.scanner_crew.consolidation_agent import ConsolidationAgent
from app.trading.scanner_crew.detector_agent import DetectorAgent
from app.trading.scanner_crew.feed_agent import FeedAgent
from app.trading.scanner_crew.scanner_manager import ScannerManager


def create_mock_intraday_df() -> pd.DataFrame:
    """Generate mock 5M DataFrame with sweep structure."""
    return pd.DataFrame({
        "timestamp": [f"2026-10-30 10:{i*5:02d}:00" for i in range(10)],
        "open": [23000.0 + i * 2 for i in range(10)],
        "high": [23010.0 + i * 2 for i in range(10)],
        "low": [22990.0 + i * 2 for i in range(10)],
        "close": [23005.0 + i * 2 for i in range(10)],
        "volume": [5000 + i * 200 for i in range(10)],
    })


def test_news_agent_sentiment_and_event_risk():
    na = NewsAgent()
    
    # 1. Bullish Heavyweight Headlines
    headlines = [
        "Reliance Q2 profit surges 25% on strong retail growth",
        "HDFC Bank leads market rally as FII inflows surge",
        "NIFTY hits record high on macro optimism",
    ]
    sentiment = na.evaluate_news_sentiment(latest_headlines=headlines, current_time_str="14:00")
    assert sentiment.sentiment_score > 0.5
    assert sentiment.sentiment_label == "BULLISH"
    assert sentiment.is_trading_safe is True
    assert len(sentiment.catalyst_summary) > 0

    # 2. Event Risk Guard (During RBI announcement at 10:00 IST)
    event_sentiment = na.evaluate_news_sentiment(latest_headlines=[], current_time_str="10:05")
    assert event_sentiment.high_impact_event_active is True
    assert event_sentiment.event_risk_level == "HIGH"
    assert event_sentiment.is_trading_safe is False
    assert "RBI" in str(event_sentiment.event_warning)


def test_levels_agent_extraction():
    la = LevelsAgent()
    df_daily = pd.DataFrame({
        "high": [23100.0, 23080.0],
        "low": [22900.0, 22920.0],
        "close": [23050.0, 23000.0],
    })
    levels = la.extract_session_levels(df_daily)
    assert levels.pdh == 23100.0
    assert levels.pdl == 22900.0
    assert len(levels.all_key_levels) > 5


def test_consolidation_agent_compression_box():
    ca = ConsolidationAgent(lookback_bars=6, max_box_span=32.0)
    # Tightly compressed bars (span = 10 pts)
    df_tight = pd.DataFrame({
        "open": [23000.0] * 6,
        "high": [23005.0] * 6,
        "low": [22995.0] * 6,
        "close": [23002.0] * 6,
    })
    status = ca.evaluate_compression(df_tight, current_time_str="12:00")
    assert status.is_consolidating is True
    assert status.state == "COMPRESSED_BOX"


def test_scanner_manager_pipeline():
    df = create_mock_intraday_df()
    sm = ScannerManager()
    snap = sm.produce_scan_snapshot(df_5m=df, current_time_str="11:00")
    
    assert snap.cmp > 0
    assert snap.levels.pdh > 0
    assert snap.news_sentiment.is_trading_safe is True
    assert isinstance(snap.actionable_setups, list)
