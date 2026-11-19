"""Unit tests for Conditional & Regime-Adaptive Dual-Agent EMA + Supertrend Policy."""

from datetime import date

from app.trading.dual_agent_ema_policy import (
    CE_DEFAULT_CONFIG,
    PE_DEFAULT_CONFIG,
    DirectionalAgent,
    DualAgentArbiter,
    DualAgentConfig,
    LotEntry,
    MarketContext,
    PositionState,
    RULE_HASH,
    VERSION,
    compute_ema,
    compute_supertrend,
    resolve_dynamic_1r,
    resolve_dynamic_buffer,
    resolve_dynamic_max_lots,
    select_dynamic_strike,
    select_non_0dte_expiry,
    tighten_stop,
)


def test_version_and_rule_hash_stable():
    assert VERSION == "dual-agent-ema-st-ratchet-v2-conditional"
    assert len(RULE_HASH) == 64


def test_tighten_stop_strictly_monotonic():
    assert tighten_stop("CALL", 25150.0, 25175.0) == 25175.0
    assert tighten_stop("CALL", 25175.0, 25140.0) == 25175.0
    assert tighten_stop("PUT", 25300.0, 25270.0) == 25270.0
    assert tighten_stop("PUT", 25270.0, 25310.0) == 25270.0


def test_dynamic_atr_adaptive_parameters():
    cfg = DualAgentConfig()
    # Low-vol regime (ATR=10) vs High-vol regime (ATR=30)
    assert resolve_dynamic_buffer(cfg, 10.0) == 3.0  # clamped at floor
    assert resolve_dynamic_buffer(cfg, 24.0) == 6.0  # 24 * 0.25 = 6.0
    assert resolve_dynamic_buffer(cfg, 40.0) == 8.0  # clamped at ceiling

    assert resolve_dynamic_1r(CE_DEFAULT_CONFIG, 12.0, 20.0) == 25.0  # clamped at min
    assert resolve_dynamic_1r(CE_DEFAULT_CONFIG, 17.5, 20.0) == 35.0  # 17.5 * 2.0
    assert resolve_dynamic_1r(CE_DEFAULT_CONFIG, 30.0, 20.0) == 40.0  # clamped at max

    # Macro EMA(200) conditional lot cap
    assert resolve_dynamic_max_lots(cfg, "CALL", 25300.0, 25264.0) == 3  # macro aligned
    assert resolve_dynamic_max_lots(cfg, "CALL", 25180.0, 25264.0) == 2  # counter-macro capped


def test_select_non_0dte_expiry_rolls_on_expiry_day():
    exp, dte = select_non_0dte_expiry(date(2026, 10, 27), holidays=set(), avoid_0dte=True)
    assert exp == date(2026, 11, 3) and dte == 7
    exp2, dte2 = select_non_0dte_expiry(date(2026, 10, 26), holidays=set(), avoid_0dte=True)
    assert exp2 == date(2026, 10, 27) and dte2 == 1


def test_ce_agent_conditional_sweep_override_and_fakeout_filter():
    root = DualAgentConfig()
    ce = DirectionalAgent(CE_DEFAULT_CONFIG, root)
    levels = [25141.80, 25227.95, 25390.65]

    # Without a liquidity sweep, counter-5M-ST CE at 09:39 MUST be blocked!
    ctx_no_sweep = MarketContext(
        time_hhmm="09:38", base_levels=levels, atr_5m=18.0,
        st_dir_5m=-1, ema20_5m=None, ema200_5m=25264.0, recent_liquidity_sweep=False,
    )
    assert ce.on_1m_close(ctx=ctx_no_sweep, prev_e9=25160, prev_e20=25162, curr_close=25189, curr_e9=25166, curr_e20=25165) is None
    ctx_no_sweep_t1 = MarketContext(
        time_hhmm="09:39", base_levels=levels, atr_5m=18.0,
        st_dir_5m=-1, ema20_5m=None, ema200_5m=25264.0, recent_liquidity_sweep=False,
    )
    assert ce.on_1m_close(ctx=ctx_no_sweep_t1, prev_e9=25166, prev_e20=25165, curr_close=25183.5, curr_e9=25169, curr_e20=25167) is None

    # WITH a verified liquidity sweep before 10:00, CE confirms cleanly!
    ctx_sweep = MarketContext(
        time_hhmm="09:42", base_levels=levels, atr_5m=18.0,
        st_dir_5m=-1, ema20_5m=None, ema200_5m=25264.0, recent_liquidity_sweep=True,
    )
    assert ce.on_1m_close(ctx=ctx_sweep, prev_e9=25160, prev_e20=25162, curr_close=25189, curr_e9=25166, curr_e20=25165) is None
    ctx_sweep_t1 = MarketContext(
        time_hhmm="09:43", base_levels=levels, atr_5m=18.0,
        st_dir_5m=-1, ema20_5m=None, ema200_5m=25264.0, recent_liquidity_sweep=True,
    )
    prop = ce.on_1m_close(ctx=ctx_sweep_t1, prev_e9=25166, prev_e20=25165, curr_close=25183.5, curr_e9=25169, curr_e20=25167)
    assert prop is not None
    assert prop.side == "CALL"
    assert all(ch.passed for ch in prop.conditions)


