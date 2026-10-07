from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.agents.trade_crew.graph import run_tick
from app.agents.trade_crew.opinion import OpinionOutput, normalize
from app.audit import audit_context
from app.db.models import LiveAuditEvent
from app.live import veto
from app.live.store import LiveStore
from tests.conftest import ACTOR
from tests.test_opinion_eval import _intent, _opinion, _trade
from tests.test_opinions import llm, shadow_settings  # noqa: F401
from tests.test_trade_crew import (  # noqa: F401
    CALM,
    ENTRY,
    RALLY,
    Clock,
    deployment_id,
    intents,
    make_ctx,
    orders,
)


def veto_settings(**overrides):
    return shadow_settings(llm_trade_opinion="veto", opinion_eval_min_samples=1, **overrides)


def seed_evidence(live_session, deployment_id, net="-100"):
    """One closed shadow skip on a losing trade: the smallest 'veto_helps' evidence with min_samples=1."""
    store = LiveStore(live_session)
    intent_id = _intent(store, deployment_id)
    _opinion(store, deployment_id, intent_id, decision="skip", lots=0, final_lots=2)
    _trade(store, deployment_id, intent_id, net, 0)
    live_session.commit()


def enable(live_session, deployment_id, settings):
    with audit_context("alice"):
        evaluation = veto.enable(live_session, settings, deployment_id, "evidence reviewed")
    assert evaluation["verdict"] == "veto_helps"


@pytest.mark.parametrize("decision", ["take", "reduce", "skip"])
def test_clamp_never_increases_lots(decision):
    for rules_lots in range(1, 5):
        for lots in (None, *range(0, 9)):
            _, final = normalize(OpinionOutput(decision=decision, lots=lots, confidence=0.5), rules_lots)
            assert 0 <= final <= rules_lots


def test_enabling_needs_a_named_approver_the_veto_setting_and_evidence(client, live_session, deployment_id):
    url = f"/api/live/deployments/{deployment_id}/llm-veto"
    response = client.post(url, json={"enable": True})
    assert response.status_code == 400 and "named approver" in response.json()["detail"]
    response = client.post(url, json={"enable": True}, headers=ACTOR)
    assert response.status_code == 400 and "LLM_TRADE_OPINION is 'off'" in response.json()["detail"]
    response = client.post(url, json={"enable": False}, headers=ACTOR)
    assert response.status_code == 400 and "not enabled" in response.json()["detail"]
    assert client.post("/api/live/deployments/nope/llm-veto", json={"enable": True}, headers=ACTOR).status_code == 404

    with audit_context("alice"), pytest.raises(ValueError, match="needs 1 more closed skip"):
        veto.enable(live_session, veto_settings(), deployment_id)
    live_session.rollback()
    seed_evidence(live_session, deployment_id, net="250")  # the skip would have cost money
    with audit_context("alice"), pytest.raises(ValueError, match="verdict is veto_hurts"):
        veto.enable(live_session, veto_settings(), deployment_id)
    live_session.rollback()


def test_live_accounts_are_blocked_by_default():
    settings = veto_settings()
    assert "LLM_VETO_LIVE_ALLOWED" in veto.blocker(settings, SimpleNamespace(mode="live"))
    assert veto.blocker(settings, SimpleNamespace(mode="paper")) is None
    assert veto.blocker(veto_settings(llm_veto_live_allowed=True), SimpleNamespace(mode="live")) is None
    assert "no LLM API key" in veto.blocker(veto_settings(openai_api_key=None), SimpleNamespace(mode="paper"))


def test_enable_records_evidence_and_keeps_the_lifecycle(client, live_session, deployment_id):
    seed_evidence(live_session, deployment_id)
    enable(live_session, deployment_id, veto_settings())
    store = LiveStore(live_session)
    assert store.veto_enabled(deployment_id) and store.deployment_status(deployment_id) == "started"
    event = store.deployment_events(deployment_id)[-1]
    assert (event.event, event.actor) == ("llm_veto_enabled", "alice") and "veto_helps" in event.reason
    audit = live_session.scalar(select(LiveAuditEvent).where(LiveAuditEvent.action == "deployment.llm_veto_enabled"))
    assert audit.details["extra"]["verdict"] == "veto_helps" and audit.details["extra"]["interventions"] == 1
    live_session.rollback()
    body = client.get(f"/api/live/deployments/{deployment_id}").json()
    assert body["llm_veto"] is True and body["status"] == "started"
    assert client.get("/api/live/audit/verify").json()["ok"]


