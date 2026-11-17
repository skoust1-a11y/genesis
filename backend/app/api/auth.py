from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app import auth
from app.api.common import require_auth
from app.auth import registration
from app.config import get_settings
from app.db.session import session_scope

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class SignupBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(pattern=r"^[A-Za-z0-9_.@-]{1,64}$")
    password: str = Field(min_length=8, max_length=1024)


class SignupDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "reject"]


@router.post("/signup", status_code=202)
def signup(body: SignupBody, request: Request):
    registration.rate_limit(request.client.host if request.client else "unknown")
    if not registration.approvers():
        raise HTTPException(status_code=503, detail="Signup is unavailable until an account owner is configured")
    with session_scope() as db:
        registration.request_access(db, body.username, body.password)
    return {"status": "pending", "detail": "Signup request received. An owner must approve access before you can log in."}


@router.get("/signup/permissions")
def signup_permissions(actor: str = Depends(require_auth)):
    return {"can_approve": actor in registration.approvers()}


@router.get("/signup-requests")
def signup_requests(actor: str = Depends(registration.require_owner)):
    with session_scope() as db:
        return {"requests": registration.list_requests(db)}


@router.post("/signup-requests/{username}")
def review_signup(username: str, body: SignupDecision, actor: str = Depends(registration.require_owner)):
    with session_scope() as db:
        return registration.decide(db, username, body.decision, actor)


@router.post("/login")
def login(body: LoginBody):
    if auth.locked_out(body.username):
        raise HTTPException(status_code=429, detail="too many failed logins: try again in 15 minutes")
    with session_scope() as db:
        result = auth.login(db, body.username, body.password, get_settings())
    if result is None:
        raise HTTPException(status_code=401, detail="wrong username or password")
    token, expires_at = result
    return {"token": token, "username": body.username, "expires_at": expires_at}


@router.post("/logout")
def logout(authorization: str | None = Header(default=None)):
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        with session_scope() as db:
            auth.logout(db, token.strip())
    return {"ok": True}


@router.get("/me")
def me():
    return {"username": auth.authenticated_actor(), "auth_enabled": auth.enabled(get_settings())}
