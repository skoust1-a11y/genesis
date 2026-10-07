from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.common import or_400, or_404
from app.audit import verify_chain
from app.config import get_settings
from app.db.models import LiveAuditEvent
from app.db.serialize import row_to_dict
from app.db.session import get_db, get_live_db, live_schema_ready
from app.live import approvals, drawdown, equity, opinion_eval, policy_backtest, veto
from app.live.store import LiveStore

router = APIRouter(prefix="/api/live")


class BrokerAccountIn(BaseModel):
    broker: str
    account_ref_masked: str
    mode: str
    default_algo_id: str | None = None


class ApprovalIn(BaseModel):
    strategy_id: str
    version: int
    mode: str
    broker_account_id: str
    confirm_rules_sha256: str
    risk_limits: dict[str, Any] = {}
    sebi_algo_id: str | None = None
    acknowledge_verdict: bool = False


class PolicyBacktestIn(BaseModel):
    strategy_id: str
    version: int
    risk_limits: dict[str, Any] = {}


class TransitionIn(BaseModel):
    reason: str | None = None


class VetoIn(BaseModel):
    enable: bool
    reason: str | None = None


class CapitalEventIn(BaseModel):
    kind: str
    amount_inr: float
    note: str | None = None


def live_db(session: Session = Depends(get_live_db)) -> Iterator[Session]:
    if not live_schema_ready(session):
        raise HTTPException(
            status_code=503, detail="live database is not migrated: run `uv run alembic -n live upgrade head`"
        )
    yield session


def _deployment_dict(store: LiveStore, deployment) -> dict:
    return {
        **row_to_dict(deployment),
        "status": store.deployment_status(deployment.id),
        "llm_veto": store.veto_enabled(deployment.id),
        "events": [row_to_dict(e) for e in store.deployment_events(deployment.id)],
    }


@router.get("/broker-accounts")
def list_broker_accounts(db: Session = Depends(live_db)):
    return {"broker_accounts": [row_to_dict(a) for a in LiveStore(db).broker_accounts()]}


@router.post("/broker-accounts")
def create_broker_account(body: BrokerAccountIn, db: Session = Depends(live_db)):
    if body.mode not in approvals.MODES:
        raise HTTPException(status_code=400, detail=f"mode must be one of {', '.join(approvals.MODES)}")
    account = LiveStore(db).create_broker_account(**body.model_dump())
    db.commit()
    return row_to_dict(account)


# Approval gate


@router.get("/approvals/preview")
def preview_approval(strategy_id: str, version: int, research: Session = Depends(get_db)):
    return or_404(approvals.preview, research, strategy_id, version)


@router.post("/approvals/backtest")
def backtest_risk_limits(body: PolicyBacktestIn, research: Session = Depends(get_db)):
    """The version's rules backtested with and without these risk limits (policy and drawdown ladder)."""
    return or_400(policy_backtest.policy_backtest, research, body.strategy_id, body.version, body.risk_limits)


@router.post("/approvals")
def approve(body: ApprovalIn, research: Session = Depends(get_db), db: Session = Depends(live_db)):
    deployment, snapshot, warnings = or_400(approvals.approve, research, db, **body.model_dump())
    store = LiveStore(db)
    return {
        "deployment": _deployment_dict(store, deployment),
        "deployed_strategy": row_to_dict(snapshot),
        "warnings": warnings,
    }


@router.post("/deployments/{deployment_id}/llm-veto")
def set_llm_veto(deployment_id: str, body: VetoIn, db: Session = Depends(live_db)):
    """Enable (named approver, evidence required) or disable the LLM veto for one deployment."""
    store = LiveStore(db)
    or_404(store.deployment, deployment_id)
    if body.enable:
        evaluation = or_400(veto.enable, db, get_settings(), deployment_id, body.reason)
    else:
        or_400(veto.disable, db, get_settings(), deployment_id, body.reason)
        evaluation = None
    return {"deployment": _deployment_dict(store, store.deployment(deployment_id)), "evaluation": evaluation}


@router.post("/deployments/{deployment_id}/capital-events")
def add_capital_event(deployment_id: str, body: CapitalEventIn, db: Session = Depends(live_db)):
    store = LiveStore(db)
    or_404(store.deployment, deployment_id)
    event = or_400(
        equity.add_capital_event, store, get_settings(), deployment_id, body.kind, body.amount_inr, body.note
    )
    db.commit()
    return row_to_dict(event)


@router.post("/deployments/{deployment_id}/{action}")
def transition(deployment_id: str, action: str, body: TransitionIn | None = None, db: Session = Depends(live_db)):
    or_400(approvals.transition, db, deployment_id, action, body.reason if body else None)
    return _deployment_dict(LiveStore(db), LiveStore(db).deployment(deployment_id))


@router.get("/deployments")
def list_deployments(db: Session = Depends(live_db)):
    store = LiveStore(db)
    return {"deployments": [_deployment_dict(store, d) for d in store.deployments()]}


