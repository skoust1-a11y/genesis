"""Source-only frozen check; never import production-reading admission guards."""
from pathlib import Path
import subprocess

BASELINE = "af8d9f79bc24f9572f1aff13b703795c89403219"
# Exact existing admission manifest, duplicated to avoid importing its state-reading module.
FROZEN = (
    "backend/app/liquidity_signals.py",
    "backend/app/strategies/clarify.py", "backend/app/strategies/store.py",
    *[f"backend/app/trading/crew/{n}.py" for n in (
        "__init__", "entry_agent", "exit_agent", "holding_agent", "journal_agent", "manager",
        "order_flow_agent", "risk_agent", "sniper_agent", "trend_agent")],
    *[f"backend/app/trading/scanner_crew/{n}.py" for n in (
        "__init__", "consolidation_agent", "detector_agent", "feed_agent")],
    "backend/scripts/backtest_pdc_15ema.py",
)


def test_F01_frozen_source_bytes():
    from frozen_source_guard import verify
    assert verify(Path(__file__).resolve().parents[2]) == 18
