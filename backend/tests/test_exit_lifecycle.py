"""Exit lifecycle of the research runtime with paper-risk-v2, one deterministic fixture per exit path.

Bars, option book and delta are synthetic fixtures: they verify precedence, candle stamps, whole lots,
accounting and idempotency. They are not historical evidence and say nothing about profitability.
"""

from __future__ import annotations

import socket
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.config import get_settings
from app.db import crew_journal as cj
from app.db import crew_session as cs
from app.db import paper_ledger as pl
from app.trading import paper_stats as ps
from app.trading import research_runtime as rr
from app.trading.option_quote import parse_chain_leg
from app.trading.paper_risk import DeltaEvidence
from tests.test_research_runtime import DAY, bars, rows, setup

CAPITAL = Decimal("50000")
LOT = 65
SYMBOL = "NIFTY26N1024100CE"
TABLES = ("crew_trades", "crew_events", "crew_paper_trades", "crew_paper_events", "crew_paper_decisions")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda self, addr: (_ for _ in ()).throw(OSError("blocked")))


def _t(hhmmss: str) -> datetime:
    return datetime.fromisoformat(f"{DAY}T{hhmmss}+05:30")


class Clock:
    def __init__(self, hhmmss: str):
        self.t = _t(hhmmss)

    def __call__(self) -> datetime:
        return self.t


class Market:
    """Closed 5m bars ending at the last bar closed by the clock; a 24100 CE book that moves 0.4 per index point."""

    def __init__(self, signal):
        self.signal, self.closes, self.priced = signal, [24000.0], True
        self.strike, self.expiry, self.skew = 24100, "2026-11-10", timedelta(0)
        self.symbol, self.underlying = SYMBOL, "NIFTY"

    def __call__(self, now: datetime) -> rr.MarketView:
        last = now.replace(minute=now.minute - now.minute % 5, second=0, microsecond=0) - timedelta(minutes=5)
        start = (last - timedelta(minutes=5 * (len(self.closes) - 1))).strftime("%H:%M")
        bid = round(50 + 0.4 * (self.closes[-1] - 24000.0), 2)
        leg = {"liveData": {"ltp": bid + 0.2, "bid": bid, "ask": bid + 0.4, "quoteTime": (now + self.skew).isoformat()},
               "marketLot": LOT, "growwContractId": "GC-24100CE", **({"tradingSymbol": self.symbol} if self.symbol else {})}
        row = {"strike": self.strike, "ce": leg, **({"underlying": self.underlying} if self.underlying else {})}
        chain = {"contracts": [row], "expiry": self.expiry, "fetched_at": now}
        quote = parse_chain_leg(chain["contracts"][0], "CE", chain["expiry"], now)
        return rr.MarketView(df_5m=bars(self.closes, start), setups=[self.signal],
                             chain=chain if self.priced else None, key_levels=[],
                             delta_evidence={SYMBOL: DeltaEvidence(Decimal("0.4"), now, SYMBOL, "synthetic")})


def _runtime(path, market, clock, **extra):
    settings = get_settings().model_copy(update={
        "paper_capital_inr": CAPITAL, "live_trading_enabled": False, "sandbox_allow_live": False,
        "telegram_delivery_enabled": False, **extra})
    rt = rr.ResearchRuntime(path, market, settings=settings, clock=clock,
                            trusted_delta_sources=frozenset({"synthetic"}))
    rt.started = rt.start()
    return rt


def _walk(rt, clock, hhmmss):
    target = _t(hhmmss)
    while clock.t < target:
        clock.t = min(target, clock.t + timedelta(seconds=10))
        cs.heartbeat(rt.path, rt.lease, now=clock.t)


def _state(path):
    return {t: rows(path, f"SELECT * FROM {t}") for t in TABLES}


def run(path, *, ts="10:15", admit="10:19:05", steps, priced_exit=True):
    """Admit one CALL (entry 24000, stop 23970, target 24060, risk 30), then one tick per (time, close)."""
    market, clock = Market(setup(ts=f"{DAY} {ts}")), Clock(admit)
    rt = _runtime(path, market, clock)
    actions = []
    try:
        tid = rt.tick()["admissions"][0]["trade"]
        for i, (hhmmss, close) in enumerate(steps):
            market.closes.append(close)
            market.priced = priced_exit or i < len(steps) - 1
            _walk(rt, clock, hhmmss)
            actions.append(tuple(rt.tick()["exits"][0].values())[1:])
        # Idempotency: the same bar again, then a restart, change nothing once the trade is closed.
        settled = _state(path)
        _walk(rt, clock, (clock.t + timedelta(seconds=10)).strftime("%H:%M:%S"))
        assert rt.tick()["exits"] == []
    finally:
        rt.stop()
    assert _state(path) == settled
    rt = _runtime(path, market, clock)
    try:
        assert rt.tick()["exits"] == []
    finally:
        rt.stop()
    assert _state(path) == settled
    return tid, actions


