"""S1-P5 session ownership: one exclusive, fenced journal lease, and restart recovery.

A session is one process's tenure as the journal's only writer on one IST market day.
``acquire`` takes the single ``crew_lease`` row only if it is released or expired; each
acquisition bumps ``generation`` (the fencing token) and issues a fresh owner token, of which
only the SHA-256 is stored. ``heartbeat``, ``release`` and ``recover`` act only while the caller's
generation and token still hold an unexpired lease on the current IST market day; otherwise they
record FENCED and raise ``LeaseLost``. Ownership is never inferred from pid or host. Every P2/P4
operational write takes the same ``Lease`` and runs the same fence (``crew_journal._fenced``).

Every acquisition, takeover, refusal, release, fence and recovery is a ``crew_session_events`` row.
Nothing here is scheduled, and nothing here sends a message or places an order.
"""

from __future__ import annotations

import os
import secrets
import socket
from datetime import datetime, timedelta
from pathlib import Path

from app.db import crew_journal as cj
from app.db import crew_outbox as ob

# ponytail: one fixed lease length; callers heartbeat well inside it (e.g. every 10 s).
LEASE_SECONDS = 30

Lease, LeaseHeld, LeaseLost = cj.Lease, cj.LeaseHeld, cj.LeaseLost


def market_day_for(now: datetime) -> str:
    """The IST calendar day of an aware timestamp."""
    return cj._ist(now).date().isoformat()


def acquire(db_path: Path | str, *, now: datetime | None = None, ttl_seconds: float = LEASE_SECONDS,
            code_rev: str | None = None, provenance: dict | None = None) -> Lease:
    """Become the journal's only writer, or raise ``LeaseHeld`` (the refusal is still recorded).

    A released lease is ACQUIRED; an expired, unreleased one is a TAKEOVER that ends the prior session.
    """
    ttl = _ttl(ttl_seconds)
    stamp = cj._ist(now)
    at, day = stamp.isoformat(), stamp.date().isoformat()
    token = secrets.token_hex(32)

    def op(conn):
        held = cj._lease_row(conn)
        if held is not None and held["released_at"] is None and ob._parse(held["expires_at"]) > stamp:
            cj._session_event(conn, "REFUSED", day=day, at=at, prior=held,
                              detail={"holder_expires_at": held["expires_at"]})
            return LeaseHeld(f"{held['session_id']} holds the journal until {held['expires_at']}")
        generation = 1 if held is None else held["generation"] + 1
        sid = f"S_{day.replace('-', '')}_G{generation}"
        conn.execute(
            "INSERT INTO crew_sessions (session_id, market_day, pid, host, code_rev, started_at, heartbeat_at,"
            " generation) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, day, os.getpid(), socket.gethostname(), code_rev, at, at, generation),
        )
        takeover = held is not None and held["released_at"] is None
        if takeover:
            conn.execute("UPDATE crew_sessions SET ended_at=?, end_reason='LEASE_EXPIRED' WHERE session_id=?"
                         " AND ended_at IS NULL", (at, held["session_id"]))
        cj._session_event(conn, "TAKEOVER" if takeover else "ACQUIRED", day=day, at=at, session_id=sid,
                          generation=generation, prior=held, detail={**(provenance or {}), "ttl_seconds": ttl})
        lease_cols = (sid, generation, cj._sha(token), at, at, (stamp + timedelta(seconds=ttl)).isoformat())
        if held is None:
            conn.execute("INSERT INTO crew_lease (id, session_id, generation, token_sha256, acquired_at, heartbeat_at,"
                         " expires_at) VALUES (1, ?, ?, ?, ?, ?, ?)", lease_cols)
        else:
            _update_lease(conn, held["generation"], "session_id=?, generation=?, token_sha256=?, acquired_at=?,"
                          " heartbeat_at=?, expires_at=?, released_at=NULL", lease_cols)
        return Lease(sid, day, generation, token)

    return cj._immediate(db_path, op)


