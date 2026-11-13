"""P0 scanner reproductions found at 2bd74e4 (scanner-v0) and their scanner-v0.1 containment.

``defect`` tests pin behaviour of the frozen, unedited functions (``scan``, ``scan_retest``, ``LevelsAgent``
without a session date, ``DynamicLookoutWindow``) that makes a multi-session input unsafe. ``contained`` tests
pin what scanner-v0.1 feeds them instead. ``guard`` tests are independent protections that must keep passing.
Deterministic fixtures, no network, temp journals only.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from app.config import get_settings
from app.liquidity_signals import DynamicLookoutWindow, scan, scan_retest
from app.market.hours import market_status
from app.trading import admission as adm
from app.trading import research_runtime as rr
from app.trading import market_validation as mv
from app.trading.market_validation import closed_bars
from app.trading.scanner_crew.detector_agent import DetectorAgent
from app.trading.scanner_crew.feed_agent import FeedAgent
from app.trading.scanner_crew.levels_agent import LevelsAgent, SessionLevels

D1, D2 = "2026-11-05", "2026-11-06"  # prior session, evaluation session


def _bars(day: str, closes, start: str = "09:15", vol: float = 1000.0) -> list[dict]:
    t0 = datetime.fromisoformat(f"{day}T{start}")
    return [{"date": (t0 + timedelta(minutes=5 * i)).strftime("%Y-%m-%d %H:%M"), "open": c - 1.0, "high": c + 2.0,
             "low": c - 2.0, "close": float(c), "volume": vol} for i, c in enumerate(closes)]


def _pad(closes, n=75):
    return list(closes) + [closes[-1]] * (n - len(closes))


# A PDL=24000 sweep that confirms on the 15m break at 11:00 (risk 52, inside the detector's 10..55 band).
SWEEP_DAY = _pad([24010, 24015, 24020, 24020, 24025, 24028, 24030, 24035, 24032, 24025, 24015, 24008,
                  24005, 24000, 23995, 24000, 24010, 24020, 24030, 24036, 24040])
# A PDH=24100 break, retest and confirmation at 09:50 (risk 35).
RETEST_DAY = _pad([24080, 24085, 24090, 24095, 24105, 24082, 24110])
# Prior session: opens 24097, high 24100, low 24000, VWAP ~24050 -- a different open and VWAP from D2.
PRIOR_DAY = [24098 - 96 * i / 74 for i in range(75)]
LEVELS = SessionLevels(pdh=24100.0, pdl=24000.0, pdc=24002.0, pivot=24034.0, r1=24068.0, s1=23968.0)


def _vwap_above(bars):
    """The detector's own VWAP map: cumulative over whatever frame it is given."""
    cv = cpv = 0.0
    out = {}
    for b in bars:
        cv += b["volume"]
        cpv += (b["high"] + b["low"] + b["close"]) / 3 * b["volume"]
        out[b["date"]] = b["close"] >= cpv / cv
    return out


def _detect(bars, tf="5m", strategy_filter="auto", levels=LEVELS):
    det = DetectorAgent(news_agent=SimpleNamespace(evaluate_news_sentiment=lambda **_: None))
    sentiment = SimpleNamespace(high_impact_event_active=False, sentiment_score=0.0, is_trading_safe=True)
    return det.scan_multi_timeframe_strategies({tf: bars}, levels, news_sentiment=sentiment,
                                               current_time_str="14:00", strategy_filter=strategy_filter)


# --- 1. the live 5m frame spans several sessions ----------------------------------------------------------


def _yahoo(monkeypatch, days):
    import app.api.market as market

    bars = [b for d in days for b in _bars(d, PRIOR_DAY)]
    monkeypatch.setattr(market, "_chart", lambda *a, **k: {"bars": bars})
    monkeypatch.setattr(market, "parse_yahoo_chart", lambda payload: payload["bars"])
    return bars


