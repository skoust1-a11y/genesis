"""Static bearer tokens for the API.

`API_TOKENS` holds `name:sha256(token)` pairs, so only hashes live in `.env`. A request with
`Authorization: Bearer <token>` is authenticated as `name`, which becomes the audit actor.
Create a token with `uv run python -m app.auth new-token NAME`.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from contextvars import ContextVar

from app.config import Settings

_authenticated: ContextVar[str | None] = ContextVar("authenticated", default=None)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def verify(token: str, settings: Settings) -> str | None:
    """The token's name, or None. Compares against every configured hash in constant time."""
    digest = hash_token(token)
    name = None
    for known, actor in settings.token_hashes.items():
        if hmac.compare_digest(digest, known):
            name = actor
    return name


def authenticated_actor() -> str | None:
    """The token name of the current request, if it carried a valid token."""
    return _authenticated.get()


def set_authenticated(name: str | None):
    return _authenticated.set(name)


def reset_authenticated(token) -> None:
    _authenticated.reset(token)
