from __future__ import annotations

import json
import re
import uuid

from app import auth
from app.audit.context import audit_context
from app.config import get_settings

_SAFE = re.compile(r"[^A-Za-z0-9_.:@\-]")
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


def _header(scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def _clean(value: str | None, limit: int) -> str | None:
    if not value:
        return None
    value = _SAFE.sub("", value.strip())[:limit]
    return value or None


def _needs_token(scope) -> bool:
    """With API_TOKENS set: every mutating request and every /api/live read."""
    method = scope.get("method", "GET")
    return method in MUTATING or (method != "OPTIONS" and scope.get("path", "").startswith("/api/live"))


async def _reject(send, request_id: str, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", b"Bearer"),
                (b"x-request-id", request_id.encode("latin-1")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class AuditContextMiddleware:
    """Pure ASGI middleware so the context vars reach sync endpoints run in the threadpool.

    A valid `Authorization: Bearer` token makes its name the actor (X-Actor is then ignored); an
    invalid one gets 401. When API_TOKENS is set, requests that need a token and lack one get 401.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = _clean(_header(scope, b"x-request-id"), 64) or uuid.uuid4().hex
        actor = _clean(_header(scope, b"x-actor"), 128) or "anonymous"
        settings = get_settings()
        name = None
        authorization = _header(scope, b"authorization")
        if authorization and scope.get("method") != "OPTIONS":
            scheme, _, token = authorization.partition(" ")
            name = auth.verify(token.strip(), settings) if scheme.lower() == "bearer" and token.strip() else None
            if name is None:
                await _reject(send, request_id, 401, "invalid API token")
                return
            actor = name
        elif settings.token_hashes and _needs_token(scope):
            await _reject(send, request_id, 401, "this API needs a token: send Authorization: Bearer <token>")
            return

        async def send_with_id(message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        marker = auth.set_authenticated(name)
        try:
            with audit_context(actor, request_id):
                await self.app(scope, receive, send_with_id)
        finally:
            auth.reset_authenticated(marker)
