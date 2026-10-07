"""The LLM's second opinion on an entry the rules already approved (the AI Trade Check).

The model sees the strategy's own write-up as a checklist, the proposal, the rule and risk checks
that passed, point-in-time market context (the day's levels, events and headlines) and recent lessons. It can only answer take, reduce (fewer lots) or skip:
it cannot add trades, raise size or change strikes. In shadow mode its answer is recorded and
never changes the decision; veto mode (per deployment, see `app.live.veto`) applies it.

Any failure (timeout, bad output, provider error) is recorded and the rules' decision stands.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from app.config import Settings
from app.db.hashing import sha256_hex
from app.market.context import MarketContext
from app.strategies.rules import Check
from app.strategies.schema import StrategyDocument
from app.trading.decisions import describe_check

SYSTEM_PROMPT = """You are a risk reviewer for a rules-based NSE index options strategy.
The strategy's rules have ALREADY decided to open the position below, and every risk check passed.
Your only job is to say whether anything in the context argues for trading smaller or not at all.

Answer with one decision:
- take: no reason to deviate from the rules.
- reduce: trade fewer lots; set `lots` to a number below the proposed lots (at least 1).
- skip: do not open this position.
You cannot add trades, increase size or change strikes. Prefer take unless the context gives a
concrete, specific reason (for example a scheduled event inside the holding period). Give at most
five short reasons and five risk flags, and a confidence between 0 and 1.

When a STRATEGY CHECKLIST is given, it is the approved strategy's own write-up. Its machine-checkable
rules already passed; review the other items against the MARKET block (for example a trend or a
level the write-up asks for) and name each item the data shows is not met, or cannot be verified
from it, in risk_flags as "checklist: <item>". Do not skip a trade only because an item cannot be
verified."""


class OpinionOutput(BaseModel):
    decision: Literal["take", "reduce", "skip"]
    lots: int | None = Field(default=None, ge=0, description="For reduce: the lots to trade, fewer than proposed.")
    reasons: list[str] = Field(default_factory=list, max_length=5)
    risk_flags: list[str] = Field(default_factory=list, max_length=5)
    confidence: float = Field(ge=0, le=1)


@dataclass
class OpinionResult:
    status: str  # ok | error
    decision: str | None  # normalized: take | reduce | skip
    lots: int | None  # lots the opinion would trade
    output: OpinionOutput | None
    model: str
    prompt_sha256: str
    context: dict
    lesson_review_ids: list[int] = field(default_factory=list)
    latency_ms: int | None = None
    error: str | None = None


def normalize(output: OpinionOutput, rules_lots: int) -> tuple[str, int]:
    """(decision, lots), never above the rules' size. A reduce without a smaller size changes nothing."""
    if output.decision == "skip":
        return "skip", 0
    if output.decision == "reduce" and output.lots is not None and output.lots < rules_lots:
        return ("skip", 0) if output.lots == 0 else ("reduce", output.lots)
    return "take", rules_lots


def render_checklist(strategy: StrategyDocument) -> str:
    """The write-up's thesis and Entry/Exit/Risk bullets, without citation comments."""

    def clean(text: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)).strip()

    parts = [f"Thesis: {clean(strategy.thesis)}"] if strategy.thesis.strip() else []
    for title, lines in (("Entry", strategy.entry), ("Exit", strategy.exit), ("Risk", strategy.risk)):
        items = [clean(line) for line in lines if clean(line)]
        if items:
            parts.append(f"{title}:\n" + "\n".join(f"- {item}" for item in items))
    return "\n".join(parts)


def build_prompt(
    context: MarketContext,
    proposal: dict,
    entry_checks: list[Check],
    risk_checks: list[Check],
    lessons: list[str],
    strategy: StrategyDocument | None = None,
) -> str:
    economics = proposal.get("economics")
    position = {
        "lots": proposal["lots"],
        "lot_size": proposal.get("lot_size"),
        "legs": [
            {k: leg[k] for k in ("side", "option_type", "strike", "expiry", "quote")} for leg in proposal["legs"]
        ],
        "credit_inr": round(economics.credit, 2) if economics else None,  # negative: a premium paid
        "premium": ("paid" if economics.credit < 0 else "received") if economics else None,
        "target_pnl_inr": round(economics.target_pnl, 2) if economics else None,
        "max_loss_inr": round(economics.max_loss, 2) if economics else None,
    }
    lesson_text = "\n".join(f"- {line}" for line in lessons) if lessons else "- none yet"
    checklist = render_checklist(strategy) if strategy is not None else ""
    return (
        (f"STRATEGY CHECKLIST:\n{checklist}\n\n" if checklist else "")
        + f"PROPOSED POSITION:\n{json.dumps(position, default=str)}\n\n"
        f"ENTRY CHECKS (all passed):\n" + "\n".join(f"- {describe_check(c)}" for c in entry_checks) + "\n\n"
        "RISK CHECKS (all passed):\n" + "\n".join(f"- {describe_check(c)}" for c in risk_checks) + "\n\n"
        f"LESSONS FROM RECENT TRADES OF THIS DEPLOYMENT:\n{lesson_text}\n\n"
        f"{context.render()}"
    )


def _call_llm(settings: Settings, system: str, user: str) -> OpinionOutput:
    """The only network call. Tests replace it."""
    from langchain_core.messages import HumanMessage, SystemMessage

    from app.llm import chat_model

    llm = chat_model(settings, settings.opinion_model, timeout=settings.llm_opinion_timeout_s, max_retries=0)
    out = llm.with_structured_output(OpinionOutput).invoke([SystemMessage(content=system), HumanMessage(content=user)])
    return out if isinstance(out, OpinionOutput) else OpinionOutput.model_validate(out)


def get_opinion(
    settings: Settings,
    context: MarketContext,
    proposal: dict,
    entry_checks: list[Check],
    risk_checks: list[Check],
    lessons: list[tuple[int, str]],
    strategy: StrategyDocument | None = None,
) -> OpinionResult:
    """`lessons` are (review id, lesson text), newest first."""
    prompt = build_prompt(context, proposal, entry_checks, risk_checks, [text for _, text in lessons], strategy)
    base = {
        "model": settings.opinion_model,
        "prompt_sha256": sha256_hex(SYSTEM_PROMPT + "\n\n" + prompt),
        "context": context.as_dict(),
        "lesson_review_ids": [review_id for review_id, _ in lessons],
    }
    started = time.monotonic()
    try:
        output = _call_llm(settings, SYSTEM_PROMPT, prompt)
    except Exception as exc:
        return OpinionResult(
            status="error", decision=None, lots=None, output=None,
            latency_ms=int((time.monotonic() - started) * 1000), error=f"{type(exc).__name__}: {exc}"[:500], **base,
        )  # fmt: skip
    decision, lots = normalize(output, proposal["lots"])
    return OpinionResult(
        status="ok", decision=decision, lots=lots, output=output,
        latency_ms=int((time.monotonic() - started) * 1000), **base,
    )  # fmt: skip