def test_defect_feed_frame_is_multi_session_and_closed_bars_keeps_it(monkeypatch):
    days = ["2026-11-02", "2026-11-03", "2026-11-04", D1, D2]
    _yahoo(monkeypatch, days)
    feed = FeedAgent()
    fresh = feed.fetch_timeframe_candles("5m")
    assert len(fresh) == 50 and fresh["date"].str[:10].nunique() == 1  # tail(50) at the session close
    cached = feed.fetch_timeframe_candles("5m")  # inside the 3 s cache window: the untrimmed frame
    assert len(cached) == 375 and cached["date"].str[:10].nunique() == 5
    now = datetime.fromisoformat(f"{D2}T15:32:00+05:30")
    kept = closed_bars(cached, now)
    assert kept["date"].str[:10].nunique() == 5  # only the LAST bar is checked against the session date
    # Mid-morning the fresh tail(50) already crosses the session boundary (12 bars today + 38 from D1).
    mid = closed_bars(feed.candle_cache["5m"].iloc[:-63], datetime.fromisoformat(f"{D2}T10:15:00+05:30"))
    assert set(mid.tail(50)["date"].str[:10]) == {D1, D2}


# --- 2/3. scan and scan_retest decide differently on one session vs several ---------------------------------


def test_defect_scan_multi_session_suppresses_todays_sweep():
    today = _bars(D2, SWEEP_DAY)
    one = scan(today, _vwap_above(today), LEVELS.pdh, LEVELS.pdl)
    assert [(s["side"], s["time"], s["entry"], s["stop"]) for s in one] == [("Long", f"{D2} 11:00", 24040.0, 23988.0)]
    frame = _bars(D1, PRIOR_DAY) + today
    assert scan(frame, _vwap_above(frame), LEVELS.pdh, LEVELS.pdl) == []  # D1's open 24097 is used as "day open"
    assert [s.strategy for s in _detect(today, strategy_filter="sweep")] == ["Liquidity Sweep [5M]"]
    assert _detect(frame, strategy_filter="sweep") == []


def test_defect_scan_multi_session_returns_a_prior_day_signal():
    frame = _bars(D1, SWEEP_DAY) + _bars(D2, SWEEP_DAY)  # identical sessions: same HH:MM signal on both days
    got = scan(frame, _vwap_above(frame), LEVELS.pdh, LEVELS.pdl)
    assert [s["time"] for s in got] == [f"{D1} 11:00"]  # scan breaks after the first entry per side
    setups = _detect(frame, strategy_filter="sweep")
    assert [(s.timestamp, s.is_valid_for_trade_crew) for s in setups] == [(f"{D1} 11:00", True)]


def test_defect_scan_retest_multi_session_stops_at_the_prior_days_last_entry():
    today = _bars(D2, RETEST_DAY)
    one = scan_retest(today, _vwap_above(today), LEVELS.pdh, LEVELS.pdl)
    assert [(s["side"], s["time"], s["entry"]) for s in one] == [("Long", f"{D2} 09:50", 24110.0)]
    frame = _bars(D1, PRIOR_DAY) + today
    assert scan_retest(frame, _vwap_above(frame), LEVELS.pdh, LEVELS.pdl) == []  # breaks at D1 14:35
    assert any(s.strategy.startswith("Breakout & Retest (PD)") for s in _detect(today, strategy_filter="retest"))
    assert _detect(frame, strategy_filter="retest") == []


def test_defect_levels_or30_and_eqhl_come_from_the_first_session_in_the_frame():
    frame = pd.DataFrame(_bars(D1, PRIOR_DAY) + _bars(D2, RETEST_DAY))
    daily = pd.DataFrame([{"high": 24100.0, "low": 24000.0, "close": 24002.0}, {"high": 0, "low": 0, "close": 0}])
    lv = LevelsAgent().extract_session_levels(daily, frame)
    today = LevelsAgent().extract_session_levels(daily, pd.DataFrame(_bars(D2, RETEST_DAY)))
    assert (lv.or30_high, lv.or30_low) == (24100.0, 24089.51)  # D1 09:15-09:40
    assert (today.or30_high, today.or30_low) == (24107.0, 24078.0)


# --- 4. a prior-day signal at the same HH:MM ---------------------------------------------------------------


def test_defect_lookout_window_ignores_the_date():
    w = DynamicLookoutWindow(setup_name="Liquidity Sweep [5M]", base_tf="5m", multiplier=rr.LOOKOUT_MULTIPLIER,
                             detected_time_str=f"{D1} 11:00")
    assert w.is_valid_window("11:05")  # 24 hours old, accepted


def _quote(now):
    from tests.test_research_runtime import chain

    return rr.setup_quote(chain(now), SimpleNamespace(suggested_strike="NIFTY 24100 CE"))


