"""Headline ingestion: RSS/Atom feeds and NSE corporate announcements. Headlines only, never bodies.

Headlines are untrusted third-party text. They are sanitized here (tags, control characters and
length) and rendered to the LLM as quoted data by `app.market.context`; nothing in them is ever
executed or followed.
"""

from __future__ import annotations

import html
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

MAX_TITLE_CHARS = 200
IST = ZoneInfo("Asia/Kolkata")
ATOM = "{http://www.w3.org/2005/Atom}"
_TAGS = re.compile(r"<[^>]*>")
_SPACE = re.compile(r"\s+")


def clean_headline(text: str | None, limit: int = MAX_TITLE_CHARS) -> str:
    """Plain single-line text: no markup, no control or format characters, bounded length."""
    text = html.unescape(_TAGS.sub(" ", text or ""))
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C" or ch in " \t\n")
    text = _SPACE.sub(" ", text).strip().replace("`", "'")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _parse_time(value: str | None) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text)  # RFC 822, used by RSS
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))  # RFC 3339, used by Atom
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)  # Indian feeds that omit the zone publish in IST
    return dt.astimezone(UTC)


def parse_feed(xml_text: str, source: str, fetched_at: datetime) -> list[dict]:
    """Items from an RSS 2.0 or Atom document. Items without a title, link or date are dropped."""
    if "<!ENTITY" in xml_text.upper():
        raise ValueError(f"{source}: feeds with entity declarations are refused")
    root = ET.fromstring(xml_text)
    if root.tag == f"{ATOM}feed":
        entries = (
            (
                e.findtext(f"{ATOM}title"),
                next((link.get("href") for link in e.findall(f"{ATOM}link") if link.get("rel", "alternate") == "alternate"), None),
                e.findtext(f"{ATOM}published") or e.findtext(f"{ATOM}updated"),
            )
            for e in root.findall(f"{ATOM}entry")
        )  # fmt: skip
    else:
        entries = (
            (i.findtext("title"), i.findtext("link") or i.findtext("guid"), i.findtext("pubDate"))
            for i in root.iter("item")
        )
    out = []
    for title, link, published in entries:
        title, published_at = clean_headline(title), _parse_time(published)
        link = (link or "").strip()
        if not (title and link and published_at) or not link.startswith(("http://", "https://")):
            continue
        out.append(
            {
                "source": source, "title": title, "url": link[:1000], "published_at": published_at,
                "fetched_at": fetched_at, "symbol": None,
            }
        )  # fmt: skip
    return out


def parse_nse_announcements(json_text: str, fetched_at: datetime) -> list[dict]:
    """Rows from NSE's corporate-announcements API (a JSON list, or {"data": [...]})."""
    data = json.loads(json_text)
    rows = data.get("data", []) if isinstance(data, dict) else data
    out = []
    for row in rows or []:
        symbol = clean_headline(row.get("symbol"), 32).upper()
        desc = clean_headline(row.get("desc") or row.get("attchmntText"))
        url = (row.get("attchmntFile") or "").strip()
        try:
            published_at = datetime.strptime((row.get("an_dt") or "").strip(), "%d-%b-%Y %H:%M:%S")
        except ValueError:
            continue
        if not (symbol and desc and url.startswith("https://")):
            continue
        out.append(
            {
                "source": "nse_announcements",
                "title": clean_headline(f"{symbol}: {desc}"),
                "url": url[:1000],
                "published_at": published_at.replace(tzinfo=IST).astimezone(UTC),
                "fetched_at": fetched_at,
                "symbol": symbol,
            }
        )
    return out
