from __future__ import annotations

from collections import Counter

from sqlalchemy import select, text

from app.audit import verify_chain
from app.db.models import AuditEvent
from app.extract.extractor import FIXTURE_MARKDOWN
from app.journal.store import JournalStore


# SQLite transactions start with BEGIN IMMEDIATE, so the test session must not hold one
# open while the API client writes.


def _actions_after(session, last_id: int) -> Counter:
    rows = session.scalars(select(AuditEvent.action).where(AuditEvent.id > last_id)).all()
    session.rollback()
    return Counter(rows)


def _last_id(session) -> int:
    last = session.scalar(select(AuditEvent.id).order_by(AuditEvent.id.desc()).limit(1)) or 0
    session.rollback()
    return last


def test_each_mutating_endpoint_writes_expected_audit_events(session, client):
    mark = _last_id(session)
    r = client.post("/api/ingest/extract", json={"source_type": "markdown", "text": FIXTURE_MARKDOWN})
    sid = r.json()["strategy_id"]
    assert _actions_after(session, mark) == Counter({"ingest.completed": 1, "strategy.created": 1})

    mark = _last_id(session)
    r = client.post("/api/review/run", json={"strategy_id": sid})
    assert r.status_code == 200
    report_id = r.json()["report_id"]
    assert _actions_after(session, mark) == Counter({"report.created": 1})

    mark = _last_id(session)
    trade_id = client.get(f"/api/reports/{report_id}/trades", params={"limit": 1}).json()["trades"][0]["id"]
    r = client.post(f"/api/trades/sim/{trade_id}/reviews", json={"lesson": "ok", "mistake_tags": ["late"]})
    assert r.status_code == 200
    assert _actions_after(session, mark) == Counter({"trade.reviewed": 1})

    improvement_id = JournalStore(session).propose(
        strategy_id=sid, title="t", rationale="r", proposed_change={"section": "Risk"}
    ).id
    session.commit()
    mark = _last_id(session)
    r = client.post(f"/api/improvements/{improvement_id}/decisions", json={"decision": "accepted"})
    assert r.status_code == 200 and r.json()["status"] == "accepted"
    assert _actions_after(session, mark) == Counter({"improvement.decided": 1})

    mark = _last_id(session)
    client.post(f"/api/strategies/{sid}/archive")
    client.post(f"/api/strategies/{sid}/archive")
    assert _actions_after(session, mark) == Counter({"strategy.archived": 1})

    mark = _last_id(session)
    r = client.post("/api/review/run", json={"strategy_id": sid})
    assert r.status_code == 200
    assert _actions_after(session, mark) == Counter({"report.created": 1})


def test_actor_and_request_id_are_recorded(session, client):
    r = client.post(
        "/api/ingest/extract",
        json={"source_type": "markdown", "text": FIXTURE_MARKDOWN},
        headers={"X-Actor": "bob", "X-Request-ID": "req-123"},
    )
    assert r.headers["x-request-id"] == "req-123"
    events = session.scalars(select(AuditEvent)).all()
    assert {e.actor for e in events} == {"bob"}
    assert {e.request_id for e in events} == {"req-123"}


def test_verify_chain_detects_edited_row(engine, session, demo):
    assert verify_chain(session)["ok"] is True
    session.rollback()
    with engine.begin() as conn:
        conn.execute(text("DROP TRIGGER audit_events_no_update"))
        conn.execute(text("UPDATE audit_events SET details = '{\"tampered\": true}' WHERE id = 2"))
    result = verify_chain(session)
    assert result == {"ok": False, "checked": 1, "first_bad_id": 2}


def test_verify_chain_detects_deleted_row(engine, session, demo):
    session.rollback()
    with engine.begin() as conn:
        conn.execute(text("DROP TRIGGER audit_events_no_delete"))
        conn.execute(text("DELETE FROM audit_events WHERE id = 2"))
    result = verify_chain(session)
    assert result["ok"] is False and result["first_bad_id"] == 3


def test_verify_endpoint(client, demo):
    body = client.get("/api/audit/verify").json()
    assert body["ok"] is True and body["checked"] >= 3
    events = client.get("/api/audit", params={"entity_type": "report"}).json()["events"]
    assert [e["action"] for e in events] == ["report.created"]
