"""S1-P4 durable Telegram outbox over the P1 ``crew_outbox`` / ``crew_outbox_events`` tables.

Lifecycle (the P1 triggers enforce every edge):
  PENDING -> SENDING -> ACKED
  PENDING | RETRY_WAIT -> SUPPRESSED
  SENDING -> UNKNOWN | RETRY_WAIT | FAILED_PERMANENT   (the last two only on proven non-delivery)
  RETRY_WAIT -> SENDING                                 (bounded by attempts <= 3)
  UNKNOWN -> RESOLVED_DELIVERED | RESOLVED_NOT_DELIVERED   (operator with channel evidence only)

Nothing here ever moves UNKNOWN back to SENDING, and nothing here places or implies an order.
Each write is one BEGIN IMMEDIATE transaction: audit row (seq = version + 1) first, then the row update.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from app.db import crew_journal as cj
from app.telegram_transport import DELIVERED, NOT_DELIVERED, DeliveryOutcome, Transport, classify_exception, redact
from app.trading.option_quote import IST

KINDS = ("RECOMMENDATION", "MONITORING", "OPS")
MAX_ATTEMPTS = 3
# ponytail: one fixed entry-recommendation shelf life; make it per-strategy if the strategies need it.
RECOMMENDATION_MAX_AGE_SECONDS = 120
_DELIVERED_STATES = ("ACKED", "RESOLVED_DELIVERED")


def publication_id_for(*, kind: str, purpose: str, trade_id: str | None = None,
                       parent_publication_id: str | None = None) -> str:
    """SHA-256 of the publication identity. The body is not part of it: one message per purpose."""
    text = "\n".join((f"kind={kind}", f"trade_id={trade_id or ''}",
                      f"parent={parent_publication_id or ''}", f"purpose={cj._text(purpose)}"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def enqueue_publication(db_path: Path | str, *, kind: str, purpose: str, body: str, chat_id: str,
                        trade_id: str | None = None, parent_publication_id: str | None = None,
                        now: datetime | None = None) -> dict:
    """Insert a publication once, bound to one destination chat. A replay with the same body and chat
    returns the stored row."""
    if kind not in KINDS:
        raise cj.InvalidTransition(f"kind must be one of {KINDS}")
    if not isinstance(chat_id, str) or not chat_id or chat_id != chat_id.strip():
        raise cj.InvalidTransition("chat_id must be a non-empty string without surrounding spaces")
    if (kind == "OPS") != (trade_id is None):
        raise cj.InvalidTransition("OPS has no trade; RECOMMENDATION and MONITORING need one")
    why = cj._text(purpose)
    if not why or not isinstance(body, str) or not body.strip():
        raise cj.InvalidTransition("purpose and body are required")
    if redact(body) != body:
        raise cj.InvalidTransition("body contains a credential-like token")
    at = _stamp(now)
    pid = publication_id_for(kind=kind, purpose=why, trade_id=trade_id, parent_publication_id=parent_publication_id)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()

    def op(conn: sqlite3.Connection) -> dict:
        existing = _pub(conn, pid)
        if existing is not None:
            if existing["body_sha256"] != digest:
                raise cj.InvalidTransition(f"publication {pid} already exists with a different body")
            if existing["chat_id"] != chat_id:
                raise cj.InvalidTransition(f"publication {pid} already exists for a different chat")
            return {"replayed": True, "publication": dict(existing)}
        state, note = "PENDING", None
        if kind != "OPS":
            trade = _trade(conn, trade_id)
            if trade is None or trade["admission_class"] != "ACTIONABLE" or trade["admitted"] != "ACTIONABLE":
                raise cj.InvalidTransition(f"{kind} requires an ACTIONABLE trade")
        if kind == "MONITORING":
            parent = _pub(conn, parent_publication_id) if parent_publication_id else None
            if parent is None or parent["kind"] != "RECOMMENDATION" or parent["state"] not in _DELIVERED_STATES:
                raise cj.InvalidTransition("MONITORING requires a delivered RECOMMENDATION parent")
            if parent["chat_id"] != chat_id:
                raise cj.InvalidTransition("MONITORING must go to its parent's chat")
        if kind == "RECOMMENDATION":
            note = _stale(trade, now=at) or _unknown_block(conn, pid)
            state = "SUPPRESSED" if note else "PENDING"
        conn.execute(
            "INSERT INTO crew_outbox (publication_id, trade_id, parent_publication_id, kind, purpose, body,"
            " body_sha256, state, created_at, chat_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (pid, trade_id, parent_publication_id, kind, why, body, digest, state, at, chat_id),
        )
        _audit(conn, pid, 1, None, state, at=at, note=note)
        return {"replayed": False, "publication": dict(_pub(conn, pid))}

    return cj._immediate(db_path, op)


def claim_publication(db_path: Path | str, publication_id: str, *, session_id: str,
                      now: datetime | None = None) -> dict:
    """PENDING/due RETRY_WAIT -> SENDING. A stale recommendation is SUPPRESSED instead; check ``state``."""
    at = _stamp(now)

    def op(conn: sqlite3.Connection) -> dict:
        row = _require(conn, publication_id)
        if row["state"] not in ("PENDING", "RETRY_WAIT"):
            raise cj.InvalidTransition(f"{publication_id} is {row['state']} and cannot be claimed")
        if row["state"] == "RETRY_WAIT" and _parse(row["not_before"]) > _parse(at):
            raise cj.InvalidTransition(f"{publication_id} is not due before {row['not_before']}")
        if row["chat_id"] is None:
            raise cj.InvalidTransition(f"{publication_id} predates schema v2 and has no recorded chat; suppress it")
        if row["kind"] == "RECOMMENDATION":
            stale = _stale(_trade(conn, row["trade_id"]), now=at)
            if stale:
                return _transition(conn, row, "SUPPRESSED", at=at, note=stale)
            blocked = _unknown_block(conn, publication_id)
            if blocked:
                raise cj.InvalidTransition(blocked)
        if row["attempts"] >= MAX_ATTEMPTS:
            raise cj.InvalidTransition(f"{publication_id} reached {MAX_ATTEMPTS} attempts")
        return _transition(conn, row, "SENDING", at=at, session_id=session_id)

    return cj._immediate(db_path, op)


def acknowledge_publication(db_path: Path | str, publication_id: str, *, session_id: str, message_id: int,
                            latency_ms: int | None = None, now: datetime | None = None) -> dict:
    """SENDING -> ACKED on HTTP 200, ok:true and a message_id not already used by another publication."""
    return record_outcome(db_path, publication_id, DeliveryOutcome(DELIVERED, 200, True, message_id,
                                                                   latency_ms=latency_ms),
                          session_id=session_id, now=now)


def mark_unknown(db_path: Path | str, publication_id: str, *, session_id: str, reason: str,
                 outcome: DeliveryOutcome | None = None, now: datetime | None = None) -> dict:
    """SENDING -> UNKNOWN. The message may have reached Telegram; only an operator can resolve it."""
    at = _stamp(now)

    def op(conn: sqlite3.Connection) -> dict:
        row = _require(conn, publication_id)
        if row["state"] == "UNKNOWN":
            return dict(row)
        _owned(row, session_id)
        return _to_unknown(conn, row, at=at, session_id=session_id, reason=reason, outcome=outcome)

    return cj._immediate(db_path, op)


def record_outcome(db_path: Path | str, publication_id: str, outcome: DeliveryOutcome, *, session_id: str,
                   now: datetime | None = None) -> dict:
    """Apply one transport outcome to a SENDING publication owned by ``session_id``."""
    at = _stamp(now)

    def op(conn: sqlite3.Connection) -> dict:
        row = _require(conn, publication_id)
        if row["state"] == "ACKED" and outcome.status == DELIVERED and row["telegram_message_id"] == outcome.message_id:
            return dict(row)
        _owned(row, session_id)
        evidence = _evidence(outcome)
        if outcome.status == DELIVERED:
            mid = outcome.message_id
            if type(mid) is not int or mid <= 0:
                return _to_unknown(conn, row, at=at, session_id=session_id, reason="DELIVERED without a message_id",
                                   outcome=outcome)
            clash = _message_owner(conn, row, mid)
            if clash is not None:
                return _to_unknown(conn, row, at=at, session_id=session_id, outcome=outcome,
                                   reason=f"message_id {mid} already belongs to {clash}")
            return _transition(conn, row, "ACKED", at=at, session_id=session_id, **{**evidence, "message_id": mid,
                                                                                    "http_status": 200, "telegram_ok": 1})
        if outcome.status == NOT_DELIVERED:
            reason = f"NOT_DELIVERED {outcome.exc_class or outcome.http_status}: {outcome.detail or ''}"
            if outcome.retry_after is not None and row["attempts"] < MAX_ATTEMPTS:
                due = (_parse(at) + timedelta(seconds=outcome.retry_after)).isoformat()
                return _transition(conn, row, "RETRY_WAIT", at=at, session_id=session_id, not_before=due,
                                   last_error=reason, note=reason, **evidence)
            return _transition(conn, row, "FAILED_PERMANENT", at=at, session_id=session_id, last_error=reason,
                               note=reason, **evidence)
        return _to_unknown(conn, row, at=at, session_id=session_id, outcome=outcome,
                           reason=f"UNKNOWN {outcome.exc_class or outcome.http_status}: {outcome.detail or ''}")

    return cj._immediate(db_path, op)


def suppress_publication(db_path: Path | str, publication_id: str, *, reason: str,
                         now: datetime | None = None) -> dict:
    """PENDING or RETRY_WAIT -> SUPPRESSED. Never applies to SENDING or UNKNOWN."""
    why = cj._text(reason)
    if not why:
        raise cj.InvalidTransition("suppression needs a reason")
    at = _stamp(now)

    def op(conn: sqlite3.Connection) -> dict:
        row = _require(conn, publication_id)
        if row["state"] == "SUPPRESSED":
            return dict(row)
        if row["state"] not in ("PENDING", "RETRY_WAIT"):
            raise cj.InvalidTransition(f"{publication_id} is {row['state']} and cannot be suppressed")
        return _transition(conn, row, "SUPPRESSED", at=at, note=why)

    return cj._immediate(db_path, op)


def resolve_unknown(db_path: Path | str, publication_id: str, *, operator: str, delivered: bool, evidence: str,
                    message_id: int | None = None, now: datetime | None = None) -> dict:
    """UNKNOWN -> RESOLVED_*. Records who inspected the channel, when, and what they saw."""
    who, note = cj._text(operator), cj._text(evidence)
    if not who or not note:
        raise cj.InvalidTransition("resolution needs an operator and channel evidence")
    if type(delivered) is not bool:
        raise cj.InvalidTransition("delivered must be True or False")
    if delivered != (type(message_id) is int and message_id > 0) or (not delivered and message_id is not None):
        raise cj.InvalidTransition("delivered needs the observed message_id; not delivered must have none")
    at = _stamp(now)

    def op(conn: sqlite3.Connection) -> dict:
        row = _require(conn, publication_id)
        if row["state"] != "UNKNOWN":
            raise cj.InvalidTransition(f"{publication_id} is {row['state']}, not UNKNOWN")
        if delivered and _message_owner(conn, row, message_id):
            raise cj.InvalidTransition(f"message_id {message_id} already belongs to another publication")
        to = "RESOLVED_DELIVERED" if delivered else "RESOLVED_NOT_DELIVERED"
        return _transition(conn, row, to, at=at, operator=who, method="channel_inspection", note=note,
                           message_id=message_id)

    return cj._immediate(db_path, op)


def recover_inflight_publications(db_path: Path | str, *, session_id: str, now: datetime | None = None) -> list[dict]:
    """Startup: every SENDING row not claimed by ``session_id`` becomes UNKNOWN. Nothing is resent."""
    at = _stamp(now)

    def op(conn: sqlite3.Connection) -> list[dict]:
        rows = conn.execute("SELECT * FROM crew_outbox WHERE state='SENDING' AND claimed_by_session IS NOT ?"
                            " ORDER BY created_at, publication_id", (session_id,)).fetchall()
        return [_to_unknown(conn, row, at=at, session_id=session_id,
                            reason=f"RECOVERED_INFLIGHT claimed by {row['claimed_by_session']} at {row['claimed_at']}")
                for row in rows]

    return cj._immediate(db_path, op)


def list_pending_publications(db_path: Path | str, *, now: datetime | None = None) -> list[dict]:
    """PENDING rows and RETRY_WAIT rows that are due. Read-only."""
    at = _parse(_stamp(now))
    conn = cj.connect_readonly(db_path)
    try:
        rows = conn.execute("SELECT * FROM crew_outbox WHERE state IN ('PENDING','RETRY_WAIT')"
                            " ORDER BY created_at, publication_id").fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows if r["state"] == "PENDING" or _parse(r["not_before"]) <= at]


def inspect_publication(db_path: Path | str, publication_id: str) -> dict:
    conn = cj.connect_readonly(db_path)
    try:
        row = _require(conn, publication_id)
        events = conn.execute("SELECT * FROM crew_outbox_events WHERE publication_id=? ORDER BY seq",
                              (publication_id,)).fetchall()
        return {"publication": dict(row), "events": [dict(e) for e in events]}
    finally:
        conn.close()


def deliver_publication(db_path: Path | str, publication_id: str, *, transport: Transport,
                        session_id: str, clock: Callable[[], datetime] | None = None) -> dict:
    """Claim, send once to the publication's own chat, record the outcome. Not scheduled by anything in P4."""
    clock = clock or (lambda: datetime.now(IST))
    claimed = claim_publication(db_path, publication_id, session_id=session_id, now=clock())
    if claimed["state"] != "SENDING":
        return claimed
    try:
        outcome = transport.send(claimed["chat_id"], claimed["body"])
    except Exception as exc:  # noqa: BLE001 - a raising transport is an unknown outcome, never a resend
        outcome = classify_exception(exc)
    return record_outcome(db_path, publication_id, outcome, session_id=session_id, now=clock())


