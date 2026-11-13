from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_actor: ContextVar[str] = ContextVar("audit_actor", default="anonymous")
_request_id: ContextVar[str | None] = ContextVar("audit_request_id", default=None)


def current_actor() -> str:
    return _actor.get()


def current_request_id() -> str | None:
    return _request_id.get()


@contextmanager
def audit_context(actor: str, request_id: str | None = None) -> Iterator[None]:
    actor_token = _actor.set(actor)
    request_token = _request_id.set(request_id)
    try:
        yield
    finally:
        _actor.reset(actor_token)
        _request_id.reset(request_token)
