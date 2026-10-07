"""Import the pre-database file store (data/strategies, data/reports) into the database.

Safe to re-run: strategies and reports whose ids already exist are skipped.

    uv run python scripts/import_file_store.py [--strategies-dir data/strategies] [--reports-dir data/reports]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.orm import Session  # noqa: E402

from app.audit import audit_context, record_event  # noqa: E402
from app.db.hashing import sha256_hex  # noqa: E402
from app.db.models import Report, Strategy  # noqa: E402
from app.db.session import session_scope  # noqa: E402
from app.reports.store import ReportStore  # noqa: E402
from app.strategies.store import StrategyStore  # noqa: E402

IMPORT_ACTOR = "import"


def _file_meta(path: Path) -> dict:
    return {"source_file": str(path), "source_sha256": sha256_hex(path.read_bytes())}


def import_file_store(session: Session, strategies_dir: Path, reports_dir: Path) -> dict:
    counts = {"strategies_imported": 0, "strategies_skipped": 0, "reports_imported": 0, "reports_skipped": 0}
    strategies = StrategyStore(session)
    reports = ReportStore(session)

    for md_path in sorted(strategies_dir.glob("*.md")) if strategies_dir.is_dir() else []:
        strategy_id = md_path.stem
        if session.get(Strategy, strategy_id) is not None:
            counts["strategies_skipped"] += 1
            continue
        saved = strategies.save(md_path.read_text(encoding="utf-8"), strategy_id=strategy_id)
        meta = _file_meta(md_path)
        json_path = md_path.with_suffix(".json")
        if json_path.exists():
            meta["metadata_file"] = _file_meta(json_path)
        record_event(session, "import.file_store", "strategy", strategy_id, {**meta, "version_id": saved.version.id})
        counts["strategies_imported"] += 1

    for report_path in sorted(reports_dir.glob("*.json")) if reports_dir.is_dir() else []:
        data = json.loads(report_path.read_text(encoding="utf-8"))
        report_id = data.get("report_id") or report_path.stem
        strategy_id = data.get("strategy_id")
        if session.get(Report, report_id) is not None or not strategy_id:
            counts["reports_skipped"] += 1
            continue
        try:
            version = strategies.latest_version(strategy_id)
        except FileNotFoundError:
            counts["reports_skipped"] += 1
            continue
        meta = _file_meta(report_path)
        started = datetime.fromtimestamp(report_path.stat().st_mtime, tz=UTC)
        reports.create_success(
            strategy_id=strategy_id,
            version=version,
            payload=data.get("report") or {},
            sim_runs=[],
            reviews=[],
            improvements=[],
            inputs={"legacy": True, **meta},
            started_at=started,
            report_id=report_id,
        )
        record_event(session, "import.file_store", "report", report_id, meta)
        counts["reports_imported"] += 1
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--strategies-dir", type=Path, default=Path("data/strategies"))
    parser.add_argument("--reports-dir", type=Path, default=Path("data/reports"))
    args = parser.parse_args()
    with audit_context(IMPORT_ACTOR), session_scope() as session:
        counts = import_file_store(session, args.strategies_dir, args.reports_dir)
    print(json.dumps(counts, indent=2))


if __name__ == "__main__":
    main()
