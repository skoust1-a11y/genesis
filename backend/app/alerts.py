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


def _telegram(settings: Settings, chat_id: str | None = None) -> Channel:
    target_chat = chat_id or settings.alert_telegram_chat_id

    def send(subject: str, body: str) -> None:
        response = httpx.post(
            f"https://api.telegram.org/bot{settings.alert_telegram_bot_token}/sendMessage",
            json={"chat_id": target_chat, "text": f"{subject}\n\n{body}"[:4000]},
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


def channels(settings: Settings, chat_id: str | None = None) -> dict[str, Channel]:
    out = {}
    target_chat = chat_id or settings.alert_telegram_chat_id
    if settings.alert_telegram_bot_token and target_chat:
        out["telegram"] = _telegram(settings, target_chat)
    if settings.alert_smtp_host and settings.alert_smtp_from and settings.alert_smtp_to:
        out["email"] = _email(settings)
    return out


def signal_chat_id(settings: Settings) -> str | None:
    return settings.signals_telegram_chat_id or settings.alert_telegram_chat_id


def signal_channels(settings: Settings) -> dict[str, Channel]:
    return channels(settings, chat_id=signal_chat_id(settings))


def send(settings: Settings, subject: str, body: str, chat_id: str | None = None) -> list[str]:
    """Deliver to every configured channel; returns the channels that succeeded."""
    log.warning("ALERT %s: %s", subject, body)
    sent = []
    for name, channel in channels(settings, chat_id=chat_id).items():
        try:
            channel(subject, body)
            sent.append(name)
        except Exception as exc:
            log.error("alert via %s failed: %s: %s", name, type(exc).__name__, exc)
    return sent


def send_signal(settings: Settings, subject: str, body: str) -> list[str]:
    """Deliver a trade signal to the signals channel (or fallback alert channel)."""
    return send(settings, subject, body, chat_id=signal_chat_id(settings))


def send_telegram_signal(settings: Settings, text: str, reply_to_message_id: int | None = None) -> int | None:
    """Delivers a formatted message to Telegram signals channel with automated retries and fallback."""
    import time
    target_chat = signal_chat_id(settings)
    if not (settings.alert_telegram_bot_token and target_chat):
        return None

    url = f"https://api.telegram.org/bot{settings.alert_telegram_bot_token}/sendMessage"
    payload: dict[str, object] = {"chat_id": target_chat, "text": text[:4000]}
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id

    # 3-attempt retry loop with backoff
    for attempt in range(1, 4):
        try:
            # Try Markdown first
            payload["parse_mode"] = "Markdown"
            res = httpx.post(url, json=payload, timeout=15)
            if res.status_code == 200:
                return res.json().get("result", {}).get("message_id")

            # If Markdown parsing failed (400 Bad Request), retry without Markdown
            if res.status_code == 400:
                payload.pop("parse_mode", None)
                res_plain = httpx.post(url, json=payload, timeout=15)
                if res_plain.status_code == 200:
                    return res_plain.json().get("result", {}).get("message_id")

            log.warning("Telegram send attempt %d returned HTTP %s: %s", attempt, res.status_code, res.text)
        except Exception as exc:
            log.warning("Telegram send attempt %d failed: %s: %s", attempt, type(exc).__name__, exc)

        if attempt < 3:
            time.sleep(1.5 * attempt)

    log.error("send_telegram_signal completely failed after 3 attempts")
    return None


def send_photo(settings: Settings, caption: str, png: bytes, chat_id: str | None = None) -> int | None:
    """A chart to Telegram; returns message_id if sent successfully, None otherwise."""
    target_chat = chat_id or settings.alert_telegram_chat_id
    if not (settings.alert_telegram_bot_token and target_chat):
        return None
    try:
        response = httpx.post(
            f"https://api.telegram.org/bot{settings.alert_telegram_bot_token}/sendPhoto",
            data={"chat_id": target_chat, "caption": caption[:1024]},
            files={"photo": ("chart.png", png, "image/png")},
            timeout=30,
        )
        if hasattr(response, "raise_for_status"):
            response.raise_for_status()
        status = getattr(response, "status_code", 200)
        if status == 200:
            result = response.json().get("result", {}) if hasattr(response, "json") else {}
            msg_id = result.get("message_id") if isinstance(result, dict) else None
            return msg_id if msg_id is not None else True
    except Exception as exc:
        log.error("photo via telegram failed: %s: %s", type(exc).__name__, exc)
    return None


def send_signal_photo(settings: Settings, caption: str, png: bytes) -> int | None:
    """Send a trade signal chart to the dedicated signals channel, returning message_id."""
    return send_photo(settings, caption, png, chat_id=signal_chat_id(settings))


def send_video(settings: Settings, caption: str, video: bytes, filename: str = "video.mp4", chat_id: str | None = None) -> bool:
    """A video animation / walk-through to Telegram with strategy name in caption."""
    target_chat = chat_id or settings.alert_telegram_chat_id
    if not (settings.alert_telegram_bot_token and target_chat):
        return False
    try:
        response = httpx.post(
            f"https://api.telegram.org/bot{settings.alert_telegram_bot_token}/sendVideo",
            data={"chat_id": target_chat, "caption": caption[:1024]},
            files={"video": (filename, video, "video/mp4")},
            timeout=60,
        )
        response.raise_for_status()
        return True
    except Exception as exc:
        log.error("video via telegram failed: %s: %s", type(exc).__name__, exc)
        return False


def send_signal_video(settings: Settings, caption: str, video: bytes, filename: str = "video.mp4") -> bool:
    """Send a strategy trade video to the dedicated signals channel."""
    return send_video(settings, caption, video, filename=filename, chat_id=signal_chat_id(settings))


def record(
    title: str,
    message: str,
    category: str = "alert",
    level: str = "info",
    deployment_id: str | None = None,
    channels: list[str] | None = None,
    details: dict | None = None,
    session: Any = None,
) -> str | None:
    """Save an alert/notification to the database for historical audit and reassessment.

    Never raises; failures are logged so calling processes are never interrupted.
    """
    try:
        from app.db.models import Notification

        def _insert(s: Any) -> str:
            notif = Notification(
                title=title,
                message=message,
                category=category,
                level=level,
                deployment_id=deployment_id,
                channels=channels or [],
                details=details or {},
            )
            s.add(notif)
            s.flush()
            return notif.id

        if session is not None:
            return _insert(session)

        from app.db.session import session_scope

        with session_scope() as s:
            return _insert(s)
    except Exception as exc:
        log.error("saving notification failed: %s: %s", type(exc).__name__, exc)
        return None


def notify(
    settings: Settings,
    event: str,
    deployment_id: str | None,
    detail: str,
    session: Any = None,
) -> list[str]:
    """`event`: halted | paused | resumed | protective_stop | reconcile_mismatch | heartbeat_missed | crashes |
    floor_raised | floor_top_up_needed | withdrawal_due."""
    try:
        target = f"deployment {deployment_id}" if deployment_id else "trade crew"
        subject = f"[genesis] {event}: {target}"
        sent = send(settings, subject, detail)
        level = (
            "critical"
            if event in {"crashes", "protective_stop", "halted"}
            else "warning"
            if event in {"reconcile_mismatch", "heartbeat_missed", "paused", "floor_top_up_needed"}
            else "info"
        )
        record(
            title=subject,
            message=detail,
            category="live",
            level=level,
            deployment_id=deployment_id,
            channels=sent,
            details={"event": event},
            session=session,
        )
        return sent
    except Exception as exc:  # never let alerting break the caller
        log.error("alert failed: %s: %s", type(exc).__name__, exc)
        return []
