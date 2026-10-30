"""Genesis Scanner Crew Package.

Dedicated market awareness, feed management, level extraction, and setup detection crew:
- FeedAgent: Delta-only multi-timeframe polling, Yahoo rate-limit backoff & VWAP proxy
- LevelsAgent: Previous Day High/Low (PDH/PDL), OR-30, and round strike pivots
- ConsolidationAgent: Midday compression box & ATR deadband detector
- NewsAgent: Macro event calendar, heavyweight headlines & event-risk volatility guard
- DetectorAgent: Multi-strategy pattern scanner (Sweeps, Trendline, Waterfall)
- ScannerManager: Master orchestrator producing real-time ScanSnapshots for Trade Crew
"""

from app.trading.scanner_crew.feed_agent import FeedAgent
from app.trading.scanner_crew.levels_agent import LevelsAgent
from app.trading.scanner_crew.consolidation_agent import ConsolidationAgent
from app.trading.scanner_crew.news_agent import NewsAgent, NewsSentiment
from app.trading.scanner_crew.detector_agent import DetectorAgent, CandidateSetup
from app.trading.scanner_crew.scanner_manager import ScannerManager, ScanSnapshot

__all__ = [
    "FeedAgent",
    "LevelsAgent",
    "ConsolidationAgent",
    "NewsAgent",
    "NewsSentiment",
    "DetectorAgent",
    "CandidateSetup",
    "ScannerManager",
    "ScanSnapshot",
]
