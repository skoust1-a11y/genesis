from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from fastapi import HTTPException

from app import auth
from app.config import get_settings

T = TypeVar("T")


def or_404(fn: Callable[..., T], *args, **kwargs) -> T:
    try:
        return fn(*args, **kwargs)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"not found: {exc.args[0] if exc.args else ''}") from exc


def or_400(fn: Callable[..., T], *args, **kwargs) -> T:
    try:
        return or_404(fn, *args, **kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def ensure_authenticated() -> str:
    """The caller's token name. 403 when no API_TOKENS are configured, 401 without a valid token.
    Applies even when API_TOKENS is empty: broker credentials and live trading always need a token."""
    if not get_settings().token_hashes:
        raise HTTPException(
            status_code=403,
            detail="this action needs API authentication: set API_TOKENS (uv run python -m app.auth new-token NAME)",
        )
    name = auth.authenticated_actor()
    if name is None:
        raise HTTPException(status_code=401, detail="this action needs a token: send Authorization: Bearer <token>")
    return name


def require_auth() -> str:
    """FastAPI dependency form of `ensure_authenticated`."""
    return ensure_authenticated()
