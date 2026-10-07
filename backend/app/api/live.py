from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.common import ensure_authenticated, or_400, or_404, require_auth
from app.audit import verify_chain
from app.brokers import credentials, registry
from app.config import get_settings
from app.db.models import LiveAuditEvent
from app.db.serialize import row_to_dict
from app.db.session import get_db, get_live_db, live_schema_ready
from app.db.types import utcnow
from app.live import approvals, drawdown, equity, kill_switch, opinion_eval, policy_backtest, veto
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
    flatten: bool = False


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


def _crew_status(store: LiveStore, deployment, status: str) -> dict:
    """Whether a Trade Crew process is ticking for this deployment, from its latest heartbeat."""
    beat = store.latest_heartbeat(deployment.id)
    age = (utcnow() - beat.at).total_seconds() if beat else None
    running = status == "started" and age is not None and age <= get_settings().heartbeat_max_age_s
    return {
        "crew_running": running,
        "last_heartbeat": row_to_dict(beat) if beat else None,
        "last_heartbeat_age_s": round(age, 1) if age is not None else None,
    }


def _deployment_dict(store: LiveStore, deployment) -> dict:
    status = store.deployment_status(deployment.id)
    return {
        **row_to_dict(deployment),
        "status": status,
        "llm_veto": store.veto_enabled(deployment.id),
        "events": [row_to_dict(e) for e in store.deployment_events(deployment.id)],
        **_crew_status(store, deployment, status),
    }


def _ensure_live_auth(deployment) -> None:
    if deployment.mode == "live":
        ensure_authenticated()


# Brokers, accounts and credentials


@router.get("/brokers")
def list_brokers():
    """Registered brokers. `configured` means every required credential resolves from `.env`
    (per-account values saved in the database are shown on each account)."""
    settings = get_settings()
    out = []
    for spec in registry.specs():
        fields = credentials.status(None, None, spec, settings)
        out.append(spec.as_dict(settings) | {
            "fields": [f.as_dict() for f in fields], "configured": credentials.configured(fields),
        })  # fmt: skip
    return {"brokers": out, "feeds": list(registry.FEEDS)}


def _account_dict(store: LiveStore, account) -> dict:
    settings = get_settings()
    try:
        spec = registry.get_spec(account.broker)
    except ValueError as exc:
        return row_to_dict(account) | {"registered": False, "configured": False, "fields": [], "detail": str(exc)}
    fields = credentials.status(store, account, spec, settings)
    return row_to_dict(account) | {
        "registered": True,
        "supports_mode": account.mode in spec.modes(settings),
        "configured": credentials.configured(fields),
        "fields": [f.as_dict() for f in fields],
    }


@router.get("/broker-accounts")
def list_broker_accounts(db: Session = Depends(live_db)):
    store = LiveStore(db)
    return {"broker_accounts": [_account_dict(store, a) for a in store.broker_accounts()]}


@router.post("/broker-accounts")
def create_broker_account(body: BrokerAccountIn, db: Session = Depends(live_db)):
    if body.mode not in approvals.MODES:
        raise HTTPException(status_code=400, detail=f"mode must be one of {', '.join(approvals.MODES)}")
    or_400(registry.check_account, body.broker, body.mode, get_settings())
    if body.mode == "live":
        ensure_authenticated()
    store = LiveStore(db)
    account = store.create_broker_account(**body.model_dump())
    db.commit()
    return _account_dict(store, account)


def _account_spec(store: LiveStore, account_id: str):
    account = or_404(store.broker_account, account_id)
    return account, or_400(registry.get_spec, account.broker)


@router.get("/broker-accounts/{account_id}/credentials", dependencies=[Depends(require_auth)])
def get_credentials(account_id: str, db: Session = Depends(live_db)):
    """Field names, where each comes from (db or env), fingerprint and when it was saved. Never values."""
    store = LiveStore(db)
    account, spec = _account_spec(store, account_id)
    fields = credentials.status(store, account, spec, get_settings())
    return {"broker_account_id": account.id, "broker": spec.name, "configured": credentials.configured(fields),
            "fields": [f.as_dict() for f in fields]}  # fmt: skip


