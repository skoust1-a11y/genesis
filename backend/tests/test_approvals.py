from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.db.models import AuditEvent
from app.live import approvals
from app.live.__main__ import main as live_cli
from tests.conftest import ACTOR, APPROVABLE_MARKDOWN, approval_body, approve_and_start, create_account


def approve(client, body, headers=ACTOR):
    return client.post("/api/live/approvals", json=body, headers=headers)


def test_preview_lists_blockers_for_the_demo_strategy(client, demo):
    preview = client.get(
        "/api/live/approvals/preview", params={"strategy_id": demo["strategy_id"], "version": 1}
    ).json()
    assert any(b.startswith("Gaps must be empty") for b in preview["blockers"])
    assert preview["warnings"] == [approvals.BACKTEST_WARNING]


def test_preview_unknown_strategy_is_404(client):
    assert client.get("/api/live/approvals/preview", params={"strategy_id": "nope", "version": 1}).status_code == 404


def test_blocked_strategy_cannot_be_approved(client, demo, paper_account):
    preview = client.get("/api/live/approvals/preview", params={"strategy_id": demo["strategy_id"], "version": 1})
    body = approval_body({"strategy_id": demo["strategy_id"], "preview": preview.json()}, paper_account)
    response = approve(client, body | {"confirm_rules_sha256": "x"})
    assert response.status_code == 400
    assert "cannot approve" in response.json()["detail"]


def test_unreviewed_version_cannot_be_approved(client, paper_account):
    ingest = client.post("/api/ingest/extract", json={"source_type": "markdown", "text": APPROVABLE_MARKDOWN}).json()
    preview = client.get(
        "/api/live/approvals/preview", params={"strategy_id": ingest["strategy_id"], "version": 1}
    ).json()
    assert preview["blockers"] == ["no completed report for this version; run a review first"]
    body = approval_body({"strategy_id": ingest["strategy_id"], "preview": preview}, paper_account)
    assert approve(client, body).status_code == 400


@pytest.mark.parametrize(
    ("overrides", "headers", "message"),
    [
        ({}, {}, "named approver"),
        ({"confirm_rules_sha256": "0" * 64}, ACTOR, "confirm_rules_sha256 does not match"),
        ({"mode": "shadow"}, ACTOR, "mode must be one of"),
        ({"mode": "live"}, ACTOR, "is a paper account, not live"),
        ({"risk_limits": {"leverage": 5}}, ACTOR, "unknown risk_limits keys"),
        ({"risk_limits": {"capital_inr": 0}}, ACTOR, "capital_inr must be positive"),
        ({"broker_account_id": "missing"}, ACTOR, "missing"),
    ],
)
def test_approval_refusals(client, approvable, paper_account, overrides, headers, message):
    response = approve(client, approval_body(approvable, paper_account, **overrides), headers=headers)
    assert response.status_code in (400, 404)
    assert message in response.json()["detail"]
    assert client.get("/api/live/deployments").json()["deployments"] == []


def test_non_profitable_verdict_needs_acknowledgement(client, approvable, paper_account, monkeypatch):
    report = SimpleNamespace(id=approvable["report_id"], verdict_label="Conditional")
    monkeypatch.setattr(approvals, "_latest_completed_report", lambda *_: report)
    body = approval_body(approvable, paper_account, acknowledge_verdict=False)
    refused = approve(client, body)
    assert refused.status_code == 400
    assert "acknowledge_verdict" in refused.json()["detail"]
    assert approve(client, body | {"acknowledge_verdict": True}).status_code == 200


