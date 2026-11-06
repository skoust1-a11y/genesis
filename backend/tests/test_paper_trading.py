"""Paper trading V1: registry, simulated fills, ledger, runtime hooks, stats and mock-only notifications.

Synthetic chains, a fake clock and temp journals only. No network, no Telegram, no broker.
"""

from __future__ import annotations

import ast
import json
import socket
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.config import get_settings
from app.db import crew_journal as cj
from app.db import crew_outbox as ob
from app.db import crew_session as cs
from app.db import paper_ledger as pl
from app.paper import order_charges
from app.telegram_transport import DELIVERED, MOCK_CHAT_PREFIX, NOT_DELIVERED, DeliveryOutcome, TelegramTransport
from app.trading import paper_exec as px
from app.trading import paper_notify as pn
from app.trading import paper_registry as reg
from app.trading import paper_stats as ps
from app.trading import research_runtime as rr
from app.trading.option_quote import parse_chain_leg
from tests.test_research_runtime import DAY, Clock, bars, prior_session, rows, setup, walk

CAPITAL = Decimal("50000")
SETTINGS = get_settings().model_copy(update={"paper_capital_inr": CAPITAL})
IND = "IND:LIQ_SWEEP_PDHL"
PAPER_TABLES = ("crew_paper_accounts", "crew_paper_trades", "crew_paper_events", "crew_paper_decisions")


def book(fetched, *, bid=40.0, ask=40.4, strikes=(24100,), side="ce", quoted=True, symbol=True):
    live = {"ltp": (bid + ask) / 2, "bid": bid, "ask": ask}
    if quoted:
        live["quoteTime"] = fetched.isoformat()
    leg = {"liveData": live, "marketLot": 75}
    rows_ = [{"strike": s, side: {**leg, **({"tradingSymbol": f"NIFTY26N10{s}{side.upper()}"} if symbol else {})}}
             for s in strikes]
    return {"contracts": rows_, "expiry": "2026-11-10", "fetched_at": fetched}


def quote(now, **kw):
    return parse_chain_leg(book(now, **kw)["contracts"][0], "CE", "2026-11-10", now)


class PaperFeed:
    def __init__(self, setups=None, closes=(24000.0,), **book_kw):
        self.setups, self.closes, self.book_kw, self.priced = list(setups or []), list(closes), book_kw, True

    def __call__(self, now):
        chain = book(now, **self.book_kw) if self.priced else book(now, quoted=False, **self.book_kw)
        return rr.MarketView(df_5m=bars(self.closes), setups=self.setups, chain=chain, key_levels=[])


@pytest.fixture
def path(tmp_path):
    return tmp_path / "crew_research.db"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    connects = []

    def blocked(self, addr):
        connects.append(addr)
        raise OSError("network blocked in paper tests")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    yield
    assert connects == []


def runtime(path, feed, clock, settings=SETTINGS):
    return rr.ResearchRuntime(path, feed, settings=settings, clock=clock)


def paper(path):
    return {t: rows(path, f"SELECT * FROM {t}") for t in PAPER_TABLES}


def outbox(path):
    return rows(path, "SELECT * FROM crew_outbox ORDER BY created_at, publication_id")


def kinds(path):
    return [r["purpose"].split("|")[1] for r in outbox(path)]


def stats(path, account, day=None):
    conn = cj.connect_readonly(path)
    try:
        return ps.account_stats(conn, account, market_day=day)
    finally:
        conn.close()


# --- registry and pure fills -----------------------------------------------------------------------------

