from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml

from app.strategies.rules import StrategyRules, StrategyRulesV2, parse_rules, parse_rules_v2

_SOURCE_COMMENT_RE = re.compile(r"<!--\s*source:\s*(.*?)\s*-->", flags=re.IGNORECASE | re.DOTALL)


@dataclass
class StrategyDocument:
    frontmatter: dict
    body: str
    thesis: str = ""
    entry: list[str] = field(default_factory=list)
    exit: list[str] = field(default_factory=list)
    risk: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    source_citations: list[str] = field(default_factory=list)
    source_verification_required: bool = False

    @property
    def name(self) -> str:
        return str(self.frontmatter.get("name", "unnamed-strategy"))

    @property
    def rules(self) -> StrategyRules | None:
        parsed, _ = parse_rules(self.frontmatter)
        if parsed is not None:
            return parsed
        v2, _ = parse_rules_v2(self.frontmatter)
        return v2.to_legacy_rules() if v2 is not None else None

    @property
    def rules_errors(self) -> list[str]:
        if "rules" not in self.frontmatter and "rules_v2" in self.frontmatter:
            _, errs = parse_rules_v2(self.frontmatter)
            return errs
        return parse_rules(self.frontmatter)[1]

    @property
    def rules_v2(self) -> StrategyRulesV2 | None:
        return parse_rules_v2(self.frontmatter)[0]

    @property
    def rules_v2_errors(self) -> list[str]:
        return parse_rules_v2(self.frontmatter)[1]

    @property
    def markdown(self) -> str:
        fm = yaml.safe_dump(self.frontmatter, sort_keys=False).strip()
        return f"---\n{fm}\n---\n\n{self.body.strip()}\n"


def _section_lines(body: str, heading: str) -> list[str]:
    pattern = rf"^##\s+{re.escape(heading)}\s*$"
    match = re.search(pattern, body, flags=re.MULTILINE | re.IGNORECASE)
    if not match:
        return []
    start = match.end()
    next_h = re.search(r"^##\s+", body[start:], flags=re.MULTILINE)
    chunk = body[start : start + next_h.start()] if next_h else body[start:]
    lines = []
    for line in chunk.splitlines():
        line = line.strip()
        if line.startswith("- "):
            lines.append(line[2:].strip())
    return lines


def parse_strategy_markdown(text: str) -> StrategyDocument:
    text = text.strip()
    if not text.startswith("---"):
        raise ValueError("Strategy markdown must start with YAML frontmatter")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ValueError("Invalid frontmatter block")
    raw_yaml = re.sub(r'(\bop:\s*)([><!=]=?)(\s|$)', r'\1"\2"\3', parts[1])
    frontmatter = yaml.safe_load(raw_yaml) or {}
    body = parts[2].strip()
    thesis_match = re.search(r"^#\s+Thesis\s*$([\s\S]*?)(?=^##\s+|\Z)", body, flags=re.MULTILINE | re.IGNORECASE)
    thesis = thesis_match.group(1).strip() if thesis_match else ""
    citations = [c.strip() for c in _SOURCE_COMMENT_RE.findall(body) if c.strip()]
    doc = StrategyDocument(
        frontmatter=frontmatter,
        body=body,
        thesis=thesis,
        entry=_section_lines(body, "Entry"),
        exit=_section_lines(body, "Exit"),
        risk=_section_lines(body, "Risk"),
        gaps=_section_lines(body, "Gaps"),
        source_citations=citations,
    )
    return doc
