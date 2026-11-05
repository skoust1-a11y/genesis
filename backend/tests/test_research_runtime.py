"""S1-P6 shadow-mode runtime. Temp journals, a fake clock and fake market data only."""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import itertools
import json
import socket
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from app.config import get_settings
from app.db import crew_journal as cj
from app.db import crew_outbox as ob
from app.db import crew_session as cs
from app.trading import admission as adm
from app.trading import research_runtime as rr
from app.trading.crew.entry_agent import EntryAgent
from app.trading.crew.holding_agent import HoldingAgent
from app.trading.crew.manager import TradeManager
from app.trading.scanner_crew.detector_agent import CandidateSetup, DetectorAgent
from app.trading.scanner_crew.levels_agent import SessionLevels

DAY = "2026-11-06"
BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
RESEARCH = ("CAPITAL_UNAVAILABLE", "CASH_UNAVAILABLE", "MAX_LOSS_UNVERIFIED")


class Clock:
    def __init__(self, hhmm: str, day: str = DAY):
        self.t = datetime.fromisoformat(f"{day}T{hhmm}:00+05:30")

    def __call__(self) -> datetime:
        return self.t

    def at(self, hhmm: str, day: str = DAY) -> None:
        self.t = datetime.fromisoformat(f"{day}T{hhmm}:00+05:30")

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


def walk(rt, clock, hhmm=None, seconds=None):
    """Move fake time forward, heartbeating every 10 s as the runtime's heartbeat thread would."""
    target = clock.t + timedelta(seconds=seconds) if seconds else datetime.fromisoformat(f"{DAY}T{hhmm}:00+05:30")
    while clock.t < target:
        clock.t = min(target, clock.t + timedelta(seconds=10))
        cs.heartbeat(rt.path, rt.lease, now=clock.t)


def setup(trigger=24000.0, stop=23970.0, target=24060.0, ts=f"{DAY} 10:15", strike="NIFTY 24100 CE", **kw):
    return CandidateSetup(**{
        "strategy": "Liquidity Sweep [5M]", "symbol": "NIFTY", "direction": "CALL", "trigger_price": trigger,
        "tentative_stop": stop, "tentative_target": target, "timeframe": "5m", "timestamp": ts,
        "rationale": "fixture", "news_sentiment_score": 0.0, "event_risk_cleared": True,
        "is_valid_for_trade_crew": True, "suggested_strike": strike, "risk_points": abs(trigger - stop),
        "target_points": abs(target - trigger), "rr_ratio": 2.0, **kw})


def chain(fetched_at, strikes=(24100,), side="ce"):
    rows = [{"strike": s, side: {"liveData": {"ltp": 110.5, "bid": 110.0, "ask": 111.0}, "marketLot": 75,
                                 "tradingSymbol": f"NIFTY26N10{s}{side.upper()}"}} for s in strikes]
    return {"contracts": rows, "expiry": "2026-11-10", "fetched_at": fetched_at}


def bars(closes, start="10:10", day=DAY):
    t0 = datetime.fromisoformat(f"{day}T{start}")
    out = []
    for i, c in enumerate(closes):
        out.append({"date": (t0 + timedelta(minutes=5 * i)).strftime("%Y-%m-%d %H:%M"), "open": c - 1.0,
                    "high": c + 2.0, "low": c - 2.0, "close": float(c), "volume": 1000.0})
    return pd.DataFrame(out)


class Feed:
    def __init__(self, setups=None, closes=(24000.0,), start="10:10", quote="fresh", strikes=(24100,), side="ce"):
        self.setups, self.closes, self.start, self.quote = list(setups or []), list(closes), start, quote
        self.strikes, self.side = strikes, side
        self.calls = 0

    def __call__(self, now):
        self.calls += 1
        fetched = {"fresh": now, "stale": now - timedelta(seconds=11), "none": None}[self.quote]
        return rr.MarketView(df_5m=bars(self.closes, self.start), setups=self.setups,
                             chain=None if fetched is None else chain(fetched, self.strikes, self.side), key_levels=[])


def runtime(path, feed, clock, **kw):
    return rr.ResearchRuntime(path, feed, settings=get_settings(), clock=clock, **kw)


@pytest.fixture
def path(tmp_path):
    return tmp_path / "crew_research.db"


def rows(path, sql, args=()):
    conn = cj.connect_readonly(path)
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def audit(path):
    return [e["event_type"] for e in rows(path, "SELECT event_type FROM crew_session_events ORDER BY id")]


def snapshot(path):
    return {t: rows(path, f"SELECT * FROM {t}") for t in ("crew_admissions", "crew_trades", "crew_events", "crew_outbox")}


def stops(path, tid):
    """The protective stop after each journal event of one trade, in order."""
    out = []
    for e in cj.get_trade_events(path, tid):
        p = e["payload"]
        out.append(p.get("stop_spot", (p.get("index_levels") or {}).get("stop", out[-1] if out else None)))
    return out


def never_loosens(direction, levels):
    return all((b >= a) if direction == "CALL" else (b <= a) for a, b in itertools.pairwise(levels))


def prior_session(path, day, hhmm, setup_id="prior"):
    """A crashed earlier session: lease never released, one open model trade."""
    cj.migrate(path)
    now = datetime.fromisoformat(f"{day}T{hhmm}:00+05:30")
    lease = cs.acquire(path, now=now)
    a = cj.record_admission(path, lease=lease, admission_class="RESEARCH_ONLY", reasons=list(RESEARCH),
                            market_day=day, instrument="NIFTY", strategy_id="Liquidity Sweep [5M]",
                            strategy_version=rr.STRATEGY_VERSION, signal_candle=now.isoformat(), direction="CALL",
                            setup_id=setup_id, now=now)
    trade = cj.open_trade(path, lease=lease, admission_id=a["admission"]["admission_id"], model_lots=3,
                          entry_spot=24000, stop_spot=23970, target_spot=24060, now=now)
    return lease, trade["trade"]["trade_id"]


# --- 1, 2, 13: startup, lease, recovery before scanning, prior-day expiry ----------------------------

