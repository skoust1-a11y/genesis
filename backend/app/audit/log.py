from __future__ import annotations

from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.audit.context import current_actor, current_request_id
from app.db.hashing import canonical_json, format_ts, normalize_json, sha256_hex
from app.db.models import AuditEvent, LiveAuditEvent
from app.db.types import utcnow

GENESIS_HASH = "0" * 64
_PG_LOCK_KEY = 0x6E6E5355  # arbitrary constant shared by all audit writers

AuditModel = type[AuditEvent] | type[LiveAuditEvent]


def event_hash(prev_hash: str, event: AuditEvent | LiveAuditEvent) -> str:
    body = {
        "occurred_at": format_ts(event.occurred_at),
        "actor": event.actor,
        "request_id": event.request_id,
        "action": event.action,
        "entity_type": event.entity_type,
        "entity_id": event.entity_id,
        "details": event.details,
    }
    return sha256_hex(prev_hash + canonical_json(body))


def _latest_hash(session: Session, model: AuditModel) -> str:
    latest = session.scalar(select(model.hash).order_by(model.id.desc()).limit(1))
    return latest or GENESIS_HASH


def record_event(
    session: Session,
    action: str,
    entity_type: str,
    entity_id: str | int,
    details: dict[str, Any] | None = None,
    model: AuditModel = AuditEvent,
) -> AuditEvent | LiveAuditEvent:
    """Append an audit event to the hash chain inside the caller's transaction.

    `model` selects the chain: AuditEvent for the research database, LiveAuditEvent for the live one.
    """
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _PG_LOCK_KEY})
    session.flush()
    prev_hash = _latest_hash(session, model)
    event = model(
        occurred_at=utcnow(),
        actor=current_actor(),
        request_id=current_request_id(),
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        details=normalize_json(details or {}),
        prev_hash=prev_hash,
    )
    event.hash = event_hash(prev_hash, event)
    session.add(event)
    session.flush()
    return event


def verify_chain(session: Session, batch_size: int = 1000, model: AuditModel = AuditEvent) -> dict[str, Any]:
    prev_hash = GENESIS_HASH
    checked = 0
    last_id = 0
    while True:
        rows = session.scalars(select(model).where(model.id > last_id).order_by(model.id).limit(batch_size)).all()
        if not rows:
            break
        for event in rows:
            if event.prev_hash != prev_hash or event_hash(prev_hash, event) != event.hash:
                return {"ok": False, "checked": checked, "first_bad_id": event.id}
            prev_hash = event.hash
            checked += 1
            last_id = event.id
    return {"ok": True, "checked": checked, "first_bad_id": None, "head_hash": prev_hash}