def test_guard_journal_fingerprint_rejects_a_prior_day_candle_before_signal_stale():
    now = datetime.fromisoformat(f"{D2}T11:05:00+05:30")
    cand = adm.SignalCandidate(market_day=D2, instrument="NIFTY", strategy_id="Liquidity Sweep [5M]",
                               strategy_version=rr.STRATEGY_VERSION, signal_candle=rr._ist_candle(f"{D1} 11:00"),
                               direction="CALL", setup_id="5m|24040.0")
    got = adm.evaluate(cand, _quote(now), now=now, settings=get_settings())
    assert (got.admission_class, got.reasons, got.signal_id) == ("REJECTED", (adm.SIGNAL_INVALID,), None)
    # SIGNAL_STALE alone would not catch it: it compares the clock with market_day, not with the candle.
    assert adm.SIGNAL_STALE not in got.reasons


def test_contained_runtime_rejects_a_prior_day_setup_before_the_lookout_window(tmp_path):
    from tests.test_research_runtime import Clock, Feed, rows, runtime, setup

    clock = Clock("11:05", day=D2)
    rt = runtime(tmp_path / "j.db", Feed([setup(ts=f"{D1} 11:00")], start="10:55"), clock)
    rt.start()
    try:
        out = rt.tick()
    finally:
        rt.stop()
    assert out["admissions"] == [{"strategy": "Liquidity Sweep [5M]", "recorded": False,
                                  "reasons": ["SIGNAL_OFF_SESSION"]}]
    assert rows(tmp_path / "j.db", "SELECT * FROM crew_admissions") == []


# --- scanner-v0.1 containment end to end ---------------------------------------------------------------------


class _Feed:
    """A FeedAgent returning fixed multi-session frames per timeframe."""

    def __init__(self, frames):
        self.frames = frames

    def fetch_timeframe_candles(self, tf, *a, **k):
        return pd.DataFrame(self.frames[tf])


def _m(day, start, n, minutes, close=24040.0):
    t0 = datetime.fromisoformat(f"{day}T{start}")
    return [{"date": (t0 + timedelta(minutes=minutes * i)).strftime("%Y-%m-%d %H:%M"), "open": close - 1,
             "high": close + 2, "low": close - 2, "close": close, "volume": 0} for i in range(n)]


def _manager(frames):
    from app.trading.scanner_crew.scanner_manager import ScannerManager

    return ScannerManager(feed_agent=_Feed(frames))


def test_contained_scanner_feed_gives_the_detector_only_closed_bars_of_this_session():
    seen = {}
    frames = {"5m": _bars(D1, PRIOR_DAY) + _bars(D2, SWEEP_DAY[:20]),  # D2 09:15..10:50; 10:50 forming
              "15m": _m(D1, "09:15", 25, 15) + _m(D2, "09:15", 7, 15),  # D2 10:45 forming at 10:52:30
              "1m": _m(D1, "15:00", 30, 1) + _m(D2, "09:15", 98, 1),  # D2 10:52 forming
              "1d": [{"date": f"{D1} 09:15", "open": 24097.0, "high": 24100.0, "low": 24000.0, "close": 24002.0}]}
    scanner = _manager(frames)
    real = scanner.produce_scan_snapshot
    scanner.produce_scan_snapshot = lambda **kw: (seen.update(kw), real(**kw))[1]
    now = datetime.fromisoformat(f"{D2}T10:52:30+05:30")
    view = rr.scanner_feed(scanner, chain_source=lambda: {}, clock=lambda: now)(now)
    got = {tf: (set(f["date"].str[:10]), f["date"].iloc[-1][11:]) for tf, f in seen["frames"].items()}
    assert got == {"5m": ({D2}, "10:45"), "15m": ({D2}, "10:30"), "1m": ({D2}, "10:51")}
    assert seen["session_date"] == D2 and "df_5m" not in seen
    assert set(view.df_5m["date"].str[:10]) == {D1, D2}  # history stays available for the regime label
    assert view.rejections == {}


