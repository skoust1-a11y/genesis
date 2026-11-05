from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from app.db import crew_journal as cj

DAY = "2026-11-06"
T0 = "2026-11-06T10:00:00+05:30"


def sid(n: int) -> str:
    return hashlib.sha256(str(n).encode()).hexdigest()


def tid(n: int) -> str:
    return f"T_{DAY.replace('-', '')}_{sid(n)[:12]}"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "journal.db"
    cj.migrate(path)
    conn = cj.connect(path)
    conn.execute("INSERT INTO crew_sessions (session_id, market_day, started_at) VALUES ('S1', ?, ?)", (DAY, T0))
    yield conn
    conn.close()


def admit(c, n, cls="ACTIONABLE", reasons=(), day=DAY):
    c.execute(
        "INSERT INTO crew_admissions (admission_id, signal_id, market_day, session_id, class, reasons, inputs, decided_at)"
        " VALUES (?, ?, ?, 'S1', ?, ?, '{}', ?)",
        (f"A{n}", sid(n), day, cls, json.dumps(list(reasons)), T0),
    )


def open_trade(c, n, cls="ACTIONABLE", lots=3):
    if cls == "ACTIONABLE":
        admit(c, n)
    else:
        admit(c, n, cls, ["QUOTE_NOT_EXECUTABLE", "CAPITAL_UNAVAILABLE"])
    c.execute(
        "INSERT INTO crew_trades (trade_id, signal_id, admission_id, admission_class, market_day, owner_session_id,"
        " state, strategy, direction, model_lots_initial, model_lots_open, opened_at)"
        " VALUES (?, ?, ?, ?, ?, 'S1', 'TRACKING', 'PDC', 'CALL', ?, ?, ?)",
        (tid(n), sid(n), f"A{n}", cls, DAY, lots, lots, T0),
    )
    event(c, n, 1, "MODEL_ENTRY", None, "TRACKING")
    return tid(n)


def event(c, n, seq, etype, frm, to, payload=None, key=None):
    c.execute(
        "INSERT INTO crew_events (event_id, trade_id, seq, event_type, from_state, to_state, idempotency_key,"
        " session_id, occurred_at, recorded_at, payload) VALUES (?, ?, ?, ?, ?, ?, ?, 'S1', ?, ?, ?)",
        (f"E{n}-{seq}", tid(n), seq, etype, frm, to, key or f"{tid(n)}:{seq}", T0, T0, json.dumps(payload or {})),
    )


def step(c, n, etype, to, lots_after=None, closed=False, payload=None):
    """Append the next event and apply it to the trade (the lockstep protocol)."""
    t = c.execute("SELECT * FROM crew_trades WHERE trade_id=?", (tid(n),)).fetchone()
    lots = t["model_lots_open"] if lots_after is None else lots_after
    body = payload if payload is not None else {"lots_before": t["model_lots_open"], "lots_after": lots}
    event(c, n, t["version"] + 1, etype, t["state"], to, body)
    c.execute(
        "UPDATE crew_trades SET state=?, model_lots_open=?, version=version+1, closed_at=? WHERE trade_id=?",
        (to, lots, T0 if closed else None, tid(n)),
    )


def pub(n):
    return sid(1000 + n)


def enqueue(c, n, kind="RECOMMENDATION", trade=None, parent=None, state="PENDING"):
    c.execute(
        "INSERT INTO crew_outbox (publication_id, trade_id, parent_publication_id, kind, purpose, body, body_sha256,"
        " state, created_at, chat_id) VALUES (?, ?, ?, ?, 'entry', 'b', ?, ?, ?, '-100')",
        (pub(n), trade, parent, kind, sid(0), state, T0),
    )
    audit(c, n, 1, None, state)


def audit(c, n, seq, frm, to, **kw):
    cols = {"publication_id": pub(n), "seq": seq, "from_state": frm, "to_state": to, "at": T0, **kw}
    c.execute(
        f"INSERT INTO crew_outbox_events ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        tuple(cols.values()),
    )


