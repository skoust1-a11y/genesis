from __future__ import annotations

import json
import uuid
from pathlib import Path

from app.config import get_settings
from app.strategies.schema import StrategyDocument, parse_strategy_markdown


class StrategyStore:
    def __init__(self) -> None:
        settings = get_settings()
        self.root = Path(settings.strategies_dir)
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, markdown: str, strategy_id: str | None = None) -> tuple[str, StrategyDocument]:
        doc = parse_strategy_markdown(markdown)
        sid = strategy_id or str(uuid.uuid4())
        path = self.root / f"{sid}.md"
        path.write_text(doc.markdown, encoding="utf-8")
        meta_path = self.root / f"{sid}.json"
        meta_path.write_text(json.dumps({"id": sid, "name": doc.name}, indent=2), encoding="utf-8")
        return sid, doc

    def get(self, strategy_id: str) -> StrategyDocument:
        path = self.root / f"{strategy_id}.md"
        return parse_strategy_markdown(path.read_text(encoding="utf-8"))

    def list_ids(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("*.md"))
