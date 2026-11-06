"""Read-only crew journal API. Every route needs a valid token, and is refused (403) while no token or user exists.

There are no mutation routes: nothing here repairs, adopts, resends, resolves UNKNOWN or quarantines.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from app.api.common import require_auth
from app.config import get_settings
from app.db import crew_reader
from app.trading import paper_stats

router = APIRouter(prefix="/api/live/crew", dependencies=[Depends(require_auth)])

DAY = r"^\d{4}-\d{2}-\d{2}$"
TEXT = r"^[^\x00-\x1f\x7f]{1,128}$"
TRADE_STATES = "^(TRACKING|PARTIALLY_EXITED|CLOSED|EXPIRED_UNRESOLVED|QUARANTINED)$"


def Limit():  # a fresh Query per parameter: one shared Query object binds every use to one name
    return Query(100, ge=1, le=crew_reader.MAX_LIMIT)


def Offset():
    return Query(0, ge=0, le=1_000_000)


def _read(fn: Callable, *args, **kwargs):
    try:
        with crew_reader.snapshot(get_settings().crew_journal_path) as conn:
            return fn(conn, *args, **kwargs)
    except crew_reader.Unavailable as e:
        raise HTTPException(status_code=503, detail={"status": e.code, "detail": e.detail}) from e
    except LookupError as e:
        raise HTTPException(status_code=404, detail="trade not found") from e


@router.get("/trades")
def list_trades(
    market_day: str | None = Query(None, pattern=DAY),
    state: str | None = Query(None, pattern=TRADE_STATES),
    strategy_id: str | None = Query(None, pattern=TEXT),
    strategy_version: str | None = Query(None, pattern=TEXT),
    execution_policy: str | None = Query(None, pattern=TEXT),
    limit: int = Limit(),
    offset: int = Offset(),
):
    return _read(crew_reader.trades, now=crew_reader.now_ist(), market_day=market_day, state=state,
                 strategy_id=strategy_id, strategy_version=strategy_version, execution_policy=execution_policy,
                 limit=limit, offset=offset)


@router.get("/trades/{trade_id}/events")
def trade_events(trade_id: str = Path(pattern=r"^[A-Za-z0-9_.:\-]{1,128}$"), limit: int = Limit(),
                 offset: int = Offset()):
    return _read(crew_reader.trade_events, trade_id, limit=limit, offset=offset)


@router.get("/admissions")
def list_admissions(
    market_day: str | None = Query(None, pattern=DAY),
    admission_class: str | None = Query(None, pattern="^(ACTIONABLE|RESEARCH_ONLY|REJECTED)$"),
    strategy_id: str | None = Query(None, pattern=TEXT),
    strategy_version: str | None = Query(None, pattern=TEXT),
    limit: int = Limit(),
    offset: int = Offset(),
):
    return _read(crew_reader.admissions, market_day=market_day, admission_class=admission_class,
                 strategy_id=strategy_id, strategy_version=strategy_version, limit=limit, offset=offset)


@router.get("/stats")
def get_stats(
    strategy_version: str = Query(..., pattern=TEXT),
    execution_policy: str = Query(..., pattern=TEXT),
    strategy_id: list[str] | None = Query(None, max_length=20),
):
    for sid in strategy_id or []:
        if not re.fullmatch(TEXT, sid) or not sid.strip():
            raise HTTPException(status_code=422, detail="strategy_id must be 1..128 printable characters")
    return _read(crew_reader.stats, strategy_version=strategy_version, execution_policy=execution_policy,
                 strategy_ids=strategy_id, now=crew_reader.now_ist())


@router.get("/paper/stats")
def get_paper_stats(market_day: str | None = Query(None, pattern=DAY)):
    return _read(paper_stats.report, market_day=market_day)


@router.get("/exceptions")
def get_exceptions(limit: int = Limit()):
    return _read(crew_reader.exceptions, now=crew_reader.now_ist(), limit=limit)


@router.get("/sessions")
def get_sessions(limit: int = Limit(), offset: int = Offset(), events_offset: int = Offset()):
    return _read(crew_reader.sessions, now=crew_reader.now_ist(), limit=limit, offset=offset,
                 events_offset=events_offset)