def move(c, n, to, **kw):
    """Append the next audit row and apply the transition to the outbox row."""
    o = c.execute("SELECT * FROM crew_outbox WHERE publication_id=?", (pub(n),)).fetchone()
    if to == "SENDING":
        kw.setdefault("session_id", "S1")
    audit(c, n, o["version"] + 1, o["state"], to, **kw)
    c.execute(
        "UPDATE crew_outbox SET state=?, version=version+1, attempts=attempts+?, claimed_at=coalesce(?, claimed_at),"
        " claimed_by_session=coalesce(?, claimed_by_session), acked_at=?, telegram_message_id=? WHERE publication_id=?",
        (to, int(to == "SENDING"), T0 if to == "SENDING" else None, kw.get("session_id"),
         T0 if to == "ACKED" else None, kw.get("message_id") if to in ("ACKED", "RESOLVED_DELIVERED") else None,
         pub(n)),
    )  # fmt: skip


ACK = {"http_status": 200, "telegram_ok": 1, "message_id": 42}
RESOLVE = {"operator": "skoust", "method": "channel_inspection", "note": "checked channel"}


def rejects(match="."):
    return pytest.raises(sqlite3.IntegrityError, match=match)


# --- schema and migration ---------------------------------------------------------------------


def test_migrate_is_idempotent_additive_and_sets_pragmas(tmp_path):
    path = tmp_path / "j.db"
    legacy = sqlite3.connect(path)
    legacy.execute("CREATE TABLE live_and_forward_trades (trade_id TEXT PRIMARY KEY)")
    legacy.execute("INSERT INTO live_and_forward_trades VALUES ('keep')")
    legacy.commit()
    legacy.close()

    assert cj.migrate(path) == cj.SCHEMA_VERSION
    c = cj.connect(path)
    schema = c.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
    c.close()
    assert cj.migrate(path) == cj.SCHEMA_VERSION
    c = cj.connect(path)
    assert c.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall() == schema
    names = {r["name"] for r in schema}
    assert {"crew_meta", "crew_sessions", "crew_admissions", "crew_trades", "crew_events", "crew_outbox",
            "crew_outbox_events"} <= names  # fmt: skip
    assert [tuple(r) for r in c.execute("SELECT trade_id FROM live_and_forward_trades")] == [("keep",)]
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert c.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    c.close()


def test_connect_never_creates_a_database(tmp_path):
    with pytest.raises(sqlite3.OperationalError):
        cj.connect(tmp_path / "missing.db")
    assert not (tmp_path / "missing.db").exists()


def test_migrate_refuses_newer_schema(tmp_path):
    path = tmp_path / "j.db"
    cj.migrate(path)
    c = cj.connect(path)
    c.execute("UPDATE crew_meta SET value='99' WHERE key='schema_version'")
    c.close()
    with pytest.raises(RuntimeError, match="newer"):
        cj.migrate(path)


# --- trade invariants -------------------------------------------------------------------------


def test_invalid_foreign_keys(db):
    with rejects("FOREIGN KEY"):
        db.execute(
            "INSERT INTO crew_admissions (admission_id, signal_id, market_day, session_id, class, reasons, inputs,"
            " decided_at) VALUES ('A9', ?, ?, 'NO_SUCH_SESSION', 'REJECTED', '[\"QUOTE_MISSING\"]', '{}', ?)",
            (sid(9), DAY, T0),
        )
    with rejects():
        db.execute(
            "INSERT INTO crew_trades (trade_id, signal_id, admission_id, admission_class, market_day, state, strategy,"
            " direction, model_lots_initial, model_lots_open, opened_at)"
            " VALUES (?, ?, 'NO_SUCH_ADMISSION', 'ACTIONABLE', ?, 'TRACKING', 'PDC', 'CALL', 1, 1, ?)",
            (tid(9), sid(9), DAY, T0),
        )
    with rejects():
        event(db, 9, 1, "MODEL_ENTRY", None, "TRACKING")


