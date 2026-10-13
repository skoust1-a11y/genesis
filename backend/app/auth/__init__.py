"""API authentication: static bearer tokens and username/password logins.

`API_TOKENS` holds `name:sha256(token)` pairs, so only hashes live in `.env`. Create one with
`uv run python -m app.auth new-token NAME`.

Users (`uv run python -m app.auth new-user NAME`) log in at `POST /api/auth/login` and get a session
token that expires after `SESSION_HOURS`. Either kind is sent as `Authorization: Bearer <token>`, and
its name becomes the audit actor.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from contextvars import ContextVar
from datetime import datetime, timedelta
from functools import lru_cache

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import User, UserSession
from app.db.session import engine_for
from app.db.types import utcnow

USERNAME = re.compile(r"[A-Za-z0-9_.@\-]{1,64}")
MIN_PASSWORD_LENGTH = 8
MAX_FAILURES, FAILURE_WINDOW_S = 5, 15 * 60
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}

_authenticated: ContextVar[str | None] = ContextVar("authenticated", default=None)
# ponytail: per-process memory, so it resets on restart and is per worker; move to the database if the API scales out.
_failures: dict[str, list[float]] = {}


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    return f"scrypt${salt.hex()}${hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT).hex()}"


def check_password(password: str, stored: str) -> bool:
    _, salt, digest = stored.split("$")
    return hmac.compare_digest(hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), **_SCRYPT).hex(), digest)


@lru_cache
def _dummy_hash() -> str:
    return hash_password(new_token())


def _reader(settings: Settings) -> Session:
    # Without BEGIN IMMEDIATE, so a long research write does not stall every authenticated request.
    return Session(engine_for(settings.database_url, immediate=False))


def users_exist(settings: Settings) -> bool:
    with _reader(settings) as s:
        return s.scalar(select(User.username).limit(1)) is not None


def enabled(settings: Settings) -> bool:
    """True once any API token or user exists; protected routes then need a valid token."""
    return bool(settings.token_hashes) or users_exist(settings)


def verify(token: str, settings: Settings) -> str | None:
    """The token's name, or None. Static tokens are compared in constant time; otherwise an unexpired login."""
    digest = hash_token(token)
    name = None
    for known, actor in settings.token_hashes.items():
        if hmac.compare_digest(digest, known):
            name = actor
    if name is not None:
        return name
    with _reader(settings) as s:
        return s.scalar(
            select(UserSession.username).where(UserSession.token_sha256 == digest, UserSession.expires_at > utcnow())
        )


def set_password(session: Session, username: str, password: str) -> None:
    """Create the user or reset their password; a reset ends every session they have."""
    if not USERNAME.fullmatch(username):
        raise ValueError("username may only use letters, digits and _ . @ - (at most 64)")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password needs at least {MIN_PASSWORD_LENGTH} characters")
    user = session.get(User, username)
    if user is None:
        session.add(User(username=username, password_hash=hash_password(password)))
        return
    user.password_hash = hash_password(password)
    session.execute(delete(UserSession).where(UserSession.username == username))


def locked_out(username: str) -> bool:
    now = time.monotonic()
    recent = [t for t in _failures.get(username, []) if now - t < FAILURE_WINDOW_S]
    _failures[username] = recent
    return len(recent) >= MAX_FAILURES


def login(session: Session, username: str, password: str, settings: Settings) -> tuple[str, datetime] | None:
    """A new session token and its expiry, or None (and a recorded failure) for a wrong username or password."""
    user = session.get(User, username)
    # Hash even for an unknown user, so the response time does not reveal which usernames exist.
    ok = check_password(password, user.password_hash if user else _dummy_hash())
    if user is None or not ok:
        _failures.setdefault(username, []).append(time.monotonic())
        return None
    _failures.pop(username, None)
    now = utcnow()
    session.execute(delete(UserSession).where(UserSession.expires_at <= now))
    token = new_token()
    expires = now + timedelta(hours=settings.session_hours)
    session.add(UserSession(token_sha256=hash_token(token), username=username, expires_at=expires))
    return token, expires


def logout(session: Session, token: str) -> None:
    session.execute(delete(UserSession).where(UserSession.token_sha256 == hash_token(token)))


def authenticated_actor() -> str | None:
    """The token name of the current request, if it carried a valid token."""
    return _authenticated.get()


def set_authenticated(name: str | None):
    return _authenticated.set(name)


def reset_authenticated(token) -> None:
    _authenticated.reset(token)
