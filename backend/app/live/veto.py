"""Turning the LLM veto on and off for one deployment.

The veto is a per-deployment switch recorded as `llm_veto_enabled` / `llm_veto_disabled`
deployment events. Enabling needs a named approver and a "veto_helps" verdict from the opinion
evaluation for that deployment; the evaluation is attached to the audit event as evidence.
When the rolling evaluation turns negative, the crew switches the deployment back to shadow.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.audit import audit_context, current_actor
from app.config import Settings
from app.db.models import Deployment
from app.live.opinion_eval import evaluate, rolling_is_negative
from app.live.store import TERMINAL_STATUSES, LiveStore

AUTO_REVERT_ACTOR = "system:veto_auto_revert"
EVIDENCE_KEYS = ("closed", "interventions", "min_samples", "difference", "verdict", "rolling", "by_decision")


def blocker(settings: Settings, deployment: Deployment) -> str | None:
    """Why the veto cannot apply to this deployment right now, or None."""
    if settings.llm_trade_opinion != "veto":
        return f"LLM_TRADE_OPINION is {settings.llm_trade_opinion!r}, not 'veto'"
    if not settings.llm_enabled:
        return "no LLM API key is configured"
    if deployment.mode == "live" and not settings.llm_veto_live_allowed:
        return "the veto is blocked on live accounts (LLM_VETO_LIVE_ALLOWED=false)"
    return None


def _evidence(evaluation: dict[str, Any]) -> dict[str, Any]:
    return {k: evaluation[k] for k in EVIDENCE_KEYS}


def _summary(evaluation: dict[str, Any]) -> str:
    diff = evaluation["difference"]
    return (
        f"verdict {evaluation['verdict']}; mean difference per intervention {diff['mean_per_intervention']} INR, "
        f"95% CI {diff['ci95']}, n={evaluation['interventions']}"
    )


def enable(live: Session, settings: Settings, deployment_id: str, reason: str | None = None) -> dict[str, Any]:
    approver = current_actor()
    if approver == "anonymous":
        raise ValueError("enabling the veto needs a named approver: send the X-Actor header")
    store = LiveStore(live)
    deployment = store.deployment(deployment_id)
    if store.deployment_status(deployment_id) in TERMINAL_STATUSES:
        raise ValueError(f"deployment is {store.deployment_status(deployment_id)}")
    if store.veto_enabled(deployment_id):
        raise ValueError("the veto is already enabled for this deployment")
    problem = blocker(settings, deployment)
    if problem:
        raise ValueError(f"cannot enable the veto: {problem}")
    evaluation = evaluate(store, settings, deployment_id)
    if evaluation["verdict"] != "veto_helps":
        why = evaluation["verdict_reason"] or f"verdict is {evaluation['verdict']}"
        raise ValueError(f"the evidence does not support a veto: {why}")
    text = _summary(evaluation) + (f". {reason}" if reason else "")
    store.add_deployment_event(deployment_id, "llm_veto_enabled", text, _evidence(evaluation))
    live.commit()
    return evaluation


def disable(live: Session, settings: Settings, deployment_id: str, reason: str | None = None) -> None:
    store = LiveStore(live)
    store.deployment(deployment_id)
    if not store.veto_enabled(deployment_id):
        raise ValueError("the veto is not enabled for this deployment")
    evaluation = evaluate(store, settings, deployment_id)
    store.add_deployment_event(deployment_id, "llm_veto_disabled", reason, _evidence(evaluation))
    live.commit()


def auto_revert(live: Session, settings: Settings, deployment_id: str) -> bool:
    """Switch an enabled veto back to shadow if the rolling evaluation lost money. Commits when it does."""
    store = LiveStore(live)
    if not store.veto_enabled(deployment_id):
        return False
    evaluation = evaluate(store, settings, deployment_id)
    if not rolling_is_negative(evaluation):
        return False
    rolling = evaluation["rolling"]
    reason = (
        f"auto-revert to shadow: the opinion cost {-rolling['difference']['total']} INR over the last "
        f"{rolling['closed']} closed outcomes ({rolling['interventions']} skips or reductions)"
    )
    with audit_context(AUTO_REVERT_ACTOR):
        store.add_deployment_event(deployment_id, "llm_veto_disabled", reason, _evidence(evaluation))
    live.commit()
    return True
