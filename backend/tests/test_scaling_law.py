import pytest
from app.trading.scaling_law import (
    LOT_CAPITAL_MILESTONES,
    ScaleUpLawConfig,
    ScalingTierState,
    TradeRecord,
    calculate_10_lots_roadmap,
    check_daily_circuit_breaker,
    evaluate_scaling,
    initialize_tier_state,
    record_trade,
)


def test_initialization():
    state_50k = initialize_tier_state(50_000.0)
    assert state_50k.current_lots == 2
    assert state_50k.safety_wall_locked == 0.0
    assert state_50k.active_risk_capital == 50_000.0

    state_100k = initialize_tier_state(100_000.0)
    assert state_100k.current_lots == 4
    assert state_100k.active_risk_capital == 100_000.0


def test_safety_wall_ratchet():
    cfg = ScaleUpLawConfig(safety_wall_profit_share_pct=50.0)
    state = initialize_tier_state(100_000.0)

    # 1st Trade: Win of +₹10,000 (+2.2R)
    trade1 = TradeRecord(
        trade_id="T1",
        date="2026-10-28",
        strat_name="LevelRetest",
        lots=4,
        pnl_inr=10_000.0,
        r_multiple=2.2,
    )
    state, decision = record_trade(state, trade1, cfg)

    assert state.current_capital == 110_000.0
    assert state.safety_wall_locked == 5_000.0  # 50% locked
    assert state.active_risk_capital == 105_000.0
    assert state.tier_trades_count == 1
    assert state.tier_accumulated_r == 2.2
    assert decision.action == "HOLD"  # Need 15 trades & +12R

    # 2nd Trade: Loss of -₹4,550 (-1.0R)
    trade2 = TradeRecord(
        trade_id="T2",
        date="2026-10-28",
        strat_name="TrendlineReversal",
        lots=4,
        pnl_inr=-4_550.0,
        r_multiple=-1.0,
    )
    state, decision = record_trade(state, trade2, cfg)

    assert state.current_capital == 105_450.0
    # Safety wall is INVIOLABLE (does NOT shrink on loss)
    assert state.safety_wall_locked == 5_000.0
    assert state.tier_trades_count == 2
    assert state.tier_accumulated_r == 1.2


def test_scale_up_4_gates():
    cfg = ScaleUpLawConfig(min_tier_trades=5, min_tier_r=6.0)  # Compact for test
    state = initialize_tier_state(120_000.0, start_lots=4)

    # Execute 4 winning trades (+2.0R each = +8.0R)
    for i in range(4):
        t = TradeRecord(
            trade_id=f"T{i}",
            date="2026-10-28",
            strat_name="LevelRetest",
            lots=4,
            pnl_inr=9_100.0,
            r_multiple=2.0,
        )
        state, decision = record_trade(state, t, cfg)
        assert decision.action == "HOLD"  # Trade count is only 4 (< 5)

    assert not decision.gate1_sample_ok
    assert decision.gate2_r_ok

    # Execute 5th trade (+2.0R, now 5 trades, +10.0R total, capital > 140k)
    t5 = TradeRecord(
        trade_id="T5",
        date="2026-10-28",
        strat_name="LevelRetest",
        lots=4,
        pnl_inr=9_100.0,
        r_multiple=2.0,
    )
    state, decision = record_trade(state, t5, cfg)

    assert decision.action == "SCALE_UP"
    assert decision.recommended_lots == 5
    assert state.current_lots == 5
    # Tier counters reset for the new tier
    assert state.tier_trades_count == 0
    assert state.tier_accumulated_r == 0.0


def test_downshift_trapdoor():
    cfg = ScaleUpLawConfig(downshift_r_drawdown=-4.0)
    state = initialize_tier_state(100_000.0, start_lots=4)

    # 4 consecutive 1R losses
    for i in range(4):
        t = TradeRecord(
            trade_id=f"L{i}",
            date="2026-10-28",
            strat_name="LevelRetest",
            lots=4,
            pnl_inr=-4_550.0,
            r_multiple=-1.0,
        )
        state, decision = record_trade(state, t, cfg)

    assert decision.action == "DOWNSHIFT"
    assert decision.recommended_lots == 3
    assert state.current_lots == 3


def test_daily_circuit_breaker():
    cfg = ScaleUpLawConfig(max_daily_losses=2, max_daily_r_loss=-2.0)

    # 1 loss
    cb1 = check_daily_circuit_breaker([
        TradeRecord(trade_id="1", date="today", strat_name="S", lots=4, pnl_inr=-4550, r_multiple=-1.0)
    ], cfg)
    assert not cb1.is_locked_out

    # 2 consecutive losses -> lockout
    cb2 = check_daily_circuit_breaker([
        TradeRecord(trade_id="1", date="today", strat_name="S", lots=4, pnl_inr=-4550, r_multiple=-1.0),
        TradeRecord(trade_id="2", date="today", strat_name="S", lots=4, pnl_inr=-4550, r_multiple=-1.0),
    ], cfg)
    assert cb2.is_locked_out
    assert cb2.consecutive_losses == 2


def test_10_lots_roadmap():
    res_100k = calculate_10_lots_roadmap(100_000.0, 4)
    assert res_100k["starting_lots"] == 4
    assert res_100k["target_lots"] == 10
    assert res_100k["target_capital_required_inr"] == 350_000.0
    assert res_100k["at_10_lots_metrics"]["risk_pct_of_capital"] == 3.25
    assert res_100k["at_10_lots_metrics"]["profit_per_standard_win_2_2r_inr"] == 25025.0
    assert res_100k["projected_months_to_10_lots"] == 3.5

    res_50k = calculate_10_lots_roadmap(50_000.0, 2)
    assert res_50k["starting_lots"] == 2
    assert res_50k["projected_months_to_10_lots"] == 5.5


def test_3_tier_execution_framework():
    from app.config import get_settings
    from app.liquidity_signals import is_super_trend_setup, resolve_strike_and_lots

    settings = get_settings()

    # Test 1: Super-Trend setup detection
    st_sig = {
        "side": "Long",
        "entry": 22610.0,
        "stop": 22585.0,
        "time": "2026-10-28 10:15",
        "pool": "Breakout & Retest (OR-30)",
        "why": ["cleared OR-30H"],
    }
    assert is_super_trend_setup(st_sig)

    # Test 2: Standard setup detection (Not super-trend)
    std_sig = {
        "side": "Long",
        "entry": 22610.0,
        "stop": 22585.0,
        "time": "2026-10-28 13:45",
        "pool": "Trendline",
        "why": ["slope -1.2"],
    }
    assert not is_super_trend_setup(std_sig)

    # Test 3: Tier 1 (General: 3 Lots / 1-OTM) on standard setup
    otm_1, lots_1, tier_1 = resolve_strike_and_lots(std_sig, settings, vix=13.5)
    assert otm_1 in (0, 1)
    assert lots_1 in (2, 3)

    # Test 4: Tier 2 (Super-Trend: 4 Lots / 2-OTM) when cheap option allows
    otm_2, lots_2, tier_2 = resolve_strike_and_lots(st_sig, settings, vix=13.5)
    assert otm_2 in (0, 2)
    assert lots_2 in (2, 3, 4)

