from __future__ import annotations

import csv
import hashlib
import io

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.base import Base, LiveBase
from app.db.models import AuditEvent, IngestRun
from app.engine.market import MARKET_DATA_PATH
from app.main import app


def test_smoke_demo_to_audit_verify(client):
    demo = client.post("/api/demo/run").json()
    assert set(demo) >= {"strategy_id", "markdown", "report", "report_id"}
    assert demo["report"]["verdict"]["label"]

    report = client.get(f"/api/reports/{demo['report_id']}").json()
    assert report["inputs"]["market_data"]["sha256"] == hashlib.sha256(MARKET_DATA_PATH.read_bytes()).hexdigest()

    r = client.get(f"/api/reports/{demo['report_id']}/trades.csv")
    assert r.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert rows
    for column in ("reason_text", "entry_conditions", "target_pnl", "review_process_verdict", "review_lesson"):
        assert column in rows[0]
    assert all(row["reason_text"] and row["review_process_verdict"] for row in rows)

    assert client.get("/api/journal").json()["trades"]
    assert client.get("/api/audit/verify").json()["ok"] is True


def test_review_response_shape_is_unchanged(client, demo):
    body = client.post("/api/review/run", json={"strategy_id": demo["strategy_id"]}).json()
    assert {"report_id", "strategy_id", "strategy_name", "report"} <= set(body)
    assert set(body["report"]) == {
        "strategy", "audit", "scenarios", "scenario_results", "cost_summary", "verdict", "narrative", "data_source",
    }  # fmt: skip


def test_failed_review_is_recorded(client, demo, session, monkeypatch):
    def boom(_doc):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr("app.api.routes.run_review_crew", boom)
    failing = TestClient(app, raise_server_exceptions=False)
    r = failing.post("/api/review/run", json={"strategy_id": demo["strategy_id"]})
    assert r.status_code == 500
    failed = client.get("/api/reports", params={"status": "failed"}).json()["reports"]
    assert len(failed) == 1 and "simulated crash" in failed[0]["error"]
    actions = session.scalars(select(AuditEvent.action)).all()
    assert actions.count("report.failed") == 1


def test_failed_ingest_is_recorded(client, session):
    r = client.post("/api/ingest/extract", json={"source_type": "youtube"})
    assert r.status_code == 400
    run = session.scalars(select(IngestRun)).one()
    assert run.status == "failed" and "url required" in run.error
    assert session.scalars(select(AuditEvent.action)).all() == ["ingest.failed"]


def test_missing_resources_return_404(client, db_url):
    assert client.get("/api/strategies/nope").status_code == 404
    assert client.get("/api/reports/nope").status_code == 404
    assert client.get("/api/reports/nope/trades").status_code == 404
    assert client.post("/api/review/run", json={"strategy_id": "nope"}).status_code == 404
    assert client.post("/api/review/run", json={}).status_code == 400


def test_migrations_match_models(engine, live_engine):
    for eng, metadata in ((engine, Base.metadata), (live_engine, LiveBase.metadata)):
        with eng.connect() as conn:
            diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), metadata)
        assert diff == []