def test_contained_todays_sweep_is_no_longer_suppressed_by_the_prior_session():
    frames = {"5m": _bars(D1, PRIOR_DAY) + _bars(D2, SWEEP_DAY[:21]), "15m": [], "1m": [],
              "1d": [{"date": f"{D1} 09:15", "open": 24097.0, "high": 24100.0, "low": 24000.0, "close": 24002.0},
                     {"date": f"{D2} 09:15", "open": 24009.0, "high": 24042.0, "low": 23993.0, "close": 24040.0}]}
    now = datetime.fromisoformat(f"{D2}T11:02:00+05:30")
    view = rr.scanner_feed(_manager(frames), chain_source=lambda: {}, clock=lambda: now)(now)
    sweeps = [(s.strategy, s.timestamp, s.trigger_price, s.tentative_stop) for s in view.setups
              if s.strategy.startswith("Liquidity Sweep")]
    assert sweeps == [("Liquidity Sweep [5M]", f"{D2} 11:00", 24040.0, 23988.0)]


def test_contained_pdh_pdl_come_from_the_dated_prior_session_or_fail_closed():
    daily = pd.DataFrame([{"date": f"{D1} 09:15", "high": 24100.0, "low": 24000.0, "close": 24002.0}])
    assert LevelsAgent().extract_session_levels(daily, session_date=D2).pdh == 24100.0  # no D2 row yet
    with pytest.raises(ValueError, match="no daily bar before"):
        LevelsAgent().extract_session_levels(daily, session_date=D1)


# --- 5. 1m / 15m frames reach the detector with their forming candle ---------------------------------------


def test_defect_15m_eql_setup_from_a_forming_candle_becomes_a_research_model_trade(tmp_path):
    """The runtime trusts the feed's bar closure; v0.1 contains this in scanner_feed's 15m closed-bar filter."""
    from tests.test_research_runtime import Clock, Feed, runtime, setup

    t0 = datetime.fromisoformat(f"{D2}T09:15")
    closes = [24060, 24050, 24040, 24030, 24025]
    b15 = [{"date": (t0 + timedelta(minutes=15 * i)).strftime("%Y-%m-%d %H:%M"), "open": c - 1, "high": c + 3,
            "low": c - 3, "close": c, "volume": 0} for i, c in enumerate(closes)]
    b15.append({"date": f"{D2} 10:45", "open": 24010.0, "high": 24014.0, "low": 23990.0, "close": 24012.0,
                "volume": 0})  # 10:45-11:00, still forming at 10:50
    levels = SessionLevels(pdh=24100.0, pdl=23900.0, pdc=24000.0, pivot=24000.0, r1=24100.0, s1=23900.0,
                           eql_levels=[24000.0])
    got = [s for s in _detect(b15, tf="15m", strategy_filter="equal", levels=levels)]
    assert [(s.strategy, s.timestamp, s.trigger_price, s.is_valid_for_trade_crew) for s in got] == [
        ("Equal Lows Sweep [15M]", f"{D2} 10:45", 24012.0, True)]  # stamped with the bar START, not its close
    clock = Clock("10:50", day=D2)
    eql = setup(trigger=24012.0, stop=23985.0, target=24093.0, ts=f"{D2} 10:45", strategy="Equal Lows Sweep [15M]",
                timeframe="15m", strike="NIFTY 24100 CE")
    rt = runtime(tmp_path / "j.db", Feed([eql], start="10:40"), clock)
    rt.start()
    try:
        out = rt.tick()
    finally:
        rt.stop()
    assert out["admissions"][0]["class"] == "RESEARCH_ONLY" and out["admissions"][0]["trade"].startswith("T")


def test_defect_15m_sweep_is_stamped_five_minutes_after_the_15m_bar_start():
    # scan() has no bar_minutes and the detector runs it on 15m frames, so a 10:45 15m bar "closes" at 10:50.
    import inspect

    from app.liquidity_signals import _at

    assert "bar_minutes" not in inspect.signature(scan).parameters
    assert _at(f"{D2} 10:45", 5) == f"{D2} 10:50"


def test_guard_forming_close_stamped_candles_are_rejected_as_future():
    now = datetime.fromisoformat(f"{D2}T10:50:30+05:30")
    for tf, start, closes_at in (("1m", "10:50", "10:51"), ("15m", "10:45", "11:00")):
        cand = adm.SignalCandidate(market_day=D2, instrument="NIFTY", strategy_id=f"Breakout & Retest (PD) [{tf.upper()}]",
                                   strategy_version=rr.STRATEGY_VERSION, signal_candle=rr._ist_candle(f"{D2} {closes_at}"),
                                   direction="CALL", setup_id=f"{tf}|24110.0")
        got = adm.evaluate(cand, _quote(now), now=now, settings=get_settings())
        assert got.reasons == (adm.MARKET_DATA_CORRUPT,), (tf, start)