def heartbeat(db_path: Path | str, lease: Lease, *, now: datetime | None = None,
              ttl_seconds: float = LEASE_SECONDS) -> str:
    """Extend an unexpired lease. Returns the new ``expires_at``. An expired lease cannot be renewed."""
    ttl = _ttl(ttl_seconds)
    stamp = cj._ist(now)
    at, expires = stamp.isoformat(), (stamp + timedelta(seconds=ttl)).isoformat()

    def op(conn):
        _update_lease(conn, lease.generation, "heartbeat_at=?, expires_at=?", (at, expires))
        conn.execute("UPDATE crew_sessions SET heartbeat_at=? WHERE session_id=?", (at, lease.session_id))
        return expires

    return cj._fenced(db_path, lease, stamp, op)


def release(db_path: Path | str, lease: Lease, *, reason: str = "RELEASED", now: datetime | None = None) -> None:
    """Give the lease up now. Works after expiry or a day change; a repeat is a no-op; after a takeover it fences."""
    why = cj._text(reason)
    if not why:
        raise cj.InvalidTransition("release needs a reason")
    stamp = cj._ist(now)
    at = stamp.isoformat()

    def op(conn):
        held = cj._lease_row(conn)
        if cj._mine(held, lease) and held["released_at"] is not None:
            return None
        lost = cj._fence(conn, lease, stamp, allow_expired=True)
        if lost:
            return lost
        cj._session_event(conn, "RELEASED", day=stamp.date().isoformat(), at=at, session_id=lease.session_id,
                          generation=lease.generation, detail={"reason": why})
        _update_lease(conn, lease.generation, "released_at=?", (at,))
        conn.execute("UPDATE crew_sessions SET ended_at=?, end_reason=? WHERE session_id=? AND ended_at IS NULL",
                     (at, why, lease.session_id))
        return None

    return cj._immediate(db_path, op)


def recover(db_path: Path | str, lease: Lease, *, now: datetime | None = None) -> dict:
    """Startup recovery, run by the lease holder in one transaction that begins with the consistency check.

    Prior-day open trades become EXPIRED_UNRESOLVED (no price). SENDING publications of other sessions become
    UNKNOWN (never resent). Today's open trades owned by another session are reported in ``orphaned_open`` and
    left untouched: the journal cannot transfer a trade's owner, and no price may be invented.
    Raises ``ConsistencyError`` and writes nothing if the journal is inconsistent. The check holds the write
    lock, so no writer (fenced or not) can commit between the check and the recovery.
    """
    stamp = cj._ist(now)
    at = stamp.isoformat()

    def op(conn):
        findings = cj._consistency_findings(conn)
        if findings:
            raise cj.ConsistencyError(findings)
        expired = cj._expire_unresolved(conn, lease.market_day, cj._candle(at))
        inflight = ob._recover_inflight(conn, lease.session_id, at)
        orphaned = conn.execute(
            "SELECT trade_id, owner_session_id, state, version FROM crew_trades"
            " WHERE state IN ('TRACKING','PARTIALLY_EXITED') AND market_day=? AND owner_session_id IS NOT ?"
            " ORDER BY trade_id", (lease.market_day, lease.session_id)).fetchall()
        unknown = conn.execute("SELECT publication_id FROM crew_outbox WHERE state='UNKNOWN'"
                               " ORDER BY created_at, publication_id").fetchall()
        report = {
            "expired": [r["trade"]["trade_id"] for r in expired if not r["replayed"]],
            "inflight_unknown": [p["publication_id"] for p in inflight],
            "orphaned_open": [dict(row) for row in orphaned],
            "unknown_publications": [row[0] for row in unknown],
        }
        cj._session_event(conn, "RECOVERED", day=lease.market_day, at=at, session_id=lease.session_id,
                          generation=lease.generation, detail=report)
        return report

    return cj._fenced(db_path, lease, stamp, op)


def _update_lease(conn, generation: int, assignments: str, values: tuple) -> None:
    cur = conn.execute(f"UPDATE crew_lease SET {assignments} WHERE id=1 AND generation=?", (*values, generation))
    if cur.rowcount != 1:
        raise cj.VersionConflict("crew_lease changed underneath this transaction")


def _ttl(value: float) -> float:
    ttl = cj._number("ttl_seconds", value)
    if ttl <= 0:
        raise cj.InvalidTransition("ttl_seconds must be positive")
    return ttl
