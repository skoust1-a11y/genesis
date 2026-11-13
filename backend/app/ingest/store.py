from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.audit import current_actor, current_request_id, record_event
from app.db.hashing import sha256_hex
from app.db.models import IngestRun


class IngestRunStore:
    def __init__(self, session: Session) -> None:
        self.session = session

    def record(
        self,
        *,
        source: dict,
        hints: str,
        started_at: datetime,
        status: str,
        extractor: str | None = None,
        model: str | None = None,
        raw_output: str | None = None,
        error: str | None = None,
    ) -> IngestRun:
        text = source.get("text")
        run = IngestRun(
            source_type=source.get("source_type") or "unknown",
            source_url=source.get("source_url") or source.get("url"),
            source_text=text,
            source_sha256=sha256_hex(text) if text else None,
            hints=hints or None,
            extractor=extractor,
            model=model,
            raw_output=raw_output,
            status=status,
            error=error,
            actor=current_actor(),
            request_id=current_request_id(),
            started_at=started_at,
        )
        self.session.add(run)
        self.session.flush()
        record_event(
            self.session,
            "ingest.completed" if status == "completed" else "ingest.failed",
            "ingest_run",
            run.id,
            {
                "source_type": run.source_type,
                "source_url": run.source_url,
                "source_sha256": run.source_sha256,
                "extractor": extractor,
                "model": model,
                "raw_output_sha256": sha256_hex(raw_output) if raw_output else None,
                "error": error,
            },
        )
        return run

    def get(self, ingest_run_id: str) -> IngestRun:
        run = self.session.get(IngestRun, ingest_run_id)
        if run is None:
            raise FileNotFoundError(ingest_run_id)
        return run
