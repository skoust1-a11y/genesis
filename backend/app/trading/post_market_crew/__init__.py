"""Genesis Post-Market Refactoring & Optimization Crew Package.

Dedicated post-market intelligence crew running after 15:30 IST to audit the day's tape:
- AutopsyAgent: Replays trades, audits exits vs ideal peaks, detects missed setups & root causes
- ParameterAgent: Walk-forward optimization on stop buffers, ADX cutoffs & dynamic R:R
- SafetyWallAgent: Updates Safety Wall profit reserves & determines tomorrow's lot size
- CodeQualityAgent: Scans logs for errors, latency, and code bottlenecks
- BriefingAgent: Generates Daily "Mistakes & Improvements" Markdown and Telegram digest
- PostMarketManager: Master orchestrator driving the post-market review pipeline
"""

from app.trading.post_market_crew.autopsy_agent import AutopsyAgent
from app.trading.post_market_crew.parameter_agent import ParameterAgent
from app.trading.post_market_crew.safety_wall_agent import SafetyWallAgent
from app.trading.post_market_crew.code_quality_agent import CodeQualityAgent
from app.trading.post_market_crew.briefing_agent import BriefingAgent
from app.trading.post_market_crew.post_market_manager import PostMarketManager

__all__ = [
    "AutopsyAgent",
    "ParameterAgent",
    "SafetyWallAgent",
    "CodeQualityAgent",
    "BriefingAgent",
    "PostMarketManager",
]
