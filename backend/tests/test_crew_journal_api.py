from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

import pytest

from app.db import crew_journal as cj
from app.db import crew_session as cs
from app.db import trade_store

DAY = "2026-11-06"
CANDLE = "2026-11-06T10:15:00+05:30"
NOW = datetime.fromisoformat(CANDLE)
LEASES: dict[Path, cs.Lease] = {}
LEVELS = {"spot": 23120.0, "stop": 23080.0, "target": 23160.0}
ROOT = trade_store.ROOT_DIR
PROD_DB = trade_store.DEFAULT_DB_PATH.resolve()
OCT30 = ROOT / "trades" / ".active_trades_2026-10-30.json"
PROD_HASH = hashlib.sha256(PROD_DB.read_bytes()).hexdigest()
OCT30_HASH = hashlib.sha256(OCT30.read_bytes()).hexdigest()
BACKEND = Path(__file__).resolve().parents[1]
FORBIDDEN = ("pnl", "fill_price", "broker_", "avg_price")


def identity(**over):
    base = {"market_day": DAY, "instrument": "NIFTY", "strategy_id": "PDC Zone", "strategy_version": "v0",
            "signal_candle": CANDLE, "direction": "CALL", "setup_id": "OR-30"}
    base.update(over)
    return base


def ready(tmp_path):
    path = tmp_path / "journal.db"
    cj.migrate(path)
    LEASES[path] = cs.acquire(path, now=NOW, ttl_seconds=3600)
    return path


def own(path):
    """The session lease and an injected clock inside it."""
    return {"lease": LEASES[path], "now": NOW}


def admit(path, **over):
    return cj.record_admission(path, **own(path), admission_class="ACTIONABLE", reasons=[], **identity(**over))


def open_model(path, **over):
    admission = admit(path, **over)
    opened = cj.open_trade(
        path, **own(path), admission_id=admission["admission"]["admission_id"], model_lots=3,
        entry_spot=23100, stop_spot=23080, target_spot=23160,
    )
    return admission, opened


def counts(path):
    conn = cj.connect(path)
    try:
        return tuple(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                     for table in ("crew_trades", "crew_events", "crew_admissions"))
    finally:
        conn.close()


def kinds(path):
    return sorted(item["kind"] for item in cj.check_consistency(path))


def no_money(payload):
    blob = json.dumps(payload).lower()
    assert not any(token in blob for token in FORBIDDEN)


def test_01_duplicate_signal_replay_after_restart(tmp_path):
    path = ready(tmp_path)
    _, first = open_model(path)
    _, second = open_model(path)
    assert first["replayed"] is False and second["replayed"] is True
    assert first["trade"]["trade_id"] == second["trade"]["trade_id"]
    assert counts(path) == (1, 1, 1)
    with pytest.raises(cj.InvalidTransition, match="does not match"):
        cj.open_trade(path, **own(path), admission_id=first["trade"]["admission_id"], model_lots=1,
                      entry_spot=23100, stop_spot=23080, target_spot=23160)
    assert counts(path) == (1, 1, 1)


def test_02_canonical_signal_identity_is_stable():
    base = identity(instrument=" nifty ", direction="call", signal_candle="2026-11-06 10:15:00+05:30")
    text = cj.canonical_signal_fingerprint(**identity())
    assert cj.canonical_signal_fingerprint(**base) == text
    digest = cj.signal_id_for(**identity())
    assert digest == hashlib.sha256(text.encode()).hexdigest()
    assert digest == digest.lower() and len(digest) == 64
    assert cj.trade_id_for(DAY, digest) == f"T_20261106_{digest[:12]}"
    assert cj.signal_id_for(**identity()) == digest
    assert cj.signal_id_for(**identity(strategy_version="v1")) != digest
    assert cj.signal_id_for(**identity(setup_id="other")) != digest
    assert "pid" not in text and "uuid" not in text


