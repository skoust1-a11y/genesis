from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import current_actor, record_event
from app.db.hashing import normalize_json, sha256_hex
from app.db.models import Strategy, StrategyVersion, new_id
from app.db.types import utcnow
from app.strategies.schema import StrategyDocument, parse_strategy_markdown


@dataclass
class SavedStrategy:
    strategy_id: str
    doc: StrategyDocument
    version: StrategyVersion
    created_version: bool


class StrategyStore:
    def __init__(self, session: Session) -> None:
        self.session = session

    def save(
        self, markdown: str, strategy_id: str | None = None, ingest_run_id: str | None = None
    ) -> SavedStrategy:
        """Create the strategy or append a version. Unchanged content returns the latest version."""
        doc = parse_strategy_markdown(markdown)
        sha = sha256_hex(doc.markdown)
        if strategy_id is not None and not (0 < len(strategy_id) <= 36):
            raise ValueError("invalid strategy_id")
        strategy = self.session.get(Strategy, strategy_id) if strategy_id else None

        if strategy is None:
            strategy = Strategy(id=strategy_id or new_id(), name=doc.name, current_version=1)
            self.session.add(strategy)
            self.session.flush()
            version = self._add_version(strategy.id, 1, doc, sha, ingest_run_id)
            record_event(
                self.session,
                "strategy.created",
                "strategy",
                strategy.id,
                {"name": doc.name, "version": 1, "content_sha256": sha, "ingest_run_id": ingest_run_id},
            )
            return SavedStrategy(strategy.id, doc, version, True)

        if strategy.archived_at is not None:
            raise ValueError("strategy is archived")
        latest = self.latest_version(strategy.id)
        if latest.content_sha256 == sha:
            return SavedStrategy(strategy.id, doc, latest, False)

        number = strategy.current_version + 1
        version = self._add_version(strategy.id, number, doc, sha, ingest_run_id)
        strategy.current_version = number
        strategy.name = doc.name
        record_event(
            self.session,
            "strategy.version_added",
            "strategy",
            strategy.id,
            {
                "name": doc.name,
                "version": number,
                "previous_sha256": latest.content_sha256,
                "content_sha256": sha,
                "ingest_run_id": ingest_run_id,
            },
        )
        return SavedStrategy(strategy.id, doc, version, True)

    def _add_version(
        self, strategy_id: str, number: int, doc: StrategyDocument, sha: str, ingest_run_id: str | None
    ) -> StrategyVersion:
        version = StrategyVersion(
            strategy_id=strategy_id,
            version=number,
            markdown=doc.markdown,
            content_sha256=sha,
            frontmatter=normalize_json(doc.frontmatter),
            entry_rules=doc.entry,
            exit_rules=doc.exit,
            gaps=doc.gaps,
            ingest_run_id=ingest_run_id,
            created_by=current_actor(),
        )
        self.session.add(version)
        self.session.flush()
        return version

    def get_strategy(self, strategy_id: str) -> Strategy:
        strategy = self.session.get(Strategy, strategy_id)
        if strategy is None:
            raise FileNotFoundError(strategy_id)
        return strategy

    def latest_version(self, strategy_id: str) -> StrategyVersion:
        strategy = self.get_strategy(strategy_id)
        return self.version(strategy_id, strategy.current_version)

    def get(self, strategy_id: str) -> StrategyDocument:
        return parse_strategy_markdown(self.latest_version(strategy_id).markdown)

    def version(self, strategy_id: str, number: int) -> StrategyVersion:
        version = self.session.scalar(
            select(StrategyVersion).where(
                StrategyVersion.strategy_id == strategy_id, StrategyVersion.version == number
            )
        )
        if version is None:
            raise FileNotFoundError(f"{strategy_id} v{number}")
        return version

    def versions(self, strategy_id: str) -> list[StrategyVersion]:
        self.get_strategy(strategy_id)
        return list(
            self.session.scalars(
                select(StrategyVersion)
                .where(StrategyVersion.strategy_id == strategy_id)
                .order_by(StrategyVersion.version)
            )
        )

    def list(self) -> list[Strategy]:
        return list(self.session.scalars(select(Strategy).order_by(Strategy.created_at.desc())))

    def archive(self, strategy_id: str) -> Strategy:
        strategy = self.get_strategy(strategy_id)
        if strategy.archived_at is None:
            strategy.archived_at = utcnow()
            record_event(self.session, "strategy.archived", "strategy", strategy.id, {"name": strategy.name})
        return strategy