def _transition(conn, row, to, *, at, session_id=None, message_id=None, http_status=None, telegram_ok=None,
                exc_class=None, latency_ms=None, response_excerpt=None, operator=None, method=None, note=None,
                not_before=None, last_error=None) -> dict:
    seq = row["version"] + 1
    _audit(conn, row["publication_id"], seq, row["state"], to, at=at, session_id=session_id, message_id=message_id,
           http_status=http_status, telegram_ok=telegram_ok, exc_class=exc_class, latency_ms=latency_ms,
           response_excerpt=response_excerpt, operator=operator, method=method, note=note)
    sending = to == "SENDING"
    cur = conn.execute(
        "UPDATE crew_outbox SET state=?, version=?, attempts=attempts+?, claimed_at=?, claimed_by_session=?,"
        " acked_at=?, telegram_message_id=?, not_before=?, last_error=coalesce(?, last_error)"
        " WHERE publication_id=? AND version=?",
        (to, seq, int(sending), at if sending else row["claimed_at"],
         session_id if sending else row["claimed_by_session"], at if to == "ACKED" else row["acked_at"],
         message_id if to in _DELIVERED_STATES else None, not_before,
         None if last_error is None else redact(last_error)[:500], row["publication_id"], row["version"]),
    )
    if cur.rowcount != 1:
        raise cj.VersionConflict(row["publication_id"])
    return dict(_pub(conn, row["publication_id"]))