def test_01_startup_takes_the_lease_and_recovers_before_any_scan(path):
    _, old_trade = prior_session(path, "2026-11-05", "11:00")
    feed, clock = Feed(), Clock("10:20")
    rt = runtime(path, feed, clock)
    report = rt.start()
    try:
        assert feed.calls == 0
        assert report["expired"] == [old_trade] and report["orphaned_open"] == []
        assert (report["session_id"], report["market_day"], report["generation"]) == ("S_20261106_G2", DAY, 2)
        lease = rows(path, "SELECT * FROM crew_lease")[0]
        assert lease["session_id"] == rt.lease.session_id and lease["released_at"] is None
        assert audit(path)[-2:] == ["TAKEOVER", "RECOVERED"]
        expired = rows(path, "SELECT * FROM crew_trades WHERE trade_id=?", (old_trade,))[0]
        assert expired["state"] == "EXPIRED_UNRESOLVED" and expired["model_lots_open"] == 3
        last = cj.get_trade_events(path, old_trade)[-1]
        assert last["event_type"] == "EXPIRED_UNRESOLVED" and last["payload"]["reason"] == "PRIOR_MARKET_DAY"
        assert not {"model_points", "model_r", "exit"} & set(last["payload"])
    finally:
        rt.stop()
    assert audit(path)[-1] == "RELEASED"
    assert rows(path, "SELECT end_reason FROM crew_sessions WHERE session_id='S_20261106_G2'")[0]["end_reason"] == "SHUTDOWN"


def test_02_an_inconsistent_journal_refuses_startup_and_writes_nothing(path):
    prior_session(path, DAY, "09:30")
    conn = cj.connect(path)
    conn.execute("INSERT INTO crew_trades (trade_id, signal_id, admission_class, market_day, state, strategy,"
                 " direction, model_lots_initial, model_lots_open, opened_at, closed_at, legacy_ref)"
                 " VALUES ('L_bad', ?, 'RESEARCH_ONLY', '2026-10-30', 'QUARANTINED', 'legacy', 'PUT', 1, 1, ?, ?, 'x')",
                 ("f" * 64, "2026-10-30T10:00:00+05:30", "2026-10-30T10:00:00+05:30"))
    conn.close()
    before = snapshot(path)
    feed, clock = Feed([setup()]), Clock("10:20")
    with pytest.raises(cj.ConsistencyError):
        runtime(path, feed, clock).start()
    assert snapshot(path) == before and feed.calls == 0
    assert audit(path)[-2:] == ["TAKEOVER", "RELEASED"] and "RECOVERED" not in audit(path)


def test_02b_no_writer_can_commit_between_the_consistency_check_and_recovery(path, monkeypatch):
    prior_session(path, "2026-11-05", "11:00")
    real, blocked = cj._consistency_findings, []

    def check_then_race(conn):
        findings = real(conn)
        racer = sqlite3.connect(path, timeout=0.1)
        try:
            racer.execute("INSERT INTO crew_meta (key, value) VALUES ('race', 'x')")
        except sqlite3.OperationalError as e:
            blocked.append(str(e))
        finally:
            racer.close()
        return findings

    monkeypatch.setattr(cj, "_consistency_findings", check_then_race)
    rt = runtime(path, Feed(), Clock("10:20"))
    rt.start()
    rt.stop()
    assert blocked and "locked" in blocked[0]


# --- 3: duplicate runtime -----------------------------------------------------------------------------

def test_03_a_second_concurrent_runtime_is_refused(path):
    clock = Clock("10:20")
    first = runtime(path, Feed(), clock)
    first.start()
    try:
        second_feed = Feed([setup()])
        with pytest.raises(cs.LeaseHeld):
            runtime(path, second_feed, clock).start()
        assert second_feed.calls == 0
        assert rows(path, "SELECT session_id FROM crew_lease")[0]["session_id"] == first.lease.session_id
        assert audit(path) == ["ACQUIRED", "RECOVERED", "REFUSED"]
        assert first.tick()["admissions"] == []
    finally:
        first.stop()


# --- 4, 5: heartbeat and lease loss ------------------------------------------------------------------

def test_04_heartbeat_renews_inside_the_ttl(path):
    with pytest.raises(ValueError):
        runtime(path, Feed(), Clock("10:20"), heartbeat_seconds=11)
    assert rr.HEARTBEAT_SECONDS * 3 <= cs.LEASE_SECONDS
    clock = Clock("10:20")
    rt = runtime(path, Feed([setup()]), clock, heartbeat_seconds=0.02)
    rt.start()
    try:
        first = rows(path, "SELECT expires_at FROM crew_lease")[0]["expires_at"]
        for _ in range(3):  # 60 s of fake time, past two TTLs, renewed every 20 s
            clock.advance(20)
            deadline = time.monotonic() + 2
            while rows(path, "SELECT expires_at FROM crew_lease")[0]["expires_at"] <= first:
                assert time.monotonic() < deadline, "heartbeat did not renew"
                time.sleep(0.01)
            first = rows(path, "SELECT expires_at FROM crew_lease")[0]["expires_at"]
        clock.at("10:21")
        time.sleep(0.1)
        assert rt.tick()["admissions"][0]["class"] == "RESEARCH_ONLY"
    finally:
        rt.stop()
    assert not rt.lost.is_set()


def test_05_losing_the_lease_mid_scan_stops_processing(path):
    clock = Clock("10:20")
    rt = runtime(path, Feed([setup()]), clock)
    rt.start()
    rt._stop.set()  # freeze the heartbeat: the process stalls past its TTL
    rt._beater.join()
    clock.advance(31)
    thief = cs.acquire(path, now=clock())
    with pytest.raises(cs.LeaseLost):
        rt.tick()
    assert rows(path, "SELECT * FROM crew_admissions") == []
    assert rt.run(threading.Event(), interval=0, once=True) == 3
    assert rt.lost.is_set()
    lease = rows(path, "SELECT * FROM crew_lease")[0]
    assert lease["session_id"] == thief.session_id and lease["released_at"] is None
    assert audit(path)[-1] == "FENCED"