@router.put("/broker-accounts/{account_id}/credentials", dependencies=[Depends(require_auth)])
def put_credentials(account_id: str, body: dict[str, str], db: Session = Depends(live_db)):
    """Save (or rotate) credential values, encrypted with BROKER_CREDENTIALS_KEY."""
    store = LiveStore(db)
    account, spec = _account_spec(store, account_id)
    or_400(credentials.save, store, account, spec, get_settings(), body)
    db.commit()
    return get_credentials(account_id, db)


@router.delete("/broker-accounts/{account_id}/credentials", dependencies=[Depends(require_auth)])
def delete_credentials(account_id: str, field: list[str] | None = Query(None), db: Session = Depends(live_db)):
    """Revoke stored values (all, or the `field`s given); lookups then fall back to `.env`."""
    store = LiveStore(db)
    account, spec = _account_spec(store, account_id)
    or_400(credentials.revoke, store, account, spec, field)
    db.commit()
    return get_credentials(account_id, db)


# Approval gate


@router.get("/approvals/preview")
def preview_approval(
    strategy_id: str,
    version: int,
    mode: str | None = None,
    broker_account_id: str | None = None,
    research: Session = Depends(get_db),
    live: Session = Depends(get_live_db),
):
    """With mode=live (and a broker_account_id), the live gates are listed as blockers too."""
    session = live if mode == "live" and live_schema_ready(live) else None
    return or_404(approvals.preview, research, strategy_id, version, mode=mode,
                  broker_account_id=broker_account_id, live=session)  # fmt: skip


@router.post("/approvals/backtest")
def backtest_risk_limits(body: PolicyBacktestIn, research: Session = Depends(get_db)):
    """The version's rules backtested with and without these risk limits (policy and drawdown ladder)."""
    return or_400(policy_backtest.policy_backtest, research, body.strategy_id, body.version, body.risk_limits)


@router.post("/approvals")
def approve(body: ApprovalIn, research: Session = Depends(get_db), db: Session = Depends(live_db)):
    if body.mode == "live" and or_404(LiveStore(db).broker_account, body.broker_account_id).mode == "live":
        ensure_authenticated()  # a paper account is refused by approve() itself
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
    _ensure_live_auth(or_404(store.deployment, deployment_id))
    if body.enable:
        evaluation = or_400(veto.enable, db, get_settings(), deployment_id, body.reason)
    else:
        or_400(veto.disable, db, get_settings(), deployment_id, body.reason)
        evaluation = None
    return {"deployment": _deployment_dict(store, store.deployment(deployment_id)), "evaluation": evaluation}


@router.post("/deployments/{deployment_id}/capital-events")
def add_capital_event(deployment_id: str, body: CapitalEventIn, db: Session = Depends(live_db)):
    store = LiveStore(db)
    _ensure_live_auth(or_404(store.deployment, deployment_id))
    event = or_400(
        equity.add_capital_event, store, get_settings(), deployment_id, body.kind, body.amount_inr, body.note
    )
    db.commit()
    return row_to_dict(event)


@router.post("/deployments/{deployment_id}/{action}")
def transition(deployment_id: str, action: str, body: TransitionIn | None = None, db: Session = Depends(live_db)):
    """start | pause | stop. `stop` cancels the deployment's working orders at the broker (kill switch);
    with `flatten: true` it also closes every open position."""
    store = LiveStore(db)
    deployment = or_404(store.deployment, deployment_id)
    body = body or TransitionIn()
    if body.flatten and action != "stop":
        raise HTTPException(status_code=400, detail="flatten is only allowed with stop")
    if body.flatten or (action == "start" and deployment.mode == "live"):
        ensure_authenticated()
    if action != "stop":
        or_400(approvals.transition, db, deployment_id, action, body.reason)
        return _deployment_dict(store, store.deployment(deployment_id))
    result = or_400(kill_switch.stop, db, get_settings(), deployment_id, body.reason, body.flatten)
    return _deployment_dict(store, store.deployment(deployment_id)) | {"kill_switch": result}


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
