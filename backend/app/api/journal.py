from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.common import or_400, or_404
from app.db.serialize import row_to_dict
from app.db.session import get_db, get_live_db
from app.journal.store import JournalStore

router = APIRouter(prefix="/api")


class ReviewIn(BaseModel):
    followed_plan: bool | None = None
    process_verdict: str | None = None
    what_went_well: str | None = None
    what_went_wrong: str | None = None
    mistake_tags: list[str] = []
    lesson: str | None = None
    improvement_suggestion: str | None = None
    suggested_rule_change: dict[str, Any] | None = None
    confidence: float | None = None
    supersedes_review_id: int | None = None


class DecisionIn(BaseModel):
    decision: str
    note: str | None = None
    markdown: str | None = None


@router.get("/journal")
def journal(
    kind: str | None = None,
    strategy_id: str | None = None,
    outcome: str | None = None,
    tag: str | None = None,
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    live_db: Session = Depends(get_live_db),
):
    rows = JournalStore(db, live_db).journal(kind, strategy_id, outcome, tag, date_from, date_to, limit, offset)
    return {"trades": rows}


def _review_dict(kind: str, review) -> dict[str, Any]:
    return {**row_to_dict(review), "trade_kind": kind}


@router.get("/trades/{kind}/{trade_id}/reviews")
def list_reviews(kind: str, trade_id: str, db: Session = Depends(get_db), live_db: Session = Depends(get_live_db)):
    reviews = or_400(JournalStore(db, live_db).reviews, kind, trade_id)
    return {"reviews": [_review_dict(kind, r) for r in reviews]}


@router.post("/trades/{kind}/{trade_id}/reviews")
def add_review(
    kind: str, trade_id: str, body: ReviewIn, db: Session = Depends(get_db), live_db: Session = Depends(get_live_db)
):
    review = or_400(JournalStore(db, live_db).add_review, kind, trade_id, body.model_dump())
    (live_db if kind == "live" else db).commit()
    return _review_dict(kind, review)


@router.get("/strategies/{strategy_id}/improvements")
def list_improvements(strategy_id: str, db: Session = Depends(get_db)):
    return {"improvements": or_404(JournalStore(db).improvements, strategy_id)}


@router.post("/improvements/{improvement_id}/decisions")
def decide_improvement(improvement_id: int, body: DecisionIn, db: Session = Depends(get_db)):
    store = JournalStore(db)
    decision = or_400(store.decide, improvement_id, body.decision, body.note, body.markdown)
    db.commit()
    return {**row_to_dict(decision), "status": store.status(improvement_id)}
