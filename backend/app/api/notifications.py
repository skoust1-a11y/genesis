from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db.models import Notification
from app.db.serialize import row_to_dict
from app.db.session import get_db
from app.db.types import utcnow

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


class NotificationCreate(BaseModel):
    title: str = Field(..., max_length=200)
    message: str
    category: str = Field(default="alert", max_length=32)
    level: str = Field(default="info", max_length=16)
    deployment_id: str | None = None
    channels: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


@router.get("")
def list_notifications(
    category: str | None = None,
    level: str | None = None,
    deployment_id: str | None = None,
    unread_only: bool = False,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    stmt = select(Notification)
    if category:
        stmt = stmt.where(Notification.category == category)
    if level:
        stmt = stmt.where(Notification.level == level)
    if deployment_id:
        stmt = stmt.where(Notification.deployment_id == deployment_id)
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    unread = db.scalar(select(func.count(Notification.id)).where(Notification.read_at.is_(None))) or 0

    items = db.scalars(stmt.order_by(Notification.created_at.desc()).limit(limit).offset(offset)).all()
    return {
        "notifications": [row_to_dict(n) for n in items],
        "total": total,
        "unread_count": unread,
    }


@router.post("")
def create_notification(body: NotificationCreate, db: Session = Depends(get_db)):
    notif = Notification(
        title=body.title,
        message=body.message,
        category=body.category,
        level=body.level,
        deployment_id=body.deployment_id,
        channels=body.channels,
        details=body.details,
    )
    db.add(notif)
    db.commit()
    db.refresh(notif)
    return row_to_dict(notif)


@router.post("/{notification_id}/read")
def mark_read(notification_id: str, db: Session = Depends(get_db)):
    notif = db.get(Notification, notification_id)
    if not notif:
        raise HTTPException(status_code=404, detail="notification not found")
    if notif.read_at is None:
        notif.read_at = utcnow()
        db.commit()
        db.refresh(notif)
    return row_to_dict(notif)


@router.post("/read-all")
def mark_all_read(db: Session = Depends(get_db)):
    now = utcnow()
    res = db.execute(
        update(Notification)
        .where(Notification.read_at.is_(None))
        .values(read_at=now)
    )
    db.commit()
    return {"marked_read": res.rowcount}
