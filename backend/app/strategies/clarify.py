from __future__ import annotations

from app.strategies.rules import EVENT_KINDS, STRUCTURES
from app.strategies.schema import StrategyDocument

STOP_KINDS = ("stop_loss_pct_of_credit", "stop_loss_pct_of_debit", "stop_loss_pct_of_capital")
TARGET_KINDS = ("profit_target_pct_of_credit", "profit_target_pct_of_debit", "profit_target_r_multiple")


def clarifying_questions(doc: StrategyDocument) -> list[dict]:
    """What to ask the author before backtesting: `required` ones block a meaningful review."""
    raw = doc.frontmatter.get("rules") or {}
    instrument = raw.get("instrument") or {}
    entry = raw.get("entry") or []
    exit_kinds = {e.get("kind") for e in raw.get("exit") or [] if isinstance(e, dict)}
    entry_fields = {c.get("field") for c in entry if isinstance(c, dict)}
    risk = raw.get("risk") or {}
    out: list[dict] = []

    def ask(qid: str, question: str, why: str, required: bool, options: list[str] | None = None) -> None:
        out.append({"id": qid, "question": question, "why": why, "required": required, "options": options or []})

    if not instrument.get("underlying"):
        ask("underlying", "Which index does this trade?", "Nothing can be backtested without it.", True,
            ["NIFTY", "BANKNIFTY", "FINNIFTY"])  # fmt: skip
    if not instrument.get("structure"):
        ask("structure", "What position is opened on a signal?", "Decides the legs, premium and risk.", True,
            list(STRUCTURES))  # fmt: skip
    if not entry:
        ask("entry", "What exact, measurable condition opens a trade?",
            "Without a checkable trigger every day looks like a setup.", True)  # fmt: skip
    if not exit_kinds & set(STOP_KINDS):
        ask("stop", "Where is the stop-loss (e.g. 1% of capital, or 100% of premium)?",
            "Unbounded losses make the result meaningless.", True)  # fmt: skip
    if not exit_kinds & set(TARGET_KINDS):
        ask("target", "Where is the profit target (e.g. 1:2 risk-reward, or 50% of premium)?",
            "Without a target the exit is guessed.", True)  # fmt: skip
    if not risk.get("max_lots"):
        ask("max_lots", "Most lots per trade?", "Sizes every trade.", True, ["1", "2", "5"])
    for error in doc.rules_errors:
        ask(f"rules_error_{len(out)}", f"Fix this rule: {error}", "The rule as written cannot be checked.", True)

    # When the setup does not apply: skipping bad days matters more than finding entries.
    if not raw.get("entry_blackout"):
        ask("blackout", "Skip new trades around which scheduled events?", "Event days break most price setups.",
            False, [*EVENT_KINDS, "none"])  # fmt: skip
    if "vix" not in entry_fields:
        ask("vix_range", "Is there an India VIX range where this setup should not be traded (e.g. above 22)?",
            "Volatility regime decides whether stops and targets are realistic.", False)  # fmt: skip
    if not risk.get("max_entries_per_day"):
        ask("max_entries_per_day", "Most trades per day?", "Selectivity: the best setup, not every setup.",
            False, ["1", "2", "3"])  # fmt: skip
    if not risk.get("max_losses_per_day"):
        ask("max_losses_per_day", "Stop for the day after how many losing trades?", "Avoids revenge trading.",
            False, ["1", "2"])  # fmt: skip
    for i, gap in enumerate(doc.gaps):
        ask(f"gap_{i}", gap, "The source left this open.", False)
    return out


def answers_as_hints(questions: list[dict], answers: dict[str, str]) -> str:
    lines = [f"- {q['question']} -> {answers[q['id']].strip()}" for q in questions if answers.get(q["id"], "").strip()]
    if not lines:
        return ""
    return "Author's answers to clarifying questions (treat as stated by the source):\n" + "\n".join(lines)
