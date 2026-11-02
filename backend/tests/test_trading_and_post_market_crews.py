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


def test_alert_agent_entry_and_trailing_alerts():
    alert_agent = AlertAgent()
    dispatched_msgs = []
    alert_agent._send = lambda msg: dispatched_msgs.append(msg) or True

    sig = TradeSignal(
        strategy="Liquidity Sweep",
        symbol="NIFTY",
        direction="CALL",
        entry_price=24360.50,
        stop_loss=24335.00,
        target_price=24415.00,
        risk_points=25.5,
        target_points=54.5,
        rr_ratio=2.14,
        lots=4,
        quantity=260,
        suggested_strike="NIFTY 24400 CE",
        timestamp="2026-11-02 10:15:00",
        rationale="PDL sweep reclaim",
        regime="TRENDING_BULL",
        adx=28.4,
    )

    # 1. Test Option Entry Zone Ticket
    ok_entry = alert_agent.send_entry_alert(sig)
    assert ok_entry is True
    assert len(dispatched_msgs) == 1
    assert "OPTION ENTRY ZONE" in dispatched_msgs[0]
    assert "DO NOT CHASE" in dispatched_msgs[0]
    assert "NIFTY 24400 CE" in dispatched_msgs[0]
    assert "Hard SL Price" in dispatched_msgs[0]

    # 2. Test Post-Entry Trailing Update
    ok_trail = alert_agent.send_trailing_update(
        symbol="NIFTY",
        strike="NIFTY 24400 CE",
        milestone="+1R Milestone",
        cmp=24386.00,
        r_multiple=1.0,
        opt_premium=135.50,
        new_sl=118.00,
        action_instruction="MOVE STOP LOSS TO COST (BREAKEVEN) NOW!",
        status_note="Trade is now 100% RISK-FREE.",
    )
    assert ok_trail is True
    assert len(dispatched_msgs) == 2
    assert "POST-ENTRY TRAILING UPDATE (+1R Milestone)" in dispatched_msgs[1]
    assert "MOVE STOP LOSS TO COST" in dispatched_msgs[1]
    assert "100% RISK-FREE" in dispatched_msgs[1]

    # 3. Test Liquidity Hopping Alert
    ok_hop = alert_agent.send_liquidity_hop_alert(
        symbol="NIFTY",
        strike="NIFTY 24400 CE",
        conquered_level_name="5M BSL (24,395)",
        conquered_price=24395.0,
        promoted_target_name="Daily PDH Pool (24,450)",
        promoted_target_price=24450.0,
        ratcheted_sl_spot=24392.0,
        ratcheted_sl_opt=130.0,
        projected_opt_target=175.0,
        locked_points=12.0,
        remaining_lots=2,
    )
    assert ok_hop is True
    assert len(dispatched_msgs) == 3
    assert "LIQUIDITY POOL CONQUERED & PROMOTED" in dispatched_msgs[2]
    assert "Daily PDH Pool" in dispatched_msgs[2]
    assert "RATCHETED STOP LOSS" in dispatched_msgs[2]


def test_exit_agent_liquidity_hopping():
    ea = ExitAgent(enable_liquidity_hopping=True)
    active_trade = {
        "direction": "CALL",
        "entry_price": 24360.0,
        "stop_loss": 24335.0,
        "target_price": 24395.0,
        "risk_points": 25.0,
        "lots": 4,
        "has_hopped": False,
    }
    key_levels = [24320.0, 24360.0, 24395.0, 24450.0, 24500.0]

    dec = ea.evaluate_exit(active_trade, cmp=24396.0, key_levels=key_levels)
    assert dec.action == "LIQUIDITY_HOP_PROMOTED"
    assert dec.is_fully_closed is False
    assert dec.booked_lots == 2
    assert dec.remaining_lots == 2
    assert dec.next_target == 24450.0
    assert dec.ratcheted_sl == 24392.0


# -----------------------------------------------------------------------------
# Genesis Architecture C (Optimized Hybrid Engine) Tests
# -----------------------------------------------------------------------------
def test_directional_thesis_structure_and_enforcement():
    from app.trading.crew.entry_agent import DirectionalThesis, select_strike_with_delta_floor
    
    thesis = DirectionalThesis(
        primary_bet="Bullish BSL Expansion to 23,210",
        targeted_pool="OR-30 High Reclaim & Weak High Stops",
        invalidation_price=23067.3,
        invalidation_condition="5M candle body close below 23,067.3 kills thesis",
        stagnation_timeout_bars=6,
        gamma_wall_clearance_pts=95.0,
        thesis_conviction="HIGH",
    )
    assert thesis.stagnation_timeout_bars == 6
    assert thesis.gamma_wall_clearance_pts >= 80.0
    assert thesis.thesis_conviction in ["HIGH", "SUPER_TREND", "SCALP_REVERSAL"]

    # Test EntryAgent evaluates sweep with Directional Thesis attachment
    ea = EntryAgent(otm_steps=2)
    # Candle sweeping PDL with long lower wick
    df = pd.DataFrame({
        "timestamp": ["2026-11-02 10:00:00", "2026-11-02 10:05:00"],
        "open": [23020.0, 22995.0],
        "high": [23025.0, 23010.0],
        "low": [22990.0, 22960.0],  # Sweeps PDL 22970.0
        "close": [23010.0, 23005.0], # Closes back above PDL with lower wick
        "volume": [1000, 2500],
    })
    sig = ea.evaluate_sweep(
        df_5m=df,
        pdh=23150.0,
        pdl=22970.0,
        call_wall=23200.0,
        put_wall=22800.0,
    )
    assert sig is not None
    assert sig.direction == "CALL"
    assert sig.thesis is not None
    assert sig.thesis.stagnation_timeout_bars == 6
    assert sig.thesis.gamma_wall_clearance_pts >= 80.0
    assert "23005" in sig.thesis.primary_bet or "Bullish" in sig.thesis.primary_bet
    assert "kills thesis" in sig.thesis.invalidation_condition


