from __future__ import annotations

import json
import uuid

from sqlalchemy import select

from app.audit import audit_context, verify_chain
from app.db.models import AuditEvent, Report, SimTrade
from app.extract.extractor import FIXTURE_MARKDOWN
from scripts.import_file_store import import_file_store


def test_import_is_idempotent_and_audited(session, tmp_path):
    strategies_dir = tmp_path / "strategies"
    reports_dir = tmp_path / "reports"
    strategies_dir.mkdir()
    reports_dir.mkdir()
    sid, rid = str(uuid.uuid4()), str(uuid.uuid4())
    (strategies_dir / f"{sid}.md").write_text(FIXTURE_MARKDOWN)
    (strategies_dir / f"{sid}.json").write_text(json.dumps({"id": sid, "name": "demo"}))
    (reports_dir / f"{rid}.json").write_text(
        json.dumps({"report_id": rid, "strategy_id": sid, "report": {"verdict": {"label": "Conditional"}}})
    )
    (reports_dir / "orphan.json").write_text(json.dumps({"report_id": "orphan", "strategy_id": "missing"}))

    with audit_context("import"):
        first = import_file_store(session, strategies_dir, reports_dir)
        session.commit()
        second = import_file_store(session, strategies_dir, reports_dir)
        session.commit()

    assert first == {"strategies_imported": 1, "strategies_skipped": 0, "reports_imported": 1, "reports_skipped": 1}
    assert second == {"strategies_imported": 0, "strategies_skipped": 1, "reports_imported": 0, "reports_skipped": 2}

    report = session.get(Report, rid)
    assert report.inputs["legacy"] is True and report.actor == "import"
    assert report.verdict_label == "Conditional"
    assert session.scalars(select(SimTrade)).all() == []

    imports = session.scalars(select(AuditEvent).where(AuditEvent.action == "import.file_store")).all()
    assert {(e.entity_type, e.entity_id) for e in imports} == {("strategy", sid), ("report", rid)}
    assert all(e.actor == "import" and e.details["source_sha256"] for e in imports)
    assert verify_chain(session)["ok"] is True