def test_approval_freezes_the_version(client, approvable, paper_account):
    response = approve(client, approval_body(approvable, paper_account, risk_limits={"max_lots": 1}))
    assert response.status_code == 200, response.text
    out = response.json()
    version = client.get(f"/api/strategies/{approvable['strategy_id']}/versions/1").json()
    snapshot = out["deployed_strategy"]
    assert snapshot["markdown"] == version["markdown"]
    assert snapshot["content_sha256"] == version["content_sha256"]
    assert snapshot["rules_sha256"] == approvable["preview"]["rules_sha256"]
    assert snapshot["approved_by"] == "alice"
    assert out["deployment"]["status"] == "approved"
    assert out["deployment"]["risk_limits"] == {"capital_inr": 500000.0, "max_lots": 1}
    assert out["warnings"] == [approvals.BACKTEST_WARNING]

    # A new version never replaces the running deployment.
    edited = APPROVABLE_MARKDOWN.replace("value: 40", "value: 45")
    review = client.post("/api/review/run", json={"strategy_id": approvable["strategy_id"], "markdown": edited})
    assert review.json()["strategy_version"] == 2
    detail = client.get(f"/api/live/deployments/{out['deployment']['id']}").json()
    assert detail["deployed_strategy"]["version"] == 1
    assert detail["deployed_strategy"]["content_sha256"] == version["content_sha256"]


def test_one_active_deployment_per_strategy_and_mode(client, approvable, paper_account):
    body = approval_body(approvable, paper_account)
    first = approve(client, body).json()["deployment"]["id"]
    duplicate = approve(client, body)
    assert duplicate.status_code == 400
    assert "still active" in duplicate.json()["detail"]
    client.post(f"/api/live/deployments/{first}/stop", json={"reason": "replace"}, headers=ACTOR)
    assert approve(client, body).status_code == 200


def test_research_audit_records_the_approval(client, approvable, paper_account, session):
    deployment_id = approve(client, approval_body(approvable, paper_account)).json()["deployment"]["id"]
    events = session.scalars(select(AuditEvent).where(AuditEvent.action == approvals.APPROVED_EVENT)).all()
    assert [e.details["deployment_id"] for e in events] == [deployment_id]
    assert events[0].actor == "alice"
    assert events[0].entity_id == approvable["strategy_id"]
    session.rollback()
    assert client.get("/api/audit/verify").json()["ok"]
    assert client.get("/api/live/audit/verify").json()["ok"]
    live_actions = {e["action"] for e in client.get("/api/live/audit").json()["events"]}
    assert {"deployed_strategy.created", "deployment.created", "deployment.approved"} <= live_actions


def test_reconcile_finds_and_fixes_a_missing_research_event(client, approvable, paper_account, monkeypatch, capsys):
    def fail(*_, **__):
        raise RuntimeError("research database unavailable")

    monkeypatch.setattr(approvals, "record_event", fail)
    with pytest.raises(RuntimeError):
        approve(client, approval_body(approvable, paper_account))
    monkeypatch.undo()
    [deployment] = client.get("/api/live/deployments").json()["deployments"]

    assert live_cli(["reconcile"]) == 1
    assert f"Missing: {deployment['id']}" in capsys.readouterr().out
    assert live_cli(["reconcile", "--fix"]) == 0
    assert live_cli(["reconcile"]) == 0
    events = client.get("/api/audit").json()["events"]
    fixed = next(e for e in events if e["action"] == approvals.APPROVED_EVENT)
    assert fixed["actor"] == "process:reconcile"


def test_deployment_transitions(client, approvable, paper_account):
    deployment_id = approve(client, approval_body(approvable, paper_account)).json()["deployment"]["id"]
    url = f"/api/live/deployments/{deployment_id}"

    anonymous = client.post(f"{url}/start", json={"reason": "go"})
    assert anonymous.status_code == 400
    assert "named actor" in anonymous.json()["detail"]
    assert client.post(f"{url}/pause", headers=ACTOR).status_code == 400

    for action, status in (("start", "started"), ("pause", "paused"), ("start", "started"), ("stop", "stopped")):
        response = client.post(f"{url}/{action}", json={"reason": action}, headers=ACTOR)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == status

    restart = client.post(f"{url}/start", headers=ACTOR)
    assert restart.status_code == 400
    assert "stopped" in restart.json()["detail"]
    assert client.post(f"{url}/explode", headers=ACTOR).status_code == 400
    events = client.get(url).json()["events"]
    assert [e["event"] for e in events] == ["approved", "started", "paused", "started", "stopped"]


def test_live_account_approval_and_helper(client, approvable):
    live_account = create_account(client, mode="live", broker="zerodha")
    deployment_id = approve_and_start(client, approvable, live_account)
    assert client.get(f"/api/live/deployments/{deployment_id}").json()["mode"] == "live"
