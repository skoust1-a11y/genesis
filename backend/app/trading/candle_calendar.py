"""Offline calendar authority. Receipts and caller-created objects confer no authority."""
from __future__ import annotations

import hashlib
import json
import weakref
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

REGISTRY_PATH = Path(__file__).resolve().parents[3] / "reference_data/candle_integrity/approved_calendars.json"


def digest(domain, value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256((domain + "\0" + encoded).encode("utf-8")).hexdigest()


def _load_registry():
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


@dataclass(frozen=True, eq=False)
class ApprovedCalendar:
    calendar_id: str
    canonical: str
    sha256: str

    def content(self):
        return json.loads(self.canonical)


_issued = weakref.WeakKeyDictionary()
_id_bindings = {}


def _untrusted():
    from app.trading.candle_integrity import CandleIntegrityError
    return CandleIntegrityError("CI_CALENDAR_UNTRUSTED")


def require_approved_calendar(calendar):
    if not isinstance(calendar, ApprovedCalendar) or _issued.get(calendar) != (
        calendar.calendar_id, calendar.canonical, calendar.sha256
    ):
        raise _untrusted()
    if digest("candle-calendar-v1", calendar.content()) != calendar.sha256:
        raise _untrusted()
    return calendar


def resolve_approved_calendar(calendar_id: str) -> ApprovedCalendar:
    try:
        if not isinstance(calendar_id, str) or not calendar_id:
            raise ValueError
        registry = _load_registry()
        if set(registry) != {"schema_version", "calendars"} or registry["schema_version"] != "approved-candle-calendars-v1":
            raise ValueError
        matches = [x for x in registry["calendars"] if x["calendar_id"] == calendar_id]
        if len(matches) != 1:
            raise ValueError
        record = matches[0]
        keys = {"calendar_id", "schema_version", "exchange", "segment", "timezone", "coverage_start",
                "coverage_end", "sessions", "exceptional_openings", "closures", "exclusions", "sources",
                "approval", "sha256"}
        if set(record) != keys:
            raise ValueError
        content = {k: v for k, v in record.items() if k != "sha256"}
        if (content["schema_version"] != "candle-calendar-v1" or content["exchange"] != "NSE"
                or content["segment"] != "cash" or content["timezone"] != "Asia/Kolkata"):
            raise ValueError
        approval = content["approval"]
        if set(approval) != {"status", "verifier", "review_ref", "approved_on"} or approval["status"] != "approved":
            raise ValueError
        if not all(isinstance(v, str) and v for v in approval.values()):
            raise ValueError
        if date.fromisoformat(approval["approved_on"]).isoformat() != approval["approved_on"]:
            raise ValueError
        sources = content["sources"]
        if not sources:
            raise ValueError
        for source in sources:
            if set(source) != {"document_id", "url", "published_on", "evidence_sha256"}:
                raise ValueError
            if not all(isinstance(v, str) and v for v in source.values()):
                raise ValueError
            if date.fromisoformat(source["published_on"]).isoformat() != source["published_on"]:
                raise ValueError
            if len(source["evidence_sha256"]) != 64 or any(c not in "0123456789abcdef" for c in source["evidence_sha256"]):
                raise ValueError
        start, end = date.fromisoformat(content["coverage_start"]), date.fromisoformat(content["coverage_end"])
        if (start.isoformat() != content["coverage_start"] or end.isoformat() != content["coverage_end"]
                or start > end):
            raise ValueError
        sessions, closures = content["sessions"], content["closures"]
        exclusions = content["exclusions"]
        for entry in exclusions:
            if set(entry) != {"date", "reason", "evidence_ref"} or not all(isinstance(v, str) and v for v in entry.values()):
                raise ValueError
            if entry["evidence_ref"] not in {s["document_id"] for s in sources}:
                raise ValueError
        excluded = [x["date"] for x in exclusions]
        inventory = sessions + closures + excluded
        expected = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
        if sorted(inventory) != expected or sessions != sorted(set(sessions)) or closures != sorted(set(closures)):
            raise ValueError
        openings = content["exceptional_openings"]
        if openings != sorted(set(openings)) or not set(openings) <= set(sessions):
            raise ValueError
        if any(date.fromisoformat(d).weekday() >= 5 and d not in openings for d in sessions):
            raise ValueError
        if start <= date(2021, 2, 24) <= end and "2021-02-24" not in excluded:
            raise ValueError
        sha = digest("candle-calendar-v1", content)
        if sha != record["sha256"] or _id_bindings.get(calendar_id, sha) != sha:
            raise ValueError
        canonical = json.dumps(content, sort_keys=True, separators=(",", ":"))
    except (ValueError, TypeError, KeyError, OSError, AttributeError):
        raise _untrusted() from None
    calendar = ApprovedCalendar(calendar_id, canonical, sha)
    _id_bindings[calendar_id] = sha
    _issued[calendar] = (calendar_id, canonical, sha)
    return calendar