# --- 6. timezone, session boundaries, Muhurat ---------------------------------------------------------------


def test_guard_closed_bars_converts_offsets_and_requires_a_bar_from_today():
    frame = pd.DataFrame(_bars(D1, PRIOR_DAY))
    assert closed_bars(frame, datetime.fromisoformat(f"{D2}T10:00:00+05:30")).empty
    utc = frame.assign(date=[(datetime.fromisoformat(d) - timedelta(hours=5, minutes=30)).strftime("%Y-%m-%dT%H:%M+00:00")
                             for d in frame["date"]])
    assert len(closed_bars(utc, datetime.fromisoformat(f"{D1}T15:32:00+05:30"))) == 75


def test_muhurat_weekday_is_open_on_the_calendar_but_fails_closed_on_data():
    muhurat = datetime.fromisoformat("2025-10-21T10:00:00+05:30")
    assert market_status(muhurat, get_settings()) == (True, "market open")  # Laxmi Pujan absent from the file
    assert closed_bars(pd.DataFrame(_bars("2025-10-20", PRIOR_DAY)), muhurat).empty  # no regular-session bars


def test_inconclusive_levels_agent_pdh_is_positional_not_dated():
    daily = pd.DataFrame([{"date": "2026-11-05", "high": 24100.0, "low": 24000.0, "close": 24002.0},
                          {"date": "2026-11-06", "high": 24300.0, "low": 24200.0, "close": 24250.0}])
    # Before Yahoo adds today's daily row, iloc[-2] is two sessions back.
    assert LevelsAgent().extract_session_levels(daily).pdh == 24100.0


# --- 7. scanner-v0.1 fail-closed session contract (missing open bar, gaps, previous session, calendar) -------

def _cal():
    from app.market.hours import holidays

    return holidays(get_settings())


def _session(day, minutes, last, drop=()):
    """Every ``minutes`` bar from 09:15 up to the bar starting at ``last`` (still forming), minus ``drop``."""
    t, stop, out = datetime.fromisoformat(f"{day}T09:15"), datetime.fromisoformat(f"{day}T{last}"), []
    while t <= stop:
        c = 24040.0 + (t.hour * 60 + t.minute) % 7
        if t.strftime("%H:%M") not in drop:
            out.append({"date": t.strftime("%Y-%m-%d %H:%M"), "open": c - 1, "high": c + 2, "low": c - 2,
                        "close": c, "volume": 0})
        t += timedelta(minutes=minutes)
    return out


PRIOR_DAILY = [{"date": D1, "open": 24097.0, "high": 24100.0, "low": 24000.0, "close": 24002.0}]


def _forming(hhmm, minutes):
    t = datetime.fromisoformat(f"{D2}T{hhmm}")
    return (t - timedelta(minutes=(t.hour * 60 + t.minute - 555) % minutes)).strftime("%H:%M")


def _view(hhmm, drop=None, daily=PRIOR_DAILY):
    """scanner_feed through the real ScannerManager at D2 ``hhmm``:30; returns (view, snapshot kwargs, snapshot)."""
    drop = drop or {}
    frames = {tf: (_bars(D1, PRIOR_DAY) if tf == "5m" else []) + _session(D2, m, _forming(hhmm, m), drop.get(tf, ()))
              for tf, m in (("1m", 1), ("5m", 5), ("15m", 15))}
    frames["1d"] = daily
    scanner, seen, snaps = _manager(frames), {}, []
    real = scanner.produce_scan_snapshot
    scanner.produce_scan_snapshot = lambda **kw: (seen.update(kw), snaps.append(real(**kw)), snaps[-1])[2]
    now = datetime.fromisoformat(f"{D2}T{hhmm}:30+05:30")
    return rr.scanner_feed(scanner, chain_source=lambda: {}, clock=lambda: now)(now), seen, snaps[-1] if snaps else None