def test_asymmetric_pe_agent_blocks_unconfirmed_green_supertrend():
    root = DualAgentConfig()
    pe = DirectionalAgent(PE_DEFAULT_CONFIG, root)
    levels = [25390.65]

    ctx_green_st = MarketContext(
        time_hhmm="09:38", base_levels=levels, atr_5m=16.0,
        st_dir_5m=1, ema20_5m=25370.0, ema200_5m=25300.0, recent_liquidity_sweep=False,
    )
    pe.on_1m_close(ctx=ctx_green_st, prev_e9=25375, prev_e20=25374, curr_close=25365, curr_e9=25372, curr_e20=25373)
    ctx_green_st_t1 = MarketContext(
        time_hhmm="09:39", base_levels=levels, atr_5m=16.0,
        st_dir_5m=1, ema20_5m=25370.0, ema200_5m=25300.0, recent_liquidity_sweep=False,
    )
    assert pe.on_1m_close(ctx=ctx_green_st_t1, prev_e9=25372, prev_e20=25373, curr_close=25362, curr_e9=25370, curr_e20=25372) is None


def test_arbiter_conflict_veto_risk_cap_and_2loss_breaker():
    arb = DualAgentArbiter(DualAgentConfig())
    ctx0 = MarketContext(time_hhmm="09:38", base_levels=[100.0], atr_5m=15.0, st_dir_5m=1)
    ctx1 = MarketContext(time_hhmm="09:39", base_levels=[100.0], atr_5m=15.0, st_dir_5m=1)
    arb.ce_agent.on_1m_close(ctx=ctx0, prev_e9=100, prev_e20=101, curr_close=120, curr_e9=112, curr_e20=111)
    prop_ce = arb.ce_agent.on_1m_close(ctx=ctx1, prev_e9=112, prev_e20=111, curr_close=122, curr_e9=115, curr_e20=112)
    assert prop_ce is not None

    act, _, reason = arb.arbitrate_entry(prop_ce, None, estimated_1lot_risk_inr=3500.0)
    assert act == "HOLD" and "RISK_CAP" in reason

    act2, _, _ = arb.arbitrate_entry(prop_ce, prop_ce, estimated_1lot_risk_inr=1000.0)
    assert act2 == "VETO_CONFLICT"

    arb.record_trade_outcome(-1000.0)
    arb.record_trade_outcome(-900.0)
    act3, _, reason3 = arb.arbitrate_entry(prop_ce, None, estimated_1lot_risk_inr=1000.0)
    assert act3 == "HOLD" and "CIRCUIT_BREAKER" in reason3

    arb.record_trade_outcome(+2500.0)
    act4, chosen, _ = arb.arbitrate_entry(prop_ce, None, estimated_1lot_risk_inr=1000.0)
    assert act4 == "ENTER" and chosen is prop_ce