def test_vetoed_entry_sends_no_orders_and_is_tracked_as_a_phantom(live_session, make_ctx, deployment_id, llm):
    seed_evidence(live_session, deployment_id)
    settings = veto_settings()
    enable(live_session, deployment_id, settings)
    clock = Clock()
    ctx = make_ctx(deployment_id, [ENTRY, RALLY], settings=settings, clock=clock)

    result = run_tick(ctx)
    assert result["outcome"] == "vetoed" and result["opinion"] == "skip"
    intent = next(i for i in intents(live_session, deployment_id) if i.id == result["intent_id"])
    assert intent.approval == "vetoed" and "vetoed by the LLM opinion: RBI policy tomorrow" in intent.reason_text
    assert orders(live_session, intent.id) == []
    store = LiveStore(live_session)
    assert intent.id not in {i.id for i in store.open_intents(deployment_id)}
    op = store.opinion_for_intent(intent.id)
    assert (op.mode, op.rules_lots, op.final_lots, op.applied) == ("veto", 2, 0, True)
    live_session.rollback()

    clock.advance(days=1)
    result = run_tick(ctx)
    [tracked] = result["phantoms"]
    assert tracked["trade_intent_id"] == intent.id and tracked["exit_reason"] == "stop_loss"
    [phantom] = LiveStore(live_session).phantom_trades(deployment_id)
    assert (phantom.kind, phantom.lots, phantom.trade_opinion_id) == ("vetoed", 2, op.id)
    assert phantom.net == phantom.gross - phantom.est_costs and phantom.net < 0 and phantom.est_costs > 0
    assert [leg["quantity"] for leg in phantom.legs] == [150] * 4
    still_open = [i.id for _, i in LiveStore(live_session).open_phantoms(deployment_id)]
    assert intent.id not in still_open and len(still_open) == (result["outcome"] == "vetoed")
    assert intent.id not in {t.open_intent_id for t in LiveStore(live_session).live_trades(deployment_id)}
    live_session.rollback()


def test_reduction_sends_fewer_lots_and_tracks_the_removed_ones(live_session, make_ctx, deployment_id, llm):
    seed_evidence(live_session, deployment_id)
    settings = veto_settings()
    enable(live_session, deployment_id, settings)
    llm["reply"]["value"] = OpinionOutput(decision="reduce", lots=1, reasons=["event risk"], confidence=0.6)
    clock = Clock()
    ctx = make_ctx(deployment_id, [ENTRY, RALLY], settings=settings, clock=clock)

    result = run_tick(ctx)
    assert result["outcome"] == "entered" and result["opinion"] == "reduce"
    intent = next(i for i in intents(live_session, deployment_id) if i.id == result["intent_id"])
    assert intent.approval == "auto" and [leg["quantity"] for leg in intent.legs] == [75] * 4
    assert "reduced 2 to 1 lots: event risk" in intent.reason_text
    assert [o.quantity for o, _, _ in orders(live_session, intent.id)] == [75] * 4
    op = LiveStore(live_session).opinion_for_intent(intent.id)
    assert (op.rules_lots, op.final_lots, op.applied) == (2, 1, True)
    live_session.rollback()

    clock.advance(days=1)
    result = run_tick(ctx)
    assert result["outcome"] == "exited" and len(result["phantoms"]) == 1
    store = LiveStore(live_session)
    trade = next(t for t in store.live_trades(deployment_id) if t.open_intent_id == intent.id)
    [phantom] = store.phantom_trades(deployment_id)
    assert (phantom.kind, phantom.lots, phantom.exit_reason) == ("reduced", 1, trade.exit_reason)
    assert [leg["quantity"] for leg in phantom.legs] == [75] * 4
    live_session.rollback()


def test_rolling_loss_reverts_the_veto_to_shadow(live_session, make_ctx, deployment_id, llm):
    seed_evidence(live_session, deployment_id, net="-1")
    settings = veto_settings()
    enable(live_session, deployment_id, settings)
    clock = Clock()
    ctx = make_ctx(deployment_id, [ENTRY, CALM, ENTRY], settings=settings, clock=clock)

    assert run_tick(ctx)["outcome"] == "vetoed"
    clock.advance(days=1)
    assert run_tick(ctx)["phantoms"][0]["exit_reason"] == "profit_target"  # the skipped trade would have paid
    clock.advance(days=1)
    result = run_tick(ctx)
    assert result["outcome"] == "entered"  # back in shadow mode: the opinion is recorded, the rules trade

    store = LiveStore(live_session)
    assert not store.veto_enabled(deployment_id) and store.deployment_status(deployment_id) == "started"
    event = store.deployment_events(deployment_id)[-1]
    assert (event.event, event.actor) == ("llm_veto_disabled", veto.AUTO_REVERT_ACTOR)
    assert event.reason.startswith("auto-revert to shadow")
    assert store.opinions(deployment_id)[-1].mode == "shadow"
    live_session.rollback()


def test_disable_via_api(client, live_session, deployment_id):
    seed_evidence(live_session, deployment_id)
    enable(live_session, deployment_id, veto_settings())
    response = client.post(
        f"/api/live/deployments/{deployment_id}/llm-veto", json={"enable": False, "reason": "manual"}, headers=ACTOR
    )
    assert response.status_code == 200, response.text
    assert response.json()["deployment"]["llm_veto"] is False
    assert response.json()["deployment"]["events"][-1]["actor"] == "alice"