def test_03_signal_identity_collision_is_refused(tmp_path):
    path = ready(tmp_path)
    victim = identity()
    sid = cj.signal_id_for(**victim)
    tid = cj.trade_id_for(DAY, sid)
    other = cj.signal_id_for(**identity(setup_id="other-setup"))
    conn = cj.connect(path)
    conn.execute(
        "INSERT INTO crew_trades (trade_id, signal_id, admission_class, market_day, state, strategy, direction,"
        " model_lots_initial, model_lots_open, opened_at, closed_at, legacy_ref)"
        " VALUES (?, ?, 'RESEARCH_ONLY', ?, 'QUARANTINED', 'legacy', 'PUT', 1, 1, ?, ?, 'decoy')",
        (tid, other, DAY, CANDLE, CANDLE),
    )
    conn.execute(
        "INSERT INTO crew_events (event_id, trade_id, seq, event_type, to_state, idempotency_key, occurred_at,"
        " recorded_at) VALUES (?, ?, 1, 'LEGACY_IMPORT', 'QUARANTINED', ?, ?, ?)",
        (f"{tid}:legacy", tid, f"{tid}:legacy", CANDLE, CANDLE),
    )
    conn.close()
    admission = admit(path)
    with pytest.raises(cj.TradeIdCollision):
        cj.open_trade(path, **own(path), admission_id=admission["admission"]["admission_id"], model_lots=3,
                      entry_spot=23100, stop_spot=23080, target_spot=23160)
    conn = cj.connect(path)
    assert conn.execute("SELECT count(*) FROM crew_trades WHERE signal_id=?", (sid,)).fetchone()[0] == 0
    conn.close()


def test_04_duplicate_admission_returns_the_original(tmp_path):
    path = ready(tmp_path)
    first = admit(path)
    second = admit(path)
    assert second["replayed"] is True
    assert second["admission"]["admission_id"] == first["admission"]["admission_id"]
    with pytest.raises(cj.AdmissionConflict):
        cj.record_admission(path, **own(path), admission_class="RESEARCH_ONLY",
                            reasons=["CAPITAL_UNAVAILABLE"], **identity())
    assert counts(path)[2] == 1


def test_05_model_entry_commits_trade_and_event_together(tmp_path):
    path = ready(tmp_path)
    _, opened = open_model(path)
    trade, event = opened["trade"], opened["event"]
    assert trade["state"] == "TRACKING" and trade["execution_state"] == "NONE"
    assert trade["model_lots_open"] == trade["model_lots_initial"] == 3 and trade["version"] == 1
    assert event["event_type"] == "MODEL_ENTRY" and event["seq"] == 1
    no_money(event["payload"])
    assert cj.get_trade_events(path, trade["trade_id"])[0]["event_id"] == event["event_id"]
    assert cj.check_consistency(path) == []


def test_06_partial_exit_is_atomic(tmp_path):
    path = ready(tmp_path)
    trade_id = open_model(path)[1]["trade"]["trade_id"]
    result = cj.partial_exit(path, **own(path), trade_id=trade_id, expected_version=1, reason="2R", lots_after=2,
                             signal_candle="2026-11-06T10:20:00+05:30", index_levels=LEVELS)
    payload = result["event"]["payload"]
    assert result["trade"]["state"] == "PARTIALLY_EXITED"
    assert result["trade"]["model_lots_open"] == 2 and result["trade"]["execution_state"] == "NONE"
    assert payload["lots_before"] == 3 and payload["lots_after"] == 2 and payload["reason"] == "2R"
    assert payload["signal_candle"] == "2026-11-06T10:20:00+05:30" and payload["index_levels"] == LEVELS
    no_money(payload)
    assert counts(path)[1] == 2 and cj.check_consistency(path) == []


def test_07_repeated_partial_exit_does_not_reduce_twice(tmp_path):
    path = ready(tmp_path)
    trade_id = open_model(path)[1]["trade"]["trade_id"]
    args = {"trade_id": trade_id, "expected_version": 1, "reason": "2R", "lots_after": 2,
            "signal_candle": "2026-11-06T10:20:00+05:30", "index_levels": LEVELS, "idempotency_key": "partial-1"}
    first = cj.partial_exit(path, **own(path), **args)
    second = cj.partial_exit(path, **own(path), **args)
    assert first["replayed"] is False and second["replayed"] is True
    assert second["trade"]["model_lots_open"] == 2 and second["trade"]["version"] == 2
    assert counts(path)[1] == 2
    with pytest.raises(cj.InvalidTransition, match="different partial"):
        cj.partial_exit(path, **own(path), **{**args, "lots_after": 1})
    assert counts(path)[1] == 2


