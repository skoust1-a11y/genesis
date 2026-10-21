from __future__ import annotations

import re
from dataclasses import dataclass

from langchain_core.messages import HumanMessage, SystemMessage

from app.config import get_settings
from app.llm import chat_model, served_model
from app.strategies.rules import ENTRY_FIELDS, EVENT_KINDS, EXIT_KINDS, OPS, STRUCTURES

FIXTURE_MARKDOWN = """---
name: nifty-premium-sell-demo
market: nse_options
underlying: NIFTY
max_lots: 2
source_youtube: fixture
rules:
  instrument:
    underlying: NIFTY
    structure: iron_condor
    short_distance: 300
    wing_width: 100
  entry:
  - field: iv_rank
    op: '>='
    value: 40
  - field: dte
    op: '>='
    value: 7
  - field: dte
    op: '<='
    value: 21
  exit:
  - kind: profit_target_pct_of_credit
    value: 50
  - kind: stop_loss_pct_of_credit
    value: 100
  risk:
    max_lots: 2
    max_daily_loss_pct: 2
---

# Thesis
Sell defined-risk premium when implied volatility is elevated.

## Entry
- When IV rank >= 40 and DTE between 7 and 21 <!-- source: fixture -->
- Action: open iron condor width 100 <!-- source: fixture -->

## Exit
- Close all when profit >= 50% of credit <!-- source: fixture -->
- Close all when loss >= 100% of credit <!-- source: fixture -->

## Risk
- Never exceed max_lots
- Stop trading for the day if daily loss exceeds 2%

## Gaps
- Confirm exact delta target for short strikes
"""


SINGLE_PASS_CHARS = 80_000  # longer sources are condensed part by part before extraction
CHUNK_CHARS = 60_000

NOTES_PROMPT = (
    "You read one part of a trading education transcript or document and write notes for a later step that "
    "turns them into a strategy. Write in English whatever the source language. As bullet points, keep every "
    "trading rule, entry and exit condition, stop-loss and target placement, timeframe, instrument, number, "
    "threshold and risk or money-management instruction, and a one-line summary of each worked chart example. "
    "Start each bullet with the source timestamp, like [mm:ss], when the text has one. Leave out greetings, "
    "audience chatter, technical problems and promotion. Never add rules the text does not state."
)


@dataclass
class ExtractionResult:
    markdown: str
    extractor: str  # "llm" | "fixture"
    model: str | None
    raw_output: str


def extract_strategy_markdown(source: dict, hints: str = "") -> str:
    return extract_strategy(source, hints).markdown


def _content(resp) -> str:
    return resp.content if isinstance(resp.content, str) else str(resp.content)