def _exit_event(path, tid):
    return [e for e in cj.get_trade_events(path, tid) if e["event_type"] == "MODEL_EXIT"][0]["payload"]


def _check_closed(path, tid, reason, *, priced=True):
    trade = rows(path, "SELECT * FROM crew_trades WHERE trade_id=?", (tid,))[0]
    assert (trade["state"], trade["model_lots_open"]) == ("CLOSED", 0)
    assert cj.check_consistency(path) == [] and pl.check_consistency(path) == []
    papers = rows(path, "SELECT * FROM crew_paper_trades")
    assert len(papers) == 2 and all(p["lots_initial"] == 1 for p in papers)  # every family is capped at 1 lot
    events = rows(path, "SELECT * FROM crew_paper_events WHERE side IS NOT NULL")
    assert all(e["qty"] % LOT == 0 and e["qty"] > 0 for e in events)
    final = rows(path, "SELECT event_type, reason FROM crew_paper_events WHERE event_type IN ('EXIT','EXIT_UNPRICED')")
    if not priced:
        assert all(p["state"] == "EXIT_UNPRICED" and p["lots_open"] == 1 for p in papers)
        assert [f["event_type"] for f in final] == ["EXIT_UNPRICED"] * 2
        assert all(f["reason"].startswith(f"{reason}:") for f in final)
        return
    assert all(p["state"] == "CLOSED" and p["lots_open"] == 0 for p in papers)
    assert final == [{"event_type": "EXIT", "reason": reason}] * 2
    conn = cj.connect_readonly(path)
    try:
        for account in {p["account_id"] for p in papers}:
            acc = ps.account_stats(conn, account)["accounting"]
            assert acc["charge_reconciliation"] == "EXACT"
            assert Decimal(acc["reserved_worst_case_inr"]) == 0 and Decimal(acc["committed_capital_inr"]) == 0
            assert Decimal(acc["cash_inr"]) == CAPITAL + Decimal(acc["realized_pnl_inr"])
    finally:
        conn.close()


@pytest.fixture
def path(tmp_path):
    return tmp_path / "crew_research.db"


def test_stop_loss_through_a_gap_books_the_model_at_the_stop_and_paper_at_the_book(path):
    tid, actions = run(path, steps=[("10:24:05", 23950.0)])
    assert actions == [("STOP_LOSS_HIT", -30.0)]
    payload = _exit_event(path, tid)
    # The model fills at the stop (-1R) even when the closed bar gapped 20 points through it; paper does not.
    assert (payload["model_r"], payload["index_levels"]["spot"]) == (-1.0, 23950.0)
    assert payload["signal_candle"] == f"{DAY}T10:20:00+05:30"  # close of the last closed bar, never the forming one
    _check_closed(path, tid, "STOP_LOSS_HIT")
    sells = rows(path, "SELECT price_inr FROM crew_paper_events WHERE event_type='EXIT'")
    assert {Decimal(s["price_inr"]) for s in sells} == {Decimal("29.95")}  # bid at 23950 less one tick


def test_target_after_a_hop_closes_the_runner_and_paper_holds_its_single_lot_through_the_partial(path):
    tid, actions = run(path, steps=[("10:24:05", 24065.0), ("10:29:05", 24110.0)])
    assert actions == [(rr.HOP, 2), ("TARGET_HIT", 105.0)]
    events = cj.get_trade_events(path, tid)
    assert [e["event_type"] for e in events] == ["MODEL_ENTRY", "MODEL_LEVELS_UPDATED", "MODEL_PARTIAL_EXIT",
                                                 "MODEL_EXIT"]
    assert (events[2]["payload"]["lots_before"], events[2]["payload"]["lots_after"]) == (3, 2)
    # model_points/model_r describe the runner only; the lot booked at the hop (+60) is not in them.
    assert (events[3]["payload"]["model_points"], events[3]["payload"]["model_r"]) == (105.0, 3.5)
    assert events[3]["payload"]["signal_candle"] == f"{DAY}T10:25:00+05:30"
    skipped = rows(path, "SELECT reason, lots_after FROM crew_paper_events WHERE event_type='PARTIAL_SKIPPED'")
    assert skipped == [{"reason": f"{rr.HOP}:SINGLE_LOT", "lots_after": 1}] * 2
    _check_closed(path, tid, "TARGET_HIT")


