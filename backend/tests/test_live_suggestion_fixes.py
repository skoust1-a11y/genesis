"""Live suggestion gates, the option ticket, the path check, and the stock flag."""

from __future__ import annotations

import inspect
from datetime import datetime, time, timedelta
from pathlib import Path

import pandas as pd
import pytest

from app.config import get_settings
from app.liquidity_signals import scan_retest, scan_trendline_reversal
from app.market.feed import IST, MarketSnapshot
from app.trading.crew.alert_agent import AlertAgent
from app.trading.crew.entry_agent import TradeSignal, select_strike_with_delta_floor
from app.trading.crew.holding_agent import HoldingAgent, compute_session_vwap
from app.trading.crew.manager import TradeManager
from app.trading.crew.order_flow_agent import OrderFlowAgent
from app.trading.crew.trend_agent import TrendAgent
from app.trading.odds import HistTrade, path_check
from app.trading.option_pnl import black76, plan_pnl
from app.trading.scanner_crew.detector_agent import CandidateSetup
from app.trading.stock_flag import pick_stock_line, scan_stock_flag, volume_ok

ROOT = Path(__file__).resolve().parents[2]


def _trend(n: int = 40, start: float = 23000.0) -> pd.DataFrame:
    closes = [start + i * 12 for i in range(n)]
    return pd.DataFrame({
        "date": [f"2026-11-02 10:10:00" if i == n - 1 else f"2026-11-02 09:15:00" for i in range(n)],
        "open": [c - 2.0 for c in closes],
        "high": [c + 6.0 for c in closes],
        "low": [c - 6.0 for c in closes],
        "close": closes,
        "volume": [0.0] * n,
    })


def _chain(spot: float, lot: int = 65) -> dict:
    atm = round(spot / 50.0) * 50
    rows = []
    for strike, ce_delta, ce_oi, pe_oi in (
        (atm + 500, 0.05, 5_000_000, 1),
        (atm + 100, 0.20, 10, 1),
        (atm + 50, 0.42, 10, 1),
        (atm - 500, 0.90, 1, 5_000_000),
    ):
        rows.append({
            "strike": strike,
            "ce": {"liveData": {"oi": ce_oi, "ltp": 20.0}, "greeks": {"delta": ce_delta}, "marketLot": lot},
            "pe": {"liveData": {"oi": pe_oi, "ltp": 20.0}, "greeks": {"delta": -0.2}, "marketLot": lot},
        })
    return {"expiry": "2026-11-10", "lot_size": lot, "contracts": rows}


def _candidate(price: float, super_flag: bool = False) -> CandidateSetup:
    return CandidateSetup(
        strategy="OR_BREAKOUT",
        symbol="NIFTY",
        direction="CALL",
        trigger_price=price,
        tentative_stop=price - 30.0,
        tentative_target=price + 90.0,
        timeframe="5m",
        timestamp="2026-11-02 10:10:00",
        rationale="test",
        news_sentiment_score=0.0,
        event_risk_cleared=True,
        is_valid_for_trade_crew=True,
        risk_points=30.0,
        target_points=90.0,
        rr_ratio=3.0,
        is_super_trend=super_flag,
    )


def _path(**_kwargs):
    return {
        "target_pct": 10.0, "stop_pct": 80.0, "neither_pct": 10.0,
        "verdict": "leans to stop", "odds_line": "VIX band", "neither_pnl": None,
        "market_lot": 65, "sessions": 1,
    }


def _signal() -> TradeSignal:
    return TradeSignal(
        strategy="OR_BREAKOUT", symbol="NIFTY", direction="CALL",
        entry_price=24000.0, stop_loss=23970.0, target_price=24090.0,
        risk_points=30.0, target_points=90.0, rr_ratio=3.0,
        lots=2, quantity=130, suggested_strike="NIFTY 24050 CE",
        timestamp="2026-11-02 10:10:00", rationale="test", regime="TREND", adx=30.0,
        path_target_pct=55.0, path_stop_pct=30.0, path_neither_pct=15.0,
        path_verdict="leans to target", odds_line="VIX 13-16",
    )


