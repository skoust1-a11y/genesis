"""Unit Tests for Pre-Market Setup Crew and Broker Execution Crew.

Verifies:
1. GapAgent: Expected open, gap points, and regime classification (GAP_AND_GO vs EXHAUSTION_FADE).
2. GammaAgent: Call Wall, Put Wall, PCR, and Opening Trap warning when spot near resistance.
3. EventRiskAgent: Calendar inspection and blackout freeze scheduling.
4. PreMarketManager: End-to-end battleplan compilation and file persistence.
5. BrokerBridgeAgent: Low-latency order dispatch and simulation fallback.
6. SmartRoutingAgent: Marketable limit slippage defense & 2-tranche whale sizing.
7. ReconciliationAgent: Parity checks between DB and terminal.
8. ExecutionManager: Smart routing integration and slippage savings tracking.
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from app.trading.pre_market_crew.gap_agent import GapAgent
from app.trading.pre_market_crew.gamma_agent import GammaAgent
from app.trading.pre_market_crew.event_agent import EventRiskAgent
from app.trading.pre_market_crew.pre_market_manager import PreMarketManager

from app.trading.execution_crew.broker_bridge_agent import BrokerBridgeAgent
from app.trading.execution_crew.smart_routing_agent import SmartRoutingAgent
from app.trading.execution_crew.reconciliation_agent import ReconciliationAgent
from app.trading.execution_crew.execution_manager import ExecutionManager


# -----------------------------------------------------------------------------
# Pre-Market Setup Crew Tests
# -----------------------------------------------------------------------------
def test_gap_agent_evaluation():
    ga = GapAgent(trend_gap_threshold=35.0, exhaustion_gap_threshold=85.0)

    # 1. Trend Gap (Bullish)
    res_trend = ga.evaluate_gap(pdc=23000.0, gift_nifty_price=23045.0, global_sentiment=0.5)
    assert res_trend.gap_points == 45.0
    assert res_trend.bias == "BULLISH"
    assert res_trend.regime == "GAP_AND_GO"

    # 2. Exhaustion Gap
    res_exhaust = ga.evaluate_gap(pdc=23000.0, gift_nifty_price=23110.0)
    assert res_exhaust.gap_points == 110.0
    assert res_exhaust.regime == "EXHAUSTION_FADE"

    # 3. Flat Open
    res_flat = ga.evaluate_gap(pdc=23000.0, gift_nifty_price=23010.0)
    assert res_flat.bias == "NEUTRAL"
    assert res_flat.regime == "FLAT_OPEN"


def test_gamma_agent_option_chain_and_trap():
    gamma = GammaAgent(trap_proximity_threshold=20.0)

    # Synthetic option chain data
    chain = [
        {"strike": 22900, "call_oi": 1000000, "put_oi": 8000000},
        {"strike": 23000, "call_oi": 3000000, "put_oi": 12000000},  # Put Wall
        {"strike": 23100, "call_oi": 8000000, "put_oi": 4000000},
        {"strike": 23200, "call_oi": 15000000, "put_oi": 1000000}, # Call Wall
    ]

    analysis = gamma.evaluate_option_chain(current_spot=23080.0, chain_data=chain)
    assert analysis.call_wall == 23200.0
    assert analysis.put_wall == 23000.0
    assert analysis.pcr > 0.8
    assert analysis.is_opening_trap is False

    # Spot opening at 23190 (within 10 pts of Call Wall 23200) -> Trap Warning
    trap_analysis = gamma.evaluate_option_chain(current_spot=23190.0, chain_data=chain)
    assert trap_analysis.is_opening_trap is True
    assert "OPENING TRAP WARNING" in str(trap_analysis.trap_warning)


def test_event_agent_risk_evaluation():
    ea = EventRiskAgent()

    # Clear day
    clear_status = ea.evaluate_calendar_risk()
    assert clear_status.has_high_impact_event is False
    assert clear_status.is_safe_to_trade_morning is True

    # High impact event scheduled
    rbi_status = ea.evaluate_calendar_risk(manual_event_name="RBI_MPC")
    assert rbi_status.has_high_impact_event is True
    assert rbi_status.blackout_start == "10:00"
    assert rbi_status.blackout_end == "10:30"
    assert rbi_status.is_safe_to_trade_morning is False


def test_pre_market_manager_battleplan(tmp_path):
    pmm = PreMarketManager(trades_dir=tmp_path)
    plan = pmm.run_pre_market_setup(
        pdc=23000.0,
        gift_nifty_price=23040.0,
        date_str="2026-10-31",
    )

    assert plan.date == "2026-10-31"
    assert plan.expected_open == 23040.0
    assert plan.gap_bias == "BULLISH"
    assert plan.approved_lots_standard == 3
    assert plan.approved_lots_super == 4

    # File persisted
    saved_file = tmp_path / "pre_market_plan.json"
    assert saved_file.exists()
    with open(saved_file) as f:
        data = json.load(f)
        assert data["expected_open"] == 23040.0


# -----------------------------------------------------------------------------
# Broker Execution Crew Tests
# -----------------------------------------------------------------------------
def test_broker_bridge_dispatch():
    bridge = BrokerBridgeAgent(is_live_broker=False)
    res = bridge.place_order(
        symbol="NIFTY 23200 CE",
        side="BUY",
        lots=3,
        quantity=195,
        price=110.50,
    )

    assert res.success is True
    assert res.status == "SIMULATED_FILLED"
    assert res.order_id.startswith("ORD_")
    assert res.quantity == 195


def test_smart_routing_standard_vs_whale():
    router = SmartRoutingAgent(stagger_threshold_lots=10, default_slippage_buffer=0.5)

    # 1. Standard 3 Lots -> Single Fill
    plan_std = router.plan_routing(lots=3, lot_qty=65, current_cmp=120.0, side="BUY")
    assert plan_std.execution_mode == "SINGLE_FILL"
    assert plan_std.total_qty == 195
    assert plan_std.limit_price == 120.50
    assert plan_std.tranche_2_qty == 0

    # 2. Whale 20 Lots -> Staggered Tranches (35% / 65%)
    plan_whale = router.plan_routing(lots=20, lot_qty=65, current_cmp=120.0, side="BUY")
    assert plan_whale.execution_mode == "STAGGERED_TRANCHES"
    assert plan_whale.total_qty == 1300
    assert plan_whale.tranche_1_qty == 7 * 65   # 35% of 20 lots = 7 lots (455 qty)
    assert plan_whale.tranche_2_qty == 13 * 65  # 13 lots (845 qty)


def test_reconciliation_parity_check():
    recon = ReconciliationAgent()

    # In sync (0 open DB trades, 0 broker positions)
    sync_status = recon.reconcile_positions(db_trades=[], broker_positions=[])
    assert sync_status.is_synchronized is True
    assert len(sync_status.discrepancies) == 0

    # Out of sync (1 open DB trade, 0 broker positions)
    db_open = [{"trade_id": "T1", "status": "ACTIVE"}]
    mismatch_status = recon.reconcile_positions(db_trades=db_open, broker_positions=[])
    assert mismatch_status.is_synchronized is False
    assert "Count mismatch" in mismatch_status.discrepancies[0]


def test_execution_manager_end_to_end():
    em = ExecutionManager(is_live_broker=False, lot_qty=65)

    # Entry
    receipt_entry = em.execute_entry(
        symbol="NIFTY 23200 CE",
        direction="CALL",
        lots=4,
        cmp_price=105.0,
    )
    assert receipt_entry.success is True
    assert receipt_entry.quantity == 260
    assert receipt_entry.slippage_saved_pts == 1.0

    # Exit
    receipt_exit = em.execute_exit(
        symbol="NIFTY 23200 CE",
        lots=4,
        cmp_price=135.0,
        reason="TARGET_HIT",
    )
    assert receipt_exit.success is True
    assert receipt_exit.slippage_saved_pts == 0.5
    assert em.total_slippage_points_saved == 1.5
