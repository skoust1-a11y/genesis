from __future__ import annotations

import json
import re

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from app.config import get_settings

FIXTURE_MARKDOWN = """---
name: nifty-premium-sell-demo
market: nse_options
underlying: NIFTY
max_lots: 2
source_youtube: fixture
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


def extract_strategy_markdown(source: dict, hints: str = "") -> str:
    settings = get_settings()
    if not settings.llm_enabled:
        return FIXTURE_MARKDOWN

    system = SystemMessage(
        content=(
            "You convert trading education text into a strategy markdown file. "
            "Output ONLY markdown with YAML frontmatter and sections: Thesis, Entry, Exit, Risk, Gaps. "
            "Never invent numeric thresholds not supported by the source; put unknowns in Gaps. "
            "Add HTML comment citations on rule bullets."
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
    llm = ChatOpenAI(model=settings.openai_model, api_key=settings.openai_api_key, temperature=0)
    resp = llm.invoke([system, user])
    content = resp.content if isinstance(resp.content, str) else str(resp.content)
    content = content.strip()
    if not content.startswith("---"):
        m = re.search(r"```(?:markdown|md)?\s*(---[\s\S]+?)```", content)
        if m:
            content = m.group(1).strip()
    if not content.startswith("---"):
        raise ValueError("Extractor did not return valid strategy markdown")
    return content
