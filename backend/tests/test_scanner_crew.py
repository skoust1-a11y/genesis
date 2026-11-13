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
    assert not na.evaluate_news_sentiment(latest_headlines=[], current_time_str="10:05").high_impact_event_active
    event_sentiment = na.evaluate_news_sentiment(
        latest_headlines=["RBI monetary policy announcement today"], current_time_str="10:05")
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


def test_scanner_manager_pipeline(monkeypatch):
    df = create_mock_intraday_df()
    monkeypatch.setattr(FeedAgent, "fetch_timeframe_candles", lambda self, *args, **kwargs: df.copy())
    sm = ScannerManager()
    snap = sm.produce_scan_snapshot(df_5m=df, current_time_str="11:00")
    
    assert snap.cmp > 0
    assert snap.levels.pdh > 0
    assert snap.news_sentiment.is_trading_safe is True
    assert isinstance(snap.actionable_setups, list)


def test_levels_agent_eqh_eql_detection():
    agent = LevelsAgent()
    # Create synthetic DataFrame with two equal peaks and two equal troughs
    bars = [
        {"date": f"2026-11-02 09:{15 + i*5}", "open": 23200, "high": 23210, "low": 23190, "close": 23205}
        for i in range(16)
    ]
    # Equal Highs at 23,280 (bars 2 and 8) with a trough at bar 5 (23,220)
    bars[2] = {"date": "2026-11-02 09:25", "open": 23260, "high": 23280.0, "low": 23250, "close": 23275}
    bars[5] = {"date": "2026-11-02 09:40", "open": 23230, "high": 23240.0, "low": 23220.0, "close": 23225}
    bars[8] = {"date": "2026-11-02 09:55", "open": 23265, "high": 23281.0, "low": 23255, "close": 23278}

    # Equal Lows at 23,150 (bars 10 and 14) with a peak at bar 12 (23,200)
    bars[10] = {"date": "2026-11-02 10:05", "open": 23160, "high": 23170.0, "low": 23150.0, "close": 23155}
    bars[12] = {"date": "2026-11-02 10:15", "open": 23180, "high": 23200.0, "low": 23175.0, "close": 23195}
    bars[14] = {"date": "2026-11-02 10:25", "open": 23165, "high": 23175.0, "low": 23151.0, "close": 23160}

    df = pd.DataFrame(bars)
    eqh, eql = agent.detect_equal_highs_lows(df, tol_pts=4.0)

    assert any(abs(h - 23280.5) <= 1.0 for h in eqh)
    assert any(abs(l - 23150.5) <= 1.0 for l in eql)


def test_detector_agent_eqh_sweep():
    detector = DetectorAgent()
    bars = [
        {"date": f"2026-11-02 09:{30 + i*5}", "open": 23250, "high": 23260, "low": 23240, "close": 23255}
        for i in range(5)
    ]
    # 6th bar sweeps 23280 EQH to 23288, closes back below at 23265
    bars.append({"date": "2026-11-02 10:05", "open": 23272, "high": 23288, "low": 23260, "close": 23265})
    levels = SessionLevels(
        pdh=23350, pdl=23100, pdc=23200, pivot=23200, r1=23300, s1=23100,
        eqh_levels=[23280.0], eql_levels=[], all_key_levels=[23280.0]
    )
    setups = detector.scan_multi_timeframe_strategies(
        tf_bars_map={"5m": bars},
        levels=levels,
        strategy_filter="equal",
    )
    assert any("Equal Highs Sweep" in s.strategy and s.direction == "PUT" for s in setups)


def test_scan_order_keeps_every_setup(monkeypatch):
    def one(**extra):
        row = {"risk": 20.0, "entry": 24000.0, "stop": 23980.0, "target": 24060.0, "side": "Long", "time": "2026-11-02 11:20"}
        row.update(extra)
        return [row]

    monkeypatch.setattr("app.liquidity_signals.scan_trendline_reversal", lambda *_a, **_k: one())
    monkeypatch.setattr(
        "app.liquidity_signals.scan_retest",
        lambda *_a, **_k: one(pool="PDH Support") + one(pool="OR-30 High"),
    )
    monkeypatch.setattr("app.liquidity_signals.scan", lambda *_a, **_k: one())
    bars = [
        {"date": f"2026-11-02 11:{15 + i * 5:02d}", "open": 100, "high": 102, "low": 98, "close": 101, "volume": 0}
        for i in range(6)
    ]
    setups = DetectorAgent().scan_multi_timeframe_strategies(
        tf_bars_map={"5m": bars},
        levels=SessionLevels(pdh=110, pdl=90, pdc=100, pivot=100, r1=110, s1=90, all_key_levels=[100]),
        news_sentiment=NewsSentiment(
            sentiment_score=0.0, sentiment_label="NEUTRAL", event_risk_level="LOW",
            is_trading_safe=True, high_impact_event_active=False, event_warning=None, catalyst_summary=[],
        ),
        current_time_str="11:40",
    )
    names = [s.strategy for s in setups]
    assert "Trendline Reversal" in names[0]
    assert names[1].startswith("Breakout & Retest (PD)")
    assert "OR-30" in names[2]
    assert names[3].startswith("Liquidity Sweep")