def test_08_full_exit_is_atomic(tmp_path):
    path = ready(tmp_path)
    trade_id = open_model(path)[1]["trade"]["trade_id"]
    result = cj.exit_trade(path, **own(path), trade_id=trade_id, expected_version=1, reason="stop",
                           signal_candle="2026-11-06T11:00:00+05:30", index_levels={"spot": 23080.0})
    assert result["trade"]["state"] == "CLOSED" and result["trade"]["model_lots_open"] == 0
    assert result["trade"]["execution_state"] == "NONE"
    assert result["event"]["event_type"] == "MODEL_EXIT" and result["event"]["idempotency_key"].endswith(":EXIT")
    no_money(result["event"]["payload"])
    assert cj.check_consistency(path) == []


def test_09_repeated_full_exit_returns_the_existing_close(tmp_path):
    path = ready(tmp_path)
    trade_id = open_model(path)[1]["trade"]["trade_id"]
    args = {"trade_id": trade_id, "reason": "stop", "signal_candle": "2026-11-06T11:00:00+05:30",
            "index_levels": {"spot": 23080.0}}
    first = cj.exit_trade(path, **own(path), expected_version=1, **args)
    second = cj.exit_trade(path, **own(path), expected_version=1, reason="other", **{k: v for k, v in args.items() if k != "reason"})
    assert first["replayed"] is False and second["replayed"] is True
    assert second["event"]["event_id"] == first["event"]["event_id"]
    assert second["trade"]["state"] == "CLOSED" and counts(path)[1] == 2


def test_10_version_conflict_writes_nothing(tmp_path):
    path = ready(tmp_path)
    trade_id = open_model(path)[1]["trade"]["trade_id"]
    with pytest.raises(cj.VersionConflict):
        cj.partial_exit(path, **own(path), trade_id=trade_id, expected_version=9, reason="2R", lots_after=2,
                        signal_candle="2026-11-06T10:20:00+05:30", index_levels=LEVELS)
    conn = cj.connect(path)
    row = conn.execute("SELECT state, model_lots_open, version FROM crew_trades").fetchone()
    assert tuple(row) == ("TRACKING", 3, 1)
    assert conn.execute("SELECT count(*) FROM crew_events").fetchone()[0] == 1
    conn.close()


def test_11_invalid_transition_does_not_reopen(tmp_path):
    path = ready(tmp_path)
    trade_id = open_model(path)[1]["trade"]["trade_id"]
    cj.exit_trade(path, **own(path), trade_id=trade_id, expected_version=1, reason="stop",
                  signal_candle="2026-11-06T11:00:00+05:30", index_levels={"spot": 23080.0})
    with pytest.raises(cj.InvalidTransition):
        cj.partial_exit(path, **own(path), trade_id=trade_id, expected_version=2, reason="2R", lots_after=1,
                        signal_candle="2026-11-06T11:05:00+05:30", index_levels=LEVELS)
    conn = cj.connect(path)
    assert conn.execute("SELECT state, model_lots_open FROM crew_trades").fetchone()[:] == ("CLOSED", 0)
    assert conn.execute("SELECT count(*) FROM crew_events WHERE event_type='MODEL_EXIT'").fetchone()[0] == 1
    conn.close()


def test_12_missing_entry_is_detected_and_not_repaired(tmp_path):
    path = ready(tmp_path)
    trade_id = open_model(path)[1]["trade"]["trade_id"]
    assert cj.check_consistency(path) == []
    conn = cj.connect(path)
    conn.execute("DROP TRIGGER IF EXISTS crew_events_no_delete")
    conn.execute("DELETE FROM crew_events WHERE trade_id=?", (trade_id,))
    conn.close()
    assert kinds(path) == ["missing_entry"]
    assert kinds(path) == ["missing_entry"]
    assert counts(path)[0] == 1


