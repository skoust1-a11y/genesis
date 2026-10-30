"""Genesis Live Trading Crew Package.

Modular, low-latency, deterministic multi-agent crew for live market execution:
- EntryAgent: Multi-strategy scanner & micro-wick execution with capped stops
- TrendAgent: Macro trend, ADX/chop filter, runway surveyor, dynamic R:R calculator
- RiskAgent: Institutional sizing, Safety Wall enforcement, daily loss circuit breaker
- ExitAgent: Hard target hits, 5M structure trailing, opposing reversal auto-exit
- AlertAgent: Resilient Telegram dispatcher with formatted tickets & CMP heartbeats
- JournalAgent: SQLite SSOT persistence, active state caching, Excel sync
- TradeManager: Master orchestrator binding the live crew together
"""

from app.trading.crew.entry_agent import EntryAgent
from app.trading.crew.trend_agent import TrendAgent
from app.trading.crew.risk_agent import RiskAgent
from app.trading.crew.exit_agent import ExitAgent
from app.trading.crew.alert_agent import AlertAgent
from app.trading.crew.journal_agent import JournalAgent
from app.trading.crew.order_flow_agent import OrderFlowAgent, OrderFlowSignal
from app.trading.crew.manager import TradeManager

__all__ = [
    "EntryAgent",
    "TrendAgent",
    "RiskAgent",
    "ExitAgent",
    "AlertAgent",
    "JournalAgent",
    "OrderFlowAgent",
    "OrderFlowSignal",
    "TradeManager",
]