def test_breakeven_milestone_then_a_return_to_entry_exits_flat(path):
    tid, actions = run(path, steps=[("10:24:05", 24025.0), ("10:29:05", 24000.0)])
    assert actions == [(rr.MILESTONE_BE, 24000.0), ("TRAILING_STOP_HIT", 0.0)]
    assert _exit_event(path, tid)["model_r"] == 0.0
    _check_closed(path, tid, "TRAILING_STOP_HIT")


def test_stagnation_fires_on_the_sixth_bar_after_the_signal_candle(path):
    tid, actions = run(path, steps=[("10:24:05", 24010.0), ("10:44:05", 24010.0), ("10:45:05", 24010.0)])
    assert actions == [("HOLD",), ("HOLD",), ("STAGNATION_TIME_STOP", 10.0)]
    assert _exit_event(path, tid)["signal_candle"] == f"{DAY}T10:45:00+05:30"
    _check_closed(path, tid, "STAGNATION_TIME_STOP")


def test_breakeven_with_at_least_0_2r_extends_stagnation_to_ten_bars(path):
    tid, actions = run(path, steps=[("10:24:05", 24025.0), ("10:45:05", 24010.0), ("11:04:05", 24010.0),
                                    ("11:05:05", 24010.0)])
    assert actions == [(rr.MILESTONE_BE, 24000.0), ("HOLD",), ("HOLD",), ("STAGNATION_TIME_STOP", 10.0)]
    _check_closed(path, tid, "STAGNATION_TIME_STOP")


def test_breakeven_below_0_2r_keeps_the_six_bar_stagnation(path):
    tid, actions = run(path, steps=[("10:24:05", 24025.0), ("10:45:05", 24005.0)])
    assert actions == [(rr.MILESTONE_BE, 24000.0), ("STAGNATION_TIME_STOP", 5.0)]
    _check_closed(path, tid, "STAGNATION_TIME_STOP")


def test_end_of_day_closes_at_the_1510_cutoff(path):
    tid, actions = run(path, ts="14:55", admit="14:59:05", steps=[("15:09:05", 24010.0), ("15:10:05", 24010.0)])
    assert actions == [("HOLD",), ("INTRADAY_CLOSE", 10.0)]
    assert _exit_event(path, tid)["signal_candle"] == f"{DAY}T15:10:00+05:30"
    _check_closed(path, tid, "INTRADAY_CLOSE")


@pytest.mark.parametrize("ts,admit,steps,expected", [
    # stagnation is checked before the cutoff: both are due at 15:10
    ("14:40", "14:44:05", [("15:09:05", 24010.0), ("15:10:05", 24010.0)], [("HOLD",), ("STAGNATION_TIME_STOP", 10.0)]),
    # the stop is checked before stagnation and before the cutoff
    ("10:15", "10:19:05", [("10:44:05", 24010.0), ("10:45:05", 23960.0)], [("HOLD",), ("STOP_LOSS_HIT", -30.0)]),
    ("14:55", "14:59:05", [("15:10:05", 23960.0)], [("STOP_LOSS_HIT", -30.0)]),
])
def test_exit_precedence(path, ts, admit, steps, expected):
    tid, actions = run(path, ts=ts, admit=admit, steps=steps)
    assert actions == expected
    _check_closed(path, tid, expected[-1][0])


def test_an_unpriced_exit_closes_the_model_but_never_books_a_paper_exit(path):
    tid, actions = run(path, steps=[("10:24:05", 23950.0)], priced_exit=False)
    assert actions == [("STOP_LOSS_HIT", -30.0)]
    _check_closed(path, tid, "STOP_LOSS_HIT", priced=False)
    assert rows(path, "SELECT * FROM crew_paper_events WHERE side='SELL'") == []


