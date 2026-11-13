"""Delivery of paper notifications; never changes paper trades or places orders."""
from pathlib import Path

from app.config import Settings
from app.db import crew_journal as cj
from app.db import crew_outbox as ob


def dispatch(db_path: Path | str, *, lease: cj.Lease, settings: Settings, clock) -> list[dict]:
    """Deliver pending paper messages through the existing audited outbox lifecycle."""
    from app.telegram_transport import TelegramTransport, delivery_authorized

    chat_id = settings.signals_telegram_chat_id or settings.alert_telegram_chat_id
    if not settings.telegram_delivery_enabled or not settings.alert_telegram_bot_token or not delivery_authorized(chat_id or ""):
        return []
    transport = TelegramTransport(settings.alert_telegram_bot_token)
    results = []
    for row in ob.list_pending_publications(db_path, now=clock()):
        if row["kind"] == "OPS" and row["purpose"].startswith("paper|") and row["chat_id"] == chat_id:
            results.append(ob.deliver_publication(db_path, row["publication_id"], transport=transport,
                                                  lease=lease, clock=clock))
    return results
