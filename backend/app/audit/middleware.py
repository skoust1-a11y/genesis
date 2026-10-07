from __future__ import annotations

import re
import uuid

from app.audit.context import audit_context

_SAFE = re.compile(r"[^A-Za-z0-9_.:@\-]")


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


class AuditContextMiddleware:
    """Pure ASGI middleware so the context vars reach sync endpoints run in the threadpool."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = _clean(_header(scope, b"x-request-id"), 64) or uuid.uuid4().hex
        actor = _clean(_header(scope, b"x-actor"), 128) or "anonymous"

        async def send_with_id(message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        with audit_context(actor, request_id):
            await self.app(scope, receive, send_with_id)