def _requote(path, priced_after: list[bool], window):
    """Stop out unpriced at 10:24:05 (exit bar 10:20), then one tick per minute with the book priced or not."""
    market, clock = Market(setup(ts=f"{DAY} 10:15")), Clock("10:19:05")
    rt = _runtime(path, market, clock, paper_exit_requote_minutes=window)
    try:
        tid = rt.tick()["admissions"][0]["trade"]
        market.closes.append(23950.0)
        market.priced = False
        _walk(rt, clock, "10:24:05")
        assert tuple(rt.tick()["exits"][0].values())[1:] == ("STOP_LOSS_HIT", -30.0)
        assert {p["state"] for p in rows(path, "SELECT * FROM crew_paper_trades")} == {"OPEN"}  # lots still held
        for minute, priced in enumerate(priced_after, start=25):
            market.priced = priced
            _walk(rt, clock, f"10:{minute}:10")
            rt.tick()
        assert rt.tick().get("paper_exit_retries") is None  # resolved trades are never retried again
    finally:
        rt.stop()
    return tid


def test_requote_window_sells_at_the_first_real_book_after_an_unpriced_exit(path):
    tid = _requote(path, [False, True], window=10)
    _check_closed(path, tid, "STOP_LOSS_HIT:REQUOTED")
    model_closed = rows(path, "SELECT closed_at FROM crew_trades WHERE trade_id=?", (tid,))[0]["closed_at"]
    sells = rows(path, "SELECT price_inr, occurred_at FROM crew_paper_events WHERE event_type='EXIT'")
    assert {Decimal(s["price_inr"]) for s in sells} == {Decimal("29.95")}  # the 10:26 book, never a made-up price
    assert all(s["occurred_at"] > model_closed for s in sells)


def test_requote_window_expires_into_exit_unpriced_without_a_sell(path):
    tid = _requote(path, [False, False, False], window=6)
    _check_closed(path, tid, "STOP_LOSS_HIT", priced=False)
    reasons = {r["reason"] for r in rows(path, "SELECT reason FROM crew_paper_events WHERE event_type='EXIT_UNPRICED'")}
    assert len(reasons) == 1 and reasons.pop().endswith(":REQUOTE_EXPIRED")
    assert rows(path, "SELECT * FROM crew_paper_events WHERE side='SELL'") == []


@pytest.mark.parametrize("minutes,ok", [(0, True), (1, True), (30, True), (-1, False), (31, False)])
def test_requote_window_is_bounded_to_30_minutes(minutes, ok):
    from pydantic import ValidationError

    from app.config import Settings
    if ok:
        assert Settings(_env_file=None, PAPER_EXIT_REQUOTE_MINUTES=minutes).paper_exit_requote_minutes == minutes
    else:
        with pytest.raises(ValidationError):
            Settings(_env_file=None, PAPER_EXIT_REQUOTE_MINUTES=minutes)


def _pending(path, window, *, ts="10:15", admit="10:19:05", steps=(("10:24:05", 23950.0),)):
    """Admit, then stop out with no book on the last step: the exit stays pending (or expires at once)."""
    market, clock = Market(setup(ts=f"{DAY} {ts}")), Clock(admit)
    rt = _runtime(path, market, clock, paper_exit_requote_minutes=window)
    tid = rt.tick()["admissions"][0]["trade"]
    for i, (hhmmss, close) in enumerate(steps):
        market.closes.append(close)
        market.priced = i < len(steps) - 1
        _walk(rt, clock, hhmmss)
        rt.tick()
    return rt, market, clock, tid


def _paper_reasons(path):
    return [r["reason"] for r in rows(path, "SELECT reason FROM crew_paper_events WHERE event_type IN"
                                            " ('EXIT','EXIT_UNPRICED') ORDER BY paper_trade_id")]


@pytest.mark.parametrize("window", [1, 4])
def test_a_window_shorter_than_the_exit_bar_lag_expires_on_the_exit_tick(path, window):
    # The exit bar closed at 10:20:00 and the exit was decided at 10:24:05: 4m05s of the window are already gone.
    rt, *_, tid = _pending(path, window)
    rt.stop()
    _check_closed(path, tid, "STOP_LOSS_HIT", priced=False)
    assert _paper_reasons(path) == ["STOP_LOSS_HIT:QUOTE_MISSING:REQUOTE_EXPIRED"] * 2


