import pytest

from app import auth
from app.auth import registration
from app.config import get_settings
from app.db.models import SignupRequest, User
from app.db.session import session_scope

BODY = {"username": "new-user", "password": "correct-horse-password"}


@pytest.fixture(autouse=True)
def owner_config(monkeypatch):
    monkeypatch.setenv("SIGNUP_APPROVER_NAMES", "owner")
    get_settings.cache_clear()
    registration._attempts.clear()
    yield
    registration._attempts.clear()
    get_settings.cache_clear()


def account(client, name):
    with session_scope() as db:
        auth.set_password(db, name, "existing-long-password")
    r = client.post("/api/auth/login", json={"username": name, "password": "existing-long-password"})
    assert r.status_code == 200
    return {"Authorization": "Bearer " + r.json()["token"]}


def test_signup_is_pending_and_only_owner_can_approve(client):
    owner = account(client, "owner")
    member = account(client, "member")
    r = client.post("/api/auth/signup", json=BODY)
    assert r.status_code == 202 and "token" not in r.json()
    with session_scope() as db:
        assert db.get(User, BODY["username"]) is None
        pending = db.get(SignupRequest, BODY["username"])
        assert pending.status == "pending" and pending.password_hash != BODY["password"]
        assert auth.check_password(BODY["password"], pending.password_hash)
    assert client.post("/api/auth/login", json=BODY).status_code == 401
    assert client.get("/api/auth/signup-requests").status_code == 401
    assert client.get("/api/auth/signup-requests", headers=member).status_code == 403
    path = "/api/auth/signup-requests/" + BODY["username"]
    assert client.post(path, json={"decision": "approve"}, headers=member).status_code == 403
    listed = client.get("/api/auth/signup-requests", headers=owner)
    assert listed.status_code == 200 and listed.json()["requests"][0]["status"] == "pending"
    assert "password" not in listed.text
    assert client.post(path, json={"decision": "approve"}, headers=owner).json()["status"] == "approved"
    assert client.post(path, json={"decision": "approve"}, headers=owner).status_code == 409
    login = client.post("/api/auth/login", json=BODY)
    assert login.status_code == 200
    headers = {"Authorization": "Bearer " + login.json()["token"]}
    assert not client.get("/api/auth/signup/permissions", headers=headers).json()["can_approve"]
    assert client.get("/api/auth/signup-requests", headers=headers).status_code == 403
    with session_scope() as db:
        request = db.get(SignupRequest, BODY["username"])
        assert request.password_hash is None and request.decided_by == "owner"


def test_duplicates_cannot_reset_pending_or_existing_password(client):
    owner = account(client, "owner")
    initial = client.post("/api/auth/signup", json=BODY)
    duplicate = client.post("/api/auth/signup", json=BODY | {"password": "attacker-password"})
    existing = client.post("/api/auth/signup", json={"username": "owner", "password": "attacker-password"})
    assert initial.json() == duplicate.json() == existing.json()
    with session_scope() as db:
        assert auth.check_password(BODY["password"], db.get(SignupRequest, BODY["username"]).password_hash)
        assert auth.check_password("existing-long-password", db.get(User, "owner").password_hash)
        assert db.get(SignupRequest, "owner") is None
    assert client.get("/api/auth/signup/permissions", headers=owner).json()["can_approve"]


def test_rejection_never_creates_a_login(client):
    owner = account(client, "owner")
    client.post("/api/auth/signup", json=BODY)
    r = client.post("/api/auth/signup-requests/new-user", json={"decision": "reject"}, headers=owner)
    assert r.json()["status"] == "rejected"
    assert client.post("/api/auth/login", json=BODY).status_code == 401
    with session_scope() as db:
        assert db.get(User, "new-user") is None
        assert db.get(SignupRequest, "new-user").password_hash is None


def test_signup_cannot_choose_privileges_and_is_rate_limited(client):
    assert client.post("/api/auth/signup", json=BODY | {"role": "owner"}).status_code == 422
    assert client.post("/api/auth/signup", json=BODY | {"username": "bad name"}).status_code == 422
    assert client.post("/api/auth/signup", json=BODY | {"password": "short"}).status_code == 422
    for i in range(registration.MAX_ATTEMPTS):
        assert client.post("/api/auth/signup", json=BODY | {"username": f"candidate-{i}"}).status_code == 202
    assert client.post("/api/auth/signup", json=BODY).status_code == 429
    # Even the first request with no existing users remains pending, never an owner.
    with session_scope() as db:
        assert db.get(User, "candidate-0") is None


def test_no_owner_configuration_disables_signup(client, monkeypatch):
    monkeypatch.setenv("SIGNUP_APPROVER_NAMES", "")
    get_settings.cache_clear()
    assert client.post("/api/auth/signup", json=BODY).status_code == 503