def test_05b_a_fenced_heartbeat_stops_the_next_tick_before_market_data(path):
    clock = Clock("10:20")
    feed = Feed([setup()])
    rt = runtime(path, feed, clock, heartbeat_seconds=0.02)
    rt.start()
    rt._stop.set()
    rt._beater.join()
    clock.advance(31)
    cs.acquire(path, now=clock())
    rt._stop.clear()
    beater = threading.Thread(target=rt._beat)
    beater.start()
    beater.join(timeout=2)
    assert rt.lost.is_set()
    with pytest.raises(cs.LeaseLost):
        rt.tick()
    assert feed.calls == 0


# --- 6-10: admission ----------------------------------------------------------------------------------

def test_06_repeat_scans_of_one_signal_are_idempotent(path):
    clock = Clock("10:20")
    rt = runtime(path, Feed([setup()]), clock)
    rt.start()
    try:
        first = rt.tick()["admissions"][0]
        walk(rt, clock, seconds=10)
        second = rt.tick()["admissions"][0]
    finally:
        rt.stop()
    assert first["replayed"] is False and first["trade"].startswith("T_20261106_")
    assert second == {**first, "replayed": True, "trade": "EXISTS"}
    assert len(rows(path, "SELECT * FROM crew_admissions")) == 1
    assert len(rows(path, "SELECT * FROM crew_trades")) == 1


def test_07_invalid_ineligible_and_expired_setups_record_nothing(path):
    clock = Clock("10:20")
    invalid = [setup(ts="not a time"), setup(trigger=24010.0, direction="SIDEWAYS")]
    ignored = [setup(trigger=24020.0, is_valid_for_trade_crew=False), setup(trigger=24030.0, ts=f"{DAY} 09:30")]
    rt = runtime(path, Feed(invalid + ignored), clock)
    rt.start()
    try:
        result = rt.tick()["admissions"]
    finally:
        rt.stop()
    assert result == [{"strategy": "Liquidity Sweep [5M]", "recorded": False, "reasons": ["SIGNAL_INVALID"]}] * 2
    assert rows(path, "SELECT * FROM crew_admissions") == [] and rows(path, "SELECT * FROM crew_trades") == []


def test_08_a_fresh_quote_is_research_only_never_actionable(path):
    clock = Clock("10:20")
    rt = runtime(path, Feed([setup()]), clock)
    rt.start()
    try:
        assert rt.tick()["admissions"][0]["class"] == "RESEARCH_ONLY"
    finally:
        rt.stop()
    stored = rows(path, "SELECT * FROM crew_admissions")[0]
    assert stored["class"] == "RESEARCH_ONLY"
    assert set(RESEARCH) <= set(json.loads(stored["reasons"]))
    inputs = json.loads(stored["inputs"])
    assert inputs["strategy_version"] == rr.STRATEGY_VERSION and inputs["strategy_id"] == "Liquidity Sweep [5M]"
    assert inputs["contract"]["tradingsymbol"] == "NIFTY26N1024100CE" and inputs["contract"]["strike"] == 24100
    assert inputs["quote"]["ltp"] == "110.5" and inputs["quote"]["grade"] in ("FRESH_REFERENCE", "EXECUTABLE")
    assert inputs["capital"]["verified"] is False


def test_08b_an_actionable_admission_stops_the_runtime_unrecorded(path, monkeypatch):
    real = adm.evaluate
    monkeypatch.setattr(adm, "evaluate", lambda *a, **k: dataclasses.replace(
        real(*a, **k), admission_class="ACTIONABLE", reasons=()))
    rt = runtime(path, Feed([setup()]), Clock("10:20"))
    rt.start()
    assert rt.run(threading.Event(), interval=0, once=True) == 1
    assert rows(path, "SELECT * FROM crew_admissions") == []
    assert audit(path)[-1] == "RELEASED"


def test_09_missing_or_stale_quotes_fail_closed(path):
    clock = Clock("10:20")
    missing, stale, wrong = setup(), setup(trigger=24005.0), setup(trigger=24010.0, strike="NIFTY 24150 CE")
    feed = Feed([missing], quote="none")
    rt = runtime(path, feed, clock)
    rt.start()
    try:
        assert rt.tick()["admissions"][0]["class"] == "REJECTED"
        feed.setups, feed.quote = [stale], "stale"
        assert rt.tick()["admissions"][0]["class"] == "REJECTED"
        feed.setups, feed.quote = [wrong], "fresh"
        assert rt.tick()["admissions"][0]["class"] == "REJECTED"
        feed.setups = [missing]  # a quote arriving later does not overturn the first decision
        later = rt.tick()["admissions"][0]
        assert later["class"] == "REJECTED" and later["replayed"] is True and "trade" not in later
    finally:
        rt.stop()
    reasons = sorted(r["reasons"] for r in rows(path, "SELECT reasons FROM crew_admissions"))
    assert reasons == ['["QUOTE_MISSING"]', '["QUOTE_MISSING"]', '["QUOTE_STALE"]']
    assert rows(path, "SELECT * FROM crew_trades") == []


def test_10_a_research_only_admission_opens_one_model_trade(path):
    clock = Clock("10:20")
    rt = runtime(path, Feed([setup()]), clock)
    rt.start()
    try:
        tid = rt.tick()["admissions"][0]["trade"]
    finally:
        rt.stop()
    trade = rows(path, "SELECT * FROM crew_trades")[0]
    assert trade["trade_id"] == tid and trade["owner_session_id"] == rt.lease.session_id
    assert (trade["admission_class"], trade["state"], trade["model_lots_initial"]) == ("RESEARCH_ONLY", "TRACKING", 3)
    assert (trade["entry_spot"], trade["stop_spot"], trade["target_spot"]) == (24000.0, 23970.0, 24060.0)
    assert (trade["contract"], trade["expiry"], trade["lot_size_ref"]) == ("NIFTY26N1024100CE", "2026-11-10", 75)
    assert trade["opened_at"] == f"{DAY}T10:15:00+05:30"
    entry = cj.get_trade_events(path, tid)[0]
    assert set(entry["payload"]) == {"contract", "direction", "entry_spot", "execution_policy", "expiry",
                                     "instrument", "model_lots", "setup_id", "signal_candle", "stop_spot",
                                     "strategy_id", "strategy_version", "target_spot"}
    assert entry["payload"]["execution_policy"] == rr.EXECUTION_POLICY