@pytest.mark.parametrize("hhmm", ["09:45", "13:25", "14:25", "15:10"])
def test_v01_a_complete_session_passes_at_every_checkpoint_with_or30_anchored_at_0915(hhmm):
    view, seen, snap = _view(hhmm)
    assert view.rejections == {}
    assert {tf: f["date"].iloc[0] for tf, f in seen["frames"].items()} == {tf: f"{D2} 09:15" for tf in ("1m", "5m", "15m")}
    first6 = _session(D2, 5, "09:40")
    assert (snap.levels.or30_high, snap.levels.or30_low) == (max(b["high"] for b in first6), min(b["low"] for b in first6))
    assert (snap.levels.pdh, snap.levels.pdl) == (24100.0, 24000.0)


@pytest.mark.parametrize("tf", ["1m", "5m", "15m"])
def test_v01_a_missing_0915_bar_rejects_only_that_timeframe_never_shifts_the_open(tf):
    view, seen, snap = _view("15:10", drop={tf: ("09:15",)})
    assert view.rejections == {tf: [mv.SESSION_OPEN_BAR_MISSING]}
    assert seen["frames"][tf].empty
    assert all(f["date"].iloc[0] == f"{D2} 09:15" for t, f in seen["frames"].items() if t != tf)
    if tf == "5m":  # late-session OR-30 is absent, not rebuilt from 09:20-09:45
        assert snap.levels.or30_high is None and snap.levels.or30_low is None
    assert not view.df_5m.empty  # exits keep their frame


@pytest.mark.parametrize("tf, gap", [("1m", "11:07"), ("5m", "11:00"), ("15m", "11:00")])
def test_v01_a_mid_session_gap_rejects_only_that_timeframe(tf, gap):
    view, seen, _ = _view("13:25", drop={tf: (gap,)})
    assert view.rejections == {tf: [mv.INTRADAY_BAR_GAP]} and seen["frames"][tf].empty


def test_v01_the_forming_bar_is_never_a_gap_but_a_missing_closed_bar_is():
    cal = _cal()
    at = datetime.fromisoformat(f"{D2}T10:52:30+05:30")
    for last in ("10:52", "10:51"):  # 10:52 forming, or not yet served at all
        frame, why = mv.session_frame(pd.DataFrame(_session(D2, 1, last)), at, 1, cal)
        assert why is None and frame["date"].iloc[-1] == f"{D2} 10:51"
    frame, why = mv.session_frame(pd.DataFrame(_session(D2, 1, "10:52", drop=("10:51",))), at, 1, cal)
    assert (frame.empty, why) == (True, mv.INTRADAY_BAR_GAP)
    # After the close the schedule ends at 15:25 (5m); Yahoo's 15:30 closing print is optional.
    late = datetime.fromisoformat(f"{D2}T15:33:00+05:30")
    assert mv.session_frame(pd.DataFrame(_session(D2, 5, "15:30")), late, 5, cal)[1] is None
    assert mv.session_frame(pd.DataFrame(_session(D2, 5, "15:25")), late, 5, cal)[1] is None
    assert mv.session_frame(pd.DataFrame(_session(D2, 5, "15:30", drop=("15:25",))), late, 5, cal)[1] == mv.INTRADAY_BAR_GAP


def test_v01_cache_hit_and_miss_give_identical_session_frames(monkeypatch):
    days = ["2026-10-30", "2026-11-02", "2026-11-03", "2026-11-04", D1, D2]
    _yahoo(monkeypatch, days)
    cal, feed = _cal(), FeedAgent()
    miss = feed.fetch_timeframe_candles("5m", limit_bars=400)
    hit = feed.fetch_timeframe_candles("5m", limit_bars=400)  # inside the 3 s cache: the untrimmed frame
    assert (len(miss), len(hit)) == (400, 450)
    for hhmm in ("09:45", "13:25", "14:25", "15:10"):
        now = datetime.fromisoformat(f"{D2}T{hhmm}:30+05:30")
        a, b = mv.session_frame(miss, now, 5, cal), mv.session_frame(hit, now, 5, cal)
        assert a[1] is b[1] is None and a[0].equals(b[0]) and a[0]["date"].iloc[0] == f"{D2} 09:15"
    # The old 50-bar request loses 09:15 by the afternoon: rejected, not shifted.
    short = feed.candle_cache["5m"].tail(50)
    assert mv.session_frame(short, datetime.fromisoformat(f"{D2}T15:32:00+05:30"), 5, cal)[1] == mv.SESSION_OPEN_BAR_MISSING


