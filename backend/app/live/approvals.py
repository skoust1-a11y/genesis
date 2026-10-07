"""The approval gate: the only way a strategy version reaches paper or live trading.

Approval freezes a copy of the version in the live database and creates a deployment that
trades only after an explicit start. The live database is written first; the research
audit event follows, and `reconcile` finds deployments whose research event is missing.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import audit_context, current_actor, record_event
from app.config import get_settings
from app.db.models import AuditEvent, DeployedStrategy, Deployment, Report, StrategyVersion
from app.db.types import utcnow
from app.live import gates
from app.live.drawdown import parse_policy
from app.live.store import LiveStore
from app.strategies.rules import rules_sha256, with_common_risk
from app.strategies.schema import parse_strategy_markdown
from app.strategies.store import StrategyStore
from app.trading.policy import parse_policy as parse_trade_policy

MODES = ("paper", "live")
APPROVED_EVENT = "strategy.approved_for_live"
BACKTEST_WARNING = (
    "Backtests replay this strategy's rules day by day on end-of-day closes (bundled synthetic sample "
    "unless NSE data is imported); intraday stops, gaps and fill quality are not modelled."
)
RISK_LIMIT_KEYS = ("capital_inr", "max_lots", "max_daily_loss_pct", "drawdown", "policy")
TRANSITIONS = {
    "start": (("approved", "paused"), "started"),
    "pause": (("started",), "paused"),
    "stop": (("approved", "started", "paused"), "stopped"),
}


def _latest_completed_report(research: Session, version_id: int) -> Report | None:
    return research.scalar(
        select(Report)
        .where(Report.strategy_version_id == version_id, Report.status == "completed")
        .order_by(Report.finished_at.desc())
        .limit(1)
    )


def structure_of(raw_rules: dict[str, Any] | None) -> str | None:
    instrument = (raw_rules or {}).get("instrument") or {}
    return instrument.get("structure") if isinstance(instrument, dict) else None


def live_preview_blockers(
    live: Session | None, strategy_version_id: int, raw_rules: dict | None, broker_account_id: str | None
) -> list[str]:
    """The live gates as approval blockers: LIVE_TRADING_ENABLED, broker and credentials, structure, paper record."""
    settings = get_settings()
    store = LiveStore(live) if live is not None else None
    account, blockers = None, []
    if store is not None and broker_account_id:
        try:
            account = store.broker_account(broker_account_id)
        except FileNotFoundError:
            blockers.append(f"broker account {broker_account_id} does not exist")
    blockers += gates.live_blockers(store, account, structure_of(raw_rules), settings)
    if store is not None:
        record = gates.paper_record_blocker(store, strategy_version_id, settings)
        if record:
            blockers.append(record)
    return blockers


def preview(
    research: Session,
    strategy_id: str,
    version: int,
    *,
    mode: str | None = None,
    broker_account_id: str | None = None,
    live: Session | None = None,
) -> dict[str, Any]:
    """Everything you need to see before approving, including what would block approval. With
    mode=live, the live gates are blockers too."""
    store = StrategyStore(research)
    strategy = store.get_strategy(strategy_id)
    row = store.version(strategy_id, version)
    doc = parse_strategy_markdown(row.markdown)
    raw_rules = doc.frontmatter.get("rules")
    report = _latest_completed_report(research, row.id)
    blockers = list(doc.rules_errors)
    if strategy.archived_at is not None:
        blockers.append("strategy is archived")
    if doc.gaps:
        blockers.append(f"Gaps must be empty ({len(doc.gaps)} open)")
    if report is None:
        blockers.append("no completed report for this version; run a review first")
    if mode == "live":
        blockers += live_preview_blockers(live, row.id, raw_rules, broker_account_id)
    common_risk = get_settings().common_risk
    rules = doc.rules
    return {
        "strategy_id": strategy_id,
        "version": row.version,
        "strategy_version_id": row.id,
        "name": doc.name,
        "content_sha256": row.content_sha256,
        "rules": raw_rules,
        "rules_sha256": rules_sha256(raw_rules) if raw_rules is not None else None,
        "rules_errors": doc.rules_errors,
        "common_risk": common_risk,
        "risk_in_force": (
            with_common_risk(rules, common_risk).risk.model_dump(mode="json", exclude_none=True) if rules else None
        ),
        "gaps": doc.gaps,
        "report_id": report.id if report else None,
        "verdict_label": report.verdict_label if report else None,
        "needs_verdict_acknowledgement": bool(report) and report.verdict_label != "Profitable",
        "blockers": blockers,
        "warnings": [BACKTEST_WARNING],
    }


def parse_risk_limits(raw: dict[str, Any] | None, rules: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validated risk limits as they will be frozen with the deployment."""
    raw = dict(raw or {})
    unknown = set(raw) - set(RISK_LIMIT_KEYS)
    if unknown:
        raise ValueError(f"unknown risk_limits keys: {', '.join(sorted(unknown))}")
    limits = {"capital_inr": float(raw.pop("capital_inr", get_settings().default_capital_inr))}
    if limits["capital_inr"] <= 0:
        raise ValueError("risk_limits.capital_inr must be positive")
    if "max_lots" in raw:
        limits["max_lots"] = int(raw["max_lots"])
    if "max_daily_loss_pct" in raw:
        limits["max_daily_loss_pct"] = float(raw["max_daily_loss_pct"])
    capital = limits["capital_inr"]
    if raw.get("drawdown") is not None:
        policy = parse_policy(raw["drawdown"], get_settings().max_drawdown_pct)
        floor = policy.floor
        if floor is not None and floor.floor_inr >= capital:
            raise ValueError(f"risk_limits.drawdown.floor.floor_inr must be below capital_inr ({capital:g})")
        if floor is not None and floor.raise_once and floor.raise_once.at_equity_inr <= capital:
            raise ValueError(
                f"risk_limits.drawdown.floor.raise_once.at_equity_inr must be above capital_inr ({capital:g})"
            )
        limits["drawdown"] = policy.model_dump(mode="json", exclude_none=True)
    if raw.get("policy") is not None:
        policy = parse_trade_policy(raw["policy"], rules, limits.get("max_lots"))
        if policy.withdrawal is not None and policy.withdrawal.first_target_inr <= capital:
            raise ValueError(
                f"risk_limits.policy.withdrawal.first_target_inr must be above capital_inr ({capital:g})"
            )
        limits["policy"] = policy.model_dump(mode="json", exclude_none=True)
    return limits


