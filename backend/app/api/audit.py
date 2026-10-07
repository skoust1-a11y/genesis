from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import verify_chain
from app.db.models import AuditEvent
from app.db.serialize import row_to_dict
from app.db.session import get_db

router = APIRouter(prefix="/api/audit")


@router.get("")
def list_events(
    entity_type: str | None = None,
    entity_id: str | None = None,
    action: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    stmt = select(AuditEvent)
    if entity_type:
        stmt = stmt.where(AuditEvent.entity_type == entity_type)
    if entity_id:
        stmt = stmt.where(AuditEvent.entity_id == entity_id)
    if action:
        stmt = stmt.where(AuditEvent.action == action)
    stmt = stmt.order_by(AuditEvent.id.desc()).limit(limit).offset(offset)
    return {"events": [row_to_dict(e) for e in db.scalars(stmt)]}


@router.get("/verify")
def verify(db: Session = Depends(get_db)):
    return verify_chain(db)