def test_duplicate_signal_ids_rejected(db):
    open_trade(db, 1)
    with rejects("duplicate"):
        admit(db, 1)
    with rejects("duplicate"):
        db.execute("INSERT OR REPLACE INTO crew_admissions SELECT * FROM crew_admissions WHERE admission_id='A1'")
    admit(db, 2)
    with rejects("duplicate"):
        db.execute(
            "INSERT INTO crew_trades (trade_id, signal_id, admission_id, admission_class, market_day, state, strategy,"
            " direction, model_lots_initial, model_lots_open, opened_at)"
            " VALUES (?, ?, 'A2', 'ACTIONABLE', ?, 'TRACKING', 'PDC', 'CALL', 1, 1, ?)",
            (tid(2), sid(1), DAY, T0),
        )


def test_signal_identity_is_deterministic(db):
    admit(db, 3)
    with rejects("CHECK"):
        db.execute(
            "INSERT INTO crew_admissions (admission_id, signal_id, market_day, session_id, class, reasons, inputs,"
            " decided_at) VALUES ('Ax', 'not-a-hash', ?, 'S1', 'ACTIONABLE', '[]', '{}', ?)",
            (DAY, T0),
        )
    with rejects("CHECK"):
        db.execute(
            "INSERT INTO crew_trades (trade_id, signal_id, admission_id, admission_class, market_day, state, strategy,"
            " direction, model_lots_initial, model_lots_open, opened_at)"
            " VALUES ('T_random', ?, 'A3', 'ACTIONABLE', ?, 'TRACKING', 'PDC', 'CALL', 1, 1, ?)",
            (sid(3), DAY, T0),
        )


def test_trade_must_match_its_admission(db):
    admit(db, 5, "REJECTED", ["QUOTE_MISSING"])
    with rejects():
        db.execute(
            "INSERT INTO crew_trades (trade_id, signal_id, admission_id, admission_class, market_day, state, strategy,"
            " direction, model_lots_initial, model_lots_open, opened_at)"
            " VALUES (?, ?, 'A5', 'ACTIONABLE', ?, 'TRACKING', 'PDC', 'CALL', 1, 1, ?)",
            (tid(5), sid(5), DAY, T0),
        )
    with rejects("RESEARCH_ONLY allows only"):
        admit(db, 6, "RESEARCH_ONLY", ["QUOTE_MISSING"])
    with rejects("CHECK"):
        admit(db, 7, "ACTIONABLE", ["CAPITAL_UNAVAILABLE"])


def test_duplicate_event_ids_and_sequences_rejected(db):
    open_trade(db, 1)
    with rejects("duplicate"):
        event(db, 1, 1, "MODEL_ENTRY", None, "TRACKING", key="other")
    with rejects("duplicate"):
        db.execute("INSERT OR REPLACE INTO crew_events SELECT * FROM crew_events WHERE event_id='E1-1'")
    step(db, 1, "MODEL_LEVELS_UPDATED", "TRACKING", payload={"stop_spot": 1})
    with rejects():
        event(db, 1, 2, "MODEL_LEVELS_UPDATED", "TRACKING", "TRACKING", key="fresh")
    with rejects("follow"):
        event(db, 1, 4, "MODEL_LEVELS_UPDATED", "TRACKING", "TRACKING", key="gap")


def test_events_and_admissions_are_append_only(db):
    open_trade(db, 1)
    for sql in (
        "UPDATE crew_events SET payload='{}'",
        "DELETE FROM crew_events",
        "UPDATE crew_admissions SET class='REJECTED'",
        "DELETE FROM crew_admissions",
        "DELETE FROM crew_trades",
    ):
        with rejects("append-only|cannot be deleted"):
            db.execute(sql)