def test_registry_maps_scanner_names_to_families_and_disables_pdc():
    v = reg.STRATEGY_VERSION
    assert reg.family_of("Liquidity Sweep [5M]", v) == "LIQ_SWEEP_PDHL"
    assert reg.family_of("Breakout & Retest (PD) [5M]", v) == "RETEST_PD"
    assert reg.family_of("Breakout & Retest (OR-30) [5M]", v) == "RETEST_OR30"
    assert reg.family_of("Trendline Reversal (Bullish)", v) == "TRENDLINE_CHOCH"
    assert reg.family_of("Equal Highs Sweep [5M]", v) == "EQHL_SWEEP_INLINE"
    assert reg.family_of("PDC_15EMA_V0", v) is None and reg.family_of("Liquidity Sweep [5M]", "scanner-v1") is None
    assert reg.accounts_for("Liquidity Sweep [5M]", v) == [IND, reg.SHARED]
    assert reg.accounts_for("Unknown", v) == [reg.SHARED]
    assert len(reg.account_ids()) == 6 and reg.max_positions(reg.SHARED) == 2 and reg.max_positions(IND) == 1


def test_fills_cross_the_spread_with_slippage_and_hand_computed_charges():
    now = datetime.fromisoformat(f"{DAY}T10:20:00+05:30")
    q = quote(now)
    buy = px.fill(q, "BUY", 2, now=now, settings=SETTINGS, ticks=1)
    sell = px.fill(q, "SELL", 2, now=now, settings=SETTINGS, ticks=1)
    assert (buy.price, buy.qty, buy.value) == (Decimal("40.45"), 150, Decimal("6067.50"))
    assert (sell.price, sell.value) == (Decimal("39.95"), Decimal("5992.50"))
    assert buy.slippage_inr == sell.slippage_inr == Decimal("37.50")  # 0.25 off the 40.20 mid x 150
    assert buy.charges["brokerage"] == sell.charges["brokerage"] == 20.0
    assert buy.charges["stt"] == 0.0 and sell.charges["stt"] == round(5992.50 * 0.001, 2)  # STT on sell premium only
    assert buy.charges_inr == Decimal(str(order_charges(SETTINGS, "OPT", "BUY", 40.45, 150, DAY)["total"]))
    assert px.fill(quote(now, bid=0.05, ask=0.1), "SELL", 1, now=now, settings=SETTINGS, ticks=1).price == px.TICK


def test_no_executable_book_is_unpriced_and_last_price_is_never_a_fill():
    now = datetime.fromisoformat(f"{DAY}T10:20:00+05:30")
    assert px.executable(None, now, SETTINGS) == (None, px.QUOTE_MISSING)
    assert px.executable(quote(now, quoted=False), now, SETTINGS) == (None, px.NO_BID_ASK)
    assert px.executable(quote(now - timedelta(seconds=11)), now, SETTINGS) == (None, px.QUOTE_STALE)
    assert px.executable(quote(now, bid=40.0, ask=50.0), now, SETTINGS) == (None, px.SPREAD_WIDE)
    assert px.executable(quote(now, symbol=False), now, SETTINGS) == (None, px.QUOTE_REJECTED)
    assert px.executable(quote(now), now, SETTINGS)[1] is None


def test_sizing_honours_the_loss_cap_and_the_cash_on_hand():
    now = datetime.fromisoformat(f"{DAY}T10:20:00+05:30")
    kw = dict(now=now, settings=SETTINGS, ticks=1, max_lots=3, loss_cap=Decimal("10000"))
    assert px.size(quote(now), funds=CAPITAL, **kw)[0].lots == 3
    assert px.size(quote(now, bid=110.0, ask=111.0), funds=CAPITAL, **kw)[0].lots == 1
    assert px.size(quote(now, bid=200.0, ask=201.0), funds=CAPITAL, **kw) == (None, px.RISK_LIMIT)
    assert px.size(quote(now), funds=Decimal("1000"), **kw) == (None, px.INSUFFICIENT_CAPITAL)
    entry, _ = px.size(quote(now), funds=CAPITAL, **kw)
    assert px.max_loss(entry, SETTINGS, DAY) <= Decimal("10000")
    three = px.fill(quote(now), "BUY", 3, now=now, settings=SETTINGS, ticks=1)
    tight = three.value + three.charges_inr  # pays the entry, not the exit charges: 3 lots must not fit
    entry, _ = px.size(quote(now), funds=tight, **kw)
    assert entry.lots == 2 and px.max_loss(entry, SETTINGS, DAY) <= tight


