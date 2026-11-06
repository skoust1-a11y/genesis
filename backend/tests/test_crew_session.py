"""S1-P5 session ownership and crash recovery. Temp journals only; timestamps are injected."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.db import crew_journal as cj
from app.db import crew_outbox as ob
from app.db import crew_session as cs
from app.trading.option_quote import IST

DAY = "2026-11-06"
T0 = datetime(2026, 11, 6, 9, 0, tzinfo=IST)
BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture
def path(tmp_path):
    p = tmp_path / "journal.db"
    cj.migrate(p)
    return p


def rows(path, sql, args=()):
    conn = cj.connect_readonly(path)
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def lease_row(path):
    return rows(path, "SELECT * FROM crew_lease")[0]


def audit(path):
    return [(e["event_type"], e["session_id"], e["generation"], e["prior_session_id"])
            for e in rows(path, "SELECT * FROM crew_session_events ORDER BY id")]


def open_trade(path, lease, day, hour, setup):
    candle = f"{day}T{hour}:00+05:30"
    now = datetime.fromisoformat(candle)
    a = cj.record_admission(path, lease=lease, admission_class="ACTIONABLE", reasons=[], market_day=day,
                            instrument="NIFTY", strategy_id="PDC Zone", strategy_version="v0",
                            signal_candle=candle, direction="CALL", setup_id=setup, now=now)
    return cj.open_trade(path, lease=lease, admission_id=a["admission"]["admission_id"], model_lots=2,
                         entry_spot=23100, stop_spot=23080, target_spot=23160, now=now)["trade"]["trade_id"]


def test_01_acquire_uses_the_ist_market_day_and_audits(path):
    lease = cs.acquire(path, now=datetime(2026, 11, 5, 20, 0, tzinfo=UTC))  # 01:30 IST on 6 Nov
    assert (lease.market_day, lease.generation, lease.session_id) == (DAY, 1, "S_20261106_G1")
    assert lease.token not in repr(lease)
    row = lease_row(path)
    assert row["token_sha256"] != lease.token and row["released_at"] is None
    assert datetime.fromisoformat(row["expires_at"]) - datetime.fromisoformat(row["acquired_at"]) == timedelta(seconds=30)
    assert audit(path) == [("ACQUIRED", "S_20261106_G1", 1, None)]
    session = rows(path, "SELECT * FROM crew_sessions")[0]
    assert session["market_day"] == DAY and session["generation"] == 1 and session["pid"] == os.getpid()
    with pytest.raises(cj.InvalidTransition, match="timezone-aware"):
        cs.acquire(path, now=datetime(2026, 11, 6, 9, 0))


def test_02_a_competing_owner_is_refused_and_audited(path):
    first = cs.acquire(path, now=T0)
    with pytest.raises(cs.LeaseHeld, match="S_20261106_G1"):
        cs.acquire(path, now=T0 + timedelta(seconds=29))
    assert lease_row(path)["generation"] == 1
    assert audit(path)[-1] == ("REFUSED", None, None, first.session_id)
    assert len(rows(path, "SELECT * FROM crew_sessions")) == 1


def test_03_heartbeat_extends_but_cannot_revive_an_expired_lease(path):
    lease = cs.acquire(path, now=T0)
    expires = cs.heartbeat(path, lease, now=T0 + timedelta(seconds=20))
    assert datetime.fromisoformat(expires) == T0 + timedelta(seconds=50)
    with pytest.raises(cs.LeaseHeld):
        cs.acquire(path, now=T0 + timedelta(seconds=45))
    with pytest.raises(cs.LeaseLost, match="expired"):
        cs.heartbeat(path, lease, now=T0 + timedelta(seconds=50))
    assert audit(path)[-1][0] == "FENCED"
    assert lease_row(path)["heartbeat_at"] == (T0 + timedelta(seconds=20)).isoformat()


def test_04_takeover_after_expiry_fences_the_stale_owner(path):
    old = cs.acquire(path, now=T0)
    new = cs.acquire(path, now=T0 + timedelta(seconds=31))
    assert new.generation == 2 and new.session_id == "S_20261106_G2"
    assert audit(path)[-1] == ("TAKEOVER", new.session_id, 2, old.session_id)
    ended = rows(path, "SELECT ended_at, end_reason FROM crew_sessions WHERE session_id=?", (old.session_id,))[0]
    assert ended["end_reason"] == "LEASE_EXPIRED"
    for act in (lambda: cs.heartbeat(path, old, now=T0 + timedelta(seconds=32)),
                lambda: cs.release(path, old, now=T0 + timedelta(seconds=32)),
                lambda: cs.recover(path, old, now=T0 + timedelta(seconds=32))):
        with pytest.raises(cs.LeaseLost, match="another generation"):
            act()
    assert lease_row(path)["session_id"] == new.session_id
    forged = cs.Lease(new.session_id, DAY, 2, "0" * 64)
    with pytest.raises(cs.LeaseLost):
        cs.heartbeat(path, forged, now=T0 + timedelta(seconds=33))


def test_05_controlled_release_lets_the_next_owner_acquire(path):
    first = cs.acquire(path, now=T0)
    cs.release(path, first, reason="shutdown", now=T0 + timedelta(seconds=5))
    cs.release(path, first, reason="shutdown", now=T0 + timedelta(seconds=6))  # repeat is a no-op
    second = cs.acquire(path, now=T0 + timedelta(seconds=7))
    assert second.generation == 2
    assert [a[0] for a in audit(path)] == ["ACQUIRED", "RELEASED", "ACQUIRED"]
    assert audit(path)[-1][3] == first.session_id
    ended = rows(path, "SELECT end_reason FROM crew_sessions WHERE session_id=?", (first.session_id,))[0]
    assert ended["end_reason"] == "shutdown"
    with pytest.raises(cs.LeaseLost):
        cs.heartbeat(path, first, now=T0 + timedelta(seconds=8))


def test_06_the_database_refuses_unaudited_or_illegal_lease_changes(path):
    lease = cs.acquire(path, now=T0)
    conn = cj.connect(path)
    later = (T0 + timedelta(minutes=5)).isoformat()
    attempts = [
        ("UPDATE crew_lease SET generation=2, acquired_at=?", (later,), "generation"),
        ("UPDATE crew_lease SET token_sha256=?", ("a" * 64,), "fixed for a generation"),
        ("UPDATE crew_lease SET released_at=?", (later,), "RELEASED event"),
        ("UPDATE crew_lease SET heartbeat_at=?, expires_at=?", (later, (T0 + timedelta(minutes=6)).isoformat()),
         "expired lease cannot be renewed"),
        ("DELETE FROM crew_lease", (), "cannot be deleted"),
        ("UPDATE crew_session_events SET event_type='RELEASED'", (), "append-only"),
        ("DELETE FROM crew_session_events", (), "append-only"),
        ("UPDATE crew_sessions SET generation=9", (), "immutable"),
        ("DELETE FROM crew_sessions", (), "cannot be deleted"),
        (("INSERT INTO crew_lease (id, session_id, generation, token_sha256, acquired_at, heartbeat_at, expires_at)"
          " VALUES (2, ?, 1, ?, ?, ?, ?)"), (lease.session_id, "b" * 64, later, later, later), "CHECK|first lease"),
    ]
    for sql, args, message in attempts:
        with pytest.raises(sqlite3.IntegrityError, match=message):
            conn.execute(sql, args)
    conn.execute("UPDATE crew_sessions SET ended_at=?, end_reason='x' WHERE session_id=?", (later, lease.session_id))
    with pytest.raises(sqlite3.IntegrityError, match="ended session"):
        conn.execute("UPDATE crew_sessions SET heartbeat_at=? WHERE session_id=?", (later, lease.session_id))
    conn.close()


def test_07_concurrent_acquirers_produce_exactly_one_owner(path):
    barrier, results = threading.Barrier(8), []

    def race():
        barrier.wait()
        try:
            results.append(cs.acquire(path, now=T0))
        except cs.LeaseHeld as exc:
            results.append(exc)

    threads = [threading.Thread(target=race) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    winners = [r for r in results if isinstance(r, cs.Lease)]
    assert len(winners) == 1 and len(results) == 8
    assert sorted(a[0] for a in audit(path)) == ["ACQUIRED"] + ["REFUSED"] * 7


def test_08_a_killed_owner_is_taken_over_only_after_its_lease_expires(path):
    script = (
        "import os, signal, sys\n"
        "from datetime import datetime\n"
        "from app.db import crew_session as cs\n"
        "cs.acquire(sys.argv[1], now=datetime.fromisoformat(sys.argv[2]))\n"
        "os.kill(os.getpid(), signal.SIGKILL)\n"
    )
    proc = subprocess.run([sys.executable, "-c", script, str(path), T0.isoformat()], check=False, cwd=BACKEND,
                          env={**os.environ, "PYTHONPATH": str(BACKEND)}, capture_output=True, text=True)
    assert proc.returncode < 0, proc.stderr
    with pytest.raises(cs.LeaseHeld):
        cs.acquire(path, now=T0 + timedelta(seconds=10))
    lease = cs.acquire(path, now=T0 + timedelta(seconds=30))
    assert lease.generation == 2 and audit(path)[-1][0] == "TAKEOVER"


def test_09_recovery_expires_prior_day_trades_and_reports_same_day_orphans(path):
    day1 = datetime(2026, 11, 5, 10, 0, tzinfo=IST)
    old = cs.acquire(path, now=day1, ttl_seconds=3600)
    stale_trade = open_trade(path, old, "2026-11-05", "10:15", "yesterday")
    pub = ob.enqueue_publication(path, lease=old, kind="OPS", purpose="halt", body="halt", chat_id="-100",
                                 now=day1)["publication"]["publication_id"]
    ob.claim_publication(path, pub, lease=old, now=day1)
    # The day-1 process dies. A day-2 process opens a trade and also dies.
    mid = cs.acquire(path, now=T0, ttl_seconds=1800)
    orphan = open_trade(path, mid, DAY, "09:20", "today")
    new = cs.acquire(path, now=T0 + timedelta(minutes=30))
    before = rows(path, "SELECT * FROM crew_trades WHERE trade_id=?", (orphan,))

    report = cs.recover(path, new, now=T0 + timedelta(minutes=30))
    assert report["expired"] == [stale_trade]
    assert report["inflight_unknown"] == [pub] and report["unknown_publications"] == [pub]
    assert report["orphaned_open"] == [{"trade_id": orphan, "owner_session_id": mid.session_id,
                                        "state": "TRACKING", "version": 1}]
    assert rows(path, "SELECT * FROM crew_trades WHERE trade_id=?", (orphan,)) == before
    expired = rows(path, "SELECT state, model_lots_open FROM crew_trades WHERE trade_id=?", (stale_trade,))[0]
    assert expired == {"state": "EXPIRED_UNRESOLVED", "model_lots_open": 2}
    recovered = rows(path, "SELECT * FROM crew_session_events WHERE event_type='RECOVERED'")
    assert len(recovered) == 1 and json.loads(recovered[0]["detail"]) == report
    assert cj.check_consistency(path) == []

    again = cs.recover(path, new, now=T0 + timedelta(minutes=30, seconds=10))
    assert again["expired"] == [] and again["inflight_unknown"] == []
    assert again["orphaned_open"] == report["orphaned_open"]


def test_10_recovery_needs_a_live_lease_and_writes_nothing_without_one(path):
    day1 = datetime(2026, 11, 5, 10, 0, tzinfo=IST)
    old = cs.acquire(path, now=day1, ttl_seconds=3600)
    open_trade(path, old, "2026-11-05", "10:15", "yesterday")
    lease = cs.acquire(path, now=T0)
    with pytest.raises(cs.LeaseLost, match="expired"):
        cs.recover(path, lease, now=T0 + timedelta(seconds=31))
    assert rows(path, "SELECT count(*) AS n FROM crew_trades WHERE state='EXPIRED_UNRESOLVED'")[0]["n"] == 0
    assert audit(path)[-1][0] == "FENCED"


def test_11_a_session_ends_with_its_ist_market_day(path):
    lease = cs.acquire(path, now=datetime(2026, 11, 6, 23, 59, 50, tzinfo=IST))
    with pytest.raises(cs.LeaseLost, match="market day 2026-11-06 has ended"):
        cs.heartbeat(path, lease, now=datetime(2026, 11, 7, 0, 0, 5, tzinfo=IST))
    cs.release(path, lease, reason="day rollover", now=datetime(2026, 11, 7, 0, 0, 6, tzinfo=IST))
    nxt = cs.acquire(path, now=datetime(2026, 11, 7, 0, 0, 7, tzinfo=IST))
    assert nxt.market_day == "2026-11-07" and nxt.generation == 2


def test_12_v2_journal_upgrades_and_keeps_its_sessions(tmp_path):
    p = tmp_path / "v2.db"
    conn = cj.connect(p, create=True)
    conn.executescript(f"BEGIN IMMEDIATE;\n{cj.SCHEMA}\n{cj._V2_ADD_CHAT}\n{cj.SCHEMA_V2}\nCOMMIT;")
    conn.execute("UPDATE crew_meta SET value='2' WHERE key='schema_version'")  # the v2 DDL, as shipped in P4
    conn.execute("INSERT INTO crew_sessions (session_id, market_day, started_at) VALUES ('S1', ?, ?)",
                 (DAY, T0.isoformat()))
    conn.close()
    assert cj.migrate(p) == cj.SCHEMA_VERSION and cj.migrate(p) == cj.SCHEMA_VERSION
    assert rows(p, "SELECT session_id, generation FROM crew_sessions") == [{"session_id": "S1", "generation": None}]
    assert cs.acquire(p, now=T0).generation == 1


LEVELS = {"spot": 23120.0, "stop": 23080.0, "target": 23160.0}
LATER = T0 + timedelta(minutes=12)
LATE_CANDLE = "2026-11-06T09:12:00+05:30"
PROTECTED = ("crew_admissions", "crew_trades", "crew_events", "crew_outbox", "crew_outbox_events")


def taken_over(path):
    """Old owner opens a trade, records an unopened admission, claims one publication and queues another;
    then its lease expires and a new session takes over."""
    old = cs.acquire(path, now=T0, ttl_seconds=600)
    trade = open_trade(path, old, DAY, "09:05", "mine")
    spare = cj.record_admission(path, lease=old, admission_class="ACTIONABLE", reasons=[], market_day=DAY,
                                instrument="NIFTY", strategy_id="PDC Zone", strategy_version="v0",
                                signal_candle="2026-11-06T09:06:00+05:30", direction="CALL", setup_id="spare",
                                now=T0)["admission"]["admission_id"]
    sending, queued = (ob.enqueue_publication(path, lease=old, kind="OPS", purpose=p, body=p, chat_id="-100",
                                              now=T0)["publication"]["publication_id"] for p in ("a", "b"))
    ob.claim_publication(path, sending, lease=old, now=T0)
    new = cs.acquire(path, now=T0 + timedelta(minutes=11), ttl_seconds=600)
    return old, new, trade, spare, sending, queued


def snapshot(path):
    return {t: rows(path, f"SELECT * FROM {t} ORDER BY rowid") for t in PROTECTED}


def test_13_a_stale_owner_can_write_nothing_and_every_fence_is_audited(path):
    old, new, trade, spare, sending, queued = taken_over(path)
    before = snapshot(path)
    attempts = [
        lambda: cj.record_admission(path, lease=old, admission_class="ACTIONABLE", reasons=[], market_day=DAY,
                                    instrument="NIFTY", strategy_id="PDC Zone", strategy_version="v0",
                                    signal_candle=LATE_CANDLE, direction="CALL", setup_id="late", now=LATER),
        lambda: cj.open_trade(path, lease=old, admission_id=spare, model_lots=1, entry_spot=23100,
                              stop_spot=23080, target_spot=23160, now=LATER),
        lambda: cj.partial_exit(path, lease=old, trade_id=trade, expected_version=1, reason="2R", lots_after=1,
                                signal_candle=LATE_CANDLE, index_levels=LEVELS, now=LATER),
        lambda: cj.exit_trade(path, lease=old, trade_id=trade, expected_version=1, reason="stop",
                              signal_candle=LATE_CANDLE, index_levels=LEVELS, now=LATER),
        lambda: cj.update_levels(path, lease=old, trade_id=trade, expected_version=1, signal_candle=LATE_CANDLE,
                                 stop_spot=23090, now=LATER),
        lambda: cj.expire_unresolved(path, lease=old, at=LATE_CANDLE, now=LATER),
        lambda: ob.enqueue_publication(path, lease=old, kind="OPS", purpose="late", body="late", chat_id="-100",
                                       now=LATER),
        lambda: ob.claim_publication(path, queued, lease=old, now=LATER),
        lambda: ob.acknowledge_publication(path, sending, lease=old, message_id=5, now=LATER),
        lambda: ob.mark_unknown(path, sending, lease=old, reason="late", now=LATER),
        lambda: ob.suppress_publication(path, queued, lease=old, reason="late", now=LATER),
        lambda: ob.recover_inflight_publications(path, lease=old, now=LATER),
        lambda: cs.recover(path, old, now=LATER),
    ]
    fenced_before = len([a for a in audit(path) if a[0] == "FENCED"])
    for attempt in attempts:
        with pytest.raises(cs.LeaseLost, match="another generation"):
            attempt()
    assert snapshot(path) == before
    fenced = [a for a in audit(path) if a[0] == "FENCED"][fenced_before:]
    assert fenced == [("FENCED", old.session_id, old.generation, new.session_id)] * len(attempts)


def test_14_the_new_owner_cannot_adopt_orphans_or_touch_in_flight_publications(path):
    old, new, trade, spare, sending, queued = taken_over(path)
    before = snapshot(path)
    for attempt in (
        lambda: cj.partial_exit(path, lease=new, trade_id=trade, expected_version=1, reason="2R", lots_after=1,
                                signal_candle=LATE_CANDLE, index_levels=LEVELS, now=LATER),
        lambda: cj.exit_trade(path, lease=new, trade_id=trade, expected_version=1, reason="stop",
                              signal_candle=LATE_CANDLE, index_levels=LEVELS, now=LATER),
        lambda: cj.update_levels(path, lease=new, trade_id=trade, expected_version=1, signal_candle=LATE_CANDLE,
                                 stop_spot=23090, now=LATER),
        lambda: cj.open_trade(path, lease=new, admission_id=spare, model_lots=1, entry_spot=23100,
                              stop_spot=23080, target_spot=23160, now=LATER),
    ):
        with pytest.raises(cj.InvalidTransition, match=f"belongs to session {old.session_id}"):
            attempt()
    for attempt in (lambda: ob.acknowledge_publication(path, sending, lease=new, message_id=5, now=LATER),
                    lambda: ob.mark_unknown(path, sending, lease=new, reason="x", now=LATER)):
        with pytest.raises(cj.InvalidTransition, match=f"not SENDING by {new.session_id}"):
            attempt()
    with pytest.raises(cj.InvalidTransition, match="cannot be suppressed"):
        ob.suppress_publication(path, sending, lease=new, reason="x", now=LATER)
    assert snapshot(path) == before

    report = cs.recover(path, new, now=LATER)
    assert report["inflight_unknown"] == [sending] and report["expired"] == []
    assert [o["trade_id"] for o in report["orphaned_open"]] == [trade]
    assert snapshot(path)["crew_trades"] == before["crew_trades"]
    # The queued publication was never claimed, so the live owner may still send it.
    assert ob.claim_publication(path, queued, lease=new, now=LATER)["claimed_by_session"] == new.session_id


def test_15_protected_writes_refuse_a_missing_or_forged_lease_and_audit_it(path):
    lease = cs.acquire(path, now=T0)
    pid = ob.enqueue_publication(path, lease=lease, kind="OPS", purpose="p", body="p", chat_id="-100",
                                 now=T0)["publication"]["publication_id"]
    for bogus in (None, lease.session_id, {"session_id": lease.session_id, "token": lease.token}):
        with pytest.raises(cj.InvalidTransition, match="session Lease"):
            ob.claim_publication(path, pid, lease=bogus, now=T0)
    with pytest.raises(cs.LeaseLost):
        ob.claim_publication(path, pid, lease=cs.Lease(lease.session_id, DAY, 1, "0" * 64), now=T0)
    assert audit(path)[-1] == ("FENCED", lease.session_id, 1, lease.session_id)
    with pytest.raises(cs.LeaseLost):
        ob.claim_publication(path, pid, lease=cs.Lease("S_ghost", DAY, 1, lease.token), now=T0)
    ghost = rows(path, "SELECT * FROM crew_session_events ORDER BY id DESC LIMIT 1")[0]
    assert ghost["event_type"] == "REFUSED" and json.loads(ghost["detail"])["claimed_session_id"] == "S_ghost"
    assert ob.inspect_publication(path, pid)["publication"]["state"] == "PENDING"


def test_16_admissions_are_bound_to_the_lease_market_day(path):
    lease = cs.acquire(path, now=T0)
    with pytest.raises(cj.InvalidTransition, match="not the lease's 2026-11-06"):
        cj.record_admission(path, lease=lease, admission_class="ACTIONABLE", reasons=[], market_day="2026-11-05",
                            instrument="NIFTY", strategy_id="PDC Zone", strategy_version="v0",
                            signal_candle="2026-11-05T15:00:00+05:30", direction="CALL", setup_id="x", now=T0)
    assert rows(path, "SELECT count(*) AS n FROM crew_admissions")[0]["n"] == 0


# --- MONITORING speaks only for the trade's live owner -----------------------------------------------

def monitored(path, *, start=T0, hour="09:00", ttl=600):
    """A live owner with a TRACKING trade whose RECOMMENDATION is ACKED."""
    owner = cs.acquire(path, now=start, ttl_seconds=ttl)
    trade = open_trade(path, owner, start.date().isoformat(), hour, "m")
    at = datetime.fromisoformat(f"{start.date().isoformat()}T{hour}:05+05:30")
    parent = ob.enqueue_publication(path, lease=owner, kind="RECOMMENDATION", purpose="entry", body="BUY",
                                    trade_id=trade, chat_id="-100", now=at)["publication"]["publication_id"]
    ob.claim_publication(path, parent, lease=owner, now=at)
    ob.acknowledge_publication(path, parent, lease=owner, message_id=1, now=at)
    return owner, trade, parent


def monitor(path, lease, trade, parent, at, purpose="update"):
    return ob.enqueue_publication(path, lease=lease, kind="MONITORING", purpose=purpose, body=purpose,
                                  trade_id=trade, parent_publication_id=parent, chat_id="-100", now=at)


def test_17_the_live_owner_may_monitor_only_under_an_acked_parent(path):
    owner, trade, parent = monitored(path)
    assert monitor(path, owner, trade, parent, T0 + timedelta(minutes=1))["publication"]["state"] == "PENDING"
    with pytest.raises(cj.InvalidTransition, match="delivered RECOMMENDATION parent"):
        monitor(path, owner, trade, None, T0 + timedelta(minutes=1), purpose="no-parent")


@pytest.mark.parametrize("lose, message", [
    (lambda path, owner: None, "expired"),
    (lambda path, owner: cs.release(path, owner, reason="stop", now=T0 + timedelta(minutes=1)), "released"),
    (lambda path, owner: cs.acquire(path, now=T0 + timedelta(minutes=11)), "another generation"),
], ids=["expired", "released", "superseded"])
def test_18_a_lost_owner_cannot_queue_monitoring(path, lose, message):
    owner, trade, parent = monitored(path)
    lose(path, owner)
    before = snapshot(path)
    with pytest.raises(cs.LeaseLost, match=message):
        monitor(path, owner, trade, parent, T0 + timedelta(minutes=11))
    assert snapshot(path) == before
    assert audit(path)[-1][:2] == ("FENCED", owner.session_id)


def test_19_a_new_owner_cannot_monitor_or_send_for_a_current_day_orphan(path):
    old, trade, parent = monitored(path)
    queued = monitor(path, old, trade, parent, T0 + timedelta(minutes=1))["publication"]["publication_id"]
    new = cs.acquire(path, now=T0 + timedelta(minutes=11), ttl_seconds=600)
    parent_before = ob.inspect_publication(path, parent)
    with pytest.raises(cj.InvalidTransition, match=f"ORPHAN: trade belongs to session {old.session_id}"):
        monitor(path, new, trade, parent, LATER, purpose="adopted")
    assert ob.publication_id_for(kind="MONITORING", purpose="adopted", trade_id=trade,
                                 parent_publication_id=parent) not in [r["publication_id"] for r in
                                                                       rows(path, "SELECT * FROM crew_outbox")]
    claimed = ob.claim_publication(path, queued, lease=new, now=LATER)
    assert claimed["state"] == "SUPPRESSED" and claimed["attempts"] == 0
    assert events(path, queued)[-1]["note"].startswith("ORPHAN")
    assert ob.inspect_publication(path, parent) == parent_before


def test_20_a_stale_lease_cannot_queue_monitoring(path):
    owner, trade, parent = monitored(path, start=datetime(2026, 11, 6, 23, 50, tzinfo=IST), hour="23:50", ttl=3600)
    before = snapshot(path)
    with pytest.raises(cs.LeaseLost, match="market day 2026-11-06 has ended"):
        monitor(path, owner, trade, parent, datetime(2026, 11, 7, 0, 0, 5, tzinfo=IST))
    with pytest.raises(cs.LeaseLost, match="another generation"):
        monitor(path, cs.Lease(owner.session_id, owner.market_day, owner.generation + 1, owner.token), trade, parent,
                datetime(2026, 11, 6, 23, 51, tzinfo=IST))
    assert snapshot(path) == before


def test_21_ownership_transfer_serializes_with_monitoring_enqueue(path, monkeypatch):
    old, trade, parent = monitored(path)
    inside, done, blocked = threading.Event(), [], []
    real_trade = ob._trade

    def slow_trade(conn, trade_id):
        inside.set()
        threading.Event().wait(0.3)
        blocked.append(not done)  # still inside the enqueue transaction: the takeover must be waiting
        return real_trade(conn, trade_id)

    def takeover():
        inside.wait()
        done.append(cs.acquire(path, now=T0 + timedelta(minutes=11), ttl_seconds=600))

    monkeypatch.setattr(ob, "_trade", slow_trade)
    thread = threading.Thread(target=takeover)
    thread.start()
    first = monitor(path, old, trade, parent, T0 + timedelta(minutes=9))["publication"]
    thread.join()
    assert blocked == [True] and first["state"] == "PENDING"
    monkeypatch.undo()
    new = done[0]
    assert new.generation == 2 and audit(path)[-1][0] == "TAKEOVER"
    with pytest.raises(cs.LeaseLost, match="another generation"):
        monitor(path, old, trade, parent, T0 + timedelta(minutes=9, seconds=1), purpose="late")
    with pytest.raises(cj.InvalidTransition, match="ORPHAN"):
        monitor(path, new, trade, parent, LATER, purpose="adopted")
    assert ob.claim_publication(path, first["publication_id"], lease=new, now=LATER)["state"] == "SUPPRESSED"


def test_22_a_corrupt_journal_blocks_recovery_before_any_write(path):
    old = cs.acquire(path, now=datetime(2026, 11, 5, 10, 0, tzinfo=IST), ttl_seconds=3600)
    open_trade(path, old, "2026-11-05", "10:15", "yesterday")
    conn = cj.connect(path)
    conn.execute("INSERT INTO crew_trades (trade_id, signal_id, admission_class, market_day, state, strategy,"
                 " direction, model_lots_initial, model_lots_open, opened_at, closed_at, legacy_ref)"
                 " VALUES ('L_bad', ?, 'RESEARCH_ONLY', '2026-10-30', 'QUARANTINED', 'legacy', 'PUT', 1, 1, ?, ?, 'x')",
                 ("f" * 64, "2026-10-30T10:00:00+05:30", "2026-10-30T10:00:00+05:30"))
    conn.close()
    new = cs.acquire(path, now=T0)
    before = snapshot(path)
    with pytest.raises(cj.ConsistencyError) as refused:
        cs.recover(path, new, now=T0)
    assert refused.value.findings == [{"kind": "missing_entry", "trade_id": "L_bad"}]
    assert snapshot(path) == before
    assert "RECOVERED" not in [a[0] for a in audit(path)]


def test_23_recovery_leaves_legacy_quarantine_untouched(path, tmp_path):
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"L1": {"status": "OPEN", "symbol": "NIFTY", "strategy": "PDC Zone",
                                         "direction": "CALL", "timestamp": "2026-10-30T10:00:00+05:30", "lots": 1}}))
    cj.quarantine_legacy_reference(path, legacy)
    before = snapshot(path)
    lease = cs.acquire(path, now=T0)
    assert cs.recover(path, lease, now=T0)["expired"] == []
    assert snapshot(path) == before
    with pytest.raises(cj.InvalidTransition, match="belongs to session None"):
        cj.exit_trade(path, lease=lease, trade_id="L1", expected_version=1, reason="x",
                      signal_candle="2026-11-06T09:00:00+05:30", index_levels=LEVELS, now=T0)


def test_24_unfenced_writes_are_not_reachable_from_application_code():
    # Operator-only and migration-only paths: no runtime module may call them until an authorized interface exists.
    owners = {"resolve_unknown": BACKEND / "app/db/crew_outbox.py",
              "quarantine_legacy_reference": BACKEND / "app/db/crew_journal.py"}
    for source in (BACKEND / "app").rglob("*.py"):
        text = source.read_text()
        for name, owner in owners.items():
            assert source == owner or name not in text, f"{source} references {name}"


def events(path, pid):
    return ob.inspect_publication(path, pid)["events"]