def test_full_lifecycle_and_lot_accounting(db):
    open_trade(db, 1, lots=3)
    step(db, 1, "MODEL_PARTIAL_EXIT", "PARTIALLY_EXITED", lots_after=2)
    step(db, 1, "MODEL_PARTIAL_EXIT", "PARTIALLY_EXITED", lots_after=1)
    step(db, 1, "MODEL_EXIT", "CLOSED", lots_after=0, closed=True, payload={"model_points": 20, "model_r": 1.0})
    t = db.execute("SELECT state, model_lots_open, version, execution_state FROM crew_trades").fetchone()
    assert tuple(t) == ("CLOSED", 0, 4, "NONE")
    assert db.execute("SELECT count(*) FROM crew_events WHERE to_state='CLOSED'").fetchone()[0] == 1


def test_illegal_transitions_rejected(db):
    open_trade(db, 1)
    with rejects("illegal"):
        event(db, 1, 2, "MODEL_EXIT", "TRACKING", "TRACKING")
    with rejects("illegal"):
        event(db, 1, 2, "MODEL_ENTRY", "TRACKING", "TRACKING")
    with rejects("follow"):
        event(db, 1, 2, "MODEL_EXIT", "PARTIALLY_EXITED", "CLOSED")
    step(db, 1, "MODEL_PARTIAL_EXIT", "PARTIALLY_EXITED", lots_after=2)
    with rejects("illegal"):
        event(db, 1, 3, "MODEL_PARTIAL_EXIT", "PARTIALLY_EXITED", "TRACKING")
    with rejects("match the next"):
        db.execute("UPDATE crew_trades SET state='CLOSED', model_lots_open=0, closed_at=?", (T0,))
    with rejects("match the next"):
        db.execute("UPDATE crew_trades SET stop_spot=1")


@pytest.mark.parametrize("terminal", ["CLOSED", "EXPIRED_UNRESOLVED", "QUARANTINED"])
def test_terminal_trades_cannot_reopen(db, terminal):
    open_trade(db, 1)
    etype = "MODEL_EXIT" if terminal == "CLOSED" else terminal
    step(db, 1, etype, terminal, lots_after=0 if terminal == "CLOSED" else None, closed=True, payload={})
    with rejects():
        event(db, 1, 3, "MODEL_LEVELS_UPDATED", terminal, terminal)
    with rejects():
        event(db, 1, 3, "MODEL_EXIT", "TRACKING", "CLOSED", key="second-exit")
    with rejects("terminal"):
        db.execute("UPDATE crew_trades SET state='TRACKING', closed_at=NULL, version=version+1")


def test_partial_exit_requires_matching_lot_evidence(db):
    open_trade(db, 1, lots=3)
    event(db, 1, 2, "MODEL_PARTIAL_EXIT", "TRACKING", "PARTIALLY_EXITED", {"lots_before": 3, "lots_after": 2})
    with rejects("match the next"):
        db.execute("UPDATE crew_trades SET state='PARTIALLY_EXITED', model_lots_open=1, version=2")


@pytest.mark.parametrize("initial,open_", [(0, 0), (3, 4), (3, -1), (2.5, 2), (3, "2")])
def test_invalid_model_lots_rejected(db, initial, open_):
    admit(db, 1)
    with rejects():
        db.execute(
            "INSERT INTO crew_trades (trade_id, signal_id, admission_id, admission_class, market_day, state, strategy,"
            " direction, model_lots_initial, model_lots_open, opened_at)"
            " VALUES (?, ?, 'A1', 'ACTIONABLE', ?, 'TRACKING', 'PDC', 'CALL', ?, ?, ?)",
            (tid(1), sid(1), DAY, initial, open_, T0),
        )