def _to_unknown(conn, row, *, at, session_id, reason, outcome=None) -> dict:
    return _transition(conn, row, "UNKNOWN", at=at, session_id=session_id, last_error=reason, note=redact(reason),
                       message_id=outcome.message_id if outcome else None, **(_evidence(outcome) if outcome else {}))


def _evidence(outcome: DeliveryOutcome) -> dict:
    return {"http_status": outcome.http_status,
            "telegram_ok": None if outcome.telegram_ok is None else int(outcome.telegram_ok),
            "exc_class": outcome.exc_class, "latency_ms": outcome.latency_ms,
            "response_excerpt": None if outcome.detail is None else redact(outcome.detail)[:200]}


def _audit(conn, pid, seq, frm, to, *, at, **fields) -> None:
    cols = {"publication_id": pid, "seq": seq, "from_state": frm, "to_state": to, "at": at,
            **{k: v for k, v in fields.items() if v is not None}}
    conn.execute(f"INSERT INTO crew_outbox_events ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                 tuple(cols.values()))


def _trade(conn, trade_id: str | None):
    return conn.execute(
        "SELECT t.*, a.class AS admitted, a.decided_at AS admission_decided_at FROM crew_trades t"
        " JOIN crew_admissions a ON a.admission_id = t.admission_id WHERE t.trade_id=?", (trade_id,)).fetchone()


def _stale(trade, *, now: str) -> str | None:
    """Age runs from the admission decision, so a late enqueue or claim cannot refresh a recommendation."""
    if trade is None or trade["state"] != "TRACKING":
        return "STALE: trade is no longer TRACKING"
    if trade["market_day"] != _parse(now).astimezone(IST).date().isoformat():
        return "STALE: trade is from another market day"
    decided = _parse(trade["admission_decided_at"])
    if decided.utcoffset() is None:
        return "STALE: admission decided_at has no timezone"
    if (_parse(now) - decided).total_seconds() > RECOMMENDATION_MAX_AGE_SECONDS:
        return f"STALE: older than {RECOMMENDATION_MAX_AGE_SECONDS}s since admission"
    return None


def _unknown_block(conn, pid: str) -> str | None:
    row = conn.execute("SELECT publication_id FROM crew_outbox WHERE kind='RECOMMENDATION' AND state='UNKNOWN'"
                       " AND publication_id<>? LIMIT 1", (pid,)).fetchone()
    return None if row is None else f"BLOCKED: recommendation {row['publication_id']} is UNKNOWN"


def _message_owner(conn, row, message_id: int) -> str | None:
    """Another publication holding (chat, message_id). A row without a recorded chat may be in any chat."""
    clash = conn.execute("SELECT publication_id FROM crew_outbox WHERE telegram_message_id=? AND publication_id<>?"
                         " AND (chat_id IS NULL OR ? IS NULL OR chat_id=?) LIMIT 1",
                         (message_id, row["publication_id"], row["chat_id"], row["chat_id"])).fetchone()
    return None if clash is None else clash["publication_id"]


def _owned(row, session_id: str) -> None:
    if row["state"] != "SENDING" or row["claimed_by_session"] != session_id:
        raise cj.InvalidTransition(f"{row['publication_id']} is {row['state']} by {row['claimed_by_session']},"
                                   f" not SENDING by {session_id}")


def _require(conn, pid: str):
    row = _pub(conn, pid)
    if row is None:
        raise cj.InvalidTransition(f"unknown publication {pid}")
    return row


def _pub(conn, pid: str):
    return conn.execute("SELECT * FROM crew_outbox WHERE publication_id=?", (pid,)).fetchone()


def _stamp(now: datetime | None) -> str:
    value = datetime.now(IST) if now is None else now
    if value.tzinfo is None or value.utcoffset() is None:
        raise cj.InvalidTransition("timestamps must be timezone-aware")
    return value.isoformat()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)