# --- runtime lifecycle -----------------------------------------------------------------------------------

def test_a_priced_signal_opens_both_accounts_half_exits_and_closes_with_rupee_pnl(path):
    clock = Clock("10:19")
    feed = PaperFeed([setup()])
    rt = runtime(path, feed, clock)
    rt.start()
    try:
        first = rt.tick()
        decisions = first["admissions"][0]["paper"]
        assert [(d["account_id"], d["outcome"]) for d in decisions] == [(IND, "OPENED"), (reg.SHARED, "OPENED")]
        rt.tick()  # a repeat scan decides nothing twice
        feed.closes = [24000.0, 24065.0]
        walk(rt, clock, "10:20")
        rt.tick()
        feed.closes = [24000.0, 24065.0, 24020.0]
        walk(rt, clock, "10:25")
        while rt.tick()["exits"][0]["action"] != "TRAILING_STOP_HIT":
            walk(rt, clock, seconds=10)
    finally:
        rt.stop()
    trades = rows(path, "SELECT * FROM crew_paper_trades ORDER BY account_id")
    assert [(t["account_id"], t["state"], t["lots_initial"], t["lots_open"]) for t in trades] == [
        (IND, "CLOSED", 3, 0), (reg.SHARED, "CLOSED", 3, 0)]
    events = rows(path, "SELECT * FROM crew_paper_events WHERE paper_trade_id=? ORDER BY seq", (trades[0]["paper_trade_id"],))
    assert [(e["event_type"], e["side"], e["lots"], e["lots_after"]) for e in events] == [
        ("ENTRY", "BUY", 3, 3), ("PARTIAL_EXIT", "SELL", 1, 2), ("EXIT", "SELL", 2, 0)]
    assert events[0]["price_inr"] == "40.45" and events[2]["reason"] == "TRAILING_STOP_HIT"
    assert len(rows(path, "SELECT * FROM crew_paper_decisions")) == 2
    assert pl.check_consistency(path) == []

    conn = cj.connect_readonly(path)
    try:
        net = pl.trade_cashflow(conn, trades[0]["paper_trade_id"])
        assert pl.cash(conn, IND) == CAPITAL + net
        assert net == -(sum(Decimal(e["charges_inr"]) for e in events)) + Decimal("39.95") * 75 * 3 - Decimal("40.45") * 225
    finally:
        conn.close()
    s = stats(path, IND)
    assert (s["completed"], s["accepted"], s["net_pnl_inr"], s["sample_warning"]) == (1, 1, str(net), "INSUFFICIENT_SAMPLE")
    assert s["by_family"] is None and stats(path, reg.SHARED)["by_family"]["LIQ_SWEEP_PDHL"]["trades"] == 1


def test_notifications_are_mock_only_ops_rows_and_never_duplicated(path):
    clock = Clock("10:19")
    feed = PaperFeed([setup()])
    rt = runtime(path, feed, clock)
    rt.start()
    try:
        rt.tick()
        rt.tick()
        feed.closes = [24000.0, 23960.0]
        walk(rt, clock, "10:20")
        assert rt.tick()["exits"][0]["action"] == "STOP_LOSS_HIT"
        rt.tick()
    finally:
        rt.stop()
    out = outbox(path)
    assert {(r["kind"], r["chat_id"], r["trade_id"], r["state"]) for r in out} == {("OPS", pn.CHAT_ID, None, "PENDING")}
    assert len({r["purpose"] for r in out}) == len(out)
    assert sorted(kinds(path)) == sorted(["SIGNAL_DETECTED", "PAPER_ENTRY_FILLED", "PAPER_ENTRY_FILLED",
                                          "PAPER_STOP_LOSS", "PAPER_STOP_LOSS"])
    assert all(r["body"].endswith(pn.FOOTER) for r in out)
    entry = next(r["body"] for r in out if "PAPER_ENTRY_FILLED" in r["purpose"])
    assert "Entry Premium: ₹40.45" in entry and "Quantity: 3 lots = 225" in entry and "Index Stop: 23970" in entry

    conn = cj.connect_readonly(path)
    try:
        late = pn.due(conn, DAY, datetime.fromisoformat(f"{DAY}T15:31:00+05:30"))
        early = pn.due(conn, DAY, datetime.fromisoformat(f"{DAY}T15:29:00+05:30"))
    finally:
        conn.close()
    summaries = [p for p, _ in late if "DAILY_STRATEGY_SUMMARY" in p]
    assert len(summaries) == 6 and not [p for p, _ in early if "DAILY_STRATEGY_SUMMARY" in p]