def split_into_chunks(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """Consecutive pieces of at most `size` characters, cut at line ends where possible."""
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for line in text.splitlines(keepends=True):
        for piece in (line[i : i + size] for i in range(0, len(line), size)):
            if current and length + len(piece) > size:
                chunks.append("".join(current))
                current, length = [], 0
            current.append(piece)
            length += len(piece)
    if current:
        chunks.append("".join(current))
    return chunks


def condense_source(llm, text: str) -> str:
    parts = split_into_chunks(text)
    replies = llm.batch(
        [
            [SystemMessage(content=NOTES_PROMPT), HumanMessage(content=f"Part {i} of {len(parts)}:\n\n{part}")]
            for i, part in enumerate(parts, 1)
        ]
    )
    return "\n\n".join(f"## Part {i} of {len(parts)}\n{_content(r).strip()}" for i, r in enumerate(replies, 1))


def extract_strategy(source: dict, hints: str = "") -> ExtractionResult:
    settings = get_settings()
    if not settings.llm_enabled:
        return ExtractionResult(FIXTURE_MARKDOWN, "fixture", None, FIXTURE_MARKDOWN)

    system = SystemMessage(
        content=(
            "You convert trading education text into a strategy markdown file. "
            "Output ONLY markdown with YAML frontmatter and sections: Thesis, Entry, Exit, Risk, Gaps. "
            "Never invent numeric thresholds not supported by the source; put unknowns in Gaps. "
            "Add HTML comment citations on rule bullets.\n\n"
            "Also add a `rules:` key to the frontmatter that restates the rules in machine-checkable form:\n"
            f"- instrument: underlying, structure (one of {', '.join(STRUCTURES)}), optional lot_size. "
            "Option selling (iron_condor, short_strangle, short_straddle): short_distance (points from spot to "
            "the short strikes), wing_width (points from short to long strikes, iron_condor). Option buying "
            "(long_call, long_put, bull_call_spread, bear_put_spread): optional strike_offset (points from the "
            "at-the-money strike to the bought strike, positive = out of the money), wing_width (points from the "
            "bought to the sold strike, spreads only)\n"
            f"- entry: list of {{field, op, value}} with field in {', '.join(ENTRY_FIELDS)} "
            f"and op in {', '.join(OPS)}. pct_from_prev_high is spot vs the previous day's high in % (above 0 "
            "= broke above it); pct_from_prev_low is spot vs the previous day's low (below 0 = broke below it); "
            "pct_high_from_prev_high is the day's high vs the previous day's high (above 0 = swept above it) and "
            "pct_low_from_prev_low the day's low vs the previous day's low (below 0 = swept below it). A sweep that "
            "closes back inside is pct_high_from_prev_high > 0 with pct_from_prev_high < 0 (short), or "
            "pct_low_from_prev_low < 0 with pct_from_prev_low > 0 (long)\n"
            "A trade policy's odds gate (enter only when the strategy's past trades in today's VIX band made "
            "money) is set when a deployment is approved: describe 'only when the odds favour us' under Risk.\n"
            f"- exit: list of {{kind, value}} with kind in {', '.join(EXIT_KINDS)}. Option selling needs "
            "profit_target_pct_of_credit and stop_loss_pct_of_credit; option buying needs "
            "profit_target_pct_of_debit and stop_loss_pct_of_debit (at most 100), as % of the premium paid. "
            "When the source states the stop as a rupee amount or % of the account (e.g. 'risk Rs 1,500 of "
            "Rs 50,000'), use stop_loss_pct_of_capital (above 0, at most 20) with profit_target_r_multiple "
            "(the target as a multiple of that stop, e.g. 3 for 1:3) instead of the premium pair; the two go "
            "together and are never mixed with the premium kinds\n"
            "- risk: max_lots, max_daily_loss_pct, optional max_open_positions, optional min_risk_reward, "
            "optional max_entries_per_day, optional max_consecutive_losses with cooldown_days (trading days "
            "without new entries after that many losses in a row; both or neither), optional "
            "max_loss_pct_of_capital (largest loss one trade may risk, as % of capital; 100 means never more "
            "than the account), optional max_losses_per_day (1 means no re-entry after a losing trade that day), "
            "optional min_minutes_between_trades (wait after an entry or exit, at most 375)\n"
            "Account money management (a rupee capital floor, a break after hitting it, withdrawing profits) is "
            "set when a deployment is approved, not in `rules:`: describe it under Risk, not under Gaps.\n"
            "- optional entry_blackout: {kinds, days_before, days_after} when the source says to avoid new "
            f"trades around scheduled events; kinds from {', '.join(EVENT_KINDS)}\n"
            "Only include a key when the source states its value. If a rule cannot be expressed with these "
            "keys, or a required value is unknown, leave it out of `rules:` and list it under Gaps."
        )
    )
    llm = chat_model(settings)
    text = source.get("text", "")
    notes = condense_source(llm, text) if len(text) > SINGLE_PASS_CHARS else None
    body = (
        f"NOTES (condensed part by part from a {len(text):,}-character source; cite their timestamps):\n"
        f"{notes[:SINGLE_PASS_CHARS]}"
        if notes is not None
        else f"TEXT:\n{text}"
    )
    user = HumanMessage(
        content=(
            f"Source type: {source.get('source_type')}\n"
            f"Source url: {source.get('source_url', '')}\n"
            f"Hints: {hints}\n\n"
            f"{body}"
        )
    )
    resp = llm.invoke([system, user])
    reply, model = _content(resp), served_model(resp, settings)
    raw = reply if notes is None else f"# Condensed notes\n\n{notes}\n\n# Extraction\n\n{reply}"
    content = reply.strip()
    if not content.startswith("---"):
        m = re.search(r"```(?:markdown|md)?\s*(---[\s\S]+?)```", content)
        if m:
            content = m.group(1).strip()
        else:
            idx = content.find("---")
            if idx != -1:
                content = content[idx:].strip()
                if content.endswith("```"):
                    content = content[:-3].strip()
    if not content.startswith("---"):
        raise ExtractionError("Extractor did not return valid strategy markdown", raw, model)
    return ExtractionResult(content, "llm", model, raw)


class ExtractionError(ValueError):
    def __init__(self, message: str, raw_output: str | None = None, model: str | None = None) -> None:
        super().__init__(message)
        self.raw_output = raw_output
        self.model = model