@pytest.mark.parametrize("priced", [True, False])
def test_requote_at_the_exact_window_boundary_prices_first_then_expires(path, priced):
    rt, market, clock, tid = _pending(path, 5)  # expires at 10:20:00 + 5m = 10:25:00
    try:
        _walk(rt, clock, "10:24:50")
        rt.tick()
        held = _state(path)
        assert rt.tick()["paper_exit_retries"][0]["event_type"] == "EXIT_PENDING" and _state(path) == held
        conn = cj.connect_readonly(path)
        try:  # a pending exit keeps its worst-case loss reserved against the account
            assert all(Decimal(ps.account_stats(conn, a["account_id"])["accounting"]["reserved_worst_case_inr"]) > 0
                       for a in rows(path, "SELECT account_id FROM crew_paper_trades"))
        finally:
            conn.close()
        market.priced = priced
        _walk(rt, clock, "10:25:00")
        rt.tick()
        assert rt.tick().get("paper_exit_retries") is None
    finally:
        rt.stop()
    if priced:
        _check_closed(path, tid, "STOP_LOSS_HIT:REQUOTED")
    else:
        _check_closed(path, tid, "STOP_LOSS_HIT", priced=False)
        assert _paper_reasons(path) == ["STOP_LOSS_HIT:QUOTE_MISSING:REQUOTE_EXPIRED"] * 2


def test_a_restart_inside_the_window_fails_closed_and_never_prices_late(path):
    rt, market, clock, _ = _pending(path, 10)
    rt.stop()
    market.priced, clock.t = True, _t("10:25:10")
    rt = _runtime(path, market, clock, paper_exit_requote_minutes=10)
    try:
        assert len(rt.started["paper"]["exit_missed"]) == 2 and rt.started["paper"]["expired"] == []
        assert rt.tick().get("paper_exit_retries") is None
    finally:
        rt.stop()
    assert cj.check_consistency(path) == [] and pl.check_consistency(path) == []
    assert {p["state"] for p in rows(path, "SELECT state FROM crew_paper_trades")} == {"EXIT_UNPRICED"}
    assert _paper_reasons(path) == [pl.EXIT_MISSED_AFTER_RESTART] * 2
    assert rows(path, "SELECT * FROM crew_paper_events WHERE side='SELL'") == [] and _pending_ids(path) == []
    _assert_reserved(path)


@pytest.mark.parametrize("book", [{"skew": timedelta(seconds=1)}, {"strike": 24200}, {"expiry": "2026-11-17"}])
def test_requote_never_sells_a_future_stamped_or_different_contract_book(path, book):
    rt, market, clock, tid = _pending(path, 6)
    try:
        market.priced = True
        for key, value in book.items():
            setattr(market, key, value)
        for hhmmss in ("10:25:10", "10:26:10"):
            _walk(rt, clock, hhmmss)
            rt.tick()
    finally:
        rt.stop()
    _check_closed(path, tid, "STOP_LOSS_HIT", priced=False)
    assert rows(path, "SELECT * FROM crew_paper_events WHERE side='SELL'") == []


def test_requote_never_sells_after_the_market_closes(path):
    rt, market, clock, tid = _pending(path, 30, ts="14:55", admit="14:59:05",
                                      steps=(("15:09:05", 24010.0), ("15:10:05", 24010.0)))
    try:
        market.priced = True
        _walk(rt, clock, "15:30:10")
        assert rt.tick()["paper_exit_retries"][0]["event_type"] == "EXIT_PENDING"
        _walk(rt, clock, "15:40:00")
        rt.tick()
    finally:
        rt.stop()
    _check_closed(path, tid, "INTRADAY_CLOSE", priced=False)
    assert _paper_reasons(path) == ["INTRADAY_CLOSE:MARKET_CLOSED:REQUOTE_EXPIRED"] * 2


# --- exit contract identity (inherited close path, PAPER_EXIT_REQUOTE_MINUTES=0) -------------------------------

