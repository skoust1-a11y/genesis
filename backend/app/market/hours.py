"""The NSE trading session: weekdays from MARKET_OPEN to MARKET_CLOSE IST, except listed holidays.

Holidays come from `data/holidays/nse.yaml` (copy them from NSE's annual holiday circular). A
date missing from the file is treated as a trading day; on an unlisted holiday the broker feed's
quotes go stale and the stale-snapshot check stops the tick instead.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path

import yaml

from app.config import Settings
from app.market.feed import IST


def holidays_path(settings: Settings) -> Path:
    p = Path(settings.data_dir) / "holidays" / "nse.yaml"
    if not p.exists():
        fallback = Path(__file__).resolve().parents[2] / "data" / "holidays" / "nse.yaml"
        if fallback.exists():
            return fallback
    return p


@lru_cache
def _load(path: str, mtime: float) -> dict[str, str]:
    doc = yaml.safe_load(Path(path).read_text()) or {}
    return {str(h["date"]): str(h.get("name", "holiday")) for h in doc.get("holidays") or []}


def holidays(settings: Settings) -> dict[str, str]:
    path = holidays_path(settings)
    if not path.exists():
        return {}
    return _load(str(path), path.stat().st_mtime)


def trading_days_between(start: str, end: str, settings: Settings) -> int:
    """Weekdays after `start` up to and including `end` that are not NSE holidays (0 if `end` <= `start`)."""
    off = holidays(settings)
    day, last, count = date.fromisoformat(start), date.fromisoformat(end), 0
    while day < last:
        day += timedelta(days=1)
        if day.weekday() < 5 and day.isoformat() not in off:
            count += 1
    return count


def _parse(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def market_status(now: datetime, settings: Settings) -> tuple[bool, str]:
    """(open, reason). `now` may be in any timezone."""
    local = now.astimezone(IST)
    day = local.date().isoformat()
    if local.weekday() >= 5:
        return False, f"market closed: {day} is a weekend"
    name = holidays(settings).get(day)
    if name:
        return False, f"market closed: {day} is an NSE holiday ({name})"
    start, end = _parse(settings.market_open), _parse(settings.market_close)
    if not start <= local.time() < end:
        return False, f"market closed: {local:%H:%M} IST is outside {settings.market_open}-{settings.market_close}"
    return True, "market open"
