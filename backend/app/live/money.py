"""Money rules on a running deployment: the floor, the break after a floor pause, and the withdrawal rule.

Their state lives in deployment events that are notices, not lifecycle changes (`MONEY_EVENTS`,
ignored by `deployment_status`):
- floor_paused: the floor was broken; it starts a break and a new game, so the floor drops back to
  `floor_inr` (a floor_raised before it no longer counts);
- floor_raised: the floor was raised for the current game;
- floor_top_up_needed: the break is over but equity is not above the floor, so nothing restarts
  until a deposit;
- withdrawal_due: the withdrawal rule has alerted since the latest withdrawal.

The Trade Crew applies the floor each tick; the supervisor ends breaks. A floor pause restarts by
itself only if `resume_after_days` was approved and no person has changed the deployment since.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app import alerts
from app.audit import audit_context
from app.config import Settings
from app.db.models import Deployment, DeploymentEvent
from app.live import drawdown
from app.live.equity import equity_state
from app.live.store import NOTICE_EVENTS, LiveStore
from app.market.feed import IST
from app.market.hours import trading_days_between
from app.trading import policy as trade_policy

FLOOR_PAUSED, FLOOR_RAISED = "floor_paused", "floor_raised"
TOP_UP_NEEDED, WITHDRAWAL_DUE = "floor_top_up_needed", "withdrawal_due"
RESUME_ACTOR = "system:floor_break"


@dataclass
class FloorState:
    floor: drawdown.Floor
    basis: str
    raised: bool
    floor_inr: float  # in force now
    paused: DeploymentEvent | None  # the latest floor pause


def _last(events: list[DeploymentEvent], name: str) -> DeploymentEvent | None:
    return next((e for e in reversed(events) if e.event == name), None)


def floor_state(store: LiveStore, deployment: Deployment, events: list[DeploymentEvent] | None = None):
    """The deployment's floor as it stands, or None without one."""
    policy = drawdown.policy_for(deployment)
    if policy is None or policy.floor is None:
        return None
    events = store.deployment_events(deployment.id) if events is None else events
    paused = _last(events, FLOOR_PAUSED)
    since = paused.id if paused else 0
    raised = any(e.event == FLOOR_RAISED and e.id > since for e in events)
    return FloorState(policy.floor, policy.basis, raised, drawdown.floor_in_force(policy.floor, raised), paused)


def _equity(store: LiveStore, settings: Settings, deployment: Deployment, basis: str) -> float:
    return equity_state(store, settings, [deployment], include_unrealized=basis == "mtm").equity_inr


def _day(at: datetime) -> str:
    return at.astimezone(IST).date().isoformat()


def break_days_left(fs: FloorState, settings: Settings, now: datetime) -> int | None:
    """Trading days of break left, today included; 0 once it is over. None without a break."""
    if fs.paused is None or fs.floor.resume_after_days is None:
        return None
    elapsed = trading_days_between(_day(fs.paused.created_at), _day(now), settings)
    return max(0, fs.floor.resume_after_days + 1 - elapsed)


def on_break(store: LiveStore, deployment: Deployment, fs: FloorState, events: list[DeploymentEvent]) -> bool:
    """Paused by the floor, with a break approved, and not changed by a person since."""
    if fs.paused is None or fs.floor.resume_after_days is None:
        return False
    if store.deployment_status(deployment.id) != "paused":
        return False
    lifecycle = [e for e in events if e.event not in NOTICE_EVENTS]
    return bool(lifecycle) and lifecycle[-1].event == "paused" and lifecycle[-1].id < fs.paused.id