@pytest.mark.parametrize("book,why", [
    ({"underlying": "BANKNIFTY"}, "CONTRACT_IDENTITY_MISMATCH"),
    ({"symbol": "NIFTY26N1724100CE"}, "CONTRACT_IDENTITY_MISMATCH"),
    ({"symbol": None}, "CONTRACT_IDENTITY_MISSING"),
    ({}, None),
])
def test_an_exit_sells_only_the_held_contract(path, book, why):
    assert get_settings().paper_exit_requote_minutes == 0
    market, clock = Market(setup(ts=f"{DAY} 10:15")), Clock("10:19:05")
    rt = _runtime(path, market, clock)
    try:
        tid = rt.tick()["admissions"][0]["trade"]
        market.closes.append(23950.0)
        for key, value in book.items():
            setattr(market, key, value)
        _walk(rt, clock, "10:24:05")
        assert tuple(rt.tick()["exits"][0].values())[1:] == ("STOP_LOSS_HIT", -30.0)
    finally:
        rt.stop()
    _check_closed(path, tid, "STOP_LOSS_HIT", priced=why is None)
    if why:
        assert _paper_reasons(path) == [f"STOP_LOSS_HIT:{why}"] * 2
        assert rows(path, "SELECT * FROM crew_paper_events WHERE side='SELL'") == []
        _assert_reserved(path)


# --- pending-exit lifecycle reporting ----------------------------------------------------------------------

def _assert_reserved(path):
    """Unresolved lots keep their full worst case reserved and book no realized P&L."""
    conn = cj.connect_readonly(path)
    try:
        for account in {r["account_id"] for r in rows(path, "SELECT account_id FROM crew_paper_trades")}:
            acc = ps.account_stats(conn, account)["accounting"]
            assert Decimal(acc["reserved_worst_case_inr"]) > 0 and Decimal(acc["realized_pnl_inr"]) == 0
    finally:
        conn.close()


def _pending_ids(path):
    conn = cj.connect_readonly(path)
    try:
        return sorted(i for r in ps.report(conn)["accounts"].values() for i in r["pending_exits"])
    finally:
        conn.close()


def test_a_pending_exit_is_reported_as_pending_until_it_resolves(path):
    rt, market, clock, _ = _pending(path, 10)
    papers = sorted(r["paper_trade_id"] for r in rows(path, "SELECT paper_trade_id FROM crew_paper_trades"))
    try:
        assert _pending_ids(path) == papers
        _assert_reserved(path)
        market.priced = True
        _walk(rt, clock, "10:25:10")
        rt.tick()
    finally:
        rt.stop()
    assert _pending_ids(path) == []


def test_a_stop_before_the_retry_deadline_is_reported_and_leaves_the_lots_held(path):
    rt, *_ = _pending(path, 10)
    held = _state(path)
    report = rt.stop()
    assert _state(path) == held  # nothing priced, closed or released at stop
    assert {(p["status"], p["retry_deadline"], p["state"], p["lots_open"]) for p in report["paper_pending_exits"]} == {
        ("STOPPED_BEFORE_RETRY_DEADLINE", f"{DAY}T10:30:00+05:30", "OPEN", 1)}
    assert len(report["paper_pending_exits"]) == 2
    _assert_reserved(path)


def test_a_stop_after_the_retry_deadline_without_a_tick_is_unresolved_not_expired(path):
    rt, market, clock, _ = _pending(path, 6)
    market.priced = True
    _walk(rt, clock, "10:27:10")  # past 10:26:00 but no tick ran, so EXIT_UNPRICED was never recorded
    report = rt.stop()
    assert {p["status"] for p in report["paper_pending_exits"]} == {"UNRESOLVED_AT_STOP"}
    assert {p["state"] for p in rows(path, "SELECT state FROM crew_paper_trades")} == {"OPEN"}
    _assert_reserved(path)


def test_a_next_day_restart_expires_a_pending_exit_as_unresolved_after_the_model_exit(path):
    rt, market, clock, _ = _pending(path, 10)
    rt.stop()
    clock.t = datetime.fromisoformat("2026-11-09T09:20:00+05:30")
    rt = _runtime(path, market, clock, paper_exit_requote_minutes=10)
    rt.stop()
    papers = rows(path, "SELECT * FROM crew_paper_trades")
    report = rt.started["paper"]
    assert sorted(report["expired"]) == sorted(report["expired_exit_unresolved"]) == sorted(
        p["paper_trade_id"] for p in papers)
    assert {(p["state"], p["lots_open"]) for p in papers} == {("EXPIRED_UNRESOLVED", 1)}
    assert {r["reason"] for r in rows(path, "SELECT reason FROM crew_paper_events WHERE event_type="
                                            "'EXPIRED_UNRESOLVED'")} == {"PRIOR_MARKET_DAY:MODEL_EXITED"}
    assert rows(path, "SELECT * FROM crew_paper_events WHERE side='SELL'") == []
    assert cj.check_consistency(path) == [] and pl.check_consistency(path) == []
    _assert_reserved(path)