def test_strike_selection_2_otm_and_delta_floor():
    from app.trading.crew.entry_agent import select_strike_with_delta_floor

    # Spot 24,000 -> ATM is 24000. 2-OTM CALL is 24100 CE
    strike_call, delta_call = select_strike_with_delta_floor(24000.0, "CALL", otm_steps=2, min_delta=0.35)
    assert strike_call == "NIFTY 24100 CE"
    assert delta_call >= 0.35

    # Spot 24,000 -> 2-OTM PUT is 23900 PE
    strike_put, delta_put = select_strike_with_delta_floor(24000.0, "PUT", otm_steps=2, min_delta=0.35)
    assert strike_put == "NIFTY 23900 PE"
    assert delta_put >= 0.35


def test_gamma_wall_clearance_and_vacuum_gates():
    ea = EntryAgent(otm_steps=2)
    # Candle sweeping PDL
    df = pd.DataFrame({
        "timestamp": ["2026-11-02 10:00:00", "2026-11-02 10:05:00"],
        "open": [23020.0, 22995.0],
        "high": [23025.0, 23010.0],
        "low": [22990.0, 22960.0],
        "close": [23010.0, 23005.0],
        "volume": [1000, 2500],
    })

    # Case 1: Call wall within 20 points (distance < 20) -> VETO Long
    sig_wall_too_close = ea.evaluate_sweep(
        df_5m=df,
        pdh=23150.0,
        pdl=22970.0,
        call_wall=23020.0,  # Only 15 pts away!
        put_wall=22800.0,
    )
    assert sig_wall_too_close is None

    # Case 2: Open runway to call wall < min_runway (45 pts) -> VETO Long
    sig_insufficient_runway = ea.evaluate_sweep(
        df_5m=df,
        pdh=23150.0,
        pdl=22970.0,
        call_wall=23035.0,  # 30 pts away (< 45 pts required)
        put_wall=22800.0,
    )
    assert sig_insufficient_runway is None

    # Case 3: Clear runway (call wall 23150.0, distance 145 pts >= 45 pts) -> APPROVED
    sig_clear = ea.evaluate_sweep(
        df_5m=df,
        pdh=23150.0,
        pdl=22970.0,
        call_wall=23150.0,
        put_wall=22800.0,
    )
    assert sig_clear is not None
    assert sig_clear.direction == "CALL"


def test_stagnation_time_stop_charm_guard():
    ea = ExitAgent()
    active_trade = {
        "direction": "CALL",
        "entry_price": 24000.0,
        "stop_loss": 23970.0,
        "target_price": 24090.0,
        "risk_points": 30.0,
        "lots": 3,
        "bars_in_trade": 6,
    }

    # Trade has been open for 6 bars (30m) and is only at +1.0R (cmp = 24030.0, current_r = 1.0 < 1.8)
    # Stagnation Time-Stop must trigger immediate market exit!
    dec = ea.evaluate_exit(active_trade, cmp=24030.0, bars_in_trade=6, current_time_str="10:30")
    assert dec.action == "STAGNATION_TIME_STOP"
    assert dec.is_fully_closed is True
    assert "Stagnation Time-Stop" in dec.exit_reason
    assert "prevent delta erosion" in dec.exit_reason

    # If bars_in_trade < 6, trade should HOLD
    dec_hold = ea.evaluate_exit(active_trade, cmp=24030.0, bars_in_trade=5, current_time_str="10:30")
    assert dec_hold.action == "HOLD"

    # If target 1 (+1.8R or target price) was reached, TARGET_HIT fires
    dec_target = ea.evaluate_exit(active_trade, cmp=24090.0, bars_in_trade=6, current_time_str="10:30")
    assert dec_target.action == "TARGET_HIT"


