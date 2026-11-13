"""Synthetic authority through the private loader only; never production approval."""
from datetime import date, timedelta

import pytest

from app.trading import candle_calendar as cc
from app.trading.candle_integrity import (
    CandleRequirements, validate_candle_dataset, derive_is_snapshot,
)

_records = {}


def calendar_record(days, *, exclusions=()):
    days = tuple(days)
    start, end = date.fromisoformat(days[0]), date.fromisoformat(days[-1])
    inventory = [(start + timedelta(days=i)).isoformat() for i in range((end-start).days + 1)]
    excluded = [dict(date=d, reason="synthetic exclusion", evidence_ref="SYNTHETIC-NOT-HISTORICAL") for d in exclusions]
    content = {
        "calendar_id": "TEST-ONLY-" + cc.digest("test-dates", [days, exclusions])[:24],
        "schema_version": "candle-calendar-v1", "exchange": "NSE", "segment": "cash",
        "timezone": "Asia/Kolkata", "coverage_start": days[0], "coverage_end": days[-1],
        "sessions": list(days), "exceptional_openings": [d for d in days if date.fromisoformat(d).weekday() >= 5],
        "closures": [d for d in inventory if d not in days and d not in exclusions], "exclusions": excluded,
        "sources": [{"document_id": "SYNTHETIC-NOT-HISTORICAL", "url": "https://example.invalid/test-only",
                     "published_on": "2026-01-01", "evidence_sha256": "0"*64}],
        "approval": {"status": "approved", "verifier": "PYTEST-ONLY", "review_ref": "SYNTHETIC-NOT-HISTORICAL",
                     "approved_on": "2026-01-01"},
    }
    return {**content, "sha256": cc.digest("candle-calendar-v1", content)}


def declaration(days, *, scope="development", volume_provenance="exchange_reported"):
    record = calendar_record(days)
    _records[record["calendar_id"]] = record
    return {"schema_version": "candle-dataset-declaration-v1", "scope": scope, "underlying": "NIFTY",
            "interval": "5m", "timestamp_semantics": "bar_open", "timezone": "Asia/Kolkata",
            "coverage_start": days[0], "coverage_end": days[-1], "volume_provenance": volume_provenance,
            "calendar_id": record["calendar_id"]}


@pytest.fixture
def synthetic_calendar(monkeypatch):
    _records.clear()
    monkeypatch.setattr(cc, "_load_registry", lambda: {"schema_version": "approved-candle-calendars-v1",
                                                    "calendars": list(_records.values())})
    yield
    _records.clear()


def snapshot(rows, days, *, scope="development", needs_vwap=False):
    dec = declaration(days, scope=scope)
    return validate_candle_dataset(rows, declaration=dec,
        calendar=cc.resolve_approved_calendar(dec["calendar_id"]),
        requirements=CandleRequirements("NIFTY", needs_vwap), trading_dates=days)


def selection_view(rows, days, fold):
    return derive_is_snapshot(snapshot(rows, days), fold=fold)
