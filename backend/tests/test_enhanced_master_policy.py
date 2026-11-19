"""Unit tests for the Enhanced Master Policy & Risk Architecture.

Tests all 14 improvements:
- ₹2,500 stop budget ceiling & tier sizing (2-4 lots)
- 1-loss daily circuit breaker (₹2,500 daily loss limit)
- 90-second signal staleness rejection
- DTE-aware Day 1 opening window
- Volume profile calculation & Auction Market Theory bias filtering
- Thesis management runner ratchets (0.7R BE, 2.0R lock)
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
import json
from types import SimpleNamespace
import pytest

from app.config import Settings
from app.trading import enhanced_master_policy as emp
from app.trading import paper_risk as risk
from app.trading.option_quote import ContractQuote, IST
from app.trading.paper_exit_policy import FRESH_BID
from app.trading.forward_fees import policy as get_fee_policy
from tests.test_forward_comparison import NOW, config, quote, raw, signal, tick


@pytest.fixture(autouse=True)
def restore_risk():
    yield
    emp.restore_default_risk_limits()


def test_volume_profile_and_auction_bias():
    candles = [
        {"date": "2026-11-17T09:15:00+05:30", "open": 25300, "high": 25320, "low": 25290, "close": 25310, "volume": 100},
        {"date": "2026-11-17T09:16:00+05:30", "open": 25310, "high": 25330, "low": 25300, "close": 25320, "volume": 200},
        {"date": "2026-11-17T09:17:00+05:30", "open": 25320, "high": 25325, "low": 25280, "close": 25290, "volume": 150},
        {"date": "2026-11-17T09:18:00+05:30", "open": 25290, "high": 25300, "low": 25250, "close": 25260, "volume": 500},
    ]
    prof = emp.compute_session_profile(candles, bin_size=10.0)
    assert prof is not None
    assert prof.pdh == 25330.0
    assert prof.pdl == 25250.0
    assert prof.val <= prof.poc <= prof.vah

    # Test Sell-Side bias when open is below VAL
    bias_sell = emp.check_auction_bias(day_open=prof.val - 20, current_spot=prof.val - 10, profile=prof)
    assert bias_sell == "SELL_SIDE"

    # Test Buy-Side bias when open is above VAH
    bias_buy = emp.check_auction_bias(day_open=prof.vah + 20, current_spot=prof.vah + 10, profile=prof)
    assert bias_buy == "BUY_SIDE"


def test_master_risk_sizing_up_to_4_lots(config):
    emp.install_master_risk_limits(max_lots=4)
    now = NOW
    q, ev = quote(now, price=20.0, lot=65)
    quote_obj = ContractQuote(
        underlying="NIFTY",
        expiry="2026-11-17",
        strike=25100.0,
        option_type="CE",
        provider_instrument_id=q["contracts"][0]["ce"]["growwContractId"],
        tradingsymbol=q["contracts"][0]["ce"]["tradingSymbol"],
        lot_size=65,
        ltp=Decimal("20.00"),
        bid=Decimal("20.00"),
        ask=Decimal("20.05"),
        quote_time=now,
        fetched_at=now,
        source="SYNTHETIC_BLACK76_PRIOR_AVAILABLE_VIX",
        delta=0.5,
    )

    settings = config.model_copy()
    evidence_item = ev[quote_obj.tradingsymbol]

    # Sizing with ₹2,500 budget and step=2 (OTM) unlocks up to 4 lots if affordable
    sized = risk.size(
        quote=quote_obj,
        evidence=evidence_item,
        trusted_sources=frozenset({emp.SOURCE, "SYNTHETIC_BLACK76_PRIOR_AVAILABLE_VIX"}),
        now=now,
        settings=settings,
        underlying_entry=Decimal("25000"),
        underlying_stop=Decimal("24980"),  # 20 pt underlying risk
        stop_budget=Decimal("2500.00"),
        gross_daily_loss=Decimal("0"),
        existing_reservations=Decimal("0"),
        available_cash=Decimal("50000.00"),
        max_lots=4,
    )
    assert sized.reason is None
    assert sized.entry is not None
    assert sized.entry.lots >= 2  # Unlocks multi-lot sizing!
    assert sized.estimated_stop_risk <= Decimal("2500.00")


def test_staleness_defense_rejects_signals_older_than_90s(tmp_path, config):
    settings = config.model_copy(update={"paper_exit_quote_policy": FRESH_BID})
    account = emp.EnhancedMasterAccount(
        tmp_path / "account_stale.db",
        settings,
        run_id="test-stale",
        config=emp.EnhancedMasterConfig(signal_max_age_seconds=90),
    )

    sig = signal(NOW)
    sig["signal_at"] = (NOW - timedelta(seconds=95)).isoformat()  # 95 seconds old
    engine = SimpleNamespace(advance=lambda rows, state, now: (state, [sig]))
    account.signal_engine = engine

    chain, ev = quote(NOW, price=20.0)
    frame = raw(NOW, spot=25000)
    res = account.tick(now=NOW, raw_1m=frame, chain=chain, evidence=ev, news_safe=True, fee_policy="TEST")
    assert "SIGNAL_STALE_AT_RESTART" in res["reasons"]


def test_one_loss_daily_circuit_breaker(tmp_path, config):
    settings = config.model_copy(update={"paper_exit_quote_policy": FRESH_BID})
    account = emp.EnhancedMasterAccount(
        tmp_path / "account_cb.db",
        settings,
        run_id="test-cb",
        config=emp.EnhancedMasterConfig(daily_loss_limit=Decimal("2500.00")),
    )

    # Simulate a prior closed trade that lost ₹2,500 today
    with account.connect() as c:
        today = NOW.date().isoformat()
        trade_id = "loss-trade-1"
        body = {"signal_id": trade_id, "day": today, "lots": 2, "qty": 130}
        c.execute("INSERT INTO trades VALUES (?,?,?,'CLOSED','STOP')", (trade_id, today, emp.encode(body)))
        # Buy fill at 25.00 (3250 INR)
        buy_fill = {"side": "BUY", "qty": 130, "price": "25.00", "charges_inr": "50.00"}
        c.execute("INSERT INTO fills VALUES (NULL,?,'BUY',?,?)", (trade_id, NOW.isoformat(), emp.encode(buy_fill)))
        # Sell fill at 5.00 (650 INR) -> loss = 3250 + 50 - 650 + 50 = -2700 INR
        sell_fill = {"side": "SELL", "qty": 130, "price": "5.00", "charges_inr": "50.00"}
        c.execute("INSERT INTO fills VALUES (NULL,?,'SELL',?,?)", (trade_id, NOW.isoformat(), emp.encode(sell_fill)))

    # Now tick with a new signal -> Circuit breaker must trip!
    sig = signal(NOW)
    engine = SimpleNamespace(advance=lambda rows, state, now: (state, [sig]))
    account.signal_engine = engine

    chain, ev = quote(NOW, price=20.0)
    frame = raw(NOW, spot=25000)
    res = account.tick(now=NOW, raw_1m=frame, chain=chain, evidence=ev, news_safe=True, fee_policy="TEST")
    assert "DAILY_CIRCUIT_BREAKER_MAX_LOSS_REACHED" in res["reasons"]


def test_dte_day1_opening_window(tmp_path, config):
    settings = config.model_copy(update={"paper_exit_quote_policy": FRESH_BID})
    account = emp.EnhancedMasterAccount(
        tmp_path / "account_dte.db",
        settings,
        run_id="test-dte",
        config=emp.EnhancedMasterConfig(dte_day1_opening_enabled=True),
    )

    # 09:20 AM with expiry only 2 days away (DTE < 5) -> must be blacked out
    morning_time = NOW.replace(hour=9, minute=20, second=0)
    sig = signal(morning_time)
    engine = SimpleNamespace(advance=lambda rows, state, now: (state, [sig]))
    account.signal_engine = engine

    chain, ev = quote(morning_time, price=20.0)
    chain["expiry"] = (morning_time.date() + timedelta(days=2)).isoformat()
    frame = raw(morning_time, spot=25000)
    res = account.tick(now=morning_time, raw_1m=frame, chain=chain, evidence=ev, news_safe=True, fee_policy="TEST")
    assert "OPENING_BLACKOUT_DTE_LT_5" in res["reasons"]


def test_house_money_winning_day_allows_subsequent_trades(tmp_path, config):
    settings = config.model_copy(update={"paper_exit_quote_policy": FRESH_BID})
    account = emp.EnhancedMasterAccount(
        tmp_path / "account_winner.db",
        settings,
        run_id="test-winner",
        config=emp.EnhancedMasterConfig(daily_loss_limit=Decimal("2500.00"), max_daily_trades=4),
    )

    # Simulate Trade 1 that won +5000 INR
    with account.connect() as c:
        today = NOW.date().isoformat()
        trade_id = "win-trade-1"
        body = {"signal_id": trade_id, "day": today, "lots": 2, "qty": 130}
        c.execute("INSERT INTO trades VALUES (?,?,?,'CLOSED','TARGET')", (trade_id, today, emp.encode(body)))
        # Buy fill at 20.00 (2600 INR)
        buy_fill = {"side": "BUY", "qty": 130, "price": "20.00", "charges_inr": "50.00"}
        c.execute("INSERT INTO fills VALUES (NULL,?,'BUY',?,?)", (trade_id, NOW.isoformat(), emp.encode(buy_fill)))
        # Sell fill at 60.00 (7800 INR) -> profit = +5100 INR
        sell_fill = {"side": "SELL", "qty": 130, "price": "60.00", "charges_inr": "50.00"}
        c.execute("INSERT INTO fills VALUES (NULL,?,'SELL',?,?)", (trade_id, NOW.isoformat(), emp.encode(sell_fill)))

    # Now tick with Trade 2 -> Must be admitted! (gross_loss is 0, trade count 1 < 4)
    sig = signal(NOW, id="signal-trade-2")
    engine = SimpleNamespace(advance=lambda rows, state, now: (state, [sig]))
    account.signal_engine = engine

    chain, ev = quote(NOW, price=20.0)
    frame = raw(NOW, spot=25000)
    fee_pol = get_fee_policy(NOW.date().isoformat()).policy_id
    res = account.tick(now=NOW, raw_1m=frame, chain=chain, evidence=ev, news_safe=True, fee_policy=fee_pol)
    assert len(res["reasons"]) == 0
    # Verify trade 2 opened
    with account.connect() as c:
        trades = c.execute("SELECT id, state FROM trades").fetchall()
        assert len(trades) == 2
        assert any(t["id"] == "signal-trade-2" and t["state"] == "OPEN" for t in trades)


def test_auction_bias_call_veto_in_sell_side_market(tmp_path, config):
    settings = config.model_copy(update={"paper_exit_quote_policy": FRESH_BID})
    # Set profile where VAL is 25300
    prof = emp.DayProfile(
        date="2026-11-15",
        poc=25350.0,
        vah=25400.0,
        val=25300.0,
        pdh=25450.0,
        pdl=25280.0,
        total_volume=10000.0,
    )
    today_str = NOW.date().isoformat()
    account = emp.EnhancedMasterAccount(
        tmp_path / "account_bias.db",
        settings,
        run_id="test-bias",
        config=emp.EnhancedMasterConfig(volume_profile_filter=True),
        profiles_by_day={today_str: prof},
    )

    # Day opens at 25200 (well below VAL 25300) -> Sell Side Market!
    sig_call = signal(NOW, id="call-signal")
    sig_call["side"] = "CALL"
    engine = SimpleNamespace(advance=lambda rows, state, now: (state, [sig_call]))
    account.signal_engine = engine

    chain, ev = quote(NOW, price=20.0)
    # Spot 25220, open 25200
    frame = raw(NOW, spot=25220)
    frame.loc[0, "open"] = 25200
    frame.loc[0, "low"] = 25195
    frame.loc[0, "high"] = 25225
    fee_pol = get_fee_policy(NOW.date().isoformat()).policy_id
    res = account.tick(now=NOW, raw_1m=frame, chain=chain, evidence=ev, news_safe=True, fee_policy=fee_pol)
    assert "AUCTION_SELL_SIDE_BIAS_CALL_VETO" in res["reasons"]


def test_bull_trap_invalidation_and_put_reversal_flip(tmp_path, config):
    """Test Bull Trap invalidation cuts long early and immediately retries with PUT flip."""
    settings = config.model_copy(update={"paper_exit_quote_policy": FRESH_BID})
    account = emp.EnhancedMasterAccount(
        tmp_path / "account_trap.db",
        settings,
        run_id="test-trap",
        config=emp.EnhancedMasterConfig(
            allow_thesis_reversal_flip=True,
            max_daily_reversal_flips=1,
            volume_profile_filter=False,
        ),
    )

    now = NOW
    trade_body = {
        "signal_id": "call-trap-1",
        "side": "CALL",
        "entry_spot": 25300.0,
        "stop_spot": 25270.0,
        "risk_points": 30.0,
        "lots": 2,
        "opened_at": (now - timedelta(minutes=10)).isoformat(),
        "legs": [{"ratio": 1, "strike": 25400, "kind": "CE", "action": "BUY"}],
        "suggested_strike": "NIFTY 25400 CE",
    }

    # Simulate completed 5M bars where last bar is a Bull Trap:
    # Opened at 25305, spiked to 25315, then collapsed to 25285 (range 30, red bar, broke below entry by 15)
    five_bars = [
        {"date": (now - timedelta(minutes=10)).isoformat(), "open": 25290.0, "high": 25310.0, "low": 25285.0, "close": 25305.0},
        {"date": (now - timedelta(minutes=5)).isoformat(), "open": 25305.0, "high": 25315.0, "low": 25280.0, "close": 25285.0},
    ]

    # Check _check_trap_invalidation directly
    trap = account._check_trap_invalidation(trade_body, spot=25285.0, five_bars=five_bars, now=now)
    assert trap is not None
    assert trap["action"] == "BULL_TRAP_EXIT"
    assert trap["opposite_side"] == "PUT"
    assert trap["entry_spot"] == 25285.0
    assert trap["stop_spot"] == 25320.0  # trap_high (25315) + 5.0

    # Test Bear Trap invalidation on PUT
    put_trade_body = {
        "signal_id": "put-trap-1",
        "side": "PUT",
        "entry_spot": 25300.0,
        "stop_spot": 25330.0,
        "risk_points": 30.0,
        "lots": 2,
        "opened_at": (now - timedelta(minutes=10)).isoformat(),
        "legs": [{"ratio": 1, "strike": 25200, "kind": "PE", "action": "BUY"}],
        "suggested_strike": "NIFTY 25200 PE",
    }
    # Bear trap 5M bar: Opened 25295, dropped to 25280, then squeezed to 25315 (range 35, green, broke above entry by 15)
    bear_trap_bars = [
        {"date": (now - timedelta(minutes=10)).isoformat(), "open": 25310.0, "high": 25315.0, "low": 25290.0, "close": 25295.0},
        {"date": (now - timedelta(minutes=5)).isoformat(), "open": 25295.0, "high": 25320.0, "low": 25280.0, "close": 25315.0},
    ]
    bear_trap = account._check_trap_invalidation(put_trade_body, spot=25315.0, five_bars=bear_trap_bars, now=now)
    assert bear_trap is not None
    assert bear_trap["action"] == "BEAR_TRAP_EXIT"
    assert bear_trap["opposite_side"] == "CALL"
    assert bear_trap["entry_spot"] == 25315.0
    assert bear_trap["stop_spot"] == 25275.0  # trap_low (25280) - 5.0


def test_full_roundtrip_bull_trap_reversal_flip(tmp_path, config):
    """Test full cycle: Call entered, trapped on 5M, exited early, then opposite Put flip is executed."""
    settings = config.model_copy(update={"paper_exit_quote_policy": FRESH_BID})
    now = NOW

    # Provide 5M bars feed for the account
    five_history = [
        {"date": (now - timedelta(minutes=15)).isoformat(), "open": 25000.0, "high": 25020.0, "low": 24990.0, "close": 25010.0},
        {"date": (now - timedelta(minutes=10)).isoformat(), "open": 25010.0, "high": 25030.0, "low": 25000.0, "close": 25020.0},
    ]

    account = emp.EnhancedMasterAccount(
        tmp_path / "roundtrip_trap.db",
        settings,
        run_id="test-roundtrip-trap",
        config=emp.EnhancedMasterConfig(
            allow_thesis_reversal_flip=True,
            max_daily_reversal_flips=1,
            volume_profile_filter=False,
            signal_max_age_seconds=120,
        ),
        five_source=lambda t: five_history,
    )

    # 1. Enter initial CALL
    call_sig = signal(now, id="call-initial")
    call_sig["side"] = "CALL"
    call_sig["entry_spot"] = 25020.0
    call_sig["stop_spot"] = 24995.0
    call_sig["risk_points"] = 25.0

    account.signal_engine = SimpleNamespace(advance=lambda rows, state, t: (state, [call_sig]))

    chain, ev = quote(now, price=20.0)
    frame = raw(now, spot=25020.0)
    fee_pol = get_fee_policy(now.date().isoformat()).policy_id
    res1 = account.tick(now=now, raw_1m=frame, chain=chain, evidence=ev, news_safe=True, fee_policy=fee_pol)

    with account.connect() as c:
        held = c.execute("SELECT id, state FROM trades WHERE state<>'CLOSED'").fetchall()
        assert len(held) == 1
        assert held[0]["id"] == "call-initial"

    # 2. Advance time 10 minutes, simulate Bull Trap (market breaks down to 24990, red 5M bar range 30)
    t2 = now + timedelta(minutes=10)
    five_history.append(
        {"date": (t2 - timedelta(minutes=5)).isoformat(), "open": 25025.0, "high": 25030.0, "low": 24990.0, "close": 24995.0}
    )
    frame2 = raw(t2, spot=24995.0)
    
    # Construct chain containing both CE (for closing Call) and PE (for entering Put flip)
    sym_ce = "NIFTY26NOV25100CE"
    sym_pe = "NIFTY26NOV24900PE"
    chain2 = {
        "expiry": "2026-11-17",
        "fetched_at": t2,
        "contracts": [
            {
                "underlying": "NIFTY",
                "strike": 25100,
                "ce": {
                    "marketLot": 65,
                    "growwContractId": "NSE_FO|12345",
                    "tradingSymbol": sym_ce,
                    "liveData": {
                        "ltp": 15.0,
                        "bid": "15.00",
                        "ask": "15.05",
                        "quote_time": t2.isoformat(),
                    },
                },
            },
            {
                "underlying": "NIFTY",
                "strike": 24900,
                "pe": {
                    "marketLot": 65,
                    "growwContractId": "NSE_FO|67890",
                    "tradingSymbol": sym_pe,
                    "liveData": {
                        "ltp": 25.0,
                        "bid": "25.00",
                        "ask": "25.05",
                        "quote_time": t2.isoformat(),
                    },
                },
            },
        ],
    }
    from app.trading.paper_risk import DeltaEvidence
    ev2 = {
        sym_ce: DeltaEvidence(Decimal(".4"), t2, sym_ce, emp.SOURCE),
        sym_pe: DeltaEvidence(Decimal("-.4"), t2, sym_pe, emp.SOURCE),
    }

    # No external signals
    account.signal_engine = SimpleNamespace(advance=lambda rows, state, t: (state, []))

    res2 = account.tick(now=t2, raw_1m=frame2, chain=chain2, evidence=ev2, news_safe=True, fee_policy=fee_pol)

    # 3. Verify CALL trade was closed with BULL_TRAP_EXIT and PUT reversal was entered
    with account.connect() as c:
        trades = c.execute("SELECT id, state, exit_reason FROM trades").fetchall()
        assert len(trades) == 2
        # First trade closed with BULL_TRAP_EXIT
        assert trades[0]["id"] == "call-initial"
        assert trades[0]["state"] == "CLOSED"
        assert trades[0]["exit_reason"] == "BULL_TRAP_EXIT"

        # Second trade is the opposite PUT reversal flip!
        assert trades[1]["state"] == "OPEN"

        # Check body of reversal trade
        rev_body = json.loads(c.execute("SELECT body FROM trades WHERE id=?", (trades[1]["id"],)).fetchone()[0])
        assert rev_body["side"] == "PUT"
        assert rev_body["source_family"] == "TRAP_REVERSAL_FLIP"


def test_dynamic_time_of_day_strike_rotation(tmp_path, config):
    settings = config.model_copy(update={"paper_exit_quote_policy": FRESH_BID})
    emp.install_master_risk_limits(max_lots=4)
    fee_pol_id = get_fee_policy(NOW.date().isoformat()).policy_id

    account = emp.EnhancedMasterAccount(
        tmp_path / "account_rotation.db",
        settings,
        run_id="test-rotation",
        config=emp.EnhancedMasterConfig(
            time_rotation_enabled=True,
            morning_strike_step=0,   # ATM in morning
            afternoon_strike_step=2, # 2-OTM in afternoon
        ),
    )

    # 1. Morning Test at 09:45 IST
    morning_time = datetime(2026, 11, 16, 9, 45, tzinfo=IST)
    sig_morning = {
        "signal_id": "sig-morning",
        "day": "2026-11-16",
        "side": "CALL",
        "signal_at": morning_time.isoformat(),
        "entry_spot": 25000.0,
        "stop_spot": 24980.0,
        "target_spot": 25060.0,
        "risk_points": 20.0,
        "source_family": "ZONE",
    }
    account.signal_engine = SimpleNamespace(advance=lambda rows, state, t: (state, [sig_morning]))

    def make_chain(t_now):
        return {
            "expiry": "2026-11-17",
            "fetched_at": t_now,
            "contracts": [
                {
                    "underlying": "NIFTY",
                    "strike": 25000,
                    "strikePrice": 25000,
                    "ce": {
                        "marketLot": 65,
                        "growwContractId": "NSE_FO|ATM",
                        "tradingSymbol": "NIFTY-ATM-CE",
                        "liveData": {"ltp": 50.0, "bid": "50.00", "ask": "50.05", "quote_time": t_now.isoformat()},
                    },
                },
                {
                    "underlying": "NIFTY",
                    "strike": 25100,
                    "strikePrice": 25100,
                    "ce": {
                        "marketLot": 65,
                        "growwContractId": "NSE_FO|OTM",
                        "tradingSymbol": "NIFTY-OTM-CE",
                        "liveData": {"ltp": 20.0, "bid": "20.00", "ask": "20.05", "quote_time": t_now.isoformat()},
                    },
                },
            ],
        }

    from app.trading.paper_risk import DeltaEvidence
    ev_morning = {
        "NIFTY-ATM-CE": DeltaEvidence(Decimal(".55"), morning_time, "NIFTY-ATM-CE", emp.SOURCE),
        "NIFTY-OTM-CE": DeltaEvidence(Decimal(".35"), morning_time, "NIFTY-OTM-CE", emp.SOURCE),
    }
    frame_morning = raw(morning_time, spot=25000)
    account.tick(now=morning_time, raw_1m=frame_morning, chain=make_chain(morning_time), evidence=ev_morning, news_safe=True, fee_policy=fee_pol_id)

    with account.connect() as c:
        trade = c.execute("SELECT body FROM trades WHERE id='sig-morning'").fetchone()
        assert trade is not None
        body = json.loads(trade[0])
        # Morning selected ATM (strike 25000)
        assert body["strike"] == 25000.0
        assert body["contract"] == "NIFTY-ATM-CE"

    # Close the morning trade to free position
    account2 = emp.EnhancedMasterAccount(
        tmp_path / "account_rotation2.db",
        settings,
        run_id="test-rotation2",
        config=emp.EnhancedMasterConfig(
            time_rotation_enabled=True,
            morning_strike_step=0,
            afternoon_strike_step=2,
        ),
    )

    # 2. Afternoon Test at 14:00 IST
    afternoon_time = datetime(2026, 11, 16, 14, 0, tzinfo=IST)
    sig_afternoon = {
        "signal_id": "sig-afternoon",
        "day": "2026-11-16",
        "side": "CALL",
        "signal_at": afternoon_time.isoformat(),
        "entry_spot": 25000.0,
        "stop_spot": 24980.0,
        "target_spot": 25060.0,
        "risk_points": 20.0,
        "source_family": "ZONE",
    }
    account2.signal_engine = SimpleNamespace(advance=lambda rows, state, t: (state, [sig_afternoon]))
    ev_afternoon = {
        "NIFTY-ATM-CE": DeltaEvidence(Decimal(".55"), afternoon_time, "NIFTY-ATM-CE", emp.SOURCE),
        "NIFTY-OTM-CE": DeltaEvidence(Decimal(".35"), afternoon_time, "NIFTY-OTM-CE", emp.SOURCE),
    }
    frame_afternoon = raw(afternoon_time, spot=25000)
    account2.tick(now=afternoon_time, raw_1m=frame_afternoon, chain=make_chain(afternoon_time), evidence=ev_afternoon, news_safe=True, fee_policy=fee_pol_id)

    with account2.connect() as c:
        trade = c.execute("SELECT body FROM trades WHERE id='sig-afternoon'").fetchone()
        assert trade is not None
        body = json.loads(trade[0])
        # Afternoon selected 2-OTM (strike 25100)
        assert body["strike"] == 25100.0
        assert body["contract"] == "NIFTY-OTM-CE"


def test_liquidity_hopping_multi_stage_partial_booking(tmp_path, config):
    settings = config.model_copy(update={"paper_exit_quote_policy": FRESH_BID})
    emp.install_master_risk_limits(max_lots=4)
    fee_pol_id = get_fee_policy(NOW.date().isoformat()).policy_id

    account = emp.EnhancedMasterAccount(
        tmp_path / "account_hop.db",
        settings,
        run_id="test-hop",
        config=emp.EnhancedMasterConfig(
            enable_liquidity_hopping=True,
            legacy_management=True,
            stop_budget_limit=Decimal("2500.00"),
        ),
    )

    t0 = datetime(2026, 11, 16, 10, 0, tzinfo=IST)
    sig = {
        "signal_id": "hop-trade",
        "day": "2026-11-16",
        "side": "CALL",
        "signal_at": t0.isoformat(),
        "entry_spot": 25000.0,
        "stop_spot": 24980.0,
        "target_spot": 25060.0,  # 3R = 60 pts
        "risk_points": 20.0,
        "source_family": "ZONE",
    }
    account.signal_engine = SimpleNamespace(advance=lambda rows, state, t: (state, [sig]))

    def make_quote(t, bid, ask):
        sym = "NIFTY-TEST-CE"
        chain = {
            "expiry": "2026-11-17",
            "fetched_at": t,
            "contracts": [
                {
                    "underlying": "NIFTY",
                    "strike": 25000,
                    "strikePrice": 25000,
                    "ce": {
                        "marketLot": 65,
                        "growwContractId": "NSE_FO|999",
                        "tradingSymbol": sym,
                        "liveData": {"ltp": float(bid), "bid": str(bid), "ask": str(ask), "quote_time": t.isoformat()},
                    },
                }
            ],
        }
        from app.trading.paper_risk import DeltaEvidence
        ev = {sym: DeltaEvidence(Decimal(".5"), t, sym, emp.SOURCE)}
        return chain, ev

    # 1. Enter trade at 25000 with 2 lots
    chain0, ev0 = make_quote(t0, bid=20.0, ask=20.05)
    frame0 = raw(t0, spot=25000)
    account.tick(now=t0, raw_1m=frame0, chain=chain0, evidence=ev0, news_safe=True, fee_policy=fee_pol_id)

    with account.connect() as c:
        trade = c.execute("SELECT body, state FROM trades WHERE id='hop-trade'").fetchone()
        assert trade["state"] == "OPEN"
        body = json.loads(trade["body"])
        assert body["lots"] == 2
        assert body["remaining_lots"] == 2

    # 2. Advance to Target 1 (25060) at t1 = 10:15
    t1 = t0 + timedelta(minutes=15)
    chain1, ev1 = make_quote(t1, bid=70.0, ask=70.05)
    frame1 = raw(t1, spot=25065)
    account.signal_engine = SimpleNamespace(advance=lambda rows, state, t: (state, []))
    account.levels_source = lambda now: [25120.0]  # Next macro liquidity pool

    account.tick(now=t1, raw_1m=frame1, chain=chain1, evidence=ev1, news_safe=True, fee_policy=fee_pol_id)

    # Verify Liquidity Hop was executed:
    with account.connect() as c:
        trade = c.execute("SELECT body, state FROM trades WHERE id='hop-trade'").fetchone()
        assert trade["state"] == "OPEN"  # Still OPEN because 1 lot remains!
        body = json.loads(trade["body"])
        assert body["has_hopped"] is True
        assert body["lots"] == 1  # 1 lot remaining
        assert body["remaining_lots"] == 1
        assert body["stop_spot"] >= 25055.0  # Ratcheted stop above breakeven!

        fills = c.execute("SELECT side, body FROM fills WHERE trade_id='hop-trade' ORDER BY id").fetchall()
        assert len(fills) == 2
        assert fills[0]["side"] == "BUY"
        assert json.loads(fills[0]["body"])["qty"] == 130  # Initial 2 lots
        assert fills[1]["side"] == "SELL"
        assert json.loads(fills[1]["body"])["qty"] == 65   # 1 lot booked

    # 3. Advance to Macro Target (25120) at t2 = 10:30
    t2 = t0 + timedelta(minutes=30)
    chain2, ev2 = make_quote(t2, bid=100.0, ask=100.05)
    frame2 = raw(t2, spot=25125)
    account.tick(now=t2, raw_1m=frame2, chain=chain2, evidence=ev2, news_safe=True, fee_policy=fee_pol_id)

    # Verify final runner closed at macro target
    with account.connect() as c:
        trade = c.execute("SELECT body, state, exit_reason FROM trades WHERE id='hop-trade'").fetchone()
        assert trade["state"] == "CLOSED"
        assert trade["exit_reason"] == "TARGET_HIT"

        fills = c.execute("SELECT side, body FROM fills WHERE trade_id='hop-trade' ORDER BY id").fetchall()
        assert len(fills) == 3
        assert fills[0]["side"] == "BUY"
        assert json.loads(fills[0]["body"])["qty"] == 130
        assert fills[1]["side"] == "SELL"
        assert json.loads(fills[1]["body"])["qty"] == 65
        assert fills[2]["side"] == "SELL"
        assert json.loads(fills[2]["body"])["qty"] == 65

        # Verify net PnL is highly positive
        net_pnl = account._pnl(c, "hop-trade")
        assert net_pnl > Decimal("3000.00")


def test_detect_ema_cross_signals():
    # 25 candles with downtrend then sharp upward crossover
    candles = []
    base_time = datetime(2026, 11, 16, 9, 30, tzinfo=IST)
    price = 25000.0
    for i in range(21):
        price -= 2.0  # Slow drop -> 9 EMA < 20 EMA
        candles.append({
            "date": (base_time + timedelta(minutes=i)).isoformat(),
            "open": price + 1.0,
            "high": price + 2.0,
            "low": price - 1.0,
            "close": price,
        })
    # Sharp surge to cause 9 EMA to cross 20 EMA
    for i in range(21, 25):
        price += 15.0
        candles.append({
            "date": (base_time + timedelta(minutes=i)).isoformat(),
            "open": price - 5.0,
            "high": price + 2.0,
            "low": price - 6.0,
            "close": price,
        })

    sigs = emp.detect_ema_cross_signals(candles, "2026-11-16", fast_period=9, slow_period=20, current_bar_only=False)
    assert len(sigs) >= 1
    call_sig = sigs[-1]
    assert call_sig["side"] == "CALL"
    assert call_sig["source_family"] == "EMA_9_20"
    assert call_sig["risk_points"] >= 12.0
    assert call_sig["stop_spot"] < call_sig["entry_spot"]
    assert call_sig["target_spot"] > call_sig["entry_spot"]


def test_ema_cross_end_to_end_tick_execution(tmp_path, config):
    settings = config.model_copy(update={"paper_exit_quote_policy": FRESH_BID})
    emp.install_master_risk_limits(max_lots=4)
    fee_pol_id = get_fee_policy(NOW.date().isoformat()).policy_id

    account = emp.EnhancedMasterAccount(
        tmp_path / "account_ema_live.db",
        settings,
        run_id="test-ema-live",
        config=emp.EnhancedMasterConfig(
            enable_ema_cross_strategy=True,
            time_rotation_enabled=True,
            stop_budget_limit=Decimal("2500.00"),
        ),
    )

    session_open = datetime(2026, 11, 16, 9, 15, tzinfo=IST)
    candles = []
    price = 25000.0
    for i in range(21):
        price -= 2.0
        candles.append({
            "date": (session_open + timedelta(minutes=i)).isoformat(),
            "open": price + 1.0,
            "high": price + 2.0,
            "low": price - 1.0,
            "close": price,
        })
    for i in range(21, 25):
        price += 15.0
        candles.append({
            "date": (session_open + timedelta(minutes=i)).isoformat(),
            "open": price - 5.0,
            "high": price + 2.0,
            "low": price - 6.0,
            "close": price,
        })

    # Bar 24 opens at 09:39, closes at 09:40. Tick at 09:40:05 (within 90s freshness window)
    t_cross = session_open + timedelta(minutes=25, seconds=5)
    sym = "NIFTY-EMA-CE"
    strike = round(price / 50.0) * 50.0
    chain = {
        "expiry": "2026-11-17",
        "fetched_at": t_cross,
        "contracts": [
            {
                "underlying": "NIFTY",
                "strike": strike,
                "strikePrice": strike,
                "ce": {
                    "marketLot": 65,
                    "growwContractId": "NSE_FO|EMA",
                    "tradingSymbol": sym,
                    "liveData": {"ltp": 30.0, "bid": "30.00", "ask": "30.05", "quote_time": t_cross.isoformat()},
                },
            }
        ],
    }
    from app.trading.paper_risk import DeltaEvidence
    ev = {sym: DeltaEvidence(Decimal(".5"), t_cross, sym, emp.SOURCE)}

    import pandas as pd
    df_rows = pd.DataFrame(candles)

    # Tick without external signal_engine (detects EMA cross dynamically!)
    res = account.tick(now=t_cross, raw_1m=df_rows, chain=chain, evidence=ev, news_safe=True, fee_policy=fee_pol_id)

    with account.connect() as c:
        trades = c.execute("SELECT id, state, body FROM trades").fetchall()
        assert len(trades) == 1
        assert trades[0]["state"] == "OPEN"
        body = json.loads(trades[0]["body"])
        assert body["side"] == "CALL"
        assert body["source_family"] == "EMA_9_20"
        assert body["lots"] >= 1