def test_session_deadband_gates_and_vix_corridor(tmp_path):
    tm = TradeManager(
        base_capital=50000.0,
        trades_dir=tmp_path / "trades",
        db_path=tmp_path / "trades.db",
    )
    df = create_sample_df(20, 24000.0)

    # 1. Opening Blackout (09:15 - 09:30 AM)
    res_open = tm.on_market_tick(
        cmp=24000.0,
        df_5m=df,
        pdh=24100.0,
        pdl=23900.0,
        current_time_str="09:20",
    )
    assert any(a["action"] == "VETO_OPENING_BLACKOUT" for a in res_open["actions"])

    # 2. Lunch Lull Gate (11:30 - 13:00 IST)
    res_lunch = tm.on_market_tick(
        cmp=24000.0,
        df_5m=df,
        pdh=24100.0,
        pdl=23900.0,
        current_time_str="12:15",
    )
    assert any(a["action"] == "VETO_LUNCH_LULL" for a in res_lunch["actions"])

    # 3. Terminal Expiry Zone (>= 14:30 IST)
    res_expiry = tm.on_market_tick(
        cmp=24000.0,
        df_5m=df,
        pdh=24100.0,
        pdl=23900.0,
        current_time_str="14:45",
    )
    assert any(a["action"] == "VETO_TERMINAL_EXPIRY_ZONE" for a in res_expiry["actions"])

    # 4. India VIX Corridor (< 11.5 or > 22.0)
    res_vix_low = tm.on_market_tick(
        cmp=24000.0,
        df_5m=df,
        pdh=24100.0,
        pdl=23900.0,
        current_time_str="10:15",
        india_vix=10.5,
    )
    assert any(a["action"] == "VETO_VIX_CORRIDOR" for a in res_vix_low["actions"])

    res_vix_high = tm.on_market_tick(
        cmp=24000.0,
        df_5m=df,
        pdh=24100.0,
        pdl=23900.0,
        current_time_str="10:15",
        india_vix=23.0,
    )
    assert any(a["action"] == "VETO_VIX_CORRIDOR" for a in res_vix_high["actions"])


def test_idempotent_order_deduplication(tmp_path):
    tm = TradeManager(
        base_capital=50000.0,
        trades_dir=tmp_path / "trades",
        db_path=tmp_path / "trades.db",
    )
    # Candle sweeping PDL to trigger Bullish Sweep
    df = pd.DataFrame({
        "timestamp": ["2026-11-02 10:10:00", "2026-11-02 10:15:00"],
        "open": [24020.0, 23995.0],
        "high": [24025.0, 24010.0],
        "low": [23990.0, 23960.0],
        "close": [24010.0, 24005.0],
        "volume": [1000, 2500],
    })

    # First tick -> Order opened
    res1 = tm.on_market_tick(
        cmp=24005.0,
        df_5m=df,
        pdh=24150.0,
        pdl=23970.0,
        current_time_str="10:15",
        date_str="2026-11-02",
        india_vix=14.0,
    )
    assert any(a["action"] == "ENTRY_OPENED" for a in res1["actions"])
    trade_id = res1["actions"][0]["trade_id"]

    # Manually clear active trade cache to simulate a flat state with a re-broadcast candidate
    tm.journal_agent.save_active_trades({}, date_str="2026-11-02")

    # Second tick with identical signature -> Duplicate suppressed!
    res2 = tm.on_market_tick(
        cmp=24005.0,
        df_5m=df,
        pdh=24150.0,
        pdl=23970.0,
        current_time_str="10:15",
        date_str="2026-11-02",
        india_vix=14.0,
    )
    assert any(a["action"] == "DUPLICATE_ORDER_SUPPRESSED" for a in res2["actions"])


def test_safety_wall_50_percent_ratchet_and_circuit_breaker(tmp_path):
    cfg = tmp_path / "daily_risk.json"
    ra = RiskAgent(base_capital=50000.0, default_lots=3, max_super_lots=4, config_path=cfg)

    # 1. Winning trade of ₹10,000 realized profit
    # 50% (₹5,000) automatically ratcheted into safety_wall_reserve
    ra.record_trade_outcome(is_win=True, pnl_rupees=10000.0, realized_r=2.5)
    assert ra.safety_wall_reserve == 5000.0
    assert ra.daily_losses_count == 0

    # 2. Losing trade does NOT deplete safety_wall_reserve (reserve balance locked permanently!)
    ra.record_trade_outcome(is_win=False, pnl_rupees=-2500.0, realized_r=-1.0)
    assert ra.safety_wall_reserve == 5000.0
    assert ra.daily_losses_count == 1
    assert ra.is_safety_wall_locked is False

    # 3. Cumulative daily drawdown reaches <= -2.0R -> Daily Circuit Breaker triggers
    ra.record_trade_outcome(is_win=False, pnl_rupees=-5000.0, realized_r=-3.6)
    # Total R = +2.5 - 1.0 - 3.6 = -2.1R <= -2.0R
    assert ra.daily_cumulative_r <= -2.0
    assert ra.is_safety_wall_locked is True

    # Order evaluation rejected by circuit breaker
    appr = ra.evaluate_order(entry_price=24000.0, stop_loss=23970.0)
    assert appr.approved is False
    assert "Circuit Breaker Triggered" in appr.rejection_reason


