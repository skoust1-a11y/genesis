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


def _make_shadow_bars() -> list[dict[str, float | str]]:
    """Build prev-day + today 1m bars where a clean CALL EMA9/20 cross arms at 09:42 and confirms at 09:43."""
    from datetime import datetime, timedelta
    from app.trading.dual_agent_ema_policy import IST

    bars: list[dict[str, float | str]] = []
    # Previous day: 40 bars trending up around 25000..25100 so 5M ST is green (+1) and PDC=25100
    t0 = datetime(2026, 11, 19, 14, 50, tzinfo=IST)
    for i in range(40):
        px = 25000.0 + i * 2.5
        bars.append({
            "date": (t0 + timedelta(minutes=i)).isoformat(),
            "open": px - 1.0, "high": px + 5.0, "low": px - 5.0, "close": px,
        })
    # Today 09:15..09:41: slight pullback below 25100 so EMA9 < EMA20 while 5M ST stays green (+1)
    t1 = datetime(2026, 11, 20, 9, 15, tzinfo=IST)
    # 09:15..09:29 (OR-15): 25100 -> 25086
    for i in range(15):
        px = 25100.0 - i * 1.0
        bars.append({
            "date": (t1 + timedelta(minutes=i)).isoformat(),
            "open": px + 0.5, "high": px + 5.0, "low": px - 5.0, "close": px,
        })
    # 09:30..09:41: gentle drift down from 25086 so EMA9 < EMA20 at 09:41 while 5M ST stays green (+1)
    for i in range(15, 27):
        px = 25086.0 - (i - 15) * 0.2
        bars.append({
            "date": (t1 + timedelta(minutes=i)).isoformat(),
            "open": px + 0.5, "high": px + 5.0, "low": px - 5.0, "close": px,
        })
    # 09:42 (Bar t): breakout above OR-15 low (25081.0), EMA9 crosses > EMA20
    bars.append({
        "date": (t1 + timedelta(minutes=27)).isoformat(),
        "open": 25084.0, "high": 25102.0, "low": 25084.0, "close": 25100.0,
    })
    # 09:43 (Bar t+1): confirmation bar holding above level & EMA9 > EMA20
    bars.append({
        "date": (t1 + timedelta(minutes=28)).isoformat(),
        "open": 25100.0, "high": 25105.0, "low": 25098.0, "close": 25102.0,
    })
    # 09:44..09:46: later bars
    for i in range(29, 32):
        px = 25122.0 + (i - 28) * 2.0
        bars.append({
            "date": (t1 + timedelta(minutes=i)).isoformat(),
            "open": px - 1.0, "high": px + 2.0, "low": px - 2.0, "close": px,
        })
    return bars


def _make_chain(spot_strike: int = 25100) -> dict:
    contracts = []
    for k in range(spot_strike - 150, spot_strike + 200, 50):
        contracts.append({
            "strikePrice": k,
            "ce": {"tradingSymbol": f"NIFTY {k} CE 23 NOV 26", "liveData": {"bid": 110.0, "ask": 112.0}},
            "pe": {"tradingSymbol": f"NIFTY {k} PE 23 NOV 26", "liveData": {"bid": 95.0, "ask": 97.0}},
        })
    return {"expiry": "2026-11-23", "contracts": contracts}


def test_shadow_account_catches_up_across_scan_gap_and_enters_live(tmp_path) -> None:
    from datetime import datetime
    from app.trading.dual_agent_ema_policy import IST
    from app.trading.dual_agent_shadow_account import DualAgentShadowAccount

    bars = _make_shadow_bars()
    chain = _make_chain()
    acc = DualAgentShadowAccount(tmp_path / "shadow.db")

    # Tick at 09:38 (last closed bar = 09:37), then simulate a 6-minute scan gap until 09:44
    res1 = acc.tick(now=datetime(2026, 11, 20, 9, 38, 5, tzinfo=IST), raw_1m=bars, chain=chain, news_safe=True, fee_policy="v1")
    assert res1["state"] == "SCANNING"

    # Next tick arrives at 09:44:05 (after gap spanning 09:38..09:43; 09:42 armed, 09:43 is live Bar t+1)
    res2 = acc.tick(now=datetime(2026, 11, 20, 9, 44, 5, tzinfo=IST), raw_1m=bars, chain=chain, news_safe=True, fee_policy="v1")
    assert res2["state"] == "ENTERED"
    assert acc.pos is not None and acc.pos.side == "CALL" and acc.pos.entry_time == "09:43"


