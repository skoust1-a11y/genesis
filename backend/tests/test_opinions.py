from __future__ import annotations

import json

import pytest

from app.agents.trade_crew import lessons as lessons_module
from app.agents.trade_crew import opinion as opinion_module
from app.agents.trade_crew.graph import run_tick
from app.agents.trade_crew.lessons import LessonOutput
from app.agents.trade_crew.opinion import SYSTEM_PROMPT, OpinionOutput, normalize
from app.config import get_settings
from app.live.store import LiveStore
from app.market.context import UNTRUSTED_NOTE
from tests.test_trade_crew import ENTRY, NO_SIGNAL, Clock, deployment_id, intents, make_ctx, orders  # noqa: F401


def shadow_settings(**overrides):
    return get_settings().model_copy(
        update={"llm_trade_opinion": "shadow", "openai_api_key": "test-key", "news_feeds": ""} | overrides
    )


@pytest.fixture
def llm(monkeypatch):
    calls: list[dict] = []
    reply = {
        "value": OpinionOutput(decision="skip", reasons=["RBI policy tomorrow"], risk_flags=["event"], confidence=0.7)
    }

    def fake(settings, system, user):
        calls.append({"system": system, "user": user, "model": settings.opinion_model})
        if isinstance(reply["value"], Exception):
            raise reply["value"]
        return reply["value"]

    lesson_calls: list[dict] = []
    lesson = {"value": LessonOutput(
        process_verdict="good_process_good_outcome",
        what_went_well="Theta decay as planned.",
        what_went_wrong="",
        lesson="<b>Skip</b> was wrong:\x00 IV crush paid.",
        mistake_tags=["none"],
    )}  # fmt: skip

    def fake_lesson(settings, system, user):
        lesson_calls.append({"system": system, "facts": json.loads(user)})
        if isinstance(lesson["value"], Exception):
            raise lesson["value"]
        return lesson["value"]

    monkeypatch.setattr(opinion_module, "_call_llm", fake)
    monkeypatch.setattr(lessons_module, "_call_llm", fake_lesson)
    return {"calls": calls, "reply": reply, "lesson_calls": lesson_calls, "lesson": lesson}


@pytest.mark.parametrize(
    ("decision", "lots", "expected"),
    [
        ("take", None, ("take", 2)),
        ("take", 5, ("take", 2)),
        ("skip", None, ("skip", 0)),
        ("reduce", 1, ("reduce", 1)),
        ("reduce", 0, ("skip", 0)),
        ("reduce", 2, ("take", 2)),  # cannot keep or raise size and call it a reduction
        ("reduce", 9, ("take", 2)),
        ("reduce", None, ("take", 2)),
    ],
)
def test_opinion_can_only_shrink_the_position(decision, lots, expected):
    assert normalize(OpinionOutput(decision=decision, lots=lots, confidence=0.5), 2) == expected


def test_shadow_opinion_is_recorded_and_never_changes_the_decision(live_session, make_ctx, deployment_id, llm):
    ctx = make_ctx(deployment_id, [ENTRY], settings=shadow_settings())
    result = run_tick(ctx)
    assert result["outcome"] == "entered" and result["opinion"] == "skip"

    [intent] = intents(live_session, deployment_id)
    assert intent.approval == "auto" and intent.model is None
    assert [status for _, status, _ in orders(live_session, intent.id)] == ["COMPLETE"] * 4
    store = LiveStore(live_session)
    [op] = store.opinions(deployment_id)
    assert (op.mode, op.status, op.decision, op.lots) == ("shadow", "ok", "skip", 0)
    assert (op.rules_lots, op.final_lots, op.applied) == (2, 2, False)
    assert op.trade_intent_id == intent.id and op.reasons == ["RBI policy tomorrow"] and op.confidence == 0.7
    assert op.context["snapshot"]["iv_rank"] == 80 and op.context["headlines"] == []
    assert len(op.prompt_sha256) == 64 and op.model == "gpt-4o-mini"
    live_session.rollback()

    [call] = llm["calls"]
    assert call["system"] == SYSTEM_PROMPT
    assert UNTRUSTED_NOTE in call["user"] and "PROPOSED POSITION" in call["user"]
    assert "risk.min_risk_reward" in call["user"]


def test_llm_failure_is_recorded_and_the_rules_decide(live_session, make_ctx, deployment_id, llm):
    llm["reply"]["value"] = TimeoutError("took too long")
    result = run_tick(make_ctx(deployment_id, [ENTRY], settings=shadow_settings()))
    assert result["outcome"] == "entered" and result["opinion"] is None
    [op] = LiveStore(live_session).opinions(deployment_id)
    assert op.status == "error" and op.decision is None and "TimeoutError" in op.error
    assert op.final_lots == op.rules_lots and op.applied is False
    live_session.rollback()


def test_opinion_is_off_by_default_and_never_asked_about_exits(live_session, make_ctx, deployment_id, llm):
    assert get_settings().llm_trade_opinion == "off"
    assert run_tick(make_ctx(deployment_id, [ENTRY]))["opinion"] is None
    assert run_tick(make_ctx(deployment_id, [NO_SIGNAL], settings=shadow_settings()))["outcome"] == "exited"
    assert run_tick(make_ctx(deployment_id, [NO_SIGNAL], settings=shadow_settings()))["outcome"] == "no_signal"
    assert llm["calls"] == [] and LiveStore(live_session).opinions(deployment_id) == []
    live_session.rollback()


def test_closed_trade_gets_an_llm_lesson_that_feeds_the_next_opinion(live_session, make_ctx, deployment_id, llm):
    run_tick(make_ctx(deployment_id, [ENTRY], settings=shadow_settings()))
    assert run_tick(make_ctx(deployment_id, [NO_SIGNAL], settings=shadow_settings()))["outcome"] == "exited"

    [call] = llm["lesson_calls"]
    facts = call["facts"]
    assert facts["exit_reason"] and facts["net_pnl"] and facts["llm_opinion_before_entry"]["decision"] == "skip"
    store = LiveStore(live_session)
    [trade] = store.live_trades(deployment_id)
    [review] = store.reviews(trade.id)
    assert (review.reviewer_type, review.reviewer) == ("agent", "llm:gpt-4o-mini")
    assert review.process_verdict == "good_process_good_outcome" and review.what_went_wrong is None
    assert review.lesson == "Skip was wrong: IV crush paid."
    review_id = review.id
    live_session.rollback()

    assert run_tick(make_ctx(deployment_id, [ENTRY], settings=shadow_settings()))["outcome"] == "entered"
    assert "- Skip was wrong: IV crush paid." in llm["calls"][-1]["user"]
    assert LiveStore(live_session).opinions(deployment_id)[-1].lesson_review_ids == [review_id]
    live_session.rollback()


def test_lesson_failure_leaves_the_trade_unreviewed(live_session, make_ctx, deployment_id, llm):
    llm["lesson"]["value"] = RuntimeError("rate limited")
    run_tick(make_ctx(deployment_id, [ENTRY], settings=shadow_settings()))
    assert run_tick(make_ctx(deployment_id, [NO_SIGNAL], settings=shadow_settings()))["outcome"] == "exited"
    store = LiveStore(live_session)
    [trade] = store.live_trades(deployment_id)
    assert store.reviews(trade.id) == []
    live_session.rollback()


def test_shadow_needs_an_api_key(live_session, make_ctx, deployment_id, llm):
    run_tick(make_ctx(deployment_id, [ENTRY], settings=shadow_settings(openai_api_key=None)))
    assert llm["calls"] == []
