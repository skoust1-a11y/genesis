"""Alerts to a human when the Trade Crew stops trading on its own or stops running.

Sent to Telegram (ALERT_TELEGRAM_BOT_TOKEN and ALERT_TELEGRAM_CHAT_ID) and/or email over SMTP
(ALERT_SMTP_HOST, ALERT_SMTP_FROM, ALERT_SMTP_TO, optional user and password). With neither
configured, alerts are only logged. A failed delivery is logged and never raises: an alert must
not block trading or a halt.
"""

from __future__ import annotations

import logging
import smtplib
from collections.abc import Callable
from email.message import EmailMessage

import httpx

from app.config import Settings

log = logging.getLogger("genesis.alerts")
Channel = Callable[[str, str], None]


def _telegram(settings: Settings) -> Channel:
    def send(subject: str, body: str) -> None:
        response = httpx.post(
            f"https://api.telegram.org/bot{settings.alert_telegram_bot_token}/sendMessage",
            json={"chat_id": settings.alert_telegram_chat_id, "text": f"{subject}\n\n{body}"[:4000]},
            timeout=10,
        )
        response.raise_for_status()

    return send


def _email(settings: Settings) -> Channel:
    def send(subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, settings.alert_smtp_from, settings.alert_smtp_to
        msg.set_content(body)
        with smtplib.SMTP(settings.alert_smtp_host, settings.alert_smtp_port, timeout=10) as smtp:
            smtp.starttls()
            if settings.alert_smtp_user:
                smtp.login(settings.alert_smtp_user, settings.alert_smtp_password or "")
            smtp.send_message(msg)

    return send


def channels(settings: Settings) -> dict[str, Channel]:
    out = {}
    if settings.alert_telegram_bot_token and settings.alert_telegram_chat_id:
        out["telegram"] = _telegram(settings)
    if settings.alert_smtp_host and settings.alert_smtp_from and settings.alert_smtp_to:
        out["email"] = _email(settings)
    return out


def send(settings: Settings, subject: str, body: str) -> list[str]:
    """Deliver to every configured channel; returns the channels that succeeded."""
    log.warning("ALERT %s: %s", subject, body)
    sent = []
    for name, channel in channels(settings).items():
        try:
            channel(subject, body)
            sent.append(name)
        except Exception as exc:
            log.error("alert via %s failed: %s: %s", name, type(exc).__name__, exc)
    return sent


def send_photo(settings: Settings, caption: str, png: bytes) -> bool:
    """A chart to Telegram (email gets only the text alert); like `send`, a failure is logged, never raised."""
    if not (settings.alert_telegram_bot_token and settings.alert_telegram_chat_id):
        return False
    try:
        response = httpx.post(
            f"https://api.telegram.org/bot{settings.alert_telegram_bot_token}/sendPhoto",
            data={"chat_id": settings.alert_telegram_chat_id, "caption": caption[:1024]},
            files={"photo": ("chart.png", png, "image/png")},
            timeout=30,
        )
        response.raise_for_status()
        return True
    except Exception as exc:
        log.error("photo via telegram failed: %s: %s", type(exc).__name__, exc)
        return False


def notify(settings: Settings, event: str, deployment_id: str | None, detail: str) -> list[str]:
    """`event`: halted | paused | resumed | protective_stop | reconcile_mismatch | heartbeat_missed | crashes |
    floor_raised | floor_top_up_needed | withdrawal_due."""
    try:
        target = f"deployment {deployment_id}" if deployment_id else "trade crew"
        return send(settings, f"[genesis] {event}: {target}", detail)
    except Exception as exc:  # never let alerting break the caller
        log.error("alert failed: %s: %s", type(exc).__name__, exc)
        return []