def _approval_details(deployment: Deployment, snapshot: DeployedStrategy) -> dict[str, Any]:
    extra = {}
    if deployment.mode == "live":
        extra = {
            "structure": structure_of(snapshot.rules),
            "live_allowed_structures": sorted(get_settings().allowed_live_structures),
        }
    return extra | {
        "deployment_id": deployment.id,
        "deployed_strategy_id": snapshot.id,
        "strategy_version_id": snapshot.strategy_version_id,
        "version": snapshot.version,
        "mode": deployment.mode,
        "content_sha256": snapshot.content_sha256,
        "rules_sha256": snapshot.rules_sha256,
        "report_id": snapshot.report_id,
        "verdict_label": snapshot.verdict_label,
        "approved_by": snapshot.approved_by,
    }


def approve(
    research: Session,
    live: Session,
    *,
    strategy_id: str,
    version: int,
    mode: str,
    broker_account_id: str,
    confirm_rules_sha256: str,
    risk_limits: dict[str, Any] | None = None,
    sebi_algo_id: str | None = None,
    acknowledge_verdict: bool = False,
) -> tuple[Deployment, DeployedStrategy, list[str]]:
    approver = current_actor()
    if approver == "anonymous":
        raise ValueError("approval needs a named approver: send the X-Actor header")
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    p = preview(research, strategy_id, version, mode=mode, broker_account_id=broker_account_id, live=live)
    if p["blockers"]:
        raise ValueError("cannot approve: " + "; ".join(p["blockers"]))
    if confirm_rules_sha256 != p["rules_sha256"]:
        raise ValueError(f"confirm_rules_sha256 does not match this version's rules (expected {p['rules_sha256']})")
    if p["needs_verdict_acknowledgement"] and not acknowledge_verdict:
        raise ValueError(f"latest verdict is {p['verdict_label']!r}; set acknowledge_verdict to approve anyway")
    limits = parse_risk_limits(risk_limits, p["rules"])

    store = LiveStore(live)
    account = store.broker_account(broker_account_id)
    if account.mode != mode:
        raise ValueError(f"broker account {account.id} is a {account.mode} account, not {mode}")
    for d in store.active_deployments():
        if d.strategy_id == strategy_id and d.mode == mode:
            raise ValueError(f"deployment {d.id} is still active for this strategy in {mode} mode; stop it first")

    row = research.get(StrategyVersion, p["strategy_version_id"])
    snapshot = store.create_deployed_strategy(
        strategy_id=strategy_id,
        strategy_version_id=row.id,
        version=row.version,
        name=p["name"],
        markdown=row.markdown,
        content_sha256=row.content_sha256,
        rules=p["rules"],
        rules_sha256=p["rules_sha256"],
        report_id=p["report_id"],
        verdict_label=p["verdict_label"],
        approved_by=approver,
        approved_at=utcnow(),
    )
    deployment = store.create_deployment(
        deployed_strategy_id=snapshot.id,
        broker_account_id=account.id,
        mode=mode,
        sebi_algo_id=sebi_algo_id or account.default_algo_id,
        risk_limits=limits,
        approved_by=approver,
    )
    store.add_deployment_event(deployment.id, "approved", f"{mode} approval of v{row.version}")
    live.commit()

    record_event(research, APPROVED_EVENT, "strategy", strategy_id, _approval_details(deployment, snapshot))
    research.commit()
    return deployment, snapshot, p["warnings"]


def transition(live: Session, deployment_id: str, action: str, reason: str | None = None) -> str:
    if action not in TRANSITIONS:
        raise ValueError(f"action must be one of {', '.join(TRANSITIONS)}")
    allowed, event = TRANSITIONS[action]
    if action == "start" and current_actor() == "anonymous":
        raise ValueError("starting a deployment needs a named actor: send the X-Actor header")
    store = LiveStore(live)
    store.deployment(deployment_id)
    status = store.deployment_status(deployment_id)
    if status not in allowed:
        raise ValueError(f"cannot {action} a deployment that is {status}")
    store.add_deployment_event(deployment_id, event, reason)
    live.commit()
    return event


def reconcile(research: Session, live: Session, fix: bool = False) -> list[str]:
    """Deployment ids with no research approval event. With fix=True, write the missing events."""
    recorded = {
        e.details.get("deployment_id")
        for e in research.scalars(select(AuditEvent).where(AuditEvent.action == APPROVED_EVENT))
    }
    store = LiveStore(live)
    missing = [d for d in reversed(store.deployments()) if d.id not in recorded]
    if fix and missing:
        with audit_context("process:reconcile"):
            for d in missing:
                snapshot = store.deployed_strategy(d.deployed_strategy_id)
                record_event(research, APPROVED_EVENT, "strategy", d.strategy_id, _approval_details(d, snapshot))
        research.commit()
    return [d.id for d in missing]