# --- 11: model exits ----------------------------------------------------------------------------------

def test_11_liquidity_hop_books_a_partial_then_the_ratcheted_stop_closes(path):
    clock = Clock("10:19")
    feed = Feed([setup()], closes=[24000.0])
    rt = runtime(path, feed, clock)
    rt.start()
    try:
        tid = rt.tick()["admissions"][0]["trade"]
        feed.closes = [24000.0, 24065.0]
        walk(rt, clock, "10:20")
        assert rt.tick()["exits"] == [{"trade_id": tid, "action": rr.HOP, "lots_after": 2}]
        # One milestone per tick on the same bar, as TradeManager. Both would loosen the 24057 hop stop
        # (to 24000, then 24024); the monotonic policy records each milestone and holds the stop.
        walk(rt, clock, seconds=10)
        assert rt.tick()["exits"] == [{"trade_id": tid, "action": rr.MILESTONE_BE, "stop": 24057.0}]
        walk(rt, clock, seconds=10)
        assert rt.tick()["exits"] == [{"trade_id": tid, "action": rr.MILESTONE_2R, "stop": 24057.0}]
        walk(rt, clock, seconds=10)
        held = snapshot(path)
        assert rt.tick()["exits"] == [{"trade_id": tid, "action": "HOLD"}]
        assert snapshot(path) == held
        feed.closes = [24000.0, 24065.0, 24020.0]
        walk(rt, clock, "10:25")
        exit_ = rt.tick()["exits"][0]
    finally:
        rt.stop()
    assert exit_ == {"trade_id": tid, "action": "TRAILING_STOP_HIT", "model_points": 57.0}
    events = cj.get_trade_events(path, tid)
    assert [e["event_type"] for e in events] == ["MODEL_ENTRY", "MODEL_LEVELS_UPDATED", "MODEL_PARTIAL_EXIT",
                                                 "MODEL_LEVELS_UPDATED", "MODEL_LEVELS_UPDATED", "MODEL_EXIT"]
    assert (events[1]["payload"]["stop_spot"], events[1]["payload"]["target_spot"]) == (24057.0, 24105.0)
    assert (events[2]["payload"]["lots_before"], events[2]["payload"]["lots_after"]) == (3, 2)
    assert [(e["payload"]["reason"], e["payload"]["stop_spot"]) for e in events[3:5]] == [
        (rr.MILESTONE_BE, 24057.0), (rr.MILESTONE_2R, 24057.0)]
    assert events[5]["payload"]["model_points"] == 57.0 and events[5]["payload"]["model_r"] == 1.9
    assert events[5]["payload"]["index_levels"] == {"spot": 24020.0, "stop": 24057.0, "target": 24105.0}
    assert stops(path, tid) == [23970.0, 24057.0, 24057.0, 24057.0, 24057.0, 24057.0]
    trade = rows(path, "SELECT * FROM crew_trades")[0]
    assert trade["state"] == "CLOSED" and trade["model_lots_open"] == 0
    assert cj.check_consistency(path) == []


def test_11b_an_interrupted_hop_completes_its_partial_on_the_next_tick(path, monkeypatch):
    clock = Clock("10:19")
    feed = Feed([setup()], closes=[24000.0])
    rt = runtime(path, feed, clock)
    rt.start()
    try:
        tid = rt.tick()["admissions"][0]["trade"]
        feed.closes = [24000.0, 24065.0]
        walk(rt, clock, "10:20")
        real = cj.partial_exit
        monkeypatch.setattr(cj, "partial_exit", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("io")))
        with pytest.raises(sqlite3.OperationalError):
            rt.tick()
        monkeypatch.setattr(cj, "partial_exit", real)
        walk(rt, clock, seconds=10)
        assert rt.tick()["exits"] == [{"trade_id": tid, "action": "HOP_PARTIAL_COMPLETED"}]
    finally:
        rt.stop()
    trade = rows(path, "SELECT * FROM crew_trades")[0]
    assert (trade["state"], trade["model_lots_open"], trade["target_spot"]) == ("PARTIALLY_EXITED", 2, 24105.0)


# --- 12, 14: crash, restart, orphans -----------------------------------------------------------------

def test_12_restart_after_a_crash_is_idempotent_and_never_adopts_the_orphan(path):
    clock = Clock("10:19")
    feed = Feed([setup()], closes=[24000.0])
    first = runtime(path, feed, clock)
    first.start()
    tid = first.tick()["admissions"][0]["trade"]
    first._stop.set()  # crash: heartbeat dies, the lease is never released
    first._beater.join()
    clock.advance(31)
    orphan_before = cj.get_trade_events(path, tid)

    second = runtime(path, feed, clock)
    report = second.start()
    try:
        assert [t["trade_id"] for t in report["orphaned_open"]] == [tid]
        assert report["missing_model_entries"] == []
        feed.closes = [24000.0, 23950.0]  # through the orphan's stop
        walk(second, clock, "10:21")
        result = second.tick()
        assert result["exits"] == []
        assert result["admissions"][0]["trade"] == "PRIOR_SESSION"
    finally:
        second.stop()
    assert cj.get_trade_events(path, tid) == orphan_before
    orphan = rows(path, "SELECT state, owner_session_id FROM crew_trades WHERE trade_id=?", (tid,))[0]
    assert orphan == {"state": "TRACKING", "owner_session_id": first.lease.session_id}
    assert len(rows(path, "SELECT * FROM crew_admissions")) == 1 and len(rows(path, "SELECT * FROM crew_trades")) == 1
    assert cj.check_consistency(path) == []
    stats = rr.finalized_results(path)
    assert (stats["trades"], stats["model_points"], stats["excluded"]) == (0, 0, {tid: "TRACKING"})


# --- 15: UNKNOWN publications --------------------------------------------------------------------------