def test_execution_state_fixed_to_none(db):
    admit(db, 1)
    with rejects("CHECK"):
        db.execute(
            "INSERT INTO crew_trades (trade_id, signal_id, admission_id, admission_class, market_day, state,"
            " execution_state, strategy, direction, model_lots_initial, model_lots_open, opened_at)"
            " VALUES (?, ?, 'A1', 'ACTIONABLE', ?, 'TRACKING', 'FILLED', 'PDC', 'CALL', 1, 1, ?)",
            (tid(1), sid(1), DAY, T0),
        )
    open_trade(db, 2)
    event(db, 2, 2, "MODEL_LEVELS_UPDATED", "TRACKING", "TRACKING")
    with rejects("CHECK"):
        db.execute("UPDATE crew_trades SET execution_state='FILLED', version=2 WHERE trade_id=?", (tid(2),))


@pytest.mark.parametrize("payload", [
    {"fill_price": 101.5}, {"pnl_rupees": 500}, {"Realized_PnL": 1}, {"broker_order_id": "x"},
    {"detail": {"avg_price": 1}}, {"legs": [{"broker_qty": 75}]},
])  # fmt: skip
def test_forbidden_execution_and_pnl_payload_keys(db, payload):
    open_trade(db, 1)
    with rejects("forbidden"):
        event(db, 1, 2, "MODEL_LEVELS_UPDATED", "TRACKING", "TRACKING", payload)


def test_one_terminal_event_per_trade(db):
    open_trade(db, 1)
    step(db, 1, "MODEL_EXIT", "CLOSED", lots_after=0, closed=True, payload={})
    assert db.execute("SELECT count(*) FROM crew_events WHERE to_state IN ('CLOSED','EXPIRED_UNRESOLVED',"
                      "'QUARANTINED')").fetchone()[0] == 1  # fmt: skip


def test_legacy_import_is_quarantined_research_only(db):
    c = db
    c.execute(
        "INSERT INTO crew_trades (trade_id, signal_id, admission_class, market_day, state, strategy, direction,"
        " model_lots_initial, model_lots_open, opened_at, closed_at, legacy_ref)"
        " VALUES ('L1', ?, 'RESEARCH_ONLY', '2026-10-30', 'QUARANTINED', 'legacy', 'CALL', 3, 3, ?, ?, 'f#58c58aa7')",
        (sid(77), T0, T0),
    )
    c.execute(
        "INSERT INTO crew_events (event_id, trade_id, seq, event_type, from_state, to_state, idempotency_key,"
        " occurred_at, recorded_at) VALUES ('EL', 'L1', 1, 'LEGACY_IMPORT', NULL, 'QUARANTINED', 'L1:import', ?, ?)",
        (T0, T0),
    )
    with rejects("CHECK"):
        c.execute(
            "INSERT INTO crew_trades (trade_id, signal_id, admission_class, market_day, state, strategy, direction,"
            " model_lots_initial, model_lots_open, opened_at) VALUES ('L2', ?, 'RESEARCH_ONLY', ?, 'TRACKING',"
            " 'legacy', 'CALL', 1, 1, ?)", (sid(78), DAY, T0))  # fmt: skip
    with rejects("ACTIONABLE"):
        c.execute(
            "INSERT INTO crew_outbox (publication_id, trade_id, kind, purpose, body, body_sha256, state, created_at,"
            " chat_id) VALUES (?, 'L1', 'RECOMMENDATION', 'entry', 'b', ?, 'PENDING', ?, '-100')",
            (pub(1), sid(0), T0))  # fmt: skip


# --- publication invariants -------------------------------------------------------------------


def test_research_only_cannot_publish(db):
    t = open_trade(db, 1, cls="RESEARCH_ONLY")
    with rejects("ACTIONABLE"):
        enqueue(db, 1, trade=t)
    with rejects("ACTIONABLE"):
        enqueue(db, 1, trade=t, state="SUPPRESSED")


def test_publication_identity_unique(db):
    t = open_trade(db, 1)
    enqueue(db, 1, trade=t)
    with rejects("duplicate"):
        enqueue(db, 1, trade=t)
    with rejects("duplicate"):
        db.execute("INSERT OR REPLACE INTO crew_outbox SELECT * FROM crew_outbox")