def test_position_dynamic_1r_lock_pyramid_trim_and_5m_exit():
    arb = DualAgentArbiter(DualAgentConfig())
    pos = PositionState(
        side="CALL", strike=25200, expiry="2026-11-24", dte=5,
        entry_time="09:39", entry_spot=25183.5, initial_risk_pts=45.0, stop_spot=25137.0,
        lots=[LotEntry(entry_spot=25183.5, entry_prem=180.0, entered_at="09:39")],
    )

    # ATR=17.5 -> dynamic 1R = 35.0 -> locks SL at Entry + 17.5 = 25201.0
    ctx1 = MarketContext(time_hhmm="09:44", base_levels=[25141.8, 25227.95], atr_5m=17.5, st_dir_5m=1, ema200_5m=25264.0)
    d1 = arb.evaluate_position(
        pos, ctx=ctx1, curr_1m_high=25220.0, curr_1m_low=25190.0, curr_1m_close=25218.5,
        mins_since_entry=5, new_5m_closed=False, prev_5m=None, curr_5m=None,
    )
    assert d1.action == "HOLD" and pos.stage == "VALIDATED" and pos.stop_spot == 25201.0

    # 5M conquers PDC -> PYRAMID_ADD
    curr_5m = {"close": 25260.0, "e9": 25240.0, "e20": 25235.0, "e200": 25264.0, "st_dir": 1, "st_val": 25210.0}
    prev_5m = {"close": 25220.0, "e9": 25215.0, "e20": 25210.0, "e200": 25264.0, "st_dir": 1, "st_val": 25200.0}
    ctx2 = MarketContext(time_hhmm="09:55", base_levels=[25141.8, 25227.95], atr_5m=17.5, st_dir_5m=1, ema20_5m=25235.0, ema200_5m=25264.0)
    d2 = arb.evaluate_position(
        pos, ctx=ctx2, curr_1m_high=25262.0, curr_1m_low=25245.0, curr_1m_close=25260.0,
        mins_since_entry=16, new_5m_closed=True, prev_5m=prev_5m, curr_5m=curr_5m,
    )
    assert d2.action == "PYRAMID_ADD" and pos.stage == "EXPANDING"
    pos.lots.append(LotEntry(entry_spot=25260.0, entry_prem=220.0, entered_at="09:55"))

    # Bar t reverse cross -> SCALE_OUT_TRIM; Bar t+1 -> EXIT_5M_CROSS_END
    rev_bar = {"close": 25240.0, "e9": 25238.0, "e20": 25239.0, "e200": 25264.0, "st_dir": 1, "st_val": 25215.0}
    d3 = arb.evaluate_position(
        pos, ctx=ctx2, curr_1m_high=25245.0, curr_1m_low=25235.0, curr_1m_close=25240.0,
        mins_since_entry=51, new_5m_closed=True, prev_5m=curr_5m, curr_5m=rev_bar,
    )
    assert d3.action == "SCALE_OUT_TRIM"
    pos.lots.pop()

    confirm_rev = {"close": 25235.0, "e9": 25236.0, "e20": 25238.0, "e200": 25264.0, "st_dir": 1, "st_val": 25215.0}
    d4 = arb.evaluate_position(
        pos, ctx=ctx2, curr_1m_high=25242.0, curr_1m_low=25230.0, curr_1m_close=25235.0,
        mins_since_entry=56, new_5m_closed=True, prev_5m=rev_bar, curr_5m=confirm_rev,
    )
    assert d4.action == "EXIT_5M_CROSS_END" and pos.stage == "CLOSED"


def test_supertrend_and_ema_sanity():
    vals = [100.0, 102.0, 104.0, 106.0, 108.0]
    ema = compute_ema(vals, 3)
    assert len(ema) == 5 and ema[-1] > ema[0]
    bars = [{"open": v - 1, "high": v + 2, "low": v - 2, "close": v} for v in vals]
    dirs, lines, atrs = compute_supertrend(bars, period=3, multiplier=2.0)
    assert len(dirs) == 5 and dirs[-1] == 1 and lines[-1] < vals[-1] and atrs[-1] > 0


def test_dynamic_itm_atm_otm_strike_selection():
    # Morning (<11:00) prefers shallow ITM (-1) when within budget
    sel_itm = select_dynamic_strike(
        spot=25185.0, side="CALL", dte=5, time_hhmm="09:39",
        quote_and_risk_fn=lambda k: (1500.0, 14000.0),
        max_risk_inr=3000.0, max_premium_inr=20000.0,
    )
    assert sel_itm is not None and sel_itm.tier == "ITM" and sel_itm.strike == 25150

    # Ranked fallback: if ITM exceeds premium budget, falls back to ATM, then OTM!
    sel_otm = select_dynamic_strike(
        spot=25185.0, side="CALL", dte=5, time_hhmm="11:30",
        quote_and_risk_fn=lambda k: (3200.0, 15000.0) if k <= 25200 else (1800.0, 10000.0),
        max_risk_inr=3000.0, max_premium_inr=20000.0,
    )
    assert sel_otm is not None and sel_otm.tier == "OTM" and sel_otm.strike == 25250