def test_15_unknown_publications_are_reported_and_never_resent(path):
    lease, _ = prior_session(path, DAY, "09:30")
    at = datetime.fromisoformat(f"{DAY}T09:30:00+05:30")
    pid = ob.enqueue_publication(path, lease=lease, kind="OPS", purpose="startup", body="online",
                                 chat_id="-100123", now=at)["publication"]["publication_id"]
    ob.claim_publication(path, pid, lease=lease, now=at)  # crash while SENDING
    clock = Clock("10:20")
    rt = runtime(path, Feed([setup()]), clock)
    report = rt.start()
    try:
        assert report["inflight_unknown"] == [pid] and report["unknown_publications"] == [pid]
        rt.tick()
        with pytest.raises(cj.InvalidTransition):
            ob.claim_publication(path, pid, lease=rt.lease, now=clock())
    finally:
        rt.stop()
    outbox = rows(path, "SELECT publication_id, state FROM crew_outbox")
    assert outbox == [{"publication_id": pid, "state": "UNKNOWN"}]


# --- 16, 17, 18, 20: no Telegram, no broker, no production or legacy writes -----------------------------

FORBIDDEN_MODULES = ("app.alerts", "app.telegram_transport", "app.db.crew_outbox", "app.db.trade_store",
                     "app.trading.crew.alert_agent", "app.trading.crew.journal_agent", "app.trading.crew.manager",
                     "app.trading.crew.risk_agent", "app.trading.execution_crew", "app.trading.orders",
                     "app.trading.post_market_crew", "app.trading.pre_market_crew", "app.paper", "requests",
                     "httpx", "urllib")
FORBIDDEN_NAMES = ("resolve_unknown", "quarantine_legacy_reference", "enqueue_publication", "deliver_publication",
                   "place_order", "_send", "send_message", "sendMessage")


def test_16_17_the_runtime_imports_no_publisher_broker_or_legacy_ledger():
    source = Path(rr.__file__).read_text()
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    bad = {m for m in imported if any(m == f or m.startswith(f + ".") for f in FORBIDDEN_MODULES)}
    assert not bad
    assert imported & {"app.brokers.groww"} == {"app.brokers.groww"}  # quotes page only, inside scanner_feed
    names = {n.id for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Name)} | \
            {n.attr for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Attribute)}
    assert not names & set(FORBIDDEN_NAMES)


def test_16_17_18_20_a_full_session_makes_no_network_call_and_touches_no_legacy_file(path, tmp_path, monkeypatch):
    def tree(root):
        return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}

    legacy_before = {d: tree(REPO / d) for d in ("trades", "reports")}
    connects, sends = [], []

    def blocked(self, addr):
        connects.append(addr)
        raise OSError("network blocked in shadow mode")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    import app.trading.crew.alert_agent as alert_mod
    import app.trading.execution_crew.execution_manager as exec_mod
    monkeypatch.setattr(alert_mod.AlertAgent, "_send", lambda *a, **k: sends.append(a))
    for name in dir(exec_mod.ExecutionManager):
        if not name.startswith("__") and callable(getattr(exec_mod.ExecutionManager, name)):
            monkeypatch.setattr(exec_mod.ExecutionManager, name, lambda *a, **k: sends.append(("broker", a)))
    monkeypatch.chdir(tmp_path)

    clock = Clock("10:19")
    feed = Feed([setup()], closes=[24000.0])
    rt = runtime(path, feed, clock)
    rt.start()
    rt.tick()
    feed.closes = [24000.0, 23960.0]
    walk(rt, clock, "10:20")
    assert rt.tick()["exits"][0]["action"] == "STOP_LOSS_HIT"
    rt.stop()

    assert connects == [] and sends == []
    assert {d: tree(REPO / d) for d in ("trades", "reports")} == legacy_before
    assert all(p.name.startswith("crew_research.db") for p in tmp_path.iterdir())  # no legacy trades/ or trades.db


def test_18_production_and_legacy_paths_are_refused_before_opening(tmp_path):
    for target in (REPO / "data" / "crew.db", REPO / "data" / "trades.db", REPO / "trades" / "crew.db",
                   BACKEND / "data" / "crew.db"):
        with pytest.raises(rr.StartupRefused):
            runtime(target, Feed(), Clock("10:20")).start()
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.execute("CREATE TABLE trades (id INTEGER)")
    conn.close()
    before = {p: p.read_bytes() for p in (empty, legacy)}
    for target in (empty, legacy):
        with pytest.raises(rr.StartupRefused):
            runtime(target, Feed(), Clock("10:20")).start()
    assert {p: p.read_bytes() for p in (empty, legacy)} == before


def test_scanner_feed_drops_the_forming_bar_and_stamps_the_chain():
    seen = {}

    class Scanner:
        feed_agent = SimpleNamespace(fetch_timeframe_candles=lambda tf: bars([24000.0, 24010.0, 24020.0]))

        def produce_scan_snapshot(self, **kw):
            seen.update(kw)
            return SimpleNamespace(actionable_setups=[setup()], levels=SimpleNamespace(all_key_levels=[24100.0]))

    clock = Clock("10:22")  # the 10:20 bar is still forming
    page = {"optionChain": {"optionContracts": chain(None)["contracts"], "aggregatedDetails": {"currentExpiry": "2026-11-10"}}}
    view = rr.scanner_feed(Scanner(), chain_source=lambda: page, clock=clock)(clock())
    assert list(view.df_5m["date"]) == [f"{DAY} 10:10", f"{DAY} 10:15"]
    assert seen["df_5m"] is view.df_5m and seen["current_time_str"] == "10:22"
    assert view.chain["fetched_at"] == clock() and view.chain["expiry"] == "2026-11-10"
    assert rr.setup_quote(view.chain, view.setups[0]).tradingsymbol == "NIFTY26N1024100CE"
    assert view.key_levels == [24100.0]


# --- 19: frozen strategy equivalence -----------------------------------------------------------------

FROZEN_SOURCES = {
    "app/trading/scanner_crew/detector_agent.py": "1fe20e02d35c6ba962f9ac691537d6c3fb3985a44c664c73b2bdc0e5b8e0881c",
    "app/liquidity_signals.py": "f8b49ca6dd7444197241777e8f5ce44a4ebccc1cd364872d9c25aac2ab556a68",
    "app/trading/crew/exit_agent.py": "163498f643887e7c8663588d1a9f2e6f8c7aef63d6397f474c5c5c084616bee1",
    "app/trading/crew/manager.py": "ac20fea40a6af1e82b236fd8df1a4ccd8c1ec394d936871f01d7475e83727ea3",
    "scripts/backtest_pdc_15ema.py": "62230eec054195a9df6860b66eab8494a988bf4be70ecf7ac73a0e1704050d6e",
}


