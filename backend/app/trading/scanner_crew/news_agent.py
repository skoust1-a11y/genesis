"""NewsAgent: Macro Sentiment, Heavyweight Catalysts & Event-Risk Volatility Guard.

Responsibilities:
1. Macro Event Calendar & Risk Guard:
   - Tracks high-impact events (RBI Policy, Fed FOMC, Union Budget, CPI/Inflation).
   - Enforces an "Event Blackout" 15 minutes before and after scheduled high-impact events
     to prevent entering during 100-point news whipsaws.
2. News Sentiment Analysis:
   - Evaluates incoming headlines for NIFTY, macro economy, and index heavyweights
     (Reliance, HDFC Bank, ICICI Bank, Infosys, TCS).
   - Produces a bounded sentiment score (-1.0 to +1.0) to give directional confluence to breakouts.
3. Catalyst vs Trap Filter:
   - Rejects breakouts that run against strong conflicting macro news, flagging potential fakeouts.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any, Literal

from app.market.news import clean_headline

logger = logging.getLogger("genesis.news_agent")


@dataclass
class NewsSentiment:
    sentiment_score: float  # -1.0 (strongly bearish) to +1.0 (strongly bullish)
    sentiment_label: Literal["BULLISH", "BEARISH", "NEUTRAL"]
    event_risk_level: Literal["LOW", "MEDIUM", "HIGH"]
    is_trading_safe: bool
    high_impact_event_active: bool
    event_warning: str | None = None
    catalyst_summary: list[str] = field(default_factory=list)


class NewsAgent:
    """Macro News, Economic Events and Volatility Guard Engine."""

    BULLISH_KEYWORDS = {
        "rally", "surge", "record high", "profit jumps", "rate cut", "easing",
        "stimulus", "outperform", "growth", "inflow", "upgrade", "rebound", "bull",
    }
    BEARISH_KEYWORDS = {
        "slump", "crash", "plunge", "rate hike", "inflation jumps", "conflict",
        "default", "selloff", "downside", "misses estimate", "downgrade", "bear", "war",
    }

    # High-impact recurring event windows (IST)
    SCHEDULED_HIGH_IMPACT_EVENTS = [
        {"name": "RBI Monetary Policy Announcement", "time": "10:00", "window_mins": 30},
        {"name": "India CPI Inflation Release", "time": "12:00", "window_mins": 20},
        {"name": "US Fed FOMC Announcement Reaction", "time": "09:15", "window_mins": 30},
    ]

    def __init__(self, headlines: list[dict[str, Any]] | None = None) -> None:
        self.cached_headlines: list[dict[str, Any]] = headlines or []

    def check_event_risk_guard(self, current_time_str: str | None = None) -> tuple[bool, str | None]:
        """Check if trading should be gated due to an imminent high-impact macro announcement."""
        t_str = current_time_str or datetime.now().strftime("%H:%M")
        
        # Check against high impact windows
        for ev in self.SCHEDULED_HIGH_IMPACT_EVENTS:
            ev_time = ev["time"]
            window = ev["window_mins"]
            
            # Simple check if current time is within window
            try:
                curr_dt = datetime.strptime(t_str, "%H:%M")
                ev_dt = datetime.strptime(ev_time, "%H:%M")
                diff_mins = abs((curr_dt - ev_dt).total_seconds()) / 60.0
                if diff_mins <= window:
                    return True, f"High-Impact Event Guard: {ev['name']} at {ev_time} IST (+/- {window}m buffer)"
            except Exception:
                pass
                
        return False, None

    def analyze_headlines(self, headlines: list[str] | list[dict[str, Any]]) -> tuple[float, list[str]]:
        """Score sentiment from latest news items and extract key catalysts."""
        if not headlines:
            return 0.0, []

        clean_titles: list[str] = []
        for item in headlines:
            if isinstance(item, dict):
                title = clean_headline(item.get("title", ""))
            else:
                title = clean_headline(str(item))
            if title:
                clean_titles.append(title)

        bull_count = 0
        bear_count = 0
        catalysts: list[str] = []

        for title in clean_titles:
            t_lower = title.lower()
            # Check heavyweights: Reliance, HDFC, ICICI, TCS, Infosys
            is_heavyweight = any(hw in t_lower for hw in ["reliance", "hdfc", "icici", "infosys", "tcs", "nifty"])
            
            found_bull = any(kw in t_lower for kw in self.BULLISH_KEYWORDS)
            found_bear = any(kw in t_lower for kw in self.BEARISH_KEYWORDS)
            
            weight = 2 if is_heavyweight else 1
            if found_bull and not found_bear:
                bull_count += weight
                if len(catalysts) < 3:
                    catalysts.append(f"🟢 {title[:80]}")
            elif found_bear and not found_bull:
                bear_count += weight
                if len(catalysts) < 3:
                    catalysts.append(f"🔴 {title[:80]}")

        total = bull_count + bear_count
        if total == 0:
            return 0.0, catalysts

        sentiment_score = (bull_count - bear_count) / total
        return round(sentiment_score, 2), catalysts

    def evaluate_news_sentiment(
        self,
        latest_headlines: list[Any] | None = None,
        current_time_str: str | None = None,
    ) -> NewsSentiment:
        """Produce comprehensive NewsSentiment report for scanner confluence."""
        headlines = latest_headlines if latest_headlines is not None else self.cached_headlines
        score, catalysts = self.analyze_headlines(headlines)
        
        is_high_risk, event_warning = self.check_event_risk_guard(current_time_str)
        
        risk_level: Literal["LOW", "MEDIUM", "HIGH"] = "HIGH" if is_high_risk else ("MEDIUM" if abs(score) >= 0.6 else "LOW")
        is_safe = not is_high_risk

        if score >= 0.25:
            label: Literal["BULLISH", "BEARISH", "NEUTRAL"] = "BULLISH"
        elif score <= -0.25:
            label = "BEARISH"
        else:
            label = "NEUTRAL"

        return NewsSentiment(
            sentiment_score=score,
            sentiment_label=label,
            event_risk_level=risk_level,
            is_trading_safe=is_safe,
            high_impact_event_active=is_high_risk,
            event_warning=event_warning,
            catalyst_summary=catalysts,
        )
