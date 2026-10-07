from __future__ import annotations

import re
from dataclasses import dataclass

from langchain_core.messages import HumanMessage, SystemMessage

from app.config import get_settings
from app.llm import chat_model
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


@dataclass
class ExtractionResult:
    markdown: str
    extractor: str  # "llm" | "fixture"
    model: str | None
    raw_output: str


def extract_strategy_markdown(source: dict, hints: str = "") -> str:
    return extract_strategy(source, hints).markdown


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
            f"- instrument: underlying, structure (one of {', '.join(STRUCTURES)}), short_distance "
            "(points from spot to the short strikes), wing_width (points from short to long strikes), "
            "optional lot_size\n"
            f"- entry: list of {{field, op, value}} with field in {', '.join(ENTRY_FIELDS)} "
            f"and op in {', '.join(OPS)}\n"
            f"- exit: list of {{kind, value}} with kind in {', '.join(EXIT_KINDS)}\n"
            "- risk: max_lots, max_daily_loss_pct, optional max_open_positions, optional min_risk_reward, "
            "optional max_entries_per_day, optional max_consecutive_losses with cooldown_days (trading days "
            "without new entries after that many losses in a row; both or neither), optional "
            "max_loss_pct_of_capital (largest loss one trade may risk, as % of capital; 100 means never more "
            "than the account)\n"
            "- optional entry_blackout: {kinds, days_before, days_after} when the source says to avoid new "
            f"trades around scheduled events; kinds from {', '.join(EVENT_KINDS)}\n"
            "Only include a key when the source states its value. If a rule cannot be expressed with these "
            "keys, or a required value is unknown, leave it out of `rules:` and list it under Gaps."
        )
    )
    user = HumanMessage(
        content=(
            f"Source type: {source.get('source_type')}\n"
            f"Source url: {source.get('source_url', '')}\n"
            f"Hints: {hints}\n\n"
            f"TEXT:\n{source.get('text', '')[:80_000]}"
        )
    )
    llm = chat_model(settings)
    resp = llm.invoke([system, user])
    raw = resp.content if isinstance(resp.content, str) else str(resp.content)
    content = raw.strip()
    if not content.startswith("---"):
        m = re.search(r"```(?:markdown|md)?\s*(---[\s\S]+?)```", content)
        if m:
            content = m.group(1).strip()
    if not content.startswith("---"):
        raise ExtractionError("Extractor did not return valid strategy markdown", raw, settings.llm_model)
    return ExtractionResult(content, "llm", settings.llm_model, raw)


class ExtractionError(ValueError):
    def __init__(self, message: str, raw_output: str | None = None, model: str | None = None) -> None:
        super().__init__(message)
        self.raw_output = raw_output
        self.model = model