def test_19a_frozen_rule_sources_are_unchanged_for_this_strategy_version():
    assert rr.STRATEGY_VERSION == "scanner-v0"
    for rel, digest in FROZEN_SOURCES.items():
        actual = hashlib.sha256((BACKEND / rel).read_bytes()).hexdigest()
        assert actual == digest, f"{rel} changed: bump STRATEGY_VERSION and re-pin"


def test_19b_scanner_setups_are_admitted_verbatim(path):
    detector_bars = [{"date": f"{DAY} 09:{30 + i * 5}", "open": 23250, "high": 23260, "low": 23240, "close": 23255}
                     for i in range(5)]
    detector_bars.append({"date": f"{DAY} 10:05", "open": 23272, "high": 23288, "low": 23260, "close": 23265})
    levels = SessionLevels(pdh=23350, pdl=23100, pdc=23200, pivot=23200, r1=23300, s1=23100,
                           eqh_levels=[23280.0], eql_levels=[], all_key_levels=[23280.0])
    found = DetectorAgent().scan_multi_timeframe_strategies(tf_bars_map={"5m": detector_bars}, levels=levels,
                                                            strategy_filter="equal")
    frozen = [s for s in found if s.is_valid_for_trade_crew]
    assert frozen
    strikes = [int(s.suggested_strike.split()[1]) for s in frozen]
    clock = Clock("10:12")

    def feed(now):
        return rr.MarketView(df_5m=pd.DataFrame(detector_bars), setups=frozen,
                             chain=chain(now, strikes, side="pe"), key_levels=[23280.0])

    rt = runtime(path, feed, clock)
    rt.start()
    try:
        rt.tick()
    finally:
        rt.stop()
    trades = {t["admission_id"]: t for t in rows(path, "SELECT * FROM crew_trades")}
    for s in frozen:
        sid = cj.signal_id_for(market_day=DAY, instrument=s.symbol, strategy_id=s.strategy,
                               strategy_version=rr.STRATEGY_VERSION, signal_candle=f"{s.timestamp}:00+05:30",
                               direction=s.direction, setup_id=f"{s.timeframe}|{s.trigger_price}")
        stored = rows(path, "SELECT * FROM crew_admissions WHERE signal_id=?", (sid,))[0]
        trade = trades[stored["admission_id"]]
        assert (trade["strategy"], trade["direction"]) == (s.strategy, s.direction)
        assert (trade["entry_spot"], trade["stop_spot"], trade["target_spot"]) == (
            s.trigger_price, s.tentative_stop, s.tentative_target)


def legacy_vs_journal(path, tmp_path, monkeypatch, steps, target):
    """Same bars, same clock through TradeManager's active-trade path (HoldingAgent held at HOLD and no opposing
    sweep, the two paths the research runtime disables) and through the runtime: stop, target, lots or exit."""
    monkeypatch.setattr(HoldingAgent, "evaluate_holding", lambda self, **k: SimpleNamespace(
        action="HOLD", cushion_sl=None, rationale="", reversal_setup=None))
    monkeypatch.setattr(EntryAgent, "evaluate_sweep", lambda self, *a, **k: None)
    tm = TradeManager(trades_dir=tmp_path / "legacy_trades", db_path=tmp_path / "legacy.db")
    tm.journal_agent.save_active_trades({"T1": {
        "trade_id": "T1", "strategy": "Liquidity Sweep [5M]", "symbol": "NIFTY", "direction": "CALL",
        "entry_price": 24000.0, "stop_loss": 23970.0, "target_price": target, "risk_points": 30.0, "lots": 3,
        "timestamp": f"{DAY} 10:15", "suggested_strike": "NIFTY 24100 CE", "status": "OPEN"}}, DAY)

    clock = Clock("10:19")
    feed = Feed([setup(target=target)], closes=[24000.0])
    rt = runtime(path, feed, clock)
    rt.start()
    tid = rt.tick()["admissions"][0]["trade"]
    legacy_steps, journal_steps = [], []
    try:
        for hhmm, tail in steps:
            closes = [24000.0] + tail
            df = bars(closes)
            res = tm.on_market_tick(cmp=closes[-1], df_5m=df, pdh=25000.0, pdl=23000.0, key_levels=[],
                                    candidate_setups=[], date_str=DAY, current_time_str=hhmm, india_vix=14.0)
            book = tm.journal_agent.load_active_trades(DAY)
            closed = next((a for a in res["actions"] if a.get("trade_id") == "T1" and "realized_points" in a), None)
            legacy_steps.append(("CLOSED", closed["realized_points"]) if closed else
                                (book["T1"]["stop_loss"], book["T1"]["target_price"], book["T1"]["lots"]))

            feed.closes = closes
            walk(rt, clock, hhmm)
            rt.tick()
            trade = rows(path, "SELECT * FROM crew_trades WHERE trade_id=?", (tid,))[0]
            last = cj.get_trade_events(path, tid)[-1]
            journal_steps.append(("CLOSED", last["payload"]["model_points"]) if trade["state"] == "CLOSED" else
                                 (trade["stop_spot"], trade["target_spot"], trade["model_lots_open"]))
    finally:
        rt.stop()
    return legacy_steps, journal_steps


def test_19c_without_a_hop_model_exits_match_trade_manager_step_for_step(path, tmp_path, monkeypatch):
    # 0.7R (stop to entry), 2R (entry + 0.8R), back through the 2R stop. Every move tightens, so v1 changes nothing.
    steps = [("10:20", [24025.0]), ("10:25", [24025.0, 24065.0]), ("10:30", [24025.0, 24065.0, 24020.0])]
    legacy, journal = legacy_vs_journal(path, tmp_path, monkeypatch, steps, target=24100.0)
    assert legacy == [(24000.0, 24100.0, 3), (24024.0, 24100.0, 3), ("CLOSED", 24.0)]
    assert journal == legacy