def _priced_chain(spot: float = 24000.0) -> dict:
    basis, iv, lot = 80.0, 0.13, 75
    expiry = (datetime.now(IST).date() + timedelta(days=6)).isoformat()
    expires = datetime.combine(datetime.fromisoformat(expiry).date(), time(15, 30), IST)
    years = max((expires - datetime.now(IST)).total_seconds() / 86400 / 365, 1 / 365)
    rows = []
    for strike in range(23800, 24250, 50):
        rows.append({
            "strike": strike,
            "ce": {
                "liveData": {"oi": 1000, "ltp": round(black76(spot + basis, strike, years, iv, "CE"), 2)},
                "greeks": {"delta": 0.42}, "marketLot": lot,
            },
            "pe": {
                "liveData": {"oi": 1000, "ltp": round(black76(spot + basis, strike, years, iv, "PE"), 2)},
                "greeks": {"delta": -0.42}, "marketLot": lot,
            },
        })
    rows.append({
        "strike": 24500,
        "ce": {"liveData": {"oi": 9_000_000, "ltp": 5.0}, "greeks": {"delta": 0.05}, "marketLot": lot},
        "pe": {"liveData": {"oi": 1, "ltp": 900.0}, "marketLot": lot},
    })
    rows.append({
        "strike": 23500,
        "ce": {"liveData": {"oi": 1, "ltp": 900.0}, "marketLot": lot},
        "pe": {"liveData": {"oi": 9_000_000, "ltp": 5.0}, "greeks": {"delta": -0.05}, "marketLot": lot},
    })
    return {"expiry": expiry, "lot_size": lot, "contracts": rows}


def _flag_bars(day_open: float, push_high: float, last_close: float, volume: float = 500.0, closed: bool = True) -> list[dict]:
    bars = [{"open": day_open, "high": day_open + 1, "low": day_open - 1, "close": day_open, "volume": 100, "date": "2026-11-02 10:00"} for _ in range(18)]
    bars.append({"open": day_open, "high": push_high, "low": day_open, "close": day_open + (push_high - day_open) * 0.8, "volume": 200, "date": "2026-11-02 10:05"})
    bars.append({"open": last_close - 1, "high": last_close - 0.5, "low": push_high - 1.2, "close": last_close - 0.8, "volume": 120, "date": "2026-11-02 10:10"})
    bars.append({
        "open": last_close - 2, "high": last_close + 1.6, "low": last_close - 2.2, "close": last_close,
        "volume": volume, "date": "2026-11-02 10:15", "closed": closed,
    })
    return bars


def test_oi_is_read_from_live_data_and_a_missing_chain_sends_no_alert(tmp_path, monkeypatch):
    walls = OrderFlowAgent().extract_option_oi_walls([
        {"strike": 24100, "ce": {"liveData": {"oi": 10}, "openInterest": 9_999_999}, "pe": {"liveData": {"oi": 1}}},
        {"strike": 24200, "ce": {"liveData": {"oi": 500}}, "pe": {"liveData": {"oi": 1}}},
        {"strike": 23800, "ce": {"liveData": {"oi": 1}}, "pe": {"liveData": {"oi": 700}}},
    ])
    assert walls["call_oi_wall"] == 24200
    assert walls["put_oi_wall"] == 23800

    sent = []
    monkeypatch.setattr(AlertAgent, "send_entry_alert", lambda *a, **k: sent.append("alert") or True)
    monkeypatch.setattr(AlertAgent, "_send", lambda *a, **k: sent.append("send") or True)
    tm = TradeManager(trades_dir=tmp_path / "trades", db_path=tmp_path / "trades.db")
    df = _trend()
    res = tm.on_market_tick(
        cmp=float(df.close.iloc[-1]), df_5m=df, pdh=25000, pdl=22000,
        candidate_setups=[_candidate(float(df.close.iloc[-1]))],
        date_str="2026-11-02", current_time_str="10:15", india_vix=14.0,
    )
    assert sent == []
    assert any(a["action"] == "VETO_MISSING_WALLS" for a in res["actions"])
    assert not any(a["action"] == "ENTRY_OPENED" for a in res["actions"])