def test_delivery_outcomes_never_change_the_paper_ledger(path):
    clock = Clock("10:19")
    rt = runtime(path, PaperFeed([setup()]), clock)
    rt.start()
    try:
        rt.tick()
        before = paper(path)
        outcomes = iter([DeliveryOutcome(DELIVERED, http_status=200, telegram_ok=True, message_id=7),
                         DeliveryOutcome(NOT_DELIVERED, http_status=400, telegram_ok=False)])

        class MockTransport:
            def __init__(self):
                self.sent = []

            def send(self, chat_id, text):
                self.sent.append(chat_id)
                try:
                    return next(outcomes)
                except StopIteration:
                    raise TimeoutError("mock timeout") from None  # an unknown outcome

        mock = MockTransport()
        for r in outbox(path):
            ob.deliver_publication(path, r["publication_id"], transport=mock, lease=rt.lease, clock=clock)
        assert set(mock.sent) == {pn.CHAT_ID} and len(mock.sent) == 3
        assert paper(path) == before
        rt.tick()  # a re-sync after failures neither resends nor re-enqueues
    finally:
        rt.stop()
    states = sorted(r["state"] for r in outbox(path))
    assert states[0] == "ACKED" and states[-1] == "UNKNOWN" and states[1] in ("FAILED_PERMANENT", "RETRY_WAIT")
    assert paper(path) == before


def test_an_unpriced_book_records_unpriced_signals_and_no_trade(path):
    clock = Clock("10:19")
    feed = PaperFeed([setup()])
    feed.priced = False
    rt = runtime(path, feed, clock)
    rt.start()
    try:
        decisions = rt.tick()["admissions"][0]["paper"]
    finally:
        rt.stop()
    assert {(d["outcome"], tuple(d["reasons"])) for d in decisions} == {("UNPRICED", (px.NO_BID_ASK,))}
    assert rows(path, "SELECT * FROM crew_paper_trades") == []
    s = stats(path, IND)
    assert (s["unpriced_signals"], s["completed"], s["net_pnl_inr"], s["cash_inr"]) == (1, 0, "0", str(CAPITAL))


def test_an_unpriced_exit_is_excluded_from_pnl_and_kept_out_of_cash(path):
    clock = Clock("10:19")
    feed = PaperFeed([setup()])
    rt = runtime(path, feed, clock)
    rt.start()
    try:
        rt.tick()
        feed.priced = False
        feed.closes = [24000.0, 23960.0]
        walk(rt, clock, "10:20")
        rt.tick()
    finally:
        rt.stop()
    trade = rows(path, "SELECT * FROM crew_paper_trades WHERE account_id=?", (IND,))[0]
    assert trade["state"] == "EXIT_UNPRICED" and trade["lots_open"] == 3
    last = rows(path, "SELECT * FROM crew_paper_events WHERE paper_trade_id=? ORDER BY seq DESC LIMIT 1",
                (trade["paper_trade_id"],))[0]
    assert last["reason"] == f"STOP_LOSS_HIT:{px.NO_BID_ASK}" and last["price_inr"] is None
    s = stats(path, IND)
    assert (s["completed"], s["net_pnl_inr"], s["unpriced_exits"]) == (0, "0", [trade["paper_trade_id"]])
    assert Decimal(s["cash_inr"]) == CAPITAL - Decimal(trade["capital_committed_inr"])
    warning = next(r["body"] for r in outbox(path) if "SYSTEM_WARNING" in r["purpose"])
    assert "not priced" in warning
    assert pl.check_consistency(path) == []


