from __future__ import annotations

from app import alerts
from app.config import get_settings
from app.db.models import Notification


def test_record_notification(session):
    notif_id = alerts.record(
        title="Test Alert",
        message="Something happened",
        category="risk",
        level="warning",
        deployment_id="dep-123",
        channels=["telegram"],
        details={"foo": "bar"},
        session=session,
    )
    assert notif_id is not None
    loaded = session.get(Notification, notif_id)
    assert loaded is not None
    assert loaded.title == "Test Alert"
    assert loaded.message == "Something happened"
    assert loaded.category == "risk"
    assert loaded.level == "warning"
    assert loaded.deployment_id == "dep-123"
    assert loaded.channels == ["telegram"]
    assert loaded.details == {"foo": "bar"}
    assert loaded.read_at is None


def test_notify_persists_to_notifications(session, monkeypatch):
    monkeypatch.setenv("ALERT_TELEGRAM_BOT_TOKEN", "")
    get_settings.cache_clear()
    settings = get_settings()

    alerts.notify(settings, "halted", "dep-456", "Emergency stop triggered", session=session)
    notif = session.query(Notification).filter_by(deployment_id="dep-456").first()
    assert notif is not None
    assert "[genesis] halted: deployment dep-456" in notif.title
    assert notif.level == "critical"
    assert notif.category == "live"
    assert notif.message == "Emergency stop triggered"


def test_notifications_api_lifecycle(client):
    # 1. Create notifications via API
    res1 = client.post(
        "/api/notifications",
        json={"title": "N1", "message": "Body 1", "category": "alert", "level": "info"},
    )
    assert res1.status_code == 200
    n1_id = res1.json()["id"]

    res2 = client.post(
        "/api/notifications",
        json={"title": "N2", "message": "Body 2", "category": "risk", "level": "critical"},
    )
    assert res2.status_code == 200
    n2_id = res2.json()["id"]

    # 2. List notifications
    res_list = client.get("/api/notifications")
    assert res_list.status_code == 200
    data = res_list.json()
    assert data["total"] >= 2
    assert data["unread_count"] >= 2

    # 3. Filter by category
    res_filtered = client.get("/api/notifications", params={"category": "risk"})
    assert res_filtered.status_code == 200
    assert all(n["category"] == "risk" for n in res_filtered.json()["notifications"])

    # 4. Mark one as read
    res_read = client.post(f"/api/notifications/{n1_id}/read")
    assert res_read.status_code == 200
    assert res_read.json()["read_at"] is not None

    # 5. Filter unread only
    res_unread = client.get("/api/notifications", params={"unread_only": True})
    assert res_unread.status_code == 200
    unread_ids = [n["id"] for n in res_unread.json()["notifications"]]
    assert n1_id not in unread_ids
    assert n2_id in unread_ids

    # 6. Mark all as read
    res_read_all = client.post("/api/notifications/read-all")
    assert res_read_all.status_code == 200
    assert res_read_all.json()["marked_read"] >= 1

    # Verify unread count is 0
    res_final = client.get("/api/notifications", params={"unread_only": True})
    assert len(res_final.json()["notifications"]) == 0
