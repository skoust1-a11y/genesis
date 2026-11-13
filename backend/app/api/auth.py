from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from app import auth
from app.config import get_settings
from app.db.session import session_scope

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


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