def test_happy_delivery_path(db):
    t = open_trade(db, 1)
    enqueue(db, 1, trade=t)
    move(db, 1, "SENDING")
    move(db, 1, "ACKED", **ACK)
    enqueue(db, 2, kind="MONITORING", trade=t, parent=pub(1))
    enqueue(db, 3, kind="OPS")
    move(db, 3, "SENDING")
    move(db, 3, "RETRY_WAIT", exc_class="ConnectTimeout")
    move(db, 3, "SENDING")
    move(db, 3, "FAILED_PERMANENT", http_status=400)
    row = db.execute("SELECT state, telegram_message_id, attempts FROM crew_outbox WHERE publication_id=?",
                     (pub(1),)).fetchone()  # fmt: skip
    assert tuple(row) == ("ACKED", 42, 1)


@pytest.mark.parametrize("path", [
    ["ACKED"], ["UNKNOWN"], ["SENDING", "PENDING"], ["SENDING", "SUPPRESSED"],
    ["SENDING", "ACKED", "SENDING"], ["SUPPRESSED", "PENDING"], ["SENDING", "FAILED_PERMANENT", "SENDING"],
])  # fmt: skip
def test_invalid_telegram_transitions(db, path):
    enqueue(db, 1, kind="OPS")
    *ok, bad = path
    for s in ok:
        move(db, 1, s, **(ACK if s == "ACKED" else {}))
    with rejects():
        move(db, 1, bad, **(ACK if bad == "ACKED" else {}))


def test_unknown_never_goes_back_to_sending(db):
    t = open_trade(db, 1)
    enqueue(db, 1, trade=t)
    move(db, 1, "SENDING")
    move(db, 1, "UNKNOWN", exc_class="ReadTimeout")
    with rejects("illegal"):
        move(db, 1, "SENDING")
    with rejects("illegal"):
        move(db, 1, "RETRY_WAIT")
    with rejects("match the next"):
        db.execute("UPDATE crew_outbox SET state='SENDING', version=version+1, attempts=attempts+1")


def test_unknown_recommendation_blocks_new_recommendations(db):
    t1, t2 = open_trade(db, 1), open_trade(db, 2)
    enqueue(db, 1, trade=t1)
    enqueue(db, 2, trade=t2)
    move(db, 1, "SENDING")
    move(db, 1, "UNKNOWN")
    with rejects("prior one is UNKNOWN"):
        enqueue(db, 3, trade=open_trade(db, 3))
    enqueue(db, 4, trade=open_trade(db, 4), state="SUPPRESSED")
    with rejects("blocked by an UNKNOWN"):
        move(db, 2, "SENDING")
    enqueue(db, 5, kind="OPS")
    move(db, 5, "SENDING")


def test_monitoring_requires_delivered_parent(db):
    t = open_trade(db, 1)
    enqueue(db, 1, trade=t)
    with rejects("SUPPRESSED while its parent"):
        enqueue(db, 2, kind="MONITORING", trade=t, parent=pub(1))
    enqueue(db, 2, kind="MONITORING", trade=t, parent=pub(1), state="SUPPRESSED")
    with rejects("same trade's RECOMMENDATION"):
        enqueue(db, 3, kind="MONITORING", trade=t)


@pytest.mark.parametrize("evidence", [
    {}, {"http_status": 200, "telegram_ok": 1}, {"http_status": 200, "telegram_ok": 0, "message_id": 42},
    {"http_status": 500, "telegram_ok": 1, "message_id": 42}, {"http_status": 200, "telegram_ok": 1, "message_id": 0},
    {"http_status": 200, "telegram_ok": 1, "message_id": "abc"},
])  # fmt: skip
def test_invalid_acked_records(db, evidence):
    enqueue(db, 1, kind="OPS")
    move(db, 1, "SENDING")
    with rejects():
        move(db, 1, "ACKED", **evidence)


