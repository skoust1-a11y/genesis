"""Unit Tests for Live Trading Crew and Post-Market Refactoring Crew.

Validates that both crews operate deterministically, adhere to institutional safety walls,
and handle all edge cases without exceptions.
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest
import pandas as pd
import numpy as np

# Live Crew
from app.trading.crew.entry_agent import EntryAgent, TradeSignal
from app.trading.crew.trend_agent import TrendAgent
from app.trading.crew.risk_agent import RiskAgent
from app.trading.crew.exit_agent import ExitAgent
from app.trading.crew.alert_agent import AlertAgent
from app.trading.crew.journal_agent import JournalAgent
from app.trading.crew.manager import TradeManager

# Post-Market Crew
from app.trading.post_market_crew.autopsy_agent import AutopsyAgent
from app.trading.post_market_crew.parameter_agent import ParameterAgent
from app.trading.post_market_crew.safety_wall_agent import SafetyWallAgent
from app.trading.post_market_crew.code_quality_agent import CodeQualityAgent
from app.trading.post_market_crew.briefing_agent import BriefingAgent
from app.trading.post_market_crew.post_market_manager import PostMarketManager


def create_sample_df(n_bars: int = 20, base_price: float = 23000.0) -> pd.DataFrame:
    """Generate synthetic 5M OHLCV dataframe for testing."""
    times = [f"2026-10-30 09:{15+i*5:02d}:00" for i in range(n_bars)]
    closes = [base_price + i * 5 for i in range(n_bars)]
    highs = [c + 6.0 for c in closes]
    lows = [c - 6.0 for c in closes]
    opens = [c - 2.0 for c in closes]
    volumes = [1000 + i * 50 for i in range(n_bars)]

    return pd.DataFrame({
        "timestamp": times,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })


# -----------------------------------------------------------------------------
# Live Trading Crew Tests
# -----------------------------------------------------------------------------
def test_trend_agent_regime_and_dynamic_rr():
    ta = TrendAgent()
    df = create_sample_df(25, 23000.0)
    
    # Blue sky runway
    regime = ta.evaluate_regime(df, 23120.0, "CALL", key_levels=[23300.0])
    assert regime.recommended_target_r >= 2.0
    assert regime.runway_points > 100.0
    assert regime.is_breakout_allowed is True


def test_risk_agent_circuit_breaker(tmp_path):
    cfg = tmp_path / "daily_risk.json"
    ra = RiskAgent(base_capital=50000.0, config_path=cfg)
    
    # Under limit
    appr = ra.evaluate_order(entry_price=23000.0, stop_loss=22970.0)
    assert appr.approved is True
    assert appr.lots == 2  # Standard ₹50k base = 2 lots

    # Stop > 55 pts ceiling -> Veto
    appr_wide = ra.evaluate_order(entry_price=23000.0, stop_loss=22940.0)
    assert appr_wide.approved is False
    assert "exceeds institutional ceiling" in appr_wide.rejection_reason

    # 2 consecutive losses -> Circuit breaker locks safety wall
    ra.record_trade_outcome(is_win=False, pnl_rupees=-2000.0)
    ra.record_trade_outcome(is_win=False, pnl_rupees=-2000.0)
    appr_locked = ra.evaluate_order(entry_price=23000.0, stop_loss=22970.0)
    assert appr_locked.approved is False
    assert "Circuit Breaker Triggered" in appr_locked.rejection_reason


def test_risk_agent_super_trend_4_lots(tmp_path):
    cfg = tmp_path / "daily_risk.json"
    ra = RiskAgent(base_capital=50000.0, default_lots=3, max_super_lots=4, config_path=cfg)

    # Standard setup -> 3 lots (195 qty)
    appr_std = ra.evaluate_order(entry_price=23000.0, stop_loss=22970.0, is_super_trend=False)
    assert appr_std.approved is True
    assert appr_std.lots == 3
    assert appr_std.quantity == 195

    # Super-Trend setup -> Scales to 4 lots (260 qty)
    appr_super = ra.evaluate_order(entry_price=23000.0, stop_loss=22970.0, is_super_trend=True)
    assert appr_super.approved is True
    assert appr_super.lots == 4
    assert appr_super.quantity == 260


def test_exit_agent_unconditional_target_and_trail():
    ea = ExitAgent()
    active_trade = {
        "direction": "CALL",
        "entry_price": 23000.0,
        "stop_loss": 22970.0,
        "target_price": 23075.0,
        "risk_points": 30.0,
        "lots": 2,
    }
    
    # 1. Target hit unconditional
    dec = ea.evaluate_exit(active_trade, cmp=23076.0)
    assert dec.action == "TARGET_HIT"
    assert dec.is_fully_closed is True
    assert dec.realized_r >= 2.5

    # 2. Structure trail on runner (crosses +1.5R, then 5M low breached)
    df = pd.DataFrame({
        "open": [23050.0, 23060.0],
        "high": [23070.0, 23065.0],
        "low": [23040.0, 23030.0],
        "close": [23060.0, 23035.0],  # Closes below prior low (23040.0)
    })
    dec_trail = ea.evaluate_exit(active_trade, cmp=23055.0, df_5m=df, current_time_str="10:30")
    assert dec_trail.action == "STRUCTURE_TRAIL_EXIT"
    assert dec_trail.is_fully_closed is True


def test_journal_agent_set_coercion(tmp_path):
    ja = JournalAgent(trades_dir=tmp_path)
    sig = TradeSignal(
        strategy="TEST_SWEEP",
        symbol="NIFTY",
        direction="CALL",
        entry_price=23000.0,
        stop_loss=22975.0,
        target_price=23075.0,
        risk_points=25.0,
        target_points=75.0,
        rr_ratio=3.0,
        lots=2,
        quantity=130,
        suggested_strike="NIFTY 23000 CE",
        timestamp="2026-10-30 10:00:00",
        rationale="Test",
        regime="TRENDING_BULL",
        adx=24.0,
    )
    t = ja.record_entry(sig, date_str="2026-10-30")
    assert t["status"] == "OPEN"
    
    # Reload and test milestones_hit set coercion
    loaded = ja.load_active_trades(date_str="2026-10-30")
    assert t["trade_id"] in loaded
    active = loaded[t["trade_id"]]
    assert isinstance(active["milestones_hit"], set)
    active["milestones_hit"].add("1.0R")  # Must not crash!
    ja.save_active_trades(loaded, date_str="2026-10-30")


# -----------------------------------------------------------------------------
# Post-Market Crew Tests
# -----------------------------------------------------------------------------
def test_autopsy_agent_audits_trades():
    aa = AutopsyAgent()
    df = create_sample_df(10, 23000.0)
    executed = [{
        "trade_id": "TRADE_01",
        "strategy": "LIQUIDITY_SWEEP",
        "direction": "CALL",
        "entry_price": 23000.0,
        "exit_price": 23060.0,
        "risk_points": 25.0,
        "realized_points": 60.0,
        "pnl_rupees": 3900.0,
    }]
    
    autopsy = aa.run_daily_autopsy(df, executed, pdh=23150.0, pdl=22900.0, date_str="2026-10-30")
    assert autopsy.trades_executed_count == 1
    assert autopsy.net_realized_points == 60.0
    assert len(autopsy.audited_trades) == 1


def test_safety_wall_50_percent_allocation(tmp_path):
    state_f = tmp_path / "safety_wall.json"
    cfg_f = tmp_path / "daily_risk.json"
    swa = SafetyWallAgent(base_capital=50000.0, state_file=state_f, config_file=cfg_f)
    
    # Day 1: Booked +₹10,000 profit -> Allocates ₹5,000 (50%) to Safety Wall
    trades = [{"pnl_rupees": 10000.0}]
    summary = swa.process_daily_pnl(trades)
    assert summary.added_to_reserve == 5000.0
    assert summary.current_reserve_balance == 5000.0
    assert summary.tomorrow_approved_lots == 2


def test_post_market_manager_pipeline(tmp_path):
    reports_d = tmp_path / "reports"
    trades_d = tmp_path / "trades"
    pmm = PostMarketManager(base_capital=50000.0, reports_dir=reports_d, trades_dir=trades_d)
    
    df = create_sample_df(15, 23000.0)
    executed = [{
        "trade_id": "TRADE_OCT30",
        "strategy": "LIQUIDITY_SWEEP",
        "direction": "CALL",
        "entry_price": 23000.0,
        "exit_price": 23075.0,
        "risk_points": 25.0,
        "realized_points": 75.0,
        "pnl_rupees": 9269.0,
        "timestamp": "2026-10-30 10:05:00",
    }]
    
    res = pmm.run_daily_review(
        df_intraday=df,
        pdh=23100.0,
        pdl=22950.0,
        date_str="2026-10-30",
        executed_trades=executed,
    )
    assert Path(res["report_path"]).exists()
    assert res["safety_wall"].added_to_reserve == 4634.5
    assert res["autopsy"].net_realized_points == 75.0