def test_13_orphan_event_is_detected_and_not_repaired(tmp_path):
    path = ready(tmp_path)
    open_model(path)
    conn = cj.connect(path)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DROP TRIGGER IF EXISTS crew_events_insert")
    conn.execute(
        "INSERT INTO crew_events (event_id, trade_id, seq, event_type, to_state, idempotency_key, occurred_at,"
        " recorded_at) VALUES ('orphan', 'missing-trade', 1, 'MODEL_ENTRY', 'TRACKING', 'orphan', ?, ?)",
        (CANDLE, CANDLE),
    )
    conn.close()
    assert kinds(path) == ["orphan_event"]
    conn = cj.connect(path)
    assert conn.execute("SELECT trade_id FROM crew_events WHERE event_id='orphan'").fetchone()[0] == "missing-trade"
    conn.close()


def test_14_sequence_gap_is_detected_and_not_repaired(tmp_path):
    path = ready(tmp_path)
    trade_id = open_model(path)[1]["trade"]["trade_id"]
    cj.update_levels(path, **own(path), trade_id=trade_id, expected_version=1, signal_candle="2026-11-06T10:20:00+05:30",
                     stop_spot=23090)
    cj.update_levels(path, **own(path), trade_id=trade_id, expected_version=2, signal_candle="2026-11-06T10:25:00+05:30",
                     stop_spot=23100)
    conn = cj.connect(path)
    conn.execute("DROP TRIGGER IF EXISTS crew_events_no_delete")
    conn.execute("DELETE FROM crew_events WHERE trade_id=? AND seq=2", (trade_id,))
    conn.close()
    assert kinds(path) == ["sequence_gap"]
    conn = cj.connect(path)
    assert [row[0] for row in conn.execute("SELECT seq FROM crew_events ORDER BY seq")] == [1, 3]
    conn.close()


def test_15_projection_and_version_mismatches_are_not_repaired(tmp_path):
    path = ready(tmp_path)
    version_id = open_model(path, setup_id="version")[1]["trade"]["trade_id"]
    lots_id = open_model(path, setup_id="lots")[1]["trade"]["trade_id"]
    cj.partial_exit(path, **own(path), trade_id=lots_id, expected_version=1, reason="2R", lots_after=2,
                    signal_candle="2026-11-06T10:20:00+05:30", index_levels=LEVELS)
    conn = cj.connect(path)
    conn.execute("DROP TRIGGER IF EXISTS crew_trades_update")
    conn.execute("UPDATE crew_trades SET version=7 WHERE trade_id=?", (version_id,))
    conn.execute("UPDATE crew_trades SET model_lots_open=1 WHERE trade_id=?", (lots_id,))
    conn.close()
    assert kinds(path) == ["projection_mismatch", "version_mismatch"]
    conn = cj.connect(path)
    assert conn.execute("SELECT version FROM crew_trades WHERE trade_id=?", (version_id,)).fetchone()[0] == 7
    assert conn.execute("SELECT model_lots_open FROM crew_trades WHERE trade_id=?", (lots_id,)).fetchone()[0] == 1
    conn.close()


def test_16_database_lock_contention_rejects_the_writer(tmp_path, monkeypatch):
    path = ready(tmp_path)
    holder = cj.connect(path)
    holder.execute("BEGIN IMMEDIATE")
    original = cj.connect

    def fast(db_path, *, create=False):
        conn = original(db_path, create=create)
        conn.execute("PRAGMA busy_timeout = 200")
        return conn

    monkeypatch.setattr(cj, "connect", fast)
    box = {}

    def attempt():
        try:
            box["result"] = admit(path, setup_id="locked")
        except sqlite3.OperationalError as exc:
            box["error"] = exc

    thread = threading.Thread(target=attempt, daemon=True)
    thread.start()
    thread.join(5)
    holder.execute("ROLLBACK")
    holder.close()
    assert not thread.is_alive()
    assert isinstance(box.get("error"), sqlite3.OperationalError)
    assert counts(path)[2] == 0
    assert admit(path, setup_id="locked")["replayed"] is False
    assert counts(path)[2] == 1