def test_19d_after_a_hop_trade_manager_loosens_the_stop_and_the_runtime_does_not(path, tmp_path, monkeypatch):
    # 0.7R, hop (stop 24057), the same bar again (2R), hold, back to 24020.
    steps = [("10:20", [24025.0]), ("10:25", [24025.0, 24065.0]), ("10:25", [24025.0, 24065.0]),
             ("10:30", [24025.0, 24065.0, 24080.0]), ("10:35", [24025.0, 24065.0, 24080.0, 24020.0])]
    legacy, journal = legacy_vs_journal(path, tmp_path, monkeypatch, steps, target=24060.0)
    # Legacy defect, kept as-is in manager.py: the 2R milestone overwrites the 24057 hop stop with 24024.
    assert legacy == [(24000.0, 24060.0, 3), (24057.0, 24105.0, 2), (24024.0, 24105.0, 2),
                      (24024.0, 24105.0, 2), ("CLOSED", 24.0)]
    assert journal == [(24000.0, 24060.0, 3), (24057.0, 24105.0, 2), (24057.0, 24105.0, 2),
                       (24057.0, 24105.0, 2), ("CLOSED", 57.0)]
    tid = rows(path, "SELECT trade_id FROM crew_trades")[0]["trade_id"]
    assert never_loosens("CALL", stops(path, tid))


# --- P6 remediation: strategy identity, short-side stops, crash integrity, finalized results -------------

def test_20_scanner_v0_is_its_own_strategy_and_the_frozen_pdc_v0_is_untouched(path):
    rt = runtime(path, Feed([setup()]), Clock("10:20"))
    rt.start()
    try:
        rt.tick()
    finally:
        rt.stop()
    inputs = json.loads(rows(path, "SELECT inputs FROM crew_admissions")[0]["inputs"])
    assert (inputs["strategy_id"], inputs["strategy_version"]) == ("Liquidity Sweep [5M]", "scanner-v0")
    labels = [r["strategy"].lower() for r in rows(path, "SELECT strategy FROM crew_trades")]
    assert labels and not any(w in label for label in labels for w in ("pdc", "15 ema", "v0"))
    assert "backtest_pdc_15ema" not in Path(rr.__file__).read_text()
    v0 = "scripts/backtest_pdc_15ema.py"
    assert hashlib.sha256((BACKEND / v0).read_bytes()).hexdigest() == FROZEN_SOURCES[v0]


def put_setup(target=23940.0):
    return setup(trigger=24000.0, stop=24030.0, target=target, direction="PUT", strike="NIFTY 23900 PE")


def test_21_a_short_after_a_hop_never_raises_its_stop(path):
    clock = Clock("10:19")
    feed = Feed([put_setup()], closes=[24000.0], strikes=(23900,), side="pe")
    rt = runtime(path, feed, clock)
    rt.start()
    try:
        tid = rt.tick()["admissions"][0]["trade"]
        feed.closes = [24000.0, 23935.0]
        walk(rt, clock, "10:20")
        assert rt.tick()["exits"] == [{"trade_id": tid, "action": rr.HOP, "lots_after": 2}]
        walk(rt, clock, seconds=10)  # legacy would raise the 23943 hop stop to entry (24000), then to 23976
        assert rt.tick()["exits"] == [{"trade_id": tid, "action": rr.MILESTONE_BE, "stop": 23943.0}]
        walk(rt, clock, seconds=10)
        assert rt.tick()["exits"] == [{"trade_id": tid, "action": rr.MILESTONE_2R, "stop": 23943.0}]
        feed.closes = [24000.0, 23935.0, 23950.0]
        walk(rt, clock, "10:25")
        assert rt.tick()["exits"] == [{"trade_id": tid, "action": "TRAILING_STOP_HIT", "model_points": 57.0}]
    finally:
        rt.stop()
    assert stops(path, tid) == [24030.0, 23943.0, 23943.0, 23943.0, 23943.0, 23943.0]
    assert never_loosens("PUT", stops(path, tid))


def test_22_a_short_without_a_hop_still_tightens_at_each_milestone(path):
    clock = Clock("10:19")
    feed = Feed([put_setup(target=23900.0)], closes=[24000.0], strikes=(23900,), side="pe")
    rt = runtime(path, feed, clock)
    rt.start()
    try:
        tid = rt.tick()["admissions"][0]["trade"]
        feed.closes = [24000.0, 23975.0]
        walk(rt, clock, "10:20")
        assert rt.tick()["exits"] == [{"trade_id": tid, "action": rr.MILESTONE_BE, "stop": 24000.0}]
        feed.closes = [24000.0, 23975.0, 23935.0]
        walk(rt, clock, "10:25")
        assert rt.tick()["exits"] == [{"trade_id": tid, "action": rr.MILESTONE_2R, "stop": 23976.0}]
        feed.closes = [24000.0, 23975.0, 23935.0, 23980.0]
        walk(rt, clock, "10:30")
        assert rt.tick()["exits"] == [{"trade_id": tid, "action": "TRAILING_STOP_HIT", "model_points": 24.0}]
    finally:
        rt.stop()
    assert stops(path, tid) == [24030.0, 24000.0, 23976.0, 23976.0]


def test_23_a_crash_inside_open_trade_leaves_a_reported_missing_entry_never_a_late_trade(path, monkeypatch):
    clock = Clock("10:20")
    feed = Feed([setup()])
    first = runtime(path, feed, clock)
    first.start()
    monkeypatch.setattr(cj, "_failpoint", "event")  # the trade insert rolls back with its MODEL_ENTRY
    with pytest.raises(cj.InvalidTransition, match="injected event failure"):
        first.tick()
    monkeypatch.setattr(cj, "_failpoint", None)
    admission = rows(path, "SELECT * FROM crew_admissions")[0]
    assert admission["class"] == "RESEARCH_ONLY" and rows(path, "SELECT * FROM crew_trades") == []
    assert cj.check_consistency(path) == []
    walk(first, clock, seconds=10)  # the same session, still alive, does not open the entry late either
    assert first.tick()["admissions"][0]["trade"] == "MISSING_ENTRY"
    first._stop.set()  # then crashes without releasing
    first._beater.join()

    clock.advance(31)
    second = runtime(path, feed, clock)
    report = second.start()
    try:
        missing = report["missing_model_entries"]
        assert [(m["admission_id"], m["session_id"]) for m in missing] == [
            (admission["admission_id"], first.lease.session_id)]
        result = second.tick()["admissions"][0]
        assert (result["replayed"], result["trade"]) == (True, "MISSING_ENTRY")
    finally:
        second.stop()
    assert rows(path, "SELECT * FROM crew_trades") == []
    assert rows(path, "SELECT * FROM crew_admissions") == [admission]
    stats = rr.finalized_results(path)
    assert stats["trades"] == 0 and [m["admission_id"] for m in stats["missing_model_entries"]] == [
        admission["admission_id"]]