def test_shared_account_takes_the_first_family_and_blocks_duplicate_exposure(path):
    clock = Clock("10:19")
    pd = setup(strategy="Breakout & Retest (PD) [5M]")
    rt = runtime(path, PaperFeed([pd, setup()]), clock)  # out of order on purpose
    rt.start()
    try:
        rt.tick()
    finally:
        rt.stop()
    shared = rows(path, "SELECT d.outcome, d.reasons, d.family FROM crew_paper_decisions d"
                        " WHERE account_id='SHARED' ORDER BY decided_at, rowid")
    assert [(d["family"], d["outcome"], json.loads(d["reasons"])) for d in shared] == [
        ("LIQ_SWEEP_PDHL", "OPENED", []), ("RETEST_PD", "REJECTED", [pl.DUPLICATE_EXPOSURE])]
    assert {r["account_id"] for r in rows(path, "SELECT account_id FROM crew_paper_trades")} == {
        IND, "IND:RETEST_PD", reg.SHARED}


def test_no_paper_entry_at_or_after_the_intraday_cutoff(path):
    clock = Clock("15:12")
    rt = runtime(path, PaperFeed([setup(ts=f"{DAY} 15:10")]), clock)
    rt.start()
    try:
        decisions = rt.tick()["admissions"][0]["paper"]
    finally:
        rt.stop()
    assert {(d["outcome"], tuple(d["reasons"])) for d in decisions} == {("REJECTED", (pl.ENTRY_AFTER_CUTOFF,))}
    assert rows(path, "SELECT * FROM crew_paper_trades") == []


def test_unregistered_and_non_5m_setups_are_rejected_with_a_reason(path):
    clock = Clock("10:19")
    rt = runtime(path, PaperFeed([setup(strategy="Mystery [5M]"), setup(timeframe="15m", trigger=24001.0)]), clock)
    rt.start()
    try:
        rt.tick()
    finally:
        rt.stop()
    reasons = {(d["account_id"], d["strategy_id"]): json.loads(d["reasons"])
               for d in rows(path, "SELECT * FROM crew_paper_decisions")}
    assert reasons[(reg.SHARED, "Mystery [5M]")] == [pl.STRATEGY_NOT_REGISTERED]
    assert reasons[(IND, "Liquidity Sweep [5M]")] == [pl.TIMEFRAME_NOT_CLOSED_BAR_SAFE]


# --- session gate and calendar ---------------------------------------------------------------------------

def test_no_admission_on_an_nse_holiday_or_outside_the_session(path):
    for day, hhmm in (("2026-11-10", "10:19"), (DAY, "09:10")):
        clock = Clock(hhmm, day=day)
        rt = runtime(path, PaperFeed([setup(ts=f"{day} 10:15")]), clock)
        rt.start()
        try:
            result = rt.tick()
        finally:
            rt.stop()
        assert result["admissions"] == [] and result["market_closed"]
    assert rows(path, "SELECT * FROM crew_admissions") == []


def test_a_year_missing_from_the_holiday_file_refuses_startup(path):
    rt = runtime(path, PaperFeed(), Clock("10:19", day="2027-01-05"))
    with pytest.raises(rr.StartupRefused, match="CALENDAR_MISSING"):
        rt.start()
    assert rt.lease is None and not path.exists()