def test_17_transaction_rollback_removes_the_trade(tmp_path):
    path = ready(tmp_path)
    admission = admit(path)
    cj._failpoint = "event"
    try:
        with pytest.raises(cj.InvalidTransition, match="injected event failure"):
            cj.open_trade(path, **own(path), admission_id=admission["admission"]["admission_id"], model_lots=3,
                          entry_spot=23100, stop_spot=23080, target_spot=23160)
    finally:
        cj._failpoint = None
    assert counts(path) == (0, 0, 1)
    opened = cj.open_trade(path, **own(path), admission_id=admission["admission"]["admission_id"], model_lots=3,
                           entry_spot=23100, stop_spot=23080, target_spot=23160)
    assert opened["replayed"] is False and counts(path) == (1, 1, 1)


def test_18_process_kill_during_a_transaction_leaves_no_partial_rows(tmp_path):
    path = ready(tmp_path)
    script = tmp_path / "die.py"
    script.write_text(
        "import os, signal, sys\n"
        "from app.db import crew_journal as cj\n"
        "conn = cj.connect(sys.argv[1])\n"
        "conn.execute('BEGIN IMMEDIATE')\n"
        "conn.execute(\"INSERT INTO crew_sessions (session_id, market_day, started_at)"
        " VALUES ('KILL','2026-11-06','2026-11-06T10:00:00+05:30')\")\n"
        "os.kill(os.getpid(), signal.SIGKILL)\n"
    )
    proc = subprocess.run(
        [sys.executable, str(script), str(path)], check=False, cwd=BACKEND,
        env={**os.environ, "PYTHONPATH": str(BACKEND)}, capture_output=True, text=True,
    )
    assert proc.returncode < 0, proc.stderr
    conn = cj.connect(path)
    sessions = [row[0] for row in conn.execute("SELECT session_id FROM crew_sessions ORDER BY session_id")]
    conn.close()
    assert sessions == [LEASES[path].session_id]


def test_19_prior_day_trades_expire_without_a_price(tmp_path):
    path = tmp_path / "journal.db"
    cj.migrate(path)
    day1 = datetime.fromisoformat("2026-11-05T14:00:00+05:30")
    LEASES[path] = cs.acquire(path, now=day1, ttl_seconds=3600)
    yesterday = {"lease": LEASES[path], "now": day1}
    for candle, setup in (("2026-11-05T14:00:00+05:30", "yesterday"), ("2026-11-05T11:00:00+05:30", "done")):
        a = cj.record_admission(path, **yesterday, admission_class="ACTIONABLE", reasons=[],
                                **identity(market_day="2026-11-05", signal_candle=candle, setup_id=setup))
        closed = cj.open_trade(path, **yesterday, admission_id=a["admission"]["admission_id"], model_lots=3,
                               entry_spot=23100, stop_spot=23080, target_spot=23160)
    cj.exit_trade(path, **yesterday, trade_id=closed["trade"]["trade_id"], expected_version=1, reason="target",
                  signal_candle="2026-11-05T13:00:00+05:30", index_levels={"spot": 23200.0})
    LEASES[path] = cs.acquire(path, now=NOW, ttl_seconds=3600)
    open_model(path)
    first = cj.expire_unresolved(path, **own(path), at="2026-11-06T09:16:00+05:30")
    assert len(first) == 1 and first[0]["replayed"] is False
    assert first[0]["trade"]["state"] == "EXPIRED_UNRESOLVED" and first[0]["trade"]["model_lots_open"] == 3
    assert set(first[0]["event"]["payload"]) == {"as_of_day", "market_day", "reason"}
    no_money(first[0]["event"]["payload"])
    again = cj.expire_unresolved(path, **own(path), at="2026-11-06T09:16:00+05:30")
    assert len(again) == 1 and again[0]["replayed"] is True
    open_rows = cj.load_open_trades(path)
    assert [row["market_day"] for row in open_rows] == [DAY]
    conn = cj.connect(path)
    closed_row = conn.execute("SELECT state, model_lots_open FROM crew_trades WHERE trade_id=?",
                              (closed["trade"]["trade_id"],)).fetchone()
    assert tuple(closed_row) == ("CLOSED", 0)
    assert conn.execute("SELECT count(*) FROM crew_events WHERE event_type='EXPIRED_UNRESOLVED'").fetchone()[0] == 1
    conn.close()