@router.get("/deployments/{deployment_id}")
def get_deployment(deployment_id: str, db: Session = Depends(live_db)):
    store = LiveStore(db)
    deployment = or_404(store.deployment, deployment_id)
    snapshot = store.deployed_strategy(deployment.deployed_strategy_id)
    return {**_deployment_dict(store, deployment), "deployed_strategy": row_to_dict(snapshot)}


@router.get("/deployments/{deployment_id}/intents")
def list_intents(deployment_id: str, db: Session = Depends(live_db)):
    return {"intents": [row_to_dict(i) for i in or_404(LiveStore(db).intents, deployment_id)]}


@router.get("/deployments/{deployment_id}/capital-events")
def list_capital_events(deployment_id: str, db: Session = Depends(live_db)):
    store = LiveStore(db)
    deployment = or_404(store.deployment, deployment_id)
    return {
        "capital_in_force_inr": equity.capital_in_force(store, get_settings(), deployment),
        "capital_events": [row_to_dict(e) for e in store.capital_events([deployment_id])],
    }


@router.get("/deployments/{deployment_id}/drawdown")
def get_drawdown(deployment_id: str, db: Session = Depends(live_db)):
    """The deployment's ladder (its policy plus the MAX_DRAWDOWN_PCT pause), current level and level history."""
    store, settings = LiveStore(db), get_settings()
    deployment = or_404(store.deployment, deployment_id)
    policy = drawdown.policy_for(deployment)
    return {
        "policy": policy.model_dump(mode="json") if policy else None,
        "ladder": [lvl.model_dump(mode="json") for lvl in drawdown.effective_ladder(policy, settings.max_drawdown_pct)],
        "level": store.drawdown_level(deployment_id),
        "events": [row_to_dict(e) for e in store.drawdown_events(deployment_id)],
    }


def _scope(store: LiveStore, deployment_id: str | None, mode: str) -> list:
    if deployment_id:
        return [or_404(store.deployment, deployment_id)]
    return [d for d in store.deployments() if d.mode == mode]


@router.get("/equity")
def get_equity(
    deployment_id: str | None = None,
    mode: str = Query("paper", pattern="^(paper|live)$"),
    basis: str = Query("mtm", pattern="^(mtm|realized)$"),
    curve: bool = False,
    db: Session = Depends(live_db),
):
    """Equity, peak and drawdown for one deployment, or for every deployment in a mode."""
    store = LiveStore(db)
    deployments = _scope(store, deployment_id, mode)
    state = equity.equity_state(
        store, get_settings(), deployments, include_unrealized=basis == "mtm", with_curve=curve
    )
    return {"deployment_ids": [d.id for d in deployments], "basis": basis, **state.as_dict(with_curve=curve)}


@router.get("/positions")
def list_positions(
    deployment_id: str | None = None,
    mode: str = Query("paper", pattern="^(paper|live)$"),
    db: Session = Depends(live_db),
):
    """Open positions with their latest marks from the Trade Crew."""
    store = LiveStore(db)
    deployments = _scope(store, deployment_id, mode)
    return {"positions": [p for d in deployments for p in equity.current_positions(store, d)]}


@router.get("/orders/{order_id}")
def get_order(order_id: str, db: Session = Depends(live_db)):
    return or_404(LiveStore(db).order_detail, order_id)


@router.get("/live-trades")
def list_live_trades(
    deployment_id: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(live_db),
):
    rows = LiveStore(db).live_trades(deployment_id, limit, offset)
    return {"live_trades": [row_to_dict(t) for t in rows]}


# LLM trade opinions


@router.get("/opinions")
def list_opinions(deployment_id: str | None = None, db: Session = Depends(live_db)):
    return {"opinions": [row_to_dict(o) for o in LiveStore(db).opinions(deployment_id)]}


@router.get("/opinions/evaluation")
def opinion_evaluation(deployment_id: str | None = None, db: Session = Depends(live_db)):
    return opinion_eval.evaluate(LiveStore(db), get_settings(), deployment_id)


@router.get("/phantom-trades")
def list_phantom_trades(deployment_id: str | None = None, db: Session = Depends(live_db)):
    return {"phantom_trades": [row_to_dict(p) for p in LiveStore(db).phantom_trades(deployment_id)]}


# Audit (the live database has its own hash chain)


@router.get("/audit")
def list_live_events(
    entity_type: str | None = None,
    entity_id: str | None = None,
    action: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(live_db),
):
    stmt = select(LiveAuditEvent)
    if entity_type:
        stmt = stmt.where(LiveAuditEvent.entity_type == entity_type)
    if entity_id:
        stmt = stmt.where(LiveAuditEvent.entity_id == entity_id)
    if action:
        stmt = stmt.where(LiveAuditEvent.action == action)
    stmt = stmt.order_by(LiveAuditEvent.id.desc()).limit(limit).offset(offset)
    return {"events": [row_to_dict(e) for e in db.scalars(stmt)]}


@router.get("/audit/verify")
def verify_live(db: Session = Depends(live_db)):
    return verify_chain(db, model=LiveAuditEvent)