def test_paper_is_off_without_paper_capital_and_never_uses_the_default_capital(path):
    clock = Clock("10:19")
    rt = runtime(path, PaperFeed([setup()]), clock, settings=get_settings().model_copy(update={"paper_capital_inr": None}))
    rt.start()
    try:
        assert "paper" not in rt.tick()["admissions"][0]
    finally:
        rt.stop()
    assert paper(path) == {t: [] for t in PAPER_TABLES} and outbox(path) == []


def test_restarting_with_another_capital_is_refused(path):
    clock = Clock("10:19")
    rt = runtime(path, PaperFeed(), clock)
    rt.start()
    rt.stop()
    other = runtime(path, PaperFeed(), clock, settings=SETTINGS.model_copy(update={"paper_capital_inr": Decimal("60000")}))
    with pytest.raises(cj.ConsistencyError):
        other.start()


# --- crash recovery --------------------------------------------------------------------------------------

def crash(rt, clock):
    rt._stop.set()
    rt._beater.join()
    clock.advance(31)


def test_a_crash_between_model_entry_and_paper_entry_cancels_the_paper_entry(path, monkeypatch):
    clock = Clock("10:19")
    feed = PaperFeed([setup()])
    first = runtime(path, feed, clock)
    first.start()
    real = pl.enter
    monkeypatch.setattr(pl, "enter", lambda *a, **k: (_ for _ in ()).throw(OSError("crash")))
    with pytest.raises(OSError):
        first.tick()
    monkeypatch.setattr(pl, "enter", real)
    crash(first, clock)
    second = runtime(path, feed, clock)
    report = second.start()
    try:
        assert [m["account_id"] for m in report["paper"]["entry_missed"]] == [IND, reg.SHARED]
        second.tick()
    finally:
        second.stop()
    decisions = rows(path, "SELECT outcome, reasons FROM crew_paper_decisions")
    assert decisions == [{"outcome": "REJECTED", "reasons": json.dumps([pl.ENTRY_MISSED_AFTER_RESTART])}] * 2
    assert kinds(path).count("PAPER_TRADE_CANCELLED") == 2


def test_a_crash_between_model_exit_and_paper_exit_is_exit_unpriced_on_restart(path, monkeypatch):
    clock = Clock("10:19")
    feed = PaperFeed([setup()])
    first = runtime(path, feed, clock)
    first.start()
    first.tick()
    feed.closes = [24000.0, 23960.0]
    walk(first, clock, "10:20")
    monkeypatch.setattr(pl, "close", lambda *a, **k: (_ for _ in ()).throw(OSError("crash")))
    with pytest.raises(OSError):
        first.tick()
    crash(first, clock)
    second = runtime(path, feed, clock)
    report = second.start()
    second.stop()
    assert len(report["paper"]["exit_missed"]) == 2
    events = rows(path, "SELECT event_type, reason FROM crew_paper_events WHERE seq=2")
    assert events == [{"event_type": "EXIT_UNPRICED", "reason": pl.EXIT_MISSED_AFTER_RESTART}] * 2
    assert pl.check_consistency(path) == []


def test_a_crash_between_model_partial_and_paper_partial_is_recorded_on_restart(path, monkeypatch):
    clock = Clock("10:19")
    feed = PaperFeed([setup()])
    first = runtime(path, feed, clock)
    first.start()
    first.tick()
    feed.closes = [24000.0, 24065.0]
    walk(first, clock, "10:20")
    monkeypatch.setattr(pl, "partial", lambda *a, **k: (_ for _ in ()).throw(OSError("crash")))
    with pytest.raises(OSError):
        first.tick()
    crash(first, clock)
    monkeypatch.undo()
    second = runtime(path, feed, clock)
    assert len(second.start()["paper"]["partial_missed"]) == 2
    second.stop()
    third = runtime(path, feed, clock)
    assert third.start()["paper"]["partial_missed"] == []  # recorded once
    third.stop()
    events = rows(path, "SELECT event_type, reason, lots_after FROM crew_paper_events WHERE seq=2")
    assert events == [{"event_type": "PARTIAL_SKIPPED", "reason": pl.PARTIAL_MISSED_AFTER_RESTART, "lots_after": 3}] * 2
    assert pl.check_consistency(path) == []


