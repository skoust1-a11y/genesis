from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import update

from app import alerts, auth, suggest
from app.config import get_settings
from app.db.models import UserSession
from app.db.session import session_scope
from app.db.types import utcnow

LOGIN = {"username": "bob", "password": "correct-horse"}


def make_user(username: str = "bob", password: str = "correct-horse") -> None:
    with session_scope() as s:
        auth.set_password(s, username, password)


def bearer(client, body: dict = LOGIN) -> dict:
    response = client.post("/api/auth/login", json=body)
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def test_login_protects_writes_and_logout_ends_the_session(client):
    paper = {"broker": "paper", "mode": "paper", "account_ref_masked": "X"}
    assert client.post("/api/live/broker-accounts", json=paper).status_code == 200  # open until a user exists
    make_user()
    assert client.post("/api/live/broker-accounts", json=paper).status_code == 401
    assert client.post("/api/auth/login", json=LOGIN | {"password": "wrong-pass"}).status_code == 401
    assert client.post("/api/auth/login", json=LOGIN | {"username": "nobody"}).status_code == 401
    headers = bearer(client)
    assert client.get("/api/auth/me", headers=headers).json() == {"username": "bob", "auth_enabled": True}
    assert client.post("/api/live/broker-accounts", json=paper, headers=headers).status_code == 200
    assert client.post("/api/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/auth/me", headers=headers).status_code == 401


def test_expired_session_and_password_reset_end_access(client):
    make_user()
    expired, reset = bearer(client), bearer(client)
    with session_scope() as s:
        token_sha = auth.hash_token(expired["Authorization"].split()[1])
        s.execute(update(UserSession).where(UserSession.token_sha256 == token_sha)
                  .values(expires_at=utcnow() - timedelta(minutes=1)))  # fmt: skip
    assert client.get("/api/auth/me", headers=expired).status_code == 401
    assert client.get("/api/auth/me", headers=reset).status_code == 200
    make_user(password="another-pass")
    assert client.get("/api/auth/me", headers=reset).status_code == 401


def test_repeated_failures_lock_the_username(client):
    make_user("carol")
    for _ in range(auth.MAX_FAILURES):
        assert client.post("/api/auth/login", json={"username": "carol", "password": "nope-nope"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "carol", "password": "correct-horse"}).status_code == 429
    auth._failures.clear()


def test_passwords_are_validated_and_hashed(session):
    for name, password in (("bad name", "long-enough"), ("dave", "short")):
        with pytest.raises(ValueError):
            auth.set_password(session, name, password)
    stored = auth.hash_password("correct-horse")
    assert "correct-horse" not in stored
    assert auth.check_password("correct-horse", stored) and not auth.check_password("wrong", stored)


def test_suggest_sends_the_strategy_check_to_telegram(approvable, monkeypatch, capsys):
    calls = []

    class Response:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(alerts.httpx, "post", lambda url, json, timeout: calls.append(json) or Response())
    monkeypatch.setenv("ALERT_TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.setenv("ALERT_TELEGRAM_CHAT_ID", "42")
    get_settings.cache_clear()
    assert suggest.main(["--strategy", approvable["strategy_id"]]) == 0
    text = calls[0]["text"]
    assert text.startswith("[genesis] suggested trades") and "approvable-condor" in text
    assert "SUGGESTED" in text or "no trade" in text
    assert "sent via telegram" in capsys.readouterr().out
