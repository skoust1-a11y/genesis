"""Scheduled market events (RBI policy, Budget, FOMC, results) for entry blackouts and LLM context.

Sources:
- YAML files in `data/events/` (macro calendar, maintained by hand):
      source: manual
      events:
        - {date: 2024-02-08, kind: rbi_policy, title: RBI MPC decision}
- NSE "Board Meetings" CSV exports (corporate filings), stored per stock symbol.
"""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from pathlib import Path

import yaml

from app.config import Settings, get_settings
from app.market.feed import EventLookup
from app.market.nse import parse_date
from app.market.store import open_market_store
from app.strategies.rules import BLACKOUT_MAX_DAYS_AFTER, BLACKOUT_MAX_DAYS_BEFORE, EVENT_KINDS


def _shift(day: str, days: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()


def load_event_yaml(path: Path) -> list[dict]:
    doc = yaml.safe_load(path.read_text()) or {}
    source = str(doc.get("source") or path.stem)
    out = []
    for i, e in enumerate(doc.get("events") or []):
        kind = str(e.get("kind", ""))
        if kind not in EVENT_KINDS:
            raise ValueError(f"{path.name} event {i}: kind must be one of {', '.join(EVENT_KINDS)}")
        out.append(
            {
                "date": parse_date(str(e["date"])),
                "kind": kind,
                "title": str(e["title"]),
                "underlying": (str(e["underlying"]).upper() if e.get("underlying") else None),
                "source": source,
            }
        )
    return out


def parse_board_meetings(text: str) -> list[dict]:
    """Rows from an NSE board-meetings CSV export: results meetings become `results` events."""
    rows = list(csv.DictReader(io.StringIO(text.lstrip("\ufeff"))))
    if not rows:
        return []
    cols = {c: c.strip().lower() for c in rows[0] if c}

    def col(word: str) -> str | None:
        return next((c for c, low in cols.items() if word in low), None)

    symbol_col, purpose_col = col("symbol"), col("purpose")
    date_col = next((c for c, low in cols.items() if "date" in low and "meeting" in low), None) or col("date")
    if not (symbol_col and date_col):
        raise ValueError("expected SYMBOL and a board meeting date column")
    out = []
    for row in rows:
        try:
            day = parse_date(row.get(date_col, ""))
        except ValueError:
            continue
        symbol = (row.get(symbol_col) or "").strip().upper()
        purpose = (row.get(purpose_col) or "").strip() if purpose_col else ""
        if not symbol:
            continue
        out.append(
            {
                "date": day,
                "kind": "results" if "result" in purpose.lower() else "board_meeting",
                "title": f"{symbol} board meeting: {purpose or 'unspecified'}"[:300],
                "underlying": symbol,
                "source": "nse_board_meetings",
            }
        )
    return out


def event_dict(e: dict, as_of: str) -> dict:
    return {
        "date": e["date"],
        "kind": e["kind"],
        "title": e["title"],
        "underlying": e["underlying"],
        "days_from": (date.fromisoformat(e["date"]) - date.fromisoformat(as_of)).days,
    }


def event_lookup(settings: Settings | None = None) -> EventLookup | None:
    """A lookup over the whole calendar, or None when no calendar is loaded.

    Events are scheduled in advance, so returning future dates near `as_of` is not lookahead.
    """
    settings = settings or get_settings()
    store = open_market_store(settings)
    try:
        if not store.has_events():
            return None
        rows = [
            {"date": e.date, "kind": e.kind, "title": e.title, "underlying": e.underlying}
            for e in store.events_between("0000-01-01", "9999-12-31")
        ]
    finally:
        store.close()
    before = max(settings.event_lookback_days, BLACKOUT_MAX_DAYS_AFTER)
    ahead = max(settings.event_lookahead_days, BLACKOUT_MAX_DAYS_BEFORE)

    def lookup(underlying: str, as_of: str) -> list[dict]:
        start, end = _shift(as_of, -before), _shift(as_of, ahead)
        return [
            event_dict(e, as_of)
            for e in rows
            if start <= e["date"] <= end and (e["underlying"] is None or e["underlying"] == underlying.upper())
        ]

    return lookup
