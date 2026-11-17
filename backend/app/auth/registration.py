"""Signup is a pending request. Only explicitly configured owners can grant access."""
import time
from collections import defaultdict

from fastapi import Depends, HTTPException
from sqlalchemy import select

from app import auth
from app.api.common import require_auth
from app.audit import record_event
from app.config import get_settings
from app.db.models.core import SignupRequest, User
from app.db.types import utcnow

_attempts = defaultdict(list)
WINDOW_SECONDS = 900
MAX_ATTEMPTS = 5


def approvers():
    return {n.strip() for n in get_settings().signup_approver_names.split(",") if n.strip()}


def require_owner(actor: str = Depends(require_auth)):
    if actor not in approvers():
        raise HTTPException(status_code=403, detail="Only an account owner can review signup requests")
    return actor


def rate_limit(address):
    now = time.monotonic()
    # Expire old addresses too, so arbitrary visitors cannot grow this map indefinitely.
    for key in list(_attempts):
        recent = [t for t in _attempts[key] if now - t < WINDOW_SECONDS]
        if recent:
            _attempts[key] = recent
        else:
            del _attempts[key]
    if len(_attempts[address]) >= MAX_ATTEMPTS:
        raise HTTPException(status_code=429, detail="Too many signup attempts. Try again in 15 minutes")
    _attempts[address].append(now)


def request_access(db, username, password):
    # Repeated requests cannot replace a pending password or reset an existing account.
    if db.get(User, username) is not None or db.get(SignupRequest, username) is not None:
        return
    db.add(SignupRequest(username=username, password_hash=auth.hash_password(password), status="pending"))
    record_event(db, "account.signup_requested", "signup_request", username, {"status": "pending"})


def list_requests(db):
    return [{"username": r.username, "status": r.status, "created_at": r.created_at,
             "decided_at": r.decided_at, "decided_by": r.decided_by}
            for r in db.scalars(select(SignupRequest).order_by(SignupRequest.created_at.desc()))]


def decide(db, username, decision, actor):
    row = db.get(SignupRequest, username)
    if row is None:
        raise HTTPException(status_code=404, detail="Signup request not found")
    if row.status != "pending":
        raise HTTPException(status_code=409, detail="This signup request has already been reviewed")
    if decision == "approve":
        if db.get(User, username) is not None or not row.password_hash:
            raise HTTPException(status_code=409, detail="Account already exists or signup credentials are unavailable")
        db.add(User(username=username, password_hash=row.password_hash))
    row.status = "approved" if decision == "approve" else "rejected"
    row.password_hash = None
    row.decided_at, row.decided_by = utcnow(), actor
    record_event(db, "account.signup_" + row.status, "signup_request", username, {"status": row.status, "decided_by": actor})
    return {"username": username, "status": row.status}