def test_missing_vix_and_a_short_adx_sample_veto(tmp_path, monkeypatch):
    from app.api import market

    tm = TradeManager(trades_dir=tmp_path / "trades", db_path=tmp_path / "trades.db")
    tick = dict(cmp=24000, df_5m=_trend(), pdh=25000, pdl=22000, date_str="2026-11-02", current_time_str="10:15")
    # The scanner owns the VIX fetch; the frozen manager never fetches one itself, even when a quote exists.
    monkeypatch.setattr(market, "fetch_quotes", lambda symbols: {s: {"price": 17.4, "time": "2026-11-02 10:15"} for s in symbols})
    assert any(a["action"] == "VETO_MISSING_VIX" for a in tm.on_market_tick(**tick)["actions"])
    assert not any(a["action"] == "VETO_MISSING_VIX" for a in tm.on_market_tick(**tick, india_vix=17.4)["actions"])
    assert TrendAgent().calculate_adx(_trend(10)) == 0.0
    regime = TrendAgent().evaluate_regime(_trend(10), 23040, "CALL", None)
    assert regime.is_breakout_allowed is False


def _live_scanner():
    import importlib.util

    spec = importlib.util.spec_from_file_location("live_scanner_under_test", ROOT / "scripts" / "live_scanner.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scanner_vix_is_fresh_or_none(monkeypatch):
    from app.api import market

    ls = _live_scanner()
    now = datetime(2026, 11, 2, 10, 15, 30, tzinfo=ls.IST)

    def vix(quote):
        monkeypatch.setattr(market, "fetch_quotes", lambda symbols: {s: quote for s in symbols})
        return ls._fetch_india_vix(now)

    assert vix({"price": 17.4, "time": "2026-11-02 10:14"}) == 17.4
    for bad in (
        None,  # missing
        {"price": None, "time": "2026-11-02 10:14"},
        {"price": "n/a", "time": "2026-11-02 10:14"},
        {"price": float("nan"), "time": "2026-11-02 10:14"},
        {"price": 0.0, "time": "2026-11-02 10:14"},
        {"price": 17.4},
        {"price": 17.4, "time": None},
        {"price": 17.4, "time": "garbage"},
        {"price": 17.4, "time": "2026-11-02 10:10"},  # 330 s old
        {"price": 17.4, "time": "2026-10-30 15:29"},  # last session's close
        {"price": 17.4, "time": "2026-11-02 10:20"},  # from the future
    ):
        assert vix(bad) is None, bad

    def down(symbols):
        raise RuntimeError("source unreachable")

    monkeypatch.setattr(market, "fetch_quotes", down)
    assert ls._fetch_india_vix(now) is None


def test_scanner_vix_drives_gate_d_like_d5d743f(tmp_path, monkeypatch):
    from app.api import market

    monkeypatch.setattr("app.trading.odds.path_check", _path)
    ls = _live_scanner()
    now = datetime(2026, 11, 2, 10, 15, 30, tzinfo=ls.IST)
    df = _trend()
    price = float(df.close.iloc[-1])

    def tick(name, vix):
        tm = TradeManager(trades_dir=tmp_path / name, db_path=tmp_path / f"{name}.db")
        res = tm.on_market_tick(
            cmp=price, df_5m=df, pdh=price + 800, pdl=price - 800, candidate_setups=[_candidate(price)],
            date_str="2026-11-02", current_time_str="10:15", india_vix=vix, nifty_chain=_chain(price),
        )
        return [a["action"] for a in res["actions"]]

    def scanned(name, quote):
        monkeypatch.setattr(market, "fetch_quotes", lambda symbols: {s: quote for s in symbols})
        return tick(name, ls._fetch_india_vix(now))

    # d5d743f read the same quote's price inside the manager, so a fresh quote must give the same tick.
    fresh = scanned("fresh", {"price": 17.4, "time": "2026-11-02 10:15"})
    assert "ENTRY_OPENED" in fresh
    assert fresh == tick("d5d743f", 17.4)
    for name, quote in (("low", 11.4), ("high", 22.1)):
        actions = scanned(name, {"price": quote, "time": "2026-11-02 10:15"})
        assert "VETO_VIX_CORRIDOR" in actions and "ENTRY_OPENED" not in actions
    for name, quote in (("stale", {"price": 17.4, "time": "2026-11-02 10:05"}), ("missing", None)):
        actions = scanned(name, quote)
        assert "VETO_MISSING_VIX" in actions and "ENTRY_OPENED" not in actions


def test_every_entry_capable_caller_supplies_checked_vix():
    from app.api import market

    callers = {
        p.relative_to(ROOT).as_posix()
        for d in ("scripts", "backend/app", "backend/scripts")
        for p in (ROOT / d).rglob("*.py")
        if ".on_market_tick(" in p.read_text(encoding="utf-8", errors="ignore")
    }
    replays = {"scripts/diagnose_trades.py": "tm.alert_agent._send = lambda m: True",
               "scripts/resimulate_since_oct15.py": "silent_alert_agent._send = lambda msg: True"}
    assert callers == {"scripts/live_scanner.py", *replays}
    assert "india_vix=_fetch_india_vix(datetime.now(IST))" in (ROOT / "scripts" / "live_scanner.py").read_text()
    # Offline replays: a temp journal and a stubbed send, so their fixed VIX reaches no account or channel.
    for rel, stub in replays.items():
        text = (ROOT / rel).read_text()
        assert "tempfile.mkdtemp(" in text and stub in text, rel
    # The frozen manager falls back to app.api.market.get_quote; while it is absent, a None VIX always vetoes.
    assert not hasattr(market, "get_quote")


def test_lunch_rr_does_not_bypass_and_super_trend_sizes_from_adx_only(tmp_path, monkeypatch):
    monkeypatch.setattr("app.trading.odds.path_check", _path)
    tm = TradeManager(trades_dir=tmp_path / "trades", db_path=tmp_path / "trades.db")
    quiet = _candidate(24000)
    quiet.risk_points = 20.0
    lunch = tm.on_market_tick(
        cmp=24000, df_5m=_trend(), pdh=25000, pdl=22000,
        candidate_setups=[quiet],
        date_str="2026-11-02", current_time_str="12:15", india_vix=14.0,
        nifty_chain=_chain(24000),
    )
    assert any(a["action"] == "VETO_LUNCH_LULL" for a in lunch["actions"])

    df = _trend()
    price = float(df.close.iloc[-1])
    opened = tm.on_market_tick(
        cmp=price, df_5m=df, pdh=price + 800, pdl=price - 800,
        candidate_setups=[_candidate(price, super_flag=False)],
        date_str="2026-11-02", current_time_str="10:15", india_vix=14.0,
        nifty_chain=_chain(price),
    )
    entry = next(a for a in opened["actions"] if a["action"] == "ENTRY_OPENED")
    assert entry["lots"] == 4
    assert entry["path_verdict"] == "leans to stop"
    saved = tm.journal_agent.load_active_trades("2026-11-02")
    row = saved[entry["trade_id"]]
    assert row["path_verdict"] == "leans to stop"
    assert row["path_target_pct"] == 10.0

    monkeypatch.setattr(TrendAgent, "calculate_adx", lambda self, df, period=14: 22.0)
    tm.journal_agent.save_active_trades({}, "2026-11-02")
    tm.executed_order_signatures.clear()
    smaller = tm.on_market_tick(
        cmp=price, df_5m=df, pdh=price + 800, pdl=price - 800,
        candidate_setups=[_candidate(price, super_flag=True)],
        date_str="2026-11-02", current_time_str="10:20", india_vix=14.0,
        nifty_chain=_chain(price),
    )
    again = next(a for a in smaller["actions"] if a["action"] == "ENTRY_OPENED")
    assert again["lots"] == 3


def test_strike_walks_the_live_delta_and_a_missing_quote_is_not_a_model_price(monkeypatch):
    chain = [
        {"strike": 24100, "ce": {"greeks": {"delta": 0.20}}},
        {"strike": 24050, "ce": {"greeks": {"delta": 0.40}}},
        {"strike": 23900, "pe": {"greeks": {"delta": -0.20}}},
        {"strike": 23950, "pe": {"greeks": {"delta": -0.41}}},
    ]
    assert select_strike_with_delta_floor(24000, "CALL", chain=chain) == ("NIFTY 24050 CE", 0.4)
    assert select_strike_with_delta_floor(24000, "PUT", chain=chain) == ("NIFTY 23950 PE", 0.41)
    assert select_strike_with_delta_floor(24000, "CALL", chain=[{"strike": 24100, "ce": {"greeks": {"delta": 0.1}}}])[1] == 0.0

    sent = []
    monkeypatch.setattr(AlertAgent, "_send", lambda self, text: sent.append(text) or True)
    assert AlertAgent(is_paper_trading=False).send_entry_alert(_signal(), nifty_chain=None) is False
    assert sent == []
    # Paper mode reports the entry as unpriced instead of inventing a model premium.
    assert AlertAgent().send_entry_alert(_signal(), nifty_chain=None) is True
    assert len(sent) == 1 and "UNPRICED" in sent[0] and "not an executable ticket" in sent[0]
    assert "27" not in "".join(sent)


def test_ticket_stop_and_1r_are_plan_pnl(monkeypatch):
    sent = []
    monkeypatch.setattr(AlertAgent, "_send", lambda self, text: sent.append(text) or True)
    chain = _priced_chain()
    signal = _signal()
    assert AlertAgent().send_entry_alert(signal, nifty_chain=chain) is True
    quotes = {}
    for row in chain["contracts"]:
        for side, opt in (("ce", "CE"), ("pe", "PE")):
            ltp = ((row.get(side) or {}).get("liveData") or {}).get("ltp")
            if ltp:
                quotes[(float(row["strike"]), opt, chain["expiry"])] = float(ltp)
    now = datetime.now(IST)
    snap = MarketSnapshot(
        underlying="NIFTY", timestamp=now, as_of=now.date().isoformat(), spot=signal.entry_price,
        iv_rank=None, dte=6, expiry=chain["expiry"], quotes=quotes, lot_size=75,
    )
    plan = plan_pnl(
        snap, get_settings(), "CE", 24050, signal.entry_price,
        {"stop": signal.stop_loss, "1r": signal.entry_price + signal.risk_points, "2r": signal.entry_price + 2 * signal.risk_points},
        lots=1, hold_days=0.0,
    )
    by_name = {row["exit"]: row["premium"] for row in plan["exits"]}
    text = sent[0]
    assert f"Rs {by_name['stop']:.1f}" in text
    assert f"Rs {by_name['1r']:.1f}" in text
    assert "27.8" not in text
    assert "leans to target" in text


def test_volume_zero_is_not_a_surge_and_vwap_is_typical_price():
    df = pd.DataFrame({
        "open": [100, 100, 100], "high": [101, 101, 110], "low": [99, 99, 99],
        "close": [100.5, 100.5, 100.2], "volume": [0, 0, 0],
    })
    trap, _, _ = OrderFlowAgent().detect_absorption_trap(df, "CALL")
    assert trap is False
    flat = pd.DataFrame({"high": [10, 12], "low": [8, 10], "close": [9, 11], "volume": [0, 0]})
    assert compute_session_vwap(flat) == pytest.approx(10.0)

    bars = [{"open": 110, "high": 112, "low": 108, "close": 110, "volume": 0} for _ in range(5)]
    bars.append({"open": 100, "high": 101, "low": 94, "close": 95, "volume": 0})
    decision = HoldingAgent().evaluate_holding(
        active_trade={"direction": "CALL", "entry_price": 100.0, "stop_loss": 90.0, "risk_points": 10.0, "strategy": "OR_BREAKOUT"},
        cmp=95.0, df_5m=pd.DataFrame(bars), bars_in_trade=3, current_time_str="10:15",
    )
    assert decision.action == "PREEMPTIVE_EXIT"
    assert "VWAP" in decision.rationale


def test_path_check_returns_four_numbers_and_does_not_block(tmp_path, monkeypatch):
    history = [
        HistTrade("2026-09-01", "2026-09-02", 1000.0, 14.0),
        HistTrade("2026-09-03", "2026-09-04", -400.0, 14.0),
    ]
    sessions = [[{"date": "2026-10-01 10:15", "open": 100, "high": 200, "low": 99, "close": 150}]]
    out = path_check(
        entry=24000, stop=23970, target=24040, direction="CALL", clock="10:15",
        as_of="2026-11-02", vix=14.0, history=history, sessions=sessions, n_paths=50,
    )
    assert out["target_pct"] == 100.0
    assert out["stop_pct"] == 0.0
    assert out["neither_pct"] == 0.0
    assert out["verdict"] == "leans to target"

    monkeypatch.setattr("app.trading.odds.path_check", _path)
    tm = TradeManager(trades_dir=tmp_path / "trades", db_path=tmp_path / "trades.db")
    df = _trend()
    price = float(df.close.iloc[-1])
    res = tm.on_market_tick(
        cmp=price, df_5m=df, pdh=price + 800, pdl=price - 800,
        candidate_setups=[_candidate(price)],
        date_str="2026-11-02", current_time_str="10:15", india_vix=14.0, nifty_chain=_chain(price),
    )
    assert any(a["action"] == "ENTRY_OPENED" and a["path_verdict"] == "leans to stop" for a in res["actions"])


def test_stale_bar_blocks_new_trades_and_an_open_trade_still_reaches_holding(tmp_path, monkeypatch):
    seen = []
    real = HoldingAgent.evaluate_holding

    def wrap(self, *args, **kwargs):
        seen.append(1)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(HoldingAgent, "evaluate_holding", wrap)
    tm = TradeManager(trades_dir=tmp_path / "trades", db_path=tmp_path / "trades.db")
    stale = _trend()
    stale["quote_age_seconds"] = 900
    packs = {"RELIANCE": {"bars": _flag_bars(100, 106, 108), "pdh": 120, "pdl": 90}}
    flat = tm.on_market_tick(
        cmp=float(stale.close.iloc[-1]), df_5m=stale, pdh=25000, pdl=22000,
        date_str="2026-11-02", current_time_str="10:15", india_vix=14.0,
        nifty_chain=_chain(float(stale.close.iloc[-1])), stock_bars=packs,
    )
    assert any(a["action"] == "VETO_STALE_BAR" for a in flat["actions"])
    assert not any(a["action"] in {"ENTRY_OPENED", "STOCK_FLAG"} for a in flat["actions"])

    tm.journal_agent.save_active_trades({
        "T1": {
            "trade_id": "T1", "direction": "CALL", "entry_price": 24000.0, "stop_loss": 23970.0,
            "target_price": 24090.0, "risk_points": 30.0, "lots": 3, "timestamp": "2026-11-02 10:00:00",
            "symbol": "NIFTY", "suggested_strike": "NIFTY 24100 CE", "status": "OPEN", "strategy": "OR_BREAKOUT",
        }
    }, "2026-11-02")
    held = tm.on_market_tick(
        cmp=23990.0, df_5m=stale, pdh=25000, pdl=22000,
        df_15m=pd.DataFrame({
            "open": [24020.0, 24020.0], "high": [24030.0, 24025.0],
            "low": [24010.0, 23970.0], "close": [24015.0, 23980.0],
        }),
        date_str="2026-11-02", current_time_str="10:15", india_vix=14.0, stock_bars=packs,
    )
    assert seen
    assert any(a["action"] == "REVERSAL_EXIT" for a in held["actions"])
    assert not any(a["action"] == "STOCK_FLAG" for a in held["actions"])
    assert "T1" not in tm.journal_agent.load_active_trades("2026-11-02")


def test_stock_flag_rules():
    assert volume_ok("NIFTY", [{"volume": 0}] * 21) is True
    assert volume_ok("RELIANCE", [{"volume": 0}] * 21) is False
    bars = _flag_bars(100, 106, 108)
    assert scan_stock_flag("NIFTY", bars, nifty_median_tr=2.0, nifty_below_vwap=False) is None
    assert scan_stock_flag("RELIANCE", _flag_bars(100, 106, 108, volume=0), 2.0, False) is None
    pause = bars[:-1]
    assert scan_stock_flag("RELIANCE", pause, 2.0, False) is None
    forming = _flag_bars(100, 106, 108, closed=False)
    assert scan_stock_flag("RELIANCE", forming, 2.0, False) is None
    assert scan_stock_flag("RELIANCE", bars, 2.0, nifty_below_vwap=True) is None
    hit = scan_stock_flag("RELIANCE", bars, 2.0, False)
    assert hit["target"] == 106
    assert hit["stop"] == pytest.approx(104.8)
    line = pick_stock_line({
        "RELIANCE": {"bars": _flag_bars(100, 106, 110), "pdh": 200, "pdl": 50},
        "HDFCBANK": {"bars": _flag_bars(100, 106, 104), "pdh": 200, "pdl": 50},
    }, nifty_median_tr=2.0, nifty_below_vwap=False)
    assert line.startswith("RELIANCE LONG")
    assert "target 106" in line


def test_candle_confirmation_stays_false_and_rc_quick_cut_does_not_close(tmp_path):
    assert inspect.signature(scan_retest).parameters["candle_confirmation"].default is False
    assert inspect.signature(scan_trendline_reversal).parameters["candle_confirmation"].default is False
    tm = TradeManager(trades_dir=tmp_path / "trades", db_path=tmp_path / "trades.db")
    tm.journal_agent.save_active_trades({
        "T1": {
            "trade_id": "T1", "direction": "CALL", "entry_price": 24000.0, "stop_loss": 23970.0,
            "target_price": 24090.0, "risk_points": 30.0, "lots": 3, "timestamp": "2026-11-02 10:00",
            "entry_candle_low": 23990.0, "entry_candle_high": 24010.0,
            "symbol": "NIFTY", "suggested_strike": "NIFTY 24100 CE", "status": "OPEN", "strategy": "OR_BREAKOUT",
            "rc_quick_cut": False,
        }
    }, "2026-11-02")
    df = pd.DataFrame({
        "date": ["2026-11-02 10:00:00", "2026-11-02 10:05:00", "2026-11-02 10:10:00", "2026-11-02 10:10:00"],
        "open": [24000, 23998, 23997, 23996],
        "high": [24010, 24002, 24001, 24000],
        "low": [23990, 23980, 23980, 23992],
        "close": [24005, 23996, 23994, 23996],
        "volume": [0, 0, 0, 0],
    })
    res = tm.on_market_tick(
        cmp=23996.0, df_5m=df, pdh=25000, pdl=22000,
        date_str="2026-11-02", current_time_str="10:15", india_vix=14.0,
    )
    assert not any(a["action"] in {"REVERSAL_EXIT", "TARGET_HIT", "STAGNATION_TIME_STOP"} for a in res["actions"])
    assert tm.journal_agent.load_active_trades("2026-11-02")["T1"]["rc_quick_cut"] is True
