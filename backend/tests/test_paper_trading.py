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
from app.trading import paper_accounting as pa
from app.trading import paper_exec as px
from app.trading import paper_fees as pf
from app.trading import paper_notify as pn
from app.trading import paper_registry as reg
from app.trading import paper_risk as pr
from app.trading import paper_stats as ps
from app.trading import regime as rg
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
    leg = {"liveData": live, "marketLot": 65}
    rows_ = [{"underlying": "NIFTY", "strike": s, side: {**leg, "growwContractId": f"GC-{s}{side.upper()}",
                                  **({"tradingSymbol": f"NIFTY26N10{s}{side.upper()}"} if symbol else {})}}
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
def _explicit_policy_under_test(monkeypatch, request):
    # Preserve the V1 historical regressions explicitly; V2 tests exercise the shipped default.
    if request.node.name.startswith("test_v2_"):
        return
    import hashlib
    legacy = {"risk_policy_id": "paper-risk-v1", "max_loss_per_trade_inr": "10000",
              "daily_loss_limit_inr": "5000", "max_lots": 3,
              "max_positions": {reg.SHARED: 2, "INDEPENDENT": 1}, "slippage_ticks": 1}
    monkeypatch.setattr(reg, "RISK_POLICY", legacy)
    digest = hashlib.sha256(json.dumps(
        {"version": reg.STRATEGY_VERSION, "families": reg.FAMILIES, "timeframes": reg.PAPER_TIMEFRAMES,
         "disabled": reg.DISABLED, "risk": legacy}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    monkeypatch.setattr(reg, "REGISTRY_HASH", digest)


@pytest.fixture(autouse=True)
def _multi_lot_strategy_ceiling(monkeypatch):
    # These regressions exercise multi-lot partials and charges; the shipped one-lot family spec is in test_paper_risk.
    monkeypatch.setattr(reg, "strategy_max_lots", lambda family: 3)


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


def test_account_registry_identity_is_immutable_and_mismatch_refuses_restart(path, monkeypatch):
    import sqlite3
    clock = Clock("10:19")
    first = runtime(path, PaperFeed(), clock)
    first.start()
    first.stop()
    identities = rows(path, "SELECT * FROM crew_paper_account_identity ORDER BY account_id")
    assert len(identities) == len(reg.account_ids())
    assert all(r["registry_hash"] == reg.REGISTRY_HASH and r["strategy_version"] == reg.STRATEGY_VERSION
               for r in identities)
    conn = cj.connect(path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE crew_paper_account_identity SET registry_hash=?", ("a" * 64,))
    finally:
        conn.close()
    monkeypatch.setattr(reg, "REGISTRY_HASH", "b" * 64)
    changed = runtime(path, PaperFeed(), clock)
    with pytest.raises(cj.ConsistencyError) as error:
        changed.start()
    assert error.value.findings[0]["kind"] == "paper_registry_mismatch"
    assert rows(path, "SELECT * FROM crew_paper_account_identity ORDER BY account_id") == identities


def test_schema_upgrade_does_not_invent_legacy_account_identity(path):
    clock = Clock("10:19")
    first = runtime(path, PaperFeed(), clock)
    first.start()
    first.stop()
    before = paper(path)
    conn = cj.connect(path)
    try:
        # Recreate the v4 absence of binding evidence, only in this temporary journal.
        conn.execute("DROP TABLE crew_paper_account_identity")
        conn.execute("UPDATE crew_meta SET value='4' WHERE key='schema_version'")
    finally:
        conn.close()
    assert cj.migrate(path) == cj.SCHEMA_VERSION
    assert rows(path, "SELECT * FROM crew_paper_account_identity") == []
    assert paper(path) == before
    restart = runtime(path, PaperFeed(), clock)
    with pytest.raises(cj.ConsistencyError) as error:
        restart.start()
    assert error.value.findings[0]["kind"] == "paper_registry_migration_required"
    assert paper(path) == before


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
    assert (buy.price, buy.qty, buy.value) == (Decimal("40.45"), 130, Decimal("5258.50"))
    assert (sell.price, sell.value) == (Decimal("39.95"), Decimal("5193.50"))
    assert buy.slippage_inr == sell.slippage_inr == Decimal("32.50")  # 0.25 off the 40.20 mid x 130
    assert buy.charges["brokerage"] == sell.charges["brokerage"] == 20.0
    assert buy.charges["stt"] == 0.0 and sell.charges["stt"] == round(5193.50 * 0.001, 2)  # STT on sell premium only
    assert buy.charges_inr == Decimal(str(order_charges(SETTINGS, "OPT", "BUY", 40.45, 130, DAY)["total"]))
    assert px.fill(quote(now, bid=0.05, ask=0.1), "SELL", 1, now=now, settings=SETTINGS, ticks=1).price == px.TICK


@pytest.mark.parametrize("lots", [1, 2, 3])
def test_v2_whole_lot_charges_are_per_order_and_a_partial_exit_pays_its_own_brokerage(lots):
    now = datetime.fromisoformat(f"{DAY}T10:20:00+05:30")
    policy = pf.resolve_policy(DAY).policy_id
    q = quote(now)
    buy = px.fill(q, "BUY", lots, now=now, settings=SETTINGS, ticks=1, cost_policy_id=policy)
    one = px.fill(q, "BUY", 1, now=now, settings=SETTINGS, ticks=1, cost_policy_id=policy)
    assert buy.qty == lots * 65 and buy.value == Decimal("40.45") * lots * 65
    assert buy.charges["brokerage"] == one.charges["brokerage"]  # flat per order, not per lot
    assert buy.charges_inr == Decimal(pf.order_charges(price=buy.price, quantity=buy.qty, side="BUY",
                                                       market_day=DAY, policy_id=policy)["total"])
    if lots > 1:
        assert buy.charges_inr < one.charges_inr * lots
    # Exit as a 1-lot partial plus the remainder: two orders, two brokerages, quantities reconcile.
    legs = [1, lots - 1] if lots > 1 else [1]
    sells = [px.fill(q, "SELL", n, now=now, settings=SETTINGS, ticks=1, cost_policy_id=policy) for n in legs]
    assert sum(s.qty for s in sells) == buy.qty
    whole = px.fill(q, "SELL", lots, now=now, settings=SETTINGS, ticks=1, cost_policy_id=policy)
    assert sum(s.charges_inr for s in sells) >= whole.charges_inr
    net = sum(s.value for s in sells) - buy.value - buy.charges_inr - sum(s.charges_inr for s in sells)
    assert net == (Decimal("39.95") - Decimal("40.45")) * buy.qty - buy.charges_inr - sum(s.charges_inr for s in sells)


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
        assert net == -(sum(Decimal(e["charges_inr"]) for e in events)) + Decimal("39.95") * 65 * 3 - Decimal("40.45") * 195
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
    assert "Entry Premium: ₹40.45" in entry and "Quantity: 3 lots = 195" in entry and "Index Stop: 23970" in entry

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
        assert rt.tick()["admissions"] == []
    finally:
        rt.stop()
    assert rows(path, "SELECT * FROM crew_trades") == []
    assert rows(path, "SELECT * FROM crew_admissions") == []
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
    with monkeypatch.context() as patch:
        patch.setattr(pl, "partial", lambda *a, **k: (_ for _ in ()).throw(OSError("crash")))
        with pytest.raises(OSError):
            first.tick()
        crash(first, clock)
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

PAPER_MODULES = (pl, px, pf, pr, pa, rg, pn, reg, ps)


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

    accounting = body["accounts"][IND]["accounting"]
    assert accounting["equity_inr"] is None and accounting["unrealized_pnl_inr"] is None
    assert Decimal(accounting["reserved_worst_case_inr"]) > 0
    before = paper(path)

    def invalid_projection(*args, **kwargs):
        raise cj.ConsistencyError([{"kind": "paper_accounting", "detail": "synthetic corrupted evidence"}])

    monkeypatch.setattr(pl, "account_projection", invalid_projection)
    response = client.get("/api/live/crew/paper/stats", headers={"Authorization": "Bearer t"})
    assert response.status_code == 503
    assert response.json()["detail"]["status"] == "PAPER_ACCOUNTING_INCONSISTENT"
    assert paper(path) == before


@pytest.mark.parametrize("capital", ["1", "49999.99", "50000.01", "500000"])
def test_initial_paper_capital_must_equal_approved_amount(path, capital):
    clock = Clock("10:19")
    rt = runtime(path, PaperFeed(), clock,
                 settings=SETTINGS.model_copy(update={"paper_capital_inr": Decimal(capital)}))
    with pytest.raises(cj.ConsistencyError) as caught:
        rt.start()
    assert caught.value.findings == [{"kind": "paper_capital_not_approved", "approved_capital": "50000"}]
    assert rows(path, "SELECT * FROM crew_paper_accounts") == []


def test_canonical_projection_preserves_unpriced_exposure_without_inventing_equity(path):
    clock = Clock("10:19")
    feed = PaperFeed([setup()])
    rt = runtime(path, feed, clock)
    rt.start()
    try:
        rt.tick()
        trade = rows(path, "SELECT * FROM crew_paper_trades WHERE account_id=?", (IND,))[0]
        feed.priced = False
        feed.closes = [24000.0, 23960.0]
        walk(rt, clock, "10:20")
        rt.tick()
    finally:
        rt.stop()
    ledger_before = paper(path)
    projection = stats(path, IND)["accounting"]
    assert projection["unpriced_positions"] == [trade["paper_trade_id"]]
    assert projection["equity_inr"] is None and projection["unrealized_pnl_inr"] is None
    assert Decimal(projection["reserved_worst_case_inr"]) == Decimal(trade["max_loss_inr"])
    assert Decimal(projection["committed_capital_inr"]) == Decimal(trade["capital_committed_inr"])
    assert Decimal(projection["realized_pnl_inr"]) == Decimal(projection["gross_daily_realized_loss_inr"]) == 0
    assert Decimal(projection["cash_inr"]) + Decimal(projection["committed_capital_inr"]) == CAPITAL
    assert stats(path, IND)["accounting"] == projection and paper(path) == ledger_before


def test_historical_projection_excludes_future_sales_and_reconstructs_from_events(path):
    clock = Clock("10:19")
    feed = PaperFeed([setup()])
    rt = runtime(path, feed, clock)
    rt.start()
    try:
        rt.tick()
        conn = cj.connect_readonly(path)
        try:
            entry_projection = pl.account_projection(conn, IND, market_day=DAY)
        finally:
            conn.close()
        feed.closes = [24000.0, 23960.0]
        walk(rt, clock, "10:20")
        rt.tick()
    finally:
        rt.stop()
    conn = cj.connect_readonly(path)
    try:
        empty = pl.account_projection(conn, IND, market_day="2026-11-05")
        closed = pl.account_projection(conn, IND, market_day=DAY)
        assert empty.cash == empty.equity == CAPITAL and empty.committed == empty.reserved == 0
        assert entry_projection.equity is None and entry_projection.reserved > 0
        assert closed.equity == closed.cash and closed.committed == closed.reserved == 0
        assert closed.cash == CAPITAL + closed.realized
        shared = pl.account_projection(conn, reg.SHARED, market_day=DAY)
        assert (shared.cash, shared.realized, shared.reserved) == (closed.cash, closed.realized, closed.reserved)
        assert all(key.startswith("P_") for key, _ in shared.charge_discrepancies)
        assert {key for key, _ in shared.charge_discrepancies}.isdisjoint(
            key for key, _ in closed.charge_discrepancies)
    finally:
        conn.close()


def test_daily_summary_discloses_open_exposure_instead_of_suppressing_report(path):
    clock = Clock("10:19")
    rt = runtime(path, PaperFeed([setup()]), clock)
    rt.start()
    try:
        rt.tick()
    finally:
        rt.stop()
    before = paper(path)
    conn = cj.connect_readonly(path)
    try:
        now = datetime.fromisoformat(f"{DAY}T15:31:00+05:30")
        summaries = [(purpose, body) for purpose, body in pn.due(conn, DAY, now)
                     if "DAILY_STRATEGY_SUMMARY" in purpose]
        assert len(summaries) == len(reg.account_ids())
        body = next(body for purpose, body in summaries if f"|{IND}|" in purpose)
        assert "Unpriced positions: 1" in body
        assert "Equity: not priced" in body and "Unrealized P&L: not priced" in body
        trade = rows(path, "SELECT max_loss_inr FROM crew_paper_trades WHERE account_id=?", (IND,))[0]
        assert f"Reserved worst-case exposure: ₹{Decimal(trade['max_loss_inr']):,.2f}" in body
    finally:
        conn.close()
    assert paper(path) == before


def test_later_day_sync_recovers_missing_summaries_once_without_rewriting_ledger(path):
    clock = Clock("10:19")
    rt = runtime(path, PaperFeed([setup()]), clock)
    rt.start()
    rt.tick()
    rt.stop()
    clock.at("10:19", day="2026-11-09")
    resumed = runtime(path, PaperFeed(), clock)
    resumed.start()
    try:
        before = paper(path)
        pn.sync(path, lease=resumed.lease, now=clock())
        summaries = [r for r in outbox(path) if "DAILY_STRATEGY_SUMMARY" in r["purpose"]]
        assert len(summaries) == len(reg.account_ids())
        assert all(r["purpose"].endswith(DAY) and r["chat_id"] == pn.CHAT_ID for r in summaries)
        snapshot = outbox(path)
        assert pn.sync(path, lease=resumed.lease, now=clock()) == []
        assert outbox(path) == snapshot and paper(path) == before
    finally:
        resumed.stop()


def test_startup_recovers_summaries_without_calling_market_feed(path):
    clock = Clock("10:19")
    initial = runtime(path, PaperFeed([setup()]), clock)
    initial.start()
    initial.tick()
    initial.stop()
    clock.at("10:19", day="2026-11-09")
    calls = []

    def unavailable(now):
        calls.append(now)
        raise RuntimeError("synthetic provider outage")

    restarted = runtime(path, unavailable, clock)
    report = restarted.start()
    try:
        assert calls == [] and report["paper_notifications"]
        summaries = [r for r in outbox(path) if "DAILY_STRATEGY_SUMMARY" in r["purpose"]]
        assert len(summaries) == len(reg.account_ids())
        before = outbox(path)
        assert restarted.tick() == {"skipped": "FEED_ERROR"}
        assert len(calls) == 1 and outbox(path) == before
    finally:
        restarted.stop()
    again = runtime(path, unavailable, clock)
    report = again.start()
    try:
        assert report["paper_notifications"] == []
        assert outbox(path) == before
    finally:
        again.stop()


def test_historical_report_preserves_prior_day_state_after_recovery(path):
    clock = Clock("10:19")
    rt = runtime(path, PaperFeed([setup()]), clock)
    rt.start()
    rt.tick()
    rt.stop()
    original = stats(path, IND, DAY)
    assert len(original["open"]) == 1 and original["expired_unresolved"] == []
    clock.at("10:19", day="2026-11-09")
    restarted = runtime(path, PaperFeed(), clock)
    restarted.start()
    try:
        past = stats(path, IND, DAY)
        current = stats(path, IND, "2026-11-09")
        assert past == original
        assert current["open"] == [] and current["expired_unresolved"] == original["open"]
        assert current["signals"] == 0 and current["completed"] == 0
        assert current["accounting"]["reserved_worst_case_inr"] == original["accounting"]["reserved_worst_case_inr"]
        assert current["accounting"]["equity_inr"] is None
    finally:
        restarted.stop()


@pytest.mark.parametrize("conflict", [False, True])
def test_explicit_legacy_binding_is_atomic_idempotent_and_preserves_finances(path, monkeypatch, conflict):
    clock = Clock("10:19")
    initial = runtime(path, PaperFeed([setup()]), clock)
    initial.start()
    initial.tick()
    initial.stop()
    before = paper(path)
    conn = cj.connect(path)
    try:
        conn.execute("DROP TABLE crew_paper_account_identity")
    finally:
        conn.close()
    cj.migrate(path)
    if conflict:
        monkeypatch.setattr(reg, "REGISTRY_HASH", "f" * 64)
    lease = cs.acquire(path, now=clock())
    try:
        kwargs = dict(lease=lease, expected_registry_hash=reg.REGISTRY_HASH,
                      reason="Verify v4 synthetic journal identity", now=clock())
        if conflict:
            with pytest.raises(cj.ConsistencyError) as error:
                pl.bind_legacy_accounts(path, **kwargs)
            assert error.value.findings[0]["kind"] == "legacy_registry_conflict"
            assert rows(path, "SELECT * FROM crew_paper_account_identity") == []
        else:
            assert set(pl.bind_legacy_accounts(path, **kwargs)) == set(reg.account_ids())
            identities = rows(path, "SELECT * FROM crew_paper_account_identity ORDER BY account_id")
            assert {json.loads(r["provenance"])["evidence"] for r in identities} == {
                "RECORDED_HASH", "EXPLICIT_EMPTY_ACCOUNT_ATTESTATION"}
            assert pl.bind_legacy_accounts(path, **kwargs) == []
            assert rows(path, "SELECT * FROM crew_paper_account_identity ORDER BY account_id") == identities
        assert paper(path) == before
    finally:
        cs.release(path, lease, now=clock())


@pytest.mark.parametrize("trust,evidence_age,expected", [(True, 0, "OPENED"),
    (False, 0, "STOP_RISK_UNVERIFIABLE"), (True, 11, "STOP_RISK_UNVERIFIABLE")])
def test_v2_runtime_passes_explicit_delta_evidence_into_atomic_sizing(path, monkeypatch, trust, evidence_age, expected):
    from dataclasses import replace
    from app.trading.paper_risk import DeltaEvidence
    from app.trading import paper_fees
    monkeypatch.setitem(reg.RISK_POLICY, "risk_policy_id", "paper-risk-v2")
    monkeypatch.setattr(reg, "REGISTRY_HASH", "a" * 64)
    clock = Clock("10:19")
    base = PaperFeed([setup(stop=23980)])

    def feed(now):
        view = base(now)
        contract = quote(now).tradingsymbol
        evidence = DeltaEvidence(Decimal("0.5"), now - timedelta(seconds=evidence_age), contract, "synthetic")
        return replace(view, delta_evidence={contract: evidence})

    rt = rr.ResearchRuntime(path, feed, settings=SETTINGS, clock=clock,
                            trusted_delta_sources=frozenset({"synthetic"}) if trust else frozenset())
    rt.start()
    try:
        rt.tick()
        decisions = rows(path, "SELECT * FROM crew_paper_decisions")
        assert len(decisions) == 2
        risk_rows = rows(path, "SELECT * FROM crew_paper_risk_decisions")
        assert len(risk_rows) == 2
        snapshots = [json.loads(r["evidence"]) for r in risk_rows]
        assert all(s["account_before"]["cash"] == "50000.00" for s in snapshots)
        assert all(s["delta"]["source"] == "synthetic" and s["limits"]["daily_exposure"] == "5000.00"
                   for s in snapshots)
        import sqlite3
        conn = cj.connect(path)
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM crew_paper_risk_decisions")
        finally:
            conn.close()
        if expected == "OPENED":
            assert all(d["outcome"] == "OPENED" for d in decisions)
            messages = [row for row in outbox(path) if "PAPER_ENTRY_FILLED" in row["purpose"]]
            assert len(messages) == 2
            for message in messages:
                assert message["chat_id"] == "mock:paper"
                assert "Mode: PAPER ONLY" in message["body"]
                assert "Risk policy: paper-risk-v2" in message["body"]
                assert "Full-premium worst-case loss:" in message["body"]
                assert "Market regime (descriptive):" in message["body"]
                assert str(snapshots[0]["sizing"]["estimated_stop_risk"]) in message["body"]
            trades = rows(path, "SELECT * FROM crew_paper_trades")
            assert all(t["lots_initial"] == 1 and Decimal(t["max_loss_inr"]) <= 5000 for t in trades)
            events = rows(path, "SELECT * FROM crew_paper_events")
            assert all(json.loads(e["charges"])["policy"]["policy_id"] == paper_fees.POLICY_ID for e in events)
            assert stats(path, IND)["accounting"]["charge_reconciliation"] == "EXACT"
            pl.close(path, lease=rt.lease, model_trade_id=trades[0]["model_trade_id"], chain=book(clock()),
                     index_spot=24000, reason="SYNTHETIC_EXIT", now=clock(), settings=SETTINGS)
            exits = rows(path, "SELECT * FROM crew_paper_events WHERE side='SELL'")
            assert len(exits) == 2
            assert all(json.loads(e["charges"])["policy"]["policy_id"] == paper_fees.POLICY_ID for e in exits)
            accounting = stats(path, IND)["accounting"]
            assert accounting["charge_reconciliation"] == "EXACT"
            assert Decimal(accounting["reserved_worst_case_inr"]) == 0
            assert Decimal(accounting["cash_inr"]) == CAPITAL + Decimal(accounting["realized_pnl_inr"])
        else:
            assert all(d["outcome"] == "REJECTED" and json.loads(d["reasons"]) == [expected] for d in decisions)
            assert rows(path, "SELECT * FROM crew_paper_trades") == []
    finally:
        rt.stop()
    if expected == "OPENED":
        financial = paper(path)
        accounting = stats(path, IND)["accounting"]
        clock.at("15:31")
        resumed = runtime(path, PaperFeed(), clock)
        resumed.start()
        resumed.stop()
        assert paper(path) == financial
        assert stats(path, IND)["accounting"] == accounting
        assert pl.check_consistency(path) == []
        summaries = [row for row in outbox(path) if "DAILY_STRATEGY_SUMMARY" in row["purpose"]]
        assert len(summaries) == len(reg.account_ids())
        assert all(row["chat_id"] == "mock:paper" for row in summaries)
        publications = outbox(path)
        again = runtime(path, PaperFeed(), clock)
        assert again.start()["paper_notifications"] == []
        again.stop()
        assert outbox(path) == publications and paper(path) == financial
        conn = cj.connect(path)
        try:
            # Deliberately corrupt only this temporary journal after proving append-only behavior above.
            conn.execute("DROP TRIGGER crew_paper_events_no_update")
            conn.execute("UPDATE crew_paper_events SET charges=json_set(charges,'$.policy_hash','invalid') WHERE seq=1")
        finally:
            conn.close()
        corrupted = paper(path)
        refused = runtime(path, PaperFeed(), clock)
        with pytest.raises(cj.ConsistencyError) as error:
            refused.start()
        assert any(f["kind"] == "paper_accounting" for f in error.value.findings)
        assert paper(path) == corrupted



def test_v2_missed_entry_recovery_records_rejection_evidence_once(path, monkeypatch):
    monkeypatch.setitem(reg.RISK_POLICY, "risk_policy_id", "paper-risk-v2")
    clock = Clock("10:19")
    first = runtime(path, PaperFeed([setup()]), clock)
    first.start()
    with monkeypatch.context() as patch:
        patch.setattr(pl, "enter", lambda *a, **kw: (_ for _ in ()).throw(OSError("synthetic crash")))
        with pytest.raises(OSError):
            first.tick()
    first.stop()
    second = runtime(path, PaperFeed(), clock)
    report = second.start()
    second.stop()
    assert len(report["paper"]["entry_missed"]) == 2
    audit = rows(path, "SELECT * FROM crew_paper_risk_decisions ORDER BY account_id")
    assert len(audit) == 2
    for row in audit:
        evidence = json.loads(row["evidence"])
        assert evidence["recovery"] is True and evidence["sizing"] is None
        assert evidence["reasons"] == [pl.ENTRY_MISSED_AFTER_RESTART]
    assert rows(path, "SELECT * FROM crew_paper_events") == []
    assert pl.check_consistency(path) == []
    third = runtime(path, PaperFeed(), clock)
    third.start()
    third.stop()
    assert rows(path, "SELECT * FROM crew_paper_risk_decisions ORDER BY account_id") == audit


def test_v2_recovery_refuses_missing_risk_evidence(path, monkeypatch):
    monkeypatch.setitem(reg.RISK_POLICY, "risk_policy_id", "paper-risk-v2")
    clock = Clock("10:19")
    first = runtime(path, PaperFeed([setup()]), clock)
    first.start()
    first.tick()  # No trusted delta: two persisted rejections.
    first.stop()
    before = paper(path)
    conn = cj.connect(path)
    try:
        conn.execute("DROP TRIGGER crew_paper_risk_decisions_no_delete")
        conn.execute("DELETE FROM crew_paper_risk_decisions")
    finally:
        conn.close()
    restart = runtime(path, PaperFeed(), clock)
    with pytest.raises(cj.ConsistencyError) as error:
        restart.start()
    assert all(f["kind"] == "paper_risk_evidence" for f in error.value.findings)
    assert paper(path) == before


def test_v2_registry_change_after_start_cannot_admit_under_old_identity(path, monkeypatch):
    clock = Clock("10:19")
    rt = runtime(path, PaperFeed([setup()]), clock)
    rt.start()
    before = paper(path)
    monkeypatch.setattr(reg, "REGISTRY_HASH", "e" * 64)
    try:
        with pytest.raises(cj.ConsistencyError) as error:
            rt.tick()
        assert error.value.findings[0]["kind"] == "paper_admission_identity"
        assert paper(path) == before
        assert rows(path, "SELECT * FROM crew_paper_risk_decisions") == []
    finally:
        rt.stop()


@pytest.mark.parametrize("cheap", [False, True])
def test_v2_partial_execution_alone_releases_exposure_and_replay_is_neutral(path, cheap):
    from dataclasses import replace
    from app.trading.paper_risk import DeltaEvidence
    clock = Clock("10:19")
    prices = dict(bid=10, ask=10.4) if cheap else {}
    base = PaperFeed([setup(stop=23995)], **prices)

    def feed(now):
        view = base(now)
        contract = quote(now, **prices).tradingsymbol
        return replace(view, delta_evidence={contract: DeltaEvidence(Decimal("0.5"), now, contract, "synthetic")})

    rt = rr.ResearchRuntime(path, feed, settings=SETTINGS, clock=clock,
                            trusted_delta_sources=frozenset({"synthetic"}))
    rt.start()
    try:
        rt.tick()
        trade = rows(path, "SELECT * FROM crew_paper_trades WHERE account_id=?", (IND,))[0]
        assert trade["lots_initial"] == (3 if cheap else 1)
        before = stats(path, IND)["accounting"]
        args = dict(lease=rt.lease, model_trade_id=trade["model_trade_id"], chain=book(clock(), **prices),
                    index_spot=24020, reason="SYNTHETIC_PARTIAL", now=clock(), settings=SETTINGS)
        pl.partial(path, **args)
        after = stats(path, IND)["accounting"]
        if cheap:
            assert Decimal(after["reserved_worst_case_inr"]) < Decimal(before["reserved_worst_case_inr"])
            assert Decimal(after["committed_capital_inr"]) > 0
            assert after["charge_reconciliation"] == "EXACT"
        else:
            assert after == before
        snapshot = paper(path)
        pl.partial(path, **args)
        assert paper(path) == snapshot
        assert stats(path, IND)["accounting"] == after
        assert pl.check_consistency(path) == []
    finally:
        rt.stop()


@pytest.mark.parametrize("mutate,why", [
    (lambda row: row["ce"].update(tradingSymbol="NIFTY26N1724100CE"), "CONTRACT_IDENTITY_MISMATCH"),
    (lambda row: row.update(underlying="BANKNIFTY"), "CONTRACT_IDENTITY_MISMATCH"),
    (lambda row: row["ce"].pop("tradingSymbol"), "CONTRACT_IDENTITY_MISSING"),
])
def test_v2_partial_never_sells_a_book_for_another_contract(path, mutate, why):
    from dataclasses import replace

    from app.trading.paper_risk import DeltaEvidence
    clock = Clock("10:19")
    base = PaperFeed([setup(stop=23995)], bid=10, ask=10.4)

    def feed(now):
        view = base(now)
        contract = quote(now, bid=10, ask=10.4).tradingsymbol
        return replace(view, delta_evidence={contract: DeltaEvidence(Decimal("0.5"), now, contract, "synthetic")})

    rt = rr.ResearchRuntime(path, feed, settings=SETTINGS, clock=clock,
                            trusted_delta_sources=frozenset({"synthetic"}))
    rt.start()
    try:
        rt.tick()
        trade = rows(path, "SELECT * FROM crew_paper_trades WHERE account_id=?", (IND,))[0]
        assert trade["lots_initial"] == 3
        before = stats(path, IND)["accounting"]
        chain = book(clock(), bid=10, ask=10.4)
        mutate(chain["contracts"][0])
        out = pl.partial(path, lease=rt.lease, model_trade_id=trade["model_trade_id"], chain=chain,
                         index_spot=24020, reason="SYNTHETIC_PARTIAL", now=clock(), settings=SETTINGS)
        assert out and {o["event_type"] for o in out} == {"PARTIAL_SKIPPED"}
        skipped = rows(path, "SELECT reason, lots_after FROM crew_paper_events WHERE event_type='PARTIAL_SKIPPED'")
        assert skipped == [{"reason": f"SYNTHETIC_PARTIAL:UNPRICED:{why}", "lots_after": 3}] * len(out)
        assert rows(path, "SELECT * FROM crew_paper_events WHERE side='SELL'") == []
        assert stats(path, IND)["accounting"] == before
        assert pl.check_consistency(path) == []
    finally:
        rt.stop()


def test_v2_shared_competition_preserves_independent_accounts(path):
    from dataclasses import replace
    from app.trading.paper_risk import DeltaEvidence
    clock = Clock("10:19")
    base = PaperFeed([setup(strategy="Breakout & Retest (PD) [5M]", stop=23995), setup(stop=23995)],
                     bid=10, ask=10.4)

    def feed(now):
        view = base(now)
        contract = quote(now, bid=10, ask=10.4).tradingsymbol
        return replace(view, delta_evidence={contract: DeltaEvidence(Decimal("0.5"), now, contract, "synthetic")})

    rt = rr.ResearchRuntime(path, feed, settings=SETTINGS, clock=clock,
                            trusted_delta_sources=frozenset({"synthetic"}))
    rt.start()
    try:
        rt.tick()
        shared = rows(path, "SELECT family,outcome,reasons FROM crew_paper_decisions WHERE account_id='SHARED' ORDER BY rowid")
        assert [(d["family"], d["outcome"], json.loads(d["reasons"])) for d in shared] == [
            ("LIQ_SWEEP_PDHL", "OPENED", []), ("RETEST_PD", "REJECTED", [pl.DUPLICATE_EXPOSURE])]
        accounts = [IND, "IND:RETEST_PD", reg.SHARED]
        projections = [stats(path, aid)["accounting"] for aid in accounts]
        assert all(p["risk_policy_id"] == "paper-risk-v2" and p["policy_status"] == "V2_ADMISSION_POLICY"
                   for p in projections)
        assert len(rows(path, "SELECT * FROM crew_paper_trades")) == 3
        assert all(p["cash_inr"] == projections[0]["cash_inr"] for p in projections)
        assert all(p["reserved_worst_case_inr"] == projections[0]["reserved_worst_case_inr"] for p in projections)
        untouched = stats(path, "IND:RETEST_OR30")["accounting"]
        assert Decimal(untouched["cash_inr"]) == CAPITAL and Decimal(untouched["reserved_worst_case_inr"]) == 0
        assert len(rows(path, "SELECT * FROM crew_paper_risk_decisions")) == 4
        before = paper(path)
        rt.tick()
        assert paper(path) == before
        assert pl.check_consistency(path) == []
    finally:
        rt.stop()


def test_v2_unpriced_exit_and_later_restart_preserve_financial_exposure(path):
    from dataclasses import replace
    from app.trading.paper_risk import DeltaEvidence
    clock = Clock("10:19")
    base = PaperFeed([setup(stop=23995)], bid=10, ask=10.4)

    def feed(now):
        view = base(now)
        contract = quote(now, bid=10, ask=10.4).tradingsymbol
        return replace(view, delta_evidence={contract: DeltaEvidence(Decimal("0.5"), now, contract, "synthetic")})

    rt = rr.ResearchRuntime(path, feed, settings=SETTINGS, clock=clock,
                            trusted_delta_sources=frozenset({"synthetic"}))
    rt.start()
    try:
        rt.tick()
        trade = rows(path, "SELECT * FROM crew_paper_trades WHERE account_id=?", (IND,))[0]
        before = stats(path, IND)["accounting"]
        pl.close(path, lease=rt.lease, model_trade_id=trade["model_trade_id"], chain=None,
                 index_spot=24000, reason="SYNTHETIC_NO_BOOK", now=clock(), settings=SETTINGS)
        after = stats(path, IND)["accounting"]
        assert after == before
        assert len(stats(path, IND)["unpriced_exits"]) == 1
    finally:
        rt.stop()
    financial = paper(path)
    clock.at("10:19", day="2026-11-09")
    restarted = runtime(path, PaperFeed(), clock)
    restarted.start()
    restarted.stop()
    assert paper(path) == financial
    recovered = stats(path, IND)["accounting"]
    for key in ("cash_inr", "committed_capital_inr", "reserved_worst_case_inr", "realized_pnl_inr"):
        assert recovered[key] == before[key]
    assert recovered["equity_inr"] is None and recovered["unrealized_pnl_inr"] is None
    assert pl.check_consistency(path) == []


def test_v2_audit_write_failure_rolls_back_all_account_entries(path):
    import sqlite3
    from dataclasses import replace
    from app.trading.paper_risk import DeltaEvidence
    clock = Clock("10:19")
    base = PaperFeed([setup(stop=23995)], bid=10, ask=10.4)

    def feed(now):
        view = base(now)
        contract = quote(now, bid=10, ask=10.4).tradingsymbol
        return replace(view, delta_evidence={contract: DeltaEvidence(Decimal("0.5"), now, contract, "synthetic")})

    rt = rr.ResearchRuntime(path, feed, settings=SETTINGS, clock=clock,
                            trusted_delta_sources=frozenset({"synthetic"}))
    rt.start()
    conn = cj.connect(path)
    try:
        conn.execute("CREATE TRIGGER injected_risk_failure BEFORE INSERT ON crew_paper_risk_decisions "
                     "WHEN NEW.account_id='SHARED' BEGIN SELECT RAISE(ABORT, 'synthetic audit failure'); END")
    finally:
        conn.close()
    before = paper(path)
    try:
        with pytest.raises(cj.InvalidTransition, match="synthetic audit failure"):
            rt.tick()
        assert paper(path) == before
        assert rows(path, "SELECT * FROM crew_paper_risk_decisions") == []
        for account in (IND, reg.SHARED):
            accounting = stats(path, account)["accounting"]
            assert Decimal(accounting["cash_inr"]) == CAPITAL
            assert Decimal(accounting["reserved_worst_case_inr"]) == 0
    finally:
        rt.stop()


def test_v2_injected_stale_candles_cannot_create_model_or_paper_entries(path):
    clock = Clock("10:25")
    rt = runtime(path, PaperFeed([setup()]), clock)  # Latest fixture candle closed at 10:15.
    rt.start()
    try:
        assert rt.tick() == {"skipped": "INVALID_OR_STALE_CANDLES"}
        assert rows(path, "SELECT * FROM crew_trades") == []
        assert rows(path, "SELECT * FROM crew_paper_trades") == []
        assert rows(path, "SELECT * FROM crew_paper_decisions") == []
    finally:
        rt.stop()


def test_breakeven_trade_is_counted_separately_from_average_loss(path, monkeypatch):
    from dataclasses import replace
    original_fill = px.fill

    def neutral_fill(*args, **kwargs):
        fill = original_fill(*args, **kwargs)
        components = {key: 0 for key in fill.charges}
        return replace(fill, price=Decimal("40.45"), charges=components,
                       charges_inr=Decimal("0.00"), slippage_inr=Decimal("0.00"))

    # Explicit fee-free synthetic oracle, not a broker tariff or performance result.
    monkeypatch.setattr(px, "fill", neutral_fill)
    clock = Clock("10:19")
    rt = runtime(path, PaperFeed([setup()]), clock)
    rt.start()
    try:
        rt.tick()
        trade = rows(path, "SELECT * FROM crew_paper_trades WHERE account_id=?", (IND,))[0]
        pl.close(path, lease=rt.lease, model_trade_id=trade["model_trade_id"], chain=book(clock()),
                 index_spot=24000, reason="SYNTHETIC_BREAKEVEN", now=clock(), settings=SETTINGS)
        result = stats(path, IND)
        assert result["completed"] == result["breakeven"] == 1
        assert result["wins"] == result["losses"] == 0
        assert result["avg_loss_inr"] is None and result["avg_win_inr"] is None
        assert Decimal(result["expectancy_inr"]) == 0
        assert Decimal(result["accounting"]["realized_pnl_inr"]) == 0
        assert Decimal(result["accounting"]["cash_inr"]) == CAPITAL
    finally:
        rt.stop()


@pytest.mark.parametrize("interrupted", ["partial", "close", "owner_loss"])
def test_v2_missed_execution_restart_retains_full_unexecuted_reservation(path, monkeypatch, interrupted):
    from dataclasses import replace
    from app.trading.paper_risk import DeltaEvidence
    clock = Clock("10:19")
    base = PaperFeed([setup(stop=23995)], bid=10, ask=10.4)

    def feed(now):
        view = base(now)
        contract = quote(now, bid=10, ask=10.4).tradingsymbol
        return replace(view, delta_evidence={contract: DeltaEvidence(Decimal("0.5"), now, contract, "synthetic")})

    first = rr.ResearchRuntime(path, feed, settings=SETTINGS, clock=clock,
                               trusted_delta_sources=frozenset({"synthetic"}))
    first.start()
    first.tick()
    before = stats(path, IND)["accounting"]
    original_owner = first.lease.session_id
    if interrupted != "owner_loss":
        base.closes = [24000.0, 24065.0 if interrupted == "partial" else 23990.0]
        walk(first, clock, "10:20")
        with monkeypatch.context() as patch:
            patch.setattr(pl, interrupted, lambda *a, **kw: (_ for _ in ()).throw(OSError("synthetic partial crash")))
            with pytest.raises(OSError):
                first.tick()
    crash(first, clock)
    second = runtime(path, PaperFeed(), clock)
    report = second.start()
    second.stop()
    report_key = "partial_missed" if interrupted == "partial" else "exit_missed"
    assert len(report["paper"][report_key]) == (0 if interrupted == "owner_loss" else 2)
    if interrupted == "owner_loss":
        assert len(report["paper"]["orphaned"]) == 2
        assert all(row["owner_session_id"] == original_owner for row in report["paper"]["orphaned"])
        assert len(stats(path, IND)["orphaned_open"]) == 1
    assert all(row["owner_session_id"] == original_owner for row in rows(path, "SELECT owner_session_id FROM crew_trades"))
    assert stats(path, IND)["accounting"] == before
    assert rows(path, "SELECT * FROM crew_paper_events WHERE side='SELL'") == []
    events = rows(path, "SELECT event_type,reason FROM crew_paper_events WHERE seq=2")
    expected = {"event_type": "PARTIAL_SKIPPED", "reason": pl.PARTIAL_MISSED_AFTER_RESTART} if interrupted == "partial" else {"event_type": "EXIT_UNPRICED", "reason": pl.EXIT_MISSED_AFTER_RESTART}
    assert events == ([] if interrupted == "owner_loss" else [expected] * 2)
    financial = paper(path)
    third = runtime(path, PaperFeed(), clock)
    assert third.start()["paper"][report_key] == []
    third.stop()
    assert paper(path) == financial


@pytest.mark.parametrize("exit_time,spot,expected", [("15:00", 23990.0, "STOP_LOSS_HIT"),
                                                    ("15:10", 24000.0, "INTRADAY_CLOSE"),
                                                    ("15:00", 24010.0, "STRUCTURE_TRAIL_EXIT"),
                                                    ("15:00", 24065.0, "LIQUIDITY_HOP_PROMOTED")])
def test_v2_runtime_executes_stop_and_session_close_with_reconciliation(path, exit_time, spot, expected):
    from dataclasses import replace
    from app.trading.paper_risk import DeltaEvidence
    clock = Clock("14:59")
    candidate = setup(stop=23995, ts=f"{DAY} 14:55")
    base = PaperFeed([candidate], bid=10, ask=10.4)
    current = [24000.0]

    def feed(now):
        view = base(now)
        start = (now.replace(second=0, microsecond=0) - timedelta(minutes=now.minute % 5 + 5)).strftime("%H:%M")
        contract = quote(now, bid=10, ask=10.4).tradingsymbol
        frame = bars(current, start=start)
        if expected == "STRUCTURE_TRAIL_EXIT" and now.strftime("%H:%M") == "15:00":
            frame = bars([24015.0, 24010.0], start="14:50")
            frame.loc[0, ["open", "high", "low", "close"]] = [24015, 24020, 24011, 24015]
            frame.loc[1, ["open", "high", "low", "close"]] = [24015, 24016, 24009, 24010]
        return replace(view, df_5m=frame,
                       delta_evidence={contract: DeltaEvidence(Decimal("0.5"), now, contract, "synthetic")})

    rt = rr.ResearchRuntime(path, feed, settings=SETTINGS, clock=clock,
                            trusted_delta_sources=frozenset({"synthetic"}))
    rt.start()
    try:
        rt.tick()
        assert len(rows(path, "SELECT * FROM crew_paper_trades")) == 2
        current[:] = [spot]
        walk(rt, clock, exit_time)
        result = rt.tick()
        assert result["exits"][0]["action"] == expected
        if expected == "LIQUIDITY_HOP_PROMOTED":
            assert all(t["state"] == "PARTIALLY_EXITED" for t in rows(path, "SELECT * FROM crew_paper_trades"))
            current[:] = [24075.0]
            walk(rt, clock, "15:05")
            assert rt.tick()["exits"][0]["action"] == "TARGET_HIT"
        assert all(t["state"] == "CLOSED" for t in rows(path, "SELECT * FROM crew_paper_trades"))
        accounting = stats(path, IND)["accounting"]
        assert Decimal(accounting["reserved_worst_case_inr"]) == 0
        assert Decimal(accounting["cash_inr"]) == CAPITAL + Decimal(accounting["realized_pnl_inr"])
        assert accounting["charge_reconciliation"] == "EXACT"
        from app.trading import trade_memory, promotion
        conn = cj.connect_readonly(path)
        try:
            memory = trade_memory.report(conn, account_id=IND)
            assert memory["records"][0]["state"] == "CLOSED"
            assert memory["records"][0]["quantity"] == 0
            assert Decimal(memory["records"][0]["realized_pnl_inr"]) == Decimal(accounting["realized_pnl_inr"])
            assert memory["records"][0]["reserved_worst_case_inr"] == "0.00"
            assert memory["records"][0]["promotion_eligible"] is False
            summaries = pn.due(conn, DAY, clock().replace(hour=15, minute=35))
            assert any("DAILY_STRATEGY_SUMMARY" in purpose for purpose, _ in summaries)
        finally:
            conn.close()
        blocked = promotion.evaluate(current="WALK_FORWARD_VALIDATED", target="PAPER_APPROVED",
            binding={}, evidence=[], now=clock())
        assert not blocked.allowed and not blocked.orders_authorized
        publications = outbox(path)
        pn.sync(path, lease=rt.lease, now=clock())
        assert outbox(path) == publications
    finally:
        rt.stop()


@pytest.mark.parametrize("late", [False, True, "missing_candles", "bad_candles", "wrong_contract", "wrong_strike", "wrong_expiry", "wrong_symbol", "wrong_lot", "wrong_provider", "missing_provider", "wrong_underlying"])
def test_v2_direct_ledger_cannot_bypass_session_or_hard_cutoff(path, monkeypatch, late):
    clock = Clock("10:19")
    rt = runtime(path, PaperFeed([setup()]), clock)
    rt.start()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(pl, "enter", lambda *a, **kw: [])
            rt.tick()
        admission = rows(path, "SELECT * FROM crew_admissions")[0]
        admission["inputs"] = json.loads(admission["inputs"])
        model = rows(path, "SELECT * FROM crew_trades")[0]
        settings = SETTINGS
        frame = bars([24000.0])
        selected_quote = quote(clock())
        if late is True:
            walk(rt, clock, "15:11")
        elif late is False:
            settings = SETTINGS.model_copy(update={"market_open": "11:00"})
        elif late == "missing_candles":
            frame = None
        elif late == "bad_candles":
            frame["high"] = 1
        else:
            from dataclasses import replace
            selected_quote = replace(selected_quote, **({"strike": selected_quote.strike + 50} if late == "wrong_strike"
                                                       else {"expiry": "2026-11-12"} if late == "wrong_expiry"
                                                       else {"tradingsymbol": "OTHER"} if late == "wrong_symbol"
                                                       else {"lot_size": 75} if late == "wrong_lot"
                                                       else {"provider_instrument_id": "OTHER"} if late == "wrong_provider"
                                                       else {"provider_instrument_id": None} if late == "missing_provider"
                                                       else {"underlying": "BANKNIFTY"} if late == "wrong_underlying"
                                                       else {"option_type": "PE"}))
        decisions = pl.enter(path, lease=rt.lease, admission=admission, model_trade=model,
                             timeframe="5m", quote=quote(clock()) if late is True else selected_quote, df_5m=frame, now=clock(),
                             settings=settings, cutoff="23:59")
        assert all(d["outcome"] == "REJECTED" for d in decisions)
        if late is True:
            assert all(d["reasons"] == [pl.ENTRY_AFTER_CUTOFF] for d in decisions)
        elif late is False:
            assert all(d["reasons"][0].startswith("MARKET_CLOSED:") for d in decisions)
        elif late == "missing_provider":
            assert all(d["reasons"] == ["CONTRACT_IDENTITY_MISSING"] for d in decisions)
        elif isinstance(late, str) and late.startswith("wrong_"):
            assert all(d["reasons"] == ["CONTRACT_IDENTITY_MISMATCH"] for d in decisions)
        else:
            assert all(d["reasons"] == ["INVALID_OR_STALE_CANDLES"] for d in decisions)
        assert rows(path, "SELECT * FROM crew_paper_trades") == []
        assert rows(path, "SELECT * FROM crew_paper_events") == []
    finally:
        rt.stop()


@pytest.mark.parametrize("tamper", ["admission", "model"])
def test_v2_caller_cannot_replace_persisted_admission_inputs(path, monkeypatch, tamper):
    clock = Clock("10:19")
    rt = runtime(path, PaperFeed([setup()]), clock)
    rt.start()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(pl, "enter", lambda *a, **kw: [])
            rt.tick()
        admission = rows(path, "SELECT * FROM crew_admissions")[0]
        admission["inputs"] = json.loads(admission["inputs"])
        model = rows(path, "SELECT * FROM crew_trades")[0]
        if tamper == "admission":
            admission["inputs"]["contract"]["strike"] += 50
        else:
            model["stop_spot"] = model["entry_spot"] - 1
        before = paper(path)
        with pytest.raises(cj.ConsistencyError) as error:
            pl.enter(path, lease=rt.lease, admission=admission, model_trade=model,
                     timeframe="5m", quote=quote(clock()), df_5m=bars([24000.0]), now=clock(),
                     settings=SETTINGS, cutoff="15:10")
        assert error.value.findings[0]["kind"] == ("paper_admission_evidence" if tamper == "admission" else "paper_model_evidence")
        assert paper(path) == before
        assert rows(path, "SELECT * FROM crew_paper_risk_decisions") == []
    finally:
        rt.stop()


def test_v2_closed_model_cannot_create_late_paper_exposure(path, monkeypatch):
    clock = Clock("10:19")
    rt = runtime(path, PaperFeed([setup()]), clock)
    rt.start()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(pl, "enter", lambda *a, **kw: [])
            rt.tick()
        admission = rows(path, "SELECT * FROM crew_admissions")[0]
        admission["inputs"] = json.loads(admission["inputs"])
        model = rows(path, "SELECT * FROM crew_trades")[0]
        cj.exit_trade(path, lease=rt.lease, trade_id=model["trade_id"], expected_version=model["version"],
                      reason="SYNTHETIC_MODEL_CLOSE", signal_candle=f"{DAY}T10:15:00+05:30",
                      index_levels={"spot": 24000}, model_points=0, model_r=0, now=clock())
        model = rows(path, "SELECT * FROM crew_trades")[0]
        decisions = pl.enter(path, lease=rt.lease, admission=admission, model_trade=model,
                             timeframe="5m", quote=quote(clock()), df_5m=bars([24000.0]), now=clock(),
                             settings=SETTINGS, cutoff="15:10")
        assert all(d["reasons"] == ["MODEL_NOT_OWNED_TRACKING"] for d in decisions)
        assert rows(path, "SELECT * FROM crew_paper_trades") == []
        assert rows(path, "SELECT * FROM crew_paper_events") == []
        assert len(rows(path, "SELECT * FROM crew_paper_risk_decisions")) == 2
    finally:
        rt.stop()


def test_v2_multi_date_historical_and_live_replay_reconciles_across_tariff_regimes(path):
    from app.trading import admission as adm
    from app.trading.paper_risk import DeltaEvidence

    cj.migrate(path)
    sessions = [
        ("2025-11-06", True, pf.HISTORICAL_BACKTEST_POLICY_Oct2024, [24000.0, 24020.0, 24045.0, 24075.0, 24110.0]),
        ("2026-03-16", True, pf.HISTORICAL_BACKTEST_POLICY_Mar2026, [24100.0, 24101.0, 24100.5, 24101.2, 24100.8]),
        ("2026-04-15", True, pf.HISTORICAL_BACKTEST_POLICY_Apr2026, [24200.0, 24160.0, 24120.0, 24080.0, 24040.0]),
        ("2026-11-06", False, pf.POLICY_ID, [24000.0, 24010.0, 24025.0, 24040.0, 24060.0]),
    ]
    for day, is_hist, expected_policy_id, closes in sessions:
        t_entry = datetime.fromisoformat(f"{day}T10:19:00+05:30")
        t_partial = t_entry + timedelta(seconds=5)
        t_close = t_entry + timedelta(seconds=10)
        lease = cs.acquire(path, now=t_entry)
        try:
            cs.recover(path, lease, now=t_entry)
            pl.ensure_accounts(path, lease=lease, capital=CAPITAL, now=t_entry)
            pl.recover(path, lease=lease, now=t_entry)

            chain_entry = book(t_entry, bid=10.0, ask=10.4)
            q_entry = parse_chain_leg(chain_entry["contracts"][0], "CE", "2026-11-10", t_entry)
            cand = adm.SignalCandidate(
                market_day=day,
                instrument="NIFTY",
                strategy_id="Liquidity Sweep [5M]",
                strategy_version=reg.STRATEGY_VERSION,
                signal_candle=f"{day}T10:15:00+05:30",
                direction="CALL",
                setup_id="5m|24000.0",
            )
            evaluated = adm.evaluate(cand, q_entry, now=t_entry, settings=SETTINGS)
            persisted = adm.persist(path, evaluated, lease=lease, now=t_entry)
            opened = adm.open_research_trade(
                path,
                persisted,
                lease=lease,
                model_lots=3,
                entry_spot=24000.0,
                stop_spot=23995.0,
                target_spot=24060.0,
                execution_policy="research-exec-v1-monotonic-stop",
                now=t_entry,
            )
            df_day = bars(closes, start="09:55", day=day)
            delta = DeltaEvidence(Decimal("0.5"), t_entry, q_entry.tradingsymbol, "synthetic")

            if is_hist:
                blocked = pr.size(
                    quote=q_entry,
                    evidence=delta,
                    trusted_sources=frozenset({"synthetic"}),
                    now=t_entry,
                    settings=SETTINGS,
                    underlying_entry=Decimal("24000.0"),
                    underlying_stop=Decimal("23995.0"),
                    stop_budget=Decimal("1000.00"),
                    gross_daily_loss=Decimal("0.00"),
                    existing_reservations=Decimal("0.00"),
                    available_cash=CAPITAL,
                    allow_historical_brokerage_assumption=False,
                )
                assert blocked.entry is None and blocked.reason == "COST_POLICY_UNVERIFIED_DATE"

            decisions = pl.enter(
                path,
                lease=lease,
                admission=persisted["admission"],
                model_trade=opened["trade"],
                timeframe="5m",
                quote=q_entry,
                df_5m=df_day,
                now=t_entry,
                settings=SETTINGS,
                cutoff="15:10",
                delta_evidence=delta,
                trusted_delta_sources=frozenset({"synthetic"}),
                allow_historical_brokerage_assumption=is_hist,
            )
            assert all(d["outcome"] == "OPENED" for d in decisions)

            pl.partial(
                path,
                lease=lease,
                model_trade_id=opened["trade"]["trade_id"],
                chain=book(t_partial, bid=11.0, ask=11.4),
                index_spot=24030.0,
                reason="LIQUIDITY_HOP_PROMOTED",
                now=t_partial,
                settings=SETTINGS,
            )
            cj.exit_trade(
                path,
                lease=lease,
                trade_id=opened["trade"]["trade_id"],
                expected_version=opened["trade"]["version"],
                reason="TARGET_HIT",
                signal_candle=f"{day}T10:15:00+05:30",
                index_levels={"spot": 24060.0},
                model_points=60.0,
                model_r=2.0,
                now=t_close,
            )
            pl.close(
                path,
                lease=lease,
                model_trade_id=opened["trade"]["trade_id"],
                chain=book(t_close, bid=12.0, ask=12.4),
                index_spot=24060.0,
                reason="TARGET_HIT",
                now=t_close,
                settings=SETTINGS,
            )
        finally:
            cs.release(path, lease, now=t_close)

        day_trades = rows(path, "SELECT * FROM crew_paper_trades WHERE market_day=? AND account_id=?", (day, IND))
        assert len(day_trades) == 1 and day_trades[0]["state"] == "CLOSED"
        day_events = rows(
            path,
            "SELECT * FROM crew_paper_events WHERE paper_trade_id=? ORDER BY seq",
            (day_trades[0]["paper_trade_id"],),
        )
        assert [e["event_type"] for e in day_events] == ["ENTRY", "PARTIAL_EXIT", "EXIT"]
        for ev in day_events:
            c_json = json.loads(ev["charges"])
            assert c_json["policy"]["policy_id"] == expected_policy_id
            if is_hist:
                assert c_json["policy"]["brokerage_provenance"] == pf.HISTORICAL_BROKERAGE_PROVENANCE
            else:
                assert "brokerage_provenance" not in c_json["policy"]

    risk_rows = rows(
        path,
        "SELECT * FROM crew_paper_risk_decisions WHERE account_id=? ORDER BY rowid",
        (IND,),
    )
    assert len(risk_rows) == 4
    recorded_policies = [json.loads(r["evidence"])["cost_policy_id"] for r in risk_rows]
    assert recorded_policies == [s[2] for s in sessions]
    for r in risk_rows:
        rv = json.loads(r["evidence"])["regime_v2"]
        assert rv["version"] == rg.REGIME_VERSION
        assert rv["structure_regime"] in ("TREND_UP", "TREND_DOWN", "COMPRESSION_CHOP", "RANGE_ROTATION", "INSUFFICIENT_DATA")
        assert "/" in rv["legacy_regime"] or rv["legacy_regime"] == "UNKNOWN"

    assert pl.check_consistency(path) == []
    final_stats = stats(path, IND)
    assert final_stats["completed"] == 4
    assert final_stats["accounting"]["charge_reconciliation"] == "EXACT"
    assert Decimal(final_stats["accounting"]["reserved_worst_case_inr"]) == 0
    assert Decimal(final_stats["accounting"]["cash_inr"]) == CAPITAL + Decimal(final_stats["accounting"]["realized_pnl_inr"])


def test_enabled_runtime_sends_each_paper_notification_to_group_once(path, monkeypatch):
    from app import telegram_transport as tt

    sent = []

    class GroupTransport:
        def __init__(self, token):
            assert token == 'paper-test-token'

        def send(self, chat_id, text):
            assert chat_id == '-100123' and 'PAPER ONLY' in text
            sent.append(text)
            return tt.DeliveryOutcome(tt.DELIVERED, 200, True, len(sent))

    monkeypatch.setattr(tt, 'TelegramTransport', GroupTransport)
    monkeypatch.setattr(tt, 'delivery_authorized', lambda chat_id: chat_id == '-100123')
    clock = Clock('10:19')
    rt = runtime(path, PaperFeed([setup()]), clock)
    rt.settings = rt.settings.model_copy(update={
        'telegram_delivery_enabled': True, 'signals_telegram_chat_id': '-100123',
        'alert_telegram_bot_token': 'paper-test-token'})
    rt.start()
    try:
        rt.tick()
        assert sent and any('PAPER TRADE' in text for text in sent)
        publications = outbox(path)
        assert all(row['state'] == 'ACKED' and row['chat_id'] == '-100123' for row in publications)
        ledger = rows(path, 'SELECT * FROM crew_paper_events ORDER BY event_id')
        count = len(sent)
        rt.tick()
        assert len(sent) == count
        assert rows(path, 'SELECT * FROM crew_paper_events ORDER BY event_id') == ledger
    finally:
        rt.stop()


@pytest.mark.parametrize("mutation", [
    "duplicate", "conflict_first", "conflict_last", "missing_underlying", "underlying",
    "expiry", "strike", "side", "symbol", "lot", "provider", "missing_provider",
])
@pytest.mark.parametrize("action", ["partial", "close"])
def test_v2_exit_admission_identity_rejection_is_financially_neutral(path, mutation, action):
    from copy import deepcopy
    from dataclasses import replace
    from app.trading.paper_risk import DeltaEvidence

    clock = Clock("10:19")
    base = PaperFeed([setup(stop=23995)], bid=10, ask=10.4)

    def feed(now):
        view = base(now)
        symbol = quote(now, bid=10, ask=10.4).tradingsymbol
        return replace(view, delta_evidence={symbol: DeltaEvidence(Decimal("0.5"), now, symbol, "synthetic")})

    rt = rr.ResearchRuntime(path, feed, settings=SETTINGS, clock=clock,
                            trusted_delta_sources=frozenset({"synthetic"}))
    rt.start()
    try:
        rt.tick()
        trade = rows(path, "SELECT * FROM crew_paper_trades WHERE account_id=?", (IND,))[0]
        assert trade["lots_initial"] == 3
        before = stats(path, IND)["accounting"]
        chain = book(clock(), bid=10, ask=10.4)
        row = chain["contracts"][0]
        if mutation in ("duplicate", "conflict_first", "conflict_last"):
            other = deepcopy(row)
            if mutation != "duplicate":
                other["ce"]["growwContractId"] = "OTHER"
                other["ce"]["liveData"]["bid"] = 9
            chain["contracts"].insert(0 if mutation == "conflict_first" else 1, other)
        elif mutation == "missing_underlying":
            row.pop("underlying")
        elif mutation == "underlying":
            row["underlying"] = "BANKNIFTY"
        elif mutation == "expiry":
            chain["expiry"] = "2026-11-17"
        elif mutation == "strike":
            row["strike"] = 24200
        elif mutation == "side":
            row["pe"] = row.pop("ce")
        elif mutation == "symbol":
            row["ce"]["tradingSymbol"] = "OTHER"
        elif mutation == "lot":
            row["ce"]["marketLot"] = 75
        elif mutation == "provider":
            row["ce"]["growwContractId"] = "OTHER"
        else:
            row["ce"].pop("growwContractId")
        args = dict(lease=rt.lease, model_trade_id=trade["model_trade_id"], chain=chain,
                    index_spot=24020, reason="IDENTITY_TEST", now=clock(), settings=SETTINGS)
        getattr(pl, action)(path, **args)
        assert rows(path, "SELECT * FROM crew_paper_events WHERE side='SELL'") == []
        assert stats(path, IND)["accounting"] == before
        held = rows(path, "SELECT * FROM crew_paper_trades WHERE account_id=?", (IND,))[0]
        assert held["lots_open"] == trade["lots_open"]
        snapshot = paper(path)
        getattr(pl, action)(path, **args)
        assert paper(path) == snapshot
        assert stats(path, IND)["accounting"] == before
        assert pl.check_consistency(path) == []
    finally:
        rt.stop()


@pytest.mark.parametrize("mutation", ["duplicate", "conflict_first", "conflict_last", "missing_underlying", "underlying"])
def test_v2_ambiguous_entry_never_creates_financial_events(path, mutation):
    from copy import deepcopy
    from dataclasses import replace
    clock = Clock("10:19")
    base = PaperFeed([setup(stop=23995)])

    def feed(now):
        view = base(now)
        chain = deepcopy(view.chain)
        row = chain["contracts"][0]
        if mutation == "missing_underlying":
            row.pop("underlying")
        elif mutation == "underlying":
            row["underlying"] = "BANKNIFTY"
        else:
            other = deepcopy(row)
            if mutation != "duplicate":
                other["ce"]["growwContractId"] = "OTHER"
            chain["contracts"].insert(0 if mutation == "conflict_first" else 1, other)
        return replace(view, chain=chain)

    rt = runtime(path, feed, clock)
    rt.start()
    try:
        rt.tick()
        assert rows(path, "SELECT * FROM crew_paper_events") == []
        assert rows(path, "SELECT * FROM crew_paper_trades") == []
        snapshot = paper(path)
        rt.tick()
        assert paper(path) == snapshot
    finally:
        rt.stop()