def resume_if_due(store: LiveStore, settings: Settings, deployment: Deployment, now: datetime) -> str | None:
    """End a floor break once its trading days have passed: start the deployment again if equity is
    above the starting floor, otherwise alert once that a deposit is needed. Commits what it writes."""
    events = store.deployment_events(deployment.id)
    fs = floor_state(store, deployment, events)
    if fs is None or not on_break(store, deployment, fs, events) or break_days_left(fs, settings, now):
        return None
    equity, floor_inr = _equity(store, settings, deployment, fs.basis), fs.floor.floor_inr
    if equity <= floor_inr:
        if any(e.event == TOP_UP_NEEDED and e.id > fs.paused.id for e in events):
            return None
        reason = (
            f"break over, but equity ₹{equity:,.0f} is not above the ₹{floor_inr:,.0f} floor: "
            f"deposit more than ₹{floor_inr - equity:,.0f} and it starts again"
        )
        with audit_context(RESUME_ACTOR):
            store.add_deployment_event(deployment.id, TOP_UP_NEEDED, reason)
        store.session.commit()
        alerts.notify(settings, TOP_UP_NEEDED, deployment.id, reason)
        return "top_up_needed"
    reason = (
        f"floor break of {fs.floor.resume_after_days} trading day(s) over; equity ₹{equity:,.0f} is above "
        f"the ₹{floor_inr:,.0f} floor"
    )
    with audit_context(RESUME_ACTOR):
        store.add_deployment_event(deployment.id, "started", reason)
    store.session.commit()
    alerts.notify(settings, "resumed", deployment.id, reason)
    return "resumed"


def withdrawal_status(store: LiveStore, settings: Settings, deployment: Deployment) -> dict[str, Any] | None:
    """What the withdrawal rule says is due now, on realized equity. None without a rule."""
    policy = trade_policy.policy_from_limits(deployment.risk_limits)
    rule = policy.withdrawal if policy else None
    if rule is None:
        return None
    withdrawals = [e for e in store.capital_events([deployment.id]) if e.kind == "withdrawal"]
    withdrawn = sum(float(e.amount_inr) for e in withdrawals)
    equity = equity_state(store, settings, [deployment], include_unrealized=False).equity_inr
    base = None
    if withdrawals:
        until = withdrawals[-1].created_at + timedelta(microseconds=1)
        base = equity_state(store, settings, [deployment], include_unrealized=False, until=until).equity_inr
    fs = floor_state(store, deployment)
    keep_above = fs.floor_inr if fs else None
    return {
        "rule": rule.model_dump(mode="json", exclude_none=True),
        "due_inr": trade_policy.withdrawal_due(rule, equity, withdrawn, base, keep_above),
        "withdrawn_inr": round(withdrawn, 2),
        "realized_equity_inr": round(equity, 2),
        "base_inr": round(base, 2) if base is not None else None,
        "last_withdrawal_at": withdrawals[-1].created_at if withdrawals else None,
    }


def notify_withdrawal_due(store: LiveStore, settings: Settings, deployment: Deployment) -> float | None:
    """Alert once per withdrawal period (since the latest recorded withdrawal) when an amount is due."""
    status = withdrawal_status(store, settings, deployment)
    if not status or not status["due_inr"]:
        return None
    since = status["last_withdrawal_at"]
    notices = [e for e in store.deployment_events(deployment.id) if e.event == WITHDRAWAL_DUE]
    if any(since is None or e.created_at > since for e in notices):
        return None
    due = status["due_inr"]
    reason = (
        f"withdraw ₹{due:,.0f}: realized equity is ₹{status['realized_equity_inr']:,.0f}. "
        "Record it as a withdrawal once the money is out"
    )
    store.add_deployment_event(deployment.id, WITHDRAWAL_DUE, reason)
    store.session.commit()
    alerts.notify(settings, WITHDRAWAL_DUE, deployment.id, reason)
    return due


def money_status(store: LiveStore, settings: Settings, deployment: Deployment, now: datetime) -> dict[str, Any]:
    """The floor (with any break) and the withdrawal rule, as the deployments page shows them."""
    events = store.deployment_events(deployment.id)
    fs = floor_state(store, deployment, events)
    floor = None
    if fs is not None:
        equity = _equity(store, settings, deployment, fs.basis)
        breaking = on_break(store, deployment, fs, events)
        floor = fs.floor.model_dump(mode="json", exclude_none=True) | {
            "basis": fs.basis,
            "raised": fs.raised,
            "floor_in_force_inr": fs.floor_inr,
            "equity_inr": round(equity, 2),
            "buffer_inr": round(equity - fs.floor_inr, 2),
            "on_break": breaking,
            "break_days_left": break_days_left(fs, settings, now) if breaking else None,
            "paused_at": fs.paused.created_at if fs.paused else None,
        }
    return {"floor": floor, "withdrawal": withdrawal_status(store, settings, deployment)}