def test_20_legacy_quarantine_does_not_modify_the_source(tmp_path):
    path = ready(tmp_path)
    before = OCT30.read_bytes()
    first = cj.quarantine_legacy_reference(path, OCT30)
    second = cj.quarantine_legacy_reference(path, OCT30)
    assert OCT30.read_bytes() == before
    assert hashlib.sha256(before).hexdigest() == OCT30_HASH
    trade, event = first[0]["trade"], first[0]["event"]
    assert first[0]["replayed"] is False and second[0]["replayed"] is True
    assert trade["trade_id"] == "TRADE_NIFTY_1793365556"
    assert trade["state"] == "QUARANTINED" and trade["admission_class"] == "RESEARCH_ONLY"
    assert trade["admission_id"] is None and trade["execution_state"] == "NONE"
    assert trade["model_lots_open"] == 2 and "58c58aa7" in trade["legacy_ref"]
    assert event["event_type"] == "LEGACY_IMPORT"
    no_money(event["payload"])
    assert cj.check_consistency(path) == []
    assert cj.load_open_trades(path) == []
    conn = cj.connect(path)
    pub = hashlib.sha256(b"pub").hexdigest()
    with pytest.raises(sqlite3.IntegrityError, match="ACTIONABLE"):
        conn.execute(
            "INSERT INTO crew_outbox (publication_id, trade_id, kind, purpose, body, body_sha256, state, created_at,"
            " chat_id) VALUES (?, 'TRADE_NIFTY_1793365556', 'RECOMMENDATION', 'entry', 'b', ?, 'PENDING', ?, '-100')",
            (pub, hashlib.sha256(b"body").hexdigest(), CANDLE),
        )
    conn.close()


def test_21_open_trade_loading_is_read_only(tmp_path):
    path = ready(tmp_path)
    live = open_model(path)[1]["trade"]["trade_id"]
    closed = open_model(path, setup_id="closed")[1]["trade"]["trade_id"]
    cj.exit_trade(path, **own(path), trade_id=closed, expected_version=1, reason="stop",
                  signal_candle="2026-11-06T11:00:00+05:30", index_levels={"spot": 23080.0})
    main = hashlib.sha256(path.read_bytes()).hexdigest()
    rows = _journal_rows(path)
    loaded = cj.load_open_trades(path, market_day=DAY)
    events = cj.get_trade_events(path, live)
    assert [row["trade_id"] for row in loaded] == [live]
    assert [event["seq"] for event in events] == [1]
    # A read-only open may create the WAL index. It must not change the database pages or rows.
    assert hashlib.sha256(path.read_bytes()).hexdigest() == main
    assert _journal_rows(path) == rows
    conn = cj.connect_readonly(path)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO crew_sessions (session_id, market_day, started_at) VALUES ('X', ?, ?)", (DAY, CANDLE))
    conn.close()


def test_22_production_database_hashes_are_unchanged(production_db_opens):
    assert hashlib.sha256(PROD_DB.read_bytes()).hexdigest() == PROD_HASH
    assert hashlib.sha256(OCT30.read_bytes()).hexdigest() == OCT30_HASH
    with pytest.raises(RuntimeError, match="production database"):
        cj.connect(PROD_DB)
    production_db_opens.clear()


def _journal_rows(path):
    conn = cj.connect_readonly(path)
    try:
        trades = [tuple(row) for row in conn.execute("SELECT * FROM crew_trades ORDER BY trade_id")]
        events = [tuple(row) for row in conn.execute("SELECT * FROM crew_events ORDER BY event_id")]
        return trades, events
    finally:
        conn.close()