def test_shadow_account_persists_pending_cross_across_restart(tmp_path) -> None:
    from datetime import datetime
    from app.trading.dual_agent_ema_policy import IST
    from app.trading.dual_agent_shadow_account import DualAgentShadowAccount

    bars = _make_shadow_bars()
    chain = _make_chain()
    db_path = tmp_path / "shadow_restart.db"

    acc1 = DualAgentShadowAccount(db_path)
    res1 = acc1.tick(now=datetime(2026, 11, 20, 9, 43, 5, tzinfo=IST), raw_1m=bars, chain=chain, news_safe=True, fee_policy="v1")
    assert res1["state"] == "SCANNING"
    assert acc1.arbiter.ce_agent.pending is not None

    # Simulate process restart before Bar t+1 (09:43) closes at 09:44
    acc2 = DualAgentShadowAccount(db_path)
    assert acc2.arbiter.ce_agent.pending is not None
    res2 = acc2.tick(now=datetime(2026, 11, 20, 9, 44, 5, tzinfo=IST), raw_1m=bars, chain=chain, news_safe=True, fee_policy="v1")
    assert res2["state"] == "ENTERED"
    assert acc2.pos is not None and acc2.pos.side == "CALL"


def test_shadow_account_skips_stale_signal_during_gap_without_entering(tmp_path) -> None:
    import sqlite3
    from datetime import datetime
    from app.trading.dual_agent_ema_policy import IST
    from app.trading.dual_agent_shadow_account import DualAgentShadowAccount

    bars = _make_shadow_bars()
    chain = _make_chain()
    db_path = tmp_path / "shadow_stale.db"
    acc = DualAgentShadowAccount(db_path)

    # First tick arrives at 09:47:05 (09:43 confirmation is already 3 minutes stale)
    res = acc.tick(now=datetime(2026, 11, 20, 9, 47, 5, tzinfo=IST), raw_1m=bars, chain=chain, news_safe=True, fee_policy="v1")
    assert res["state"] == "SCANNING"
    assert acc.pos is None
    with sqlite3.connect(db_path) as conn:
        actions = [r[0] for r in conn.execute("SELECT action FROM shadow_decisions").fetchall()]
    assert "SKIPPED_STALE_CATCHUP" in actions


def test_crew_journal_connect_readonly_recovers_from_wal_disk_io_error(tmp_path, monkeypatch) -> None:
    import sqlite3
    import pytest
    from app.db import crew_journal as cj

    db_path = tmp_path / "journal.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE crew_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO crew_meta VALUES ('k', 'v')")

    real_connect = sqlite3.connect

    def flaky_connect(database, *args, **kwargs):
        if isinstance(database, str) and database.endswith("?mode=ro"):
            raise sqlite3.OperationalError("disk I/O error")
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(cj.sqlite3, "connect", flaky_connect)
    conn = cj.connect_readonly(db_path)
    try:
        assert conn.execute("SELECT value FROM crew_meta WHERE key='k'").fetchone()[0] == "v"
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO crew_meta VALUES ('x', 'y')")
    finally:
        conn.close()


def test_ce_agent_falls_back_to_1m_ema20_sl_when_structural_level_exceeds_max_risk() -> None:
    cfg = DualAgentConfig()
    ce = DirectionalAgent(CE_DEFAULT_CONFIG, cfg)
    # Mirrors 2026-11-20 09:42->09:43: structural level (25353.85) is 143.6pt away (> 55pt max)
    ctx_t = MarketContext(
        time_hhmm="09:42", base_levels=[25353.85], atr_5m=43.0,
        st_dir_5m=-1, ema20_5m=25458.92, ema200_5m=25333.92, recent_liquidity_sweep=True,
    )
    assert ce.on_1m_close(
        ctx=ctx_t, prev_e9=25456.0, prev_e20=25457.0, curr_close=25472.6, curr_e9=25459.5, curr_e20=25458.25
    ) is None

    ctx_t1 = MarketContext(
        time_hhmm="09:43", base_levels=[25353.85], atr_5m=43.0,
        st_dir_5m=-1, ema20_5m=25458.92, ema200_5m=25333.92, recent_liquidity_sweep=True,
    )
    prop = ce.on_1m_close(
        ctx=ctx_t1, prev_e9=25459.5, prev_e20=25458.25, curr_close=25489.4, curr_e9=25465.47, curr_e20=25461.22
    )
    assert prop is not None
    assert prop.side == "CALL"
    assert prop.conquered_level == 25461.22
    assert prop.initial_sl == 25461.22 - 8.0
    assert 17.2 <= prop.risk_pts <= 55.0
    assert "1M EMA20" in prop.thesis_summary