def test_a_prior_day_open_paper_trade_expires_unresolved(path):
    clock = Clock("10:19")
    first = runtime(path, PaperFeed([setup()]), clock)
    first.start()
    first.tick()
    crash(first, clock)
    clock.at("09:20", day="2026-11-09")
    second = runtime(path, PaperFeed(), clock)
    report = second.start()
    second.stop()
    assert len(report["paper"]["expired"]) == 2
    assert {t["state"] for t in rows(path, "SELECT state FROM crew_paper_trades")} == {"EXPIRED_UNRESOLVED"}
    assert stats(path, IND)["expired_unresolved"] and stats(path, IND)["completed"] == 0


def test_the_ledger_is_append_only_and_rejects_illegal_writes(path):
    clock = Clock("10:19")
    rt = runtime(path, PaperFeed([setup()]), clock)
    rt.start()
    try:
        rt.tick()
    finally:
        rt.stop()
    conn = cj.connect(path)
    try:
        for sql in ("DELETE FROM crew_paper_events", "DELETE FROM crew_paper_decisions",
                    "UPDATE crew_paper_events SET price_inr='1'", "UPDATE crew_paper_trades SET lots_open=1",
                    "UPDATE crew_paper_accounts SET capital_inr='500000'"):
            with pytest.raises(Exception):
                conn.execute(sql)
    finally:
        conn.close()
    assert pl.check_consistency(path) == []


# --- static guarantees -----------------------------------------------------------------------------------

PAPER_MODULES = (pl, px, pn, reg, ps)


def test_paper_modules_import_no_broker_or_transport_and_never_send():
    forbidden = ("app.brokers", "app.trading.orders", "app.trading.execution_crew", "app.telegram_transport",
                 "app.alerts", "httpx", "requests", "urllib")
    for module in PAPER_MODULES:
        source = Path(module.__file__).read_text()
        tree = ast.parse(source)
        imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | \
                   {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert not {m for m in imported if any(m == f or m.startswith(f + ".") for f in forbidden)}, module
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | \
                {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assert not names & {"deliver_publication", "claim_publication", "place_order", "send", "default_capital_inr"}
        assert "default_capital_inr" not in source.lower()


def test_the_real_transport_never_sends_to_a_mock_chat():
    class NoClient:
        def post(self, *a, **k):
            raise AssertionError("a mock chat reached the network client")

    assert pn.CHAT_ID.startswith(MOCK_CHAT_PREFIX)
    outcome = TelegramTransport("123456:" + "A" * 30, client=NoClient()).send(pn.CHAT_ID, "x")
    assert (outcome.status, outcome.exc_class) == (NOT_DELIVERED, "PreSendValidation")


def test_stats_api_route_reads_the_paper_ledger(path, monkeypatch, client):
    from app.auth import hash_token
    from app.db import crew_reader

    clock = Clock("10:19")
    rt = runtime(path, PaperFeed([setup()]), clock)
    rt.start()
    try:
        rt.tick()
    finally:
        rt.stop()
    monkeypatch.setenv("CREW_JOURNAL_PATH", str(path))
    monkeypatch.setenv("API_TOKENS", f"reader:{hash_token('t')}")
    monkeypatch.setattr(crew_reader, "now_ist", clock)
    get_settings.cache_clear()
    try:
        body = client.get("/api/live/crew/paper/stats", params={"market_day": DAY},
                          headers={"Authorization": "Bearer t"}).json()
    finally:
        get_settings.cache_clear()
    assert set(body["accounts"]) == set(reg.account_ids())
    assert body["accounts"][IND]["accepted"] == 1 and body["registry_hash"] == reg.REGISTRY_HASH