def test_acked_row_must_carry_the_audited_message_id(db):
    enqueue(db, 1, kind="OPS")
    move(db, 1, "SENDING")
    audit(db, 1, 3, "SENDING", "ACKED", **ACK)
    with rejects("match the next"):
        db.execute("UPDATE crew_outbox SET state='ACKED', version=3, acked_at=?, telegram_message_id=7", (T0,))


def test_operator_resolution_is_audited(db):
    t = open_trade(db, 1)
    enqueue(db, 1, trade=t)
    move(db, 1, "SENDING")
    move(db, 1, "UNKNOWN")
    for bad in ({}, {**RESOLVE, "note": " "}, {**RESOLVE, "method": "guess"}, RESOLVE):  # last: no message_id
        with rejects():
            move(db, 1, "RESOLVED_DELIVERED", **bad)
    move(db, 1, "RESOLVED_DELIVERED", message_id=99, **RESOLVE)
    row = db.execute("SELECT operator, method, note, message_id FROM crew_outbox_events WHERE seq=4").fetchone()
    assert tuple(row) == ("skoust", "channel_inspection", "checked channel", 99)
    with rejects("terminal"):
        db.execute("UPDATE crew_outbox SET last_error='x'")


def test_operator_only_on_resolution(db):
    enqueue(db, 1, kind="OPS")
    with rejects("CHECK"):
        move(db, 1, "SENDING", operator="skoust")


def test_claim_limit_of_three_attempts(db):
    enqueue(db, 1, kind="OPS")
    for _ in range(3):
        move(db, 1, "SENDING")
        move(db, 1, "RETRY_WAIT")
    with rejects("claim limit"):
        move(db, 1, "SENDING")
    move(db, 1, "SUPPRESSED")


def test_outbox_audit_is_immutable(db):
    enqueue(db, 1, kind="OPS")
    for sql in ("UPDATE crew_outbox_events SET note='x'", "DELETE FROM crew_outbox_events", "DELETE FROM crew_outbox"):
        with rejects("append-only|cannot be deleted"):
            db.execute(sql)
    with rejects("duplicate"):
        db.execute("INSERT OR REPLACE INTO crew_outbox_events SELECT * FROM crew_outbox_events")


def test_bot_token_never_stored(db):
    enqueue(db, 1, kind="OPS")
    move(db, 1, "SENDING")
    with rejects("CHECK"):
        move(db, 1, "UNKNOWN", response_excerpt="https://api.telegram.org/bot123456:ABCdef/sendMessage")
    move(db, 1, "UNKNOWN", response_excerpt="https://api.telegram.org/bot***/sendMessage")


# --- atomicity --------------------------------------------------------------------------------


def test_transaction_rolls_back_on_constraint_failure(db):
    with pytest.raises(sqlite3.IntegrityError), cj.transaction(db):
        admit(db, 1)
        db.execute(
            "INSERT INTO crew_trades (trade_id, signal_id, admission_id, admission_class, market_day, state, strategy,"
            " direction, model_lots_initial, model_lots_open, opened_at)"
            " VALUES (?, ?, 'A1', 'ACTIONABLE', ?, 'TRACKING', 'PDC', 'CALL', 2, 5, ?)",
            (tid(1), sid(1), DAY, T0),
        )
    assert not db.in_transaction
    assert db.execute("SELECT count(*) FROM crew_admissions").fetchone()[0] == 0
    with cj.transaction(db):
        open_trade(db, 1)
    assert db.execute("SELECT count(*) FROM crew_trades").fetchone()[0] == 1


def test_production_trades_db_is_guarded(production_db_opens):
    from app.db import trade_store

    with pytest.raises(RuntimeError, match="production database"):
        cj.connect(trade_store.ROOT_DIR / "data" / "trades.db")
    production_db_opens.clear()
