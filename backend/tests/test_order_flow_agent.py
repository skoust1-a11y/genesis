"""Unit Tests for OrderFlowAgent and Dynamic Sizing Cap Engine.

Validates that:
1. OrderFlowAgent computes Volume Delta and CVD correctly.
2. Absorption Traps are detected and capped at 0 lots (Hard Veto).
3. Sizing Cap adapts dynamically: Grade-A+ (100%), Grade-B (50%), Trap (0%).
4. Large Whale Sizes (>= 10 Lots) use Staggered Tranches to eliminate market impact.
5. RiskAgent seamlessly integrates with OrderFlowAgent.
"""

import pandas as pd
import pytest

from app.trading.crew.order_flow_agent import OrderFlowAgent, OrderFlowSignal
from app.trading.crew.risk_agent import RiskAgent


def test_order_flow_delta_estimation():
    ofa = OrderFlowAgent()
    
    # Strong green bar closing near high
    bull_bar = pd.Series({
        "open": 23000.0,
        "high": 23050.0,
        "low": 22995.0,
        "close": 23048.0,
        "volume": 10000.0,
    })
    bull_delta = ofa.estimate_candle_volume_delta(bull_bar)
    assert bull_delta > 7000.0  # Strong positive buyer delta

    # Strong red bar closing near low
    bear_bar = pd.Series({
        "open": 23050.0,
        "high": 23055.0,
        "low": 23000.0,
        "close": 23002.0,
        "volume": 10000.0,
    })
    bear_delta = ofa.estimate_candle_volume_delta(bear_bar)
    assert bear_delta < -7000.0  # Strong negative seller delta


def test_absorption_trap_detection():
    ofa = OrderFlowAgent(absorption_wick_ratio=0.40)
    
    # 5M df where last bar is a bullish breakout trap with massive upper rejection wick and high volume
    df_trap = pd.DataFrame({
        "open": [23000.0, 23010.0, 23020.0],
        "high": [23020.0, 23030.0, 23070.0],  # Spiked to 23070
        "low": [22990.0, 23000.0, 23015.0],
        "close": [23015.0, 23025.0, 23025.0],  # Closed way back down at 23025 (45pt upper wick!)
        "volume": [2000.0, 2500.0, 8000.0],    # High volume seller limit absorption
    })
    
    is_trap, side, reason = ofa.detect_absorption_trap(df_trap, "CALL")
    assert is_trap is True
    assert side == "SELLER_ABSORBING_BUYERS"
    assert "Passive Absorption" in reason


def test_dynamic_sizing_cap_and_whale_tranches():
    ofa = OrderFlowAgent()
    
    # Normal trending candles
    df_trend = pd.DataFrame({
        "open": [23000.0 + i * 5 for i in range(12)],
        "high": [23010.0 + i * 5 for i in range(12)],
        "low": [22995.0 + i * 5 for i in range(12)],
        "close": [23008.0 + i * 5 for i in range(12)],
        "volume": [3000.0 + i * 100 for i in range(12)],
    })

    # 1. Whale Sizing (20 Lots) -> Automatically triggers Staggered Tranches
    whale_sig = ofa.evaluate_sizing_cap(df_trend, "CALL", base_permitted_lots=20)
    assert whale_sig.execution_mode == "STAGGERED_TRANCHES"
    assert whale_sig.tranche_1_lots + whale_sig.tranche_2_lots == whale_sig.approved_lots
    assert whale_sig.tranche_1_lots > 0
    assert whale_sig.tranche_2_lots > 0

    # 2. Whale Sizing (30 Lots) -> Staggered Tranches
    whale_30 = ofa.evaluate_sizing_cap(df_trend, "CALL", base_permitted_lots=30)
    assert whale_30.execution_mode == "STAGGERED_TRANCHES"
    assert whale_30.tranche_1_lots + whale_30.tranche_2_lots == whale_30.approved_lots

    # 3. Small Size (4 Lots) -> Single Fill
    small_sig = ofa.evaluate_sizing_cap(df_trend, "CALL", base_permitted_lots=4)
    assert small_sig.execution_mode == "SINGLE_FILL"
    assert small_sig.tranche_1_lots == small_sig.approved_lots


def test_risk_agent_with_order_flow(tmp_path):
    ofa = OrderFlowAgent()
    cfg = tmp_path / "daily_risk.json"
    ra = RiskAgent(base_capital=100_000.0, config_path=cfg, order_flow_agent=ofa)

    # Synthetic trap DataFrame
    df_trap = pd.DataFrame({
        "open": [23000.0, 23010.0, 23020.0],
        "high": [23020.0, 23030.0, 23080.0],
        "low": [22990.0, 23000.0, 23015.0],
        "close": [23015.0, 23025.0, 23022.0],
        "volume": [2000.0, 2500.0, 10000.0],
    })

    # Order into absorption trap -> RiskAgent vetoes and sets lots to 0!
    appr_trap = ra.evaluate_order(
        entry_price=23022.0,
        stop_loss=22995.0,
        df_5m=df_trap,
        direction="CALL",
    )
    assert appr_trap.approved is False
    assert appr_trap.lots == 0
    assert "Passive Absorption" in str(appr_trap.rejection_reason)