def test_blue_sky_atr_pyramiding():
    arb = DualAgentArbiter(DualAgentConfig())
    # Already above all marked levels (PDH=24600, EMA200=24550) -> Blue Sky!
    pos = PositionState(
        side="CALL", strike=24600, expiry="2026-11-10", dte=4,
        entry_time="12:11", entry_spot=24623.0, initial_risk_pts=30.0, stop_spot=24640.0,
        lots=[LotEntry(entry_spot=24623.0, entry_prem=175.0, entered_at="12:11")],
    )
    curr_5m = {"close": 24675.0, "e9": 24660.0, "e20": 24645.0, "e200": 24550.0, "st_dir": 1, "st_val": 24642.0}
    prev_5m = {"close": 24655.0, "e9": 24650.0, "e20": 24640.0, "e200": 24550.0, "st_dir": 1, "st_val": 24640.0}
    ctx = MarketContext(
        time_hhmm="12:40", base_levels=[24400.0, 24500.0, 24600.0],
        atr_5m=18.0, st_dir_5m=1, ema20_5m=24645.0, ema200_5m=24550.0,
    )
    # Profit = 52 pts >= 2.5 * 18 (45 pts) -> MUST trigger Blue-Sky PYRAMID_ADD!
    dec = arb.evaluate_position(
        pos, ctx=ctx, curr_1m_high=24676.0, curr_1m_low=24665.0, curr_1m_close=24675.0,
        mins_since_entry=29, new_5m_closed=True, prev_5m=prev_5m, curr_5m=curr_5m,
    )
    assert dec.action == "PYRAMID_ADD"
    assert any(ch.name == "pyramid_blue_sky_atr" and ch.passed for ch in dec.conditions)


def test_confirmed_eqh_reversal_filters_fakeouts_and_allows_pe_entry() -> None:
    from app.trading.dual_agent_ema_policy import (
        detect_confirmed_eqh_reversal,
        find_equal_high_levels,
    )

    closed_5m = [
        {"high": 25000.0},
        {"high": 25050.0},
        {"high": 25020.0},
        {"high": 25030.0},
        {"high": 25054.0},
        {"high": 25015.0},
    ]
    eqh = find_equal_high_levels(closed_5m, atr_5m=16.0)
    assert len(eqh) == 1 and eqh[0] == 25054.0

    recent_1m = [
        {"high": 25045.0, "close": 25040.0},
        {"high": 25060.0, "close": 25048.0},
        {"high": 25049.0, "close": 25042.0},
    ]
    # Unconfirmed: 5M candle is green -> must reject
    ok_green, _ = detect_confirmed_eqh_reversal(
        recent_1m, eqh, {"open": 25035.0, "close": 25045.0, "e9": 25048.0}
    )
    assert ok_green is False

    # Confirmed: 5M candle closes red below 5M EMA(9) and below EQH -> must pass
    ok_red, sweep_hi = detect_confirmed_eqh_reversal(
        recent_1m, eqh, {"open": 25052.0, "close": 25040.0, "e9": 25046.0}
    )
    assert ok_red is True and sweep_hi == 25060.0

    # PEAgent allows pre-Supertrend entry when confirmed EQH reversal is active
    cfg = DualAgentConfig()
    pe = DirectionalAgent(PE_DEFAULT_CONFIG, cfg)
    ctx = MarketContext(
        time_hhmm="10:15",
        base_levels=[25060.0],
        atr_5m=16.0,
        st_dir_5m=1,  # Still green on 5M ST(7,2)
        st_val_5m=24980.0,
        ema20_5m=25048.0,
        ema200_5m=24900.0,
        recent_liquidity_sweep=True,
    )
    assert pe.on_1m_close(ctx=ctx, prev_e9=25046.0, prev_e20=25045.0, curr_close=25042.0, curr_e9=25043.0, curr_e20=25044.0) is None
    prop = pe.on_1m_close(ctx=ctx, prev_e9=25043.0, prev_e20=25044.0, curr_close=25040.0, curr_e9=25041.0, curr_e20=25043.0)
    assert prop is not None and prop.side == "PUT"
