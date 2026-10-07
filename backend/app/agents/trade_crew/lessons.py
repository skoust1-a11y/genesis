"""After a live trade closes, an LLM writes a short post-trade review whose lesson feeds later opinions.

Only runs when trade opinions are enabled. The review is stored as an agent review
(`reviewer_type="agent"`, `reviewer="llm:<model>"`) next to human reviews. Lesson text is cleaned
and capped before storage because it is quoted back into future prompts.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field

from app.config import Settings
from app.db.models import LiveTrade, TradeIntent, TradeOpinion
from app.market.news import clean_headline

LESSON_CHARS = 300

SYSTEM_PROMPT = """You review a closed trade from a rules-based NSE index options strategy.
Judge the process separately from the outcome: a good process can lose and a bad one can win.
If an LLM opinion was recorded before entry, say whether it was right in hindsight.
Write one lesson of at most two sentences that would help decide a similar entry next time.
Do not suggest changing the strategy's rules; that is done by humans."""


class LessonOutput(BaseModel):
    process_verdict: Literal[
        "good_process_good_outcome", "good_process_bad_outcome", "bad_process_good_outcome", "bad_process_bad_outcome"
    ]
    what_went_well: str = Field(max_length=500)
    what_went_wrong: str = Field(max_length=500)
    lesson: str = Field(max_length=600)
    mistake_tags: list[str] = Field(default_factory=list, max_length=5)


def trade_facts(trade: LiveTrade, intent: TradeIntent, opinion: TradeOpinion | None) -> dict:
    return {
        "opened_at": trade.opened_at.isoformat(),
        "closed_at": trade.closed_at.isoformat(),
        "holding_minutes": trade.holding_minutes,
        "entry_rule": intent.entry_rule,
        "entry_reason": intent.reason_text,
        "market_at_entry": intent.market_snapshot,
        "legs": trade.legs,
        "exit_rule": trade.exit_rule,
        "exit_reason": trade.exit_reason,
        "target_pnl": str(intent.target_pnl),
        "net_pnl": str(trade.net),
        "pnl_vs_target": str(trade.pnl_vs_target),
        "outcome": trade.outcome_label,
        "llm_opinion_before_entry": (
            {"decision": opinion.decision, "reasons": opinion.reasons, "confidence": opinion.confidence}
            if opinion is not None and opinion.status == "ok"
            else None
        ),
    }


def _call_llm(settings: Settings, system: str, user: str) -> LessonOutput:
    """The only network call. Tests replace it."""
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(
        model=settings.opinion_model,
        api_key=settings.openai_api_key,
        temperature=0,
        timeout=settings.llm_opinion_timeout_s,
        max_retries=0,
    )
    out = llm.with_structured_output(LessonOutput).invoke([SystemMessage(content=system), HumanMessage(content=user)])
    return out if isinstance(out, LessonOutput) else LessonOutput.model_validate(out)


def write_lesson(settings: Settings, facts: dict) -> dict | None:
    """Review fields for `LiveStore.add_agent_review`, or None if the LLM call fails."""
    try:
        out = _call_llm(settings, SYSTEM_PROMPT, json.dumps(facts, default=str))
    except Exception:
        return None
    return {
        "followed_plan": True,  # the crew only acts through the rules
        "process_verdict": out.process_verdict,
        "what_went_well": clean_headline(out.what_went_well, 500) or None,
        "what_went_wrong": clean_headline(out.what_went_wrong, 500) or None,
        "lesson": clean_headline(out.lesson, LESSON_CHARS) or None,
        "mistake_tags": [clean_headline(t, 40) for t in out.mistake_tags if t.strip()],
        "confidence": None,
    }
