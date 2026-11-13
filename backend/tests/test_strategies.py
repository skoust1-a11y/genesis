from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import AuditEvent, Strategy, StrategyVersion
from app.extract.extractor import FIXTURE_MARKDOWN
from app.strategies.store import StrategyStore


def test_same_markdown_does_not_add_version_and_changed_markdown_does(session):
    store = StrategyStore(session)
    first = store.save(FIXTURE_MARKDOWN)
    again = store.save(FIXTURE_MARKDOWN, strategy_id=first.strategy_id)
    assert again.created_version is False
    assert again.version.id == first.version.id

    changed = store.save(FIXTURE_MARKDOWN.replace("max_lots: 2", "max_lots: 1"), strategy_id=first.strategy_id)
    session.commit()
    assert changed.created_version is True
    assert changed.version.version == 2
    assert session.get(Strategy, first.strategy_id).current_version == 2
    assert len(store.versions(first.strategy_id)) == 2
    assert changed.version.content_sha256 != first.version.content_sha256

    actions = session.scalars(select(AuditEvent.action).order_by(AuditEvent.id)).all()
    assert actions == ["strategy.created", "strategy.version_added"]


def test_archived_strategy_rejects_new_versions(session):
    store = StrategyStore(session)
    saved = store.save(FIXTURE_MARKDOWN)
    store.archive(saved.strategy_id)
    store.archive(saved.strategy_id)
    session.commit()
    assert session.get(Strategy, saved.strategy_id).archived_at is not None
    with pytest.raises(ValueError, match="archived"):
        store.save(FIXTURE_MARKDOWN.replace("NIFTY", "BANKNIFTY"), strategy_id=saved.strategy_id)
    archived_events = session.scalars(select(AuditEvent).where(AuditEvent.action == "strategy.archived")).all()
    assert len(archived_events) == 1


def test_version_endpoints(client, demo):
    sid = demo["strategy_id"]
    versions = client.get(f"/api/strategies/{sid}/versions").json()["versions"]
    assert [v["version"] for v in versions] == [1]
    assert "markdown" not in versions[0]
    v1 = client.get(f"/api/strategies/{sid}/versions/1").json()
    assert v1["markdown"].startswith("---")
    assert v1["ingest_run_id"]
    run = client.get(f"/api/ingest-runs/{v1['ingest_run_id']}").json()
    assert run["status"] == "completed"
    assert run["extractor"] == "fixture"
    assert run["source_sha256"]
    assert client.get(f"/api/strategies/{sid}/versions/9").status_code == 404
    listed = client.get("/api/strategies").json()["strategies"]
    assert listed[0]["id"] == sid and listed[0]["current_version"] == 1


def test_version_rows_record_actor(session, client):
    client.post(
        "/api/ingest/extract",
        json={"source_type": "markdown", "text": FIXTURE_MARKDOWN},
        headers={"X-Actor": "alice"},
    )
    version = session.scalars(select(StrategyVersion)).one()
    assert version.created_by == "alice"