@pytest.mark.parametrize("crash_after", ["hop_levels", "partial_exit"])
def test_24_a_crash_mid_trade_leaves_an_unresolved_orphan_with_its_identity(path, monkeypatch, crash_after):
    clock = Clock("10:19")
    feed = Feed([setup()], closes=[24000.0])
    first = runtime(path, feed, clock)
    first.start()
    tid = first.tick()["admissions"][0]["trade"]
    feed.closes = [24000.0, 24065.0]
    walk(first, clock, "10:20")
    if crash_after == "hop_levels":
        monkeypatch.setattr(cj, "partial_exit", lambda *a, **k: (_ for _ in ()).throw(SystemExit("killed")))
        with pytest.raises(SystemExit):
            first.tick()
    else:
        first.tick()
    first._stop.set()  # crash: heartbeat dies, the lease is never released
    first._beater.join()
    clock.advance(31)
    history = cj.get_trade_events(path, tid)
    assert history[-1]["event_type"] == ("MODEL_LEVELS_UPDATED" if crash_after == "hop_levels" else "MODEL_PARTIAL_EXIT")

    second = runtime(path, feed, clock)
    report = second.start()
    try:
        assert [(t["trade_id"], t["owner_session_id"]) for t in report["orphaned_open"]] == [
            (tid, first.lease.session_id)]
        feed.closes = [24000.0, 24065.0, 23900.0]  # through every stop
        walk(second, clock, "10:25")
        result = second.tick()
        assert result["exits"] == [] and result["admissions"][0]["trade"] == "PRIOR_SESSION"
    finally:
        second.stop()
    assert cj.get_trade_events(path, tid) == history
    assert len(rows(path, "SELECT * FROM crew_trades")) == 1 and len(rows(path, "SELECT * FROM crew_admissions")) == 1
    state = rows(path, "SELECT state FROM crew_trades")[0]["state"]
    assert rr.finalized_results(path)["excluded"] == {tid: state}
    assert cj.check_consistency(path) == []


def test_25_finalized_results_count_closed_trades_of_this_policy_only_and_carry_no_premium(path):
    _, expired = prior_session(path, "2026-11-05", "11:00")
    clock = Clock("10:19")
    feed = Feed([setup()], closes=[24000.0])
    rt = runtime(path, feed, clock)
    rt.start()
    try:
        tid = rt.tick()["admissions"][0]["trade"]
        feed.closes = [24000.0, 23960.0]
        walk(rt, clock, "10:20")
        rt.tick()
        a = cj.record_admission(path, lease=rt.lease, admission_class="RESEARCH_ONLY", reasons=list(RESEARCH),
                                market_day=DAY, instrument="NIFTY", strategy_id="Liquidity Sweep [5M]",
                                strategy_version=rr.STRATEGY_VERSION, signal_candle=f"{DAY}T10:20:00+05:30",
                                direction="CALL", setup_id="no-policy", now=clock())
        other = cj.open_trade(path, lease=rt.lease, admission_id=a["admission"]["admission_id"], model_lots=3,
                              entry_spot=24000, stop_spot=23970, target_spot=24060, now=clock())["trade"]
        cj.exit_trade(path, lease=rt.lease, trade_id=other["trade_id"], expected_version=1, reason="TARGET_HIT",
                      signal_candle=f"{DAY}T10:25:00+05:30", index_levels={"spot": 24060.0}, model_points=60.0,
                      model_r=2.0, now=clock())
        b = cj.record_admission(path, lease=rt.lease, admission_class="RESEARCH_ONLY", reasons=list(RESEARCH),
                                market_day=DAY, instrument="NIFTY", strategy_id="PDC Zone + 15 EMA",
                                strategy_version="pdc-v0", signal_candle=f"{DAY}T10:20:00+05:30",
                                direction="CALL", setup_id="pdc", now=clock())
        pdc = cj.open_trade(path, lease=rt.lease, admission_id=b["admission"]["admission_id"], model_lots=3,
                            entry_spot=24000, stop_spot=23970, target_spot=24060,
                            execution_policy=rr.EXECUTION_POLICY, now=clock())["trade"]
        cj.exit_trade(path, lease=rt.lease, trade_id=pdc["trade_id"], expected_version=1, reason="TARGET_HIT",
                      signal_candle=f"{DAY}T10:25:00+05:30", index_levels={"spot": 24060.0}, model_points=60.0,
                      model_r=2.0, now=clock())
    finally:
        rt.stop()
    stats = rr.finalized_results(path)
    assert stats["finalized"] == [{"trade_id": tid, "model_points": -30.0, "model_r": -1.0}]
    assert (stats["trades"], stats["model_points"], stats["model_r"]) == (1, -30.0, -1.0)
    assert stats["excluded"] == {expired: "EXPIRED_UNRESOLVED", other["trade_id"]: "OTHER_EXECUTION_POLICY",
                                 pdc["trade_id"]: "OTHER_STRATEGY_VERSION"}
    assert (stats["strategy_version"], stats["execution_policy"]) == (rr.STRATEGY_VERSION, rr.EXECUTION_POLICY)
    text = json.dumps(stats).lower() + json.dumps(rows(path, "SELECT payload FROM crew_events")).lower()
    assert not any(word in text for word in ("pnl", "premium", "rupee", "fill"))
