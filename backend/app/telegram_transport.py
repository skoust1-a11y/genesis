"""S1-P4 Telegram transport: one sendMessage per call, a structured outcome, no retry and no fallback.

Outcomes:
  DELIVERED      HTTP 200, ok:true and an integer message_id > 0.
  NOT_DELIVERED  Proven non-delivery: rejected before any network call, or a well-formed Telegram
                 4xx refusal (ok:false, matching error_code). 429 carries retry_after.
  UNKNOWN        Everything else: any exception (ReadTimeout, ConnectTimeout, reset, ...), 5xx,
                 or a body that does not prove either outcome. Never assume an exception means unsent.

The bot token never leaves this module unredacted: outcomes, logs and httpx's request log are redacted.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

log = logging.getLogger("genesis.telegram")

DELIVERED, NOT_DELIVERED, UNKNOWN = "DELIVERED", "NOT_DELIVERED", "UNKNOWN"
MAX_TEXT = 4096
MOCK_CHAT_PREFIX = "mock:"
ARM_ENV = "GENESIS_TELEGRAM_ARMED"
_EXCERPT = 200
_TOKEN = re.compile(r"(bot)?\d{5,}:[A-Za-z0-9_-]{20,}")


def redact(text: object) -> str:
    return _TOKEN.sub("[REDACTED]", str(text))


class _RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg, record.args = redact(record.getMessage()), ()
        return True


# httpx logs every request URL at INFO, and the Telegram URL contains the bot token.
logging.getLogger("httpx").addFilter(_RedactFilter())


@dataclass(frozen=True)
class DeliveryOutcome:
    status: str
    http_status: int | None = None
    telegram_ok: bool | None = None
    message_id: int | None = None
    retry_after: int | None = None
    exc_class: str | None = None
    latency_ms: int | None = None
    detail: str | None = None


class Transport(Protocol):
    def send(self, chat_id: str, text: str) -> DeliveryOutcome: ...


def classify_response(status_code: int, content: bytes) -> DeliveryOutcome:
    try:
        body = json.loads(content)
    except (ValueError, TypeError):
        body = None
    ok = body.get("ok") if isinstance(body, dict) else None
    excerpt = redact(content[:_EXCERPT].decode("utf-8", "replace")) if content else None
    if status_code == 200:
        result = body.get("result") if isinstance(body, dict) else None
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if ok is True and type(message_id) is int and message_id > 0:
            return DeliveryOutcome(DELIVERED, 200, True, message_id)
        return DeliveryOutcome(UNKNOWN, 200, ok if isinstance(ok, bool) else None, detail=excerpt)
    if 400 <= status_code < 500 and ok is False and body.get("error_code") == status_code:
        detail = redact(str(body.get("description", ""))[:_EXCERPT])
        retry = None
        if status_code == 429:
            params = body.get("parameters")
            raw = params.get("retry_after") if isinstance(params, dict) else None
            retry = raw if type(raw) is int and raw >= 0 else 1
        return DeliveryOutcome(NOT_DELIVERED, status_code, False, retry_after=retry, detail=detail)
    return DeliveryOutcome(UNKNOWN, status_code, ok if isinstance(ok, bool) else None, detail=excerpt)


def classify_exception(exc: BaseException) -> DeliveryOutcome:
    return DeliveryOutcome(UNKNOWN, exc_class=type(exc).__name__, detail=redact(exc)[:_EXCERPT])


def delivery_armed() -> bool:
    # Read from the process environment, never .env, so the persistent setting alone cannot send.
    return os.environ.get(ARM_ENV) == "1"


def delivery_authorized(chat_id: str, *, client: httpx.Client | None = None) -> bool:
    """Allow mocks, or enabled delivery to the configured signals group from a process armed for it."""
    if not chat_id or str(chat_id).startswith(MOCK_CHAT_PREFIX):
        return False
    if client is not None and type(client._transport_for_url(httpx.URL("https://api.telegram.org"))) is httpx.MockTransport:
        return True
    if not delivery_armed():
        return False
    from app.config import get_settings

    settings = get_settings()
    group = settings.signals_telegram_chat_id or settings.alert_telegram_chat_id
    return bool(settings.telegram_delivery_enabled and group and str(group).startswith("-") and chat_id == group)


class TelegramTransport:
    """One audited sendMessage attempt to an authorized destination."""

    def __init__(self, token: str, *, timeout: float = 10.0, client: httpx.Client | None = None):
        self._token = token
        self._timeout = timeout
        self._client = client

    def send(self, chat_id: str, text: str) -> DeliveryOutcome:
        if not self._token or not chat_id or not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            return self._logged(DeliveryOutcome(NOT_DELIVERED, exc_class="PreSendValidation",
                                                detail="missing token or chat, or empty/oversized text"))
        if str(chat_id).startswith(MOCK_CHAT_PREFIX):
            return self._logged(DeliveryOutcome(NOT_DELIVERED, exc_class="PreSendValidation",
                                                detail="mock chat ids are never delivered"))
        if not delivery_authorized(chat_id, client=self._client):
            return self._logged(DeliveryOutcome(NOT_DELIVERED, exc_class="DeliveryDisabled",
                                                detail="real Telegram delivery is not approved"))
        client = self._client or httpx.Client()
        start = time.monotonic()
        try:
            response = client.post(f"https://api.telegram.org/bot{self._token}/sendMessage",
                                   json={"chat_id": chat_id, "text": text}, timeout=self._timeout)
            outcome = classify_response(response.status_code, response.content)
        except Exception as exc:  # noqa: BLE001 - every transport failure is an UNKNOWN outcome
            outcome = classify_exception(exc)
        finally:
            if self._client is None:
                client.close()
        latency = int((time.monotonic() - start) * 1000)
        return self._logged(DeliveryOutcome(**{**outcome.__dict__, "latency_ms": latency}))

    def _logged(self, outcome: DeliveryOutcome) -> DeliveryOutcome:
        if self._token:
            outcome = DeliveryOutcome(**{**outcome.__dict__,
                                         "detail": None if outcome.detail is None
                                         else redact(outcome.detail.replace(self._token, "[REDACTED]"))})
        log.info("telegram send %s http=%s ok=%s message_id=%s exc=%s latency_ms=%s", outcome.status,
                 outcome.http_status, outcome.telegram_ok, outcome.message_id, outcome.exc_class, outcome.latency_ms)
        return outcome