def test_v01_nse_calendar_holidays_special_sessions_and_uncovered_years():
    from datetime import date

    cal = _cal()
    assert not mv.regular_session(date(2026, 11, 10), cal)  # Diwali-Balipratipada
    assert mv.previous_session(date(2026, 11, 11), cal) == date(2026, 11, 9)  # skips the holiday
    assert mv.previous_session(date(2026, 11, 9), cal) == date(2026, 11, 6)  # skips the weekend
    assert mv.previous_session(date(2025, 10, 23), cal) is None  # Muhurat 10-21 behind the 10-22 holiday
    assert mv.previous_session(date(2020, 1, 1), cal) is None  # 2019 is not in the holiday file
    assert not mv.regular_session(date(2027, 1, 4), cal)
    for day in ("2026-11-10", "2025-10-21", "2021-02-24", "2026-11-08"):  # holiday, Muhurat, NSE halt, Sunday
        now = datetime.fromisoformat(f"{day}T11:00:00+05:30")
        frame, why = mv.session_frame(pd.DataFrame(_session(day, 5, "10:55")), now, 5, cal)
        assert (frame.empty, why) == (True, mv.SESSION_CALENDAR_UNVERIFIED)


def test_v01_previous_session_levels_are_verified_by_date():
    cal = _cal()
    now = datetime.fromisoformat("2026-11-11T10:00:00+05:30")
    row = lambda d: {"date": d, "open": 1.0, "high": 2.0, "low": 1.0, "close": 1.5}  # noqa: E731
    assert mv.previous_session_problem(pd.DataFrame([row("2026-11-06"), row("2026-11-09"), row("2026-11-11")]),
                                       now, cal) is None
    assert "no daily row for 2026-11-09" in mv.previous_session_problem(pd.DataFrame([row("2026-11-06")]), now, cal)
    assert mv.previous_session_problem(pd.DataFrame(), now, cal)
    monday = datetime.fromisoformat("2026-11-09T10:00:00+05:30")  # a weekend special session the calendar lacks
    assert "calendar does not list" in mv.previous_session_problem(
        pd.DataFrame([row("2026-11-06"), row("2026-11-08")]), monday, cal)


def test_v01_missing_previous_session_rejects_every_timeframe_but_keeps_the_exit_frame():
    older = [{"date": "2026-11-04", "open": 24097.0, "high": 24300.0, "low": 24000.0, "close": 24002.0}]
    view, seen, snap = _view("11:02", daily=older)  # D1 (2026-11-05) row absent: never fall back to 11-04
    assert snap is None and view.setups == [] and view.key_levels == []
    assert view.rejections == {tf: [mv.PREVIOUS_SESSION_LEVELS_MISSING] for tf in ("1m", "5m", "15m")}
    assert not view.df_5m.empty


def test_v01_runtime_still_monitors_exits_and_reports_insufficient_monitoring_data(tmp_path):
    from dataclasses import replace

    from tests.test_research_runtime import Clock, Feed, rows, runtime, setup, walk

    class Rejecting(Feed):
        rejections = {}

        def __call__(self, now):
            return replace(super().__call__(now), rejections=self.rejections)

    clock = Clock("10:19")
    feed = Rejecting([setup()], closes=[24000.0])
    rt = runtime(tmp_path / "j.db", feed, clock)
    rt.start()
    try:
        first = rt.tick()
        assert "monitoring_data" not in first and first["admissions"][0]["class"] == "RESEARCH_ONLY"
        feed.setups, feed.closes = [], [24000.0, 23960.0]
        feed.rejections = {tf: [mv.PREVIOUS_SESSION_LEVELS_MISSING] for tf in ("1m", "5m", "15m")}
        walk(rt, clock, "10:20")
        out = rt.tick()
    finally:
        rt.stop()
    assert out["exits"][0]["action"] == "STOP_LOSS_HIT"  # existing exit behaviour preserved
    assert out["monitoring_data"] == [mv.PREVIOUS_SESSION_LEVELS_MISSING]
    assert out["rejected_timeframes"] == feed.rejections
    assert [r["state"] for r in rows(tmp_path / "j.db", "SELECT state FROM crew_trades")] == ["CLOSED"]
