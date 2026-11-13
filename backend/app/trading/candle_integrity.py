"""Strict candle certification, detached snapshots, and non-certified selection views.

Canonical OHLCV decimals use shortest round-trip numeric strings without exponents.
Only required fields enter UTC canonical rows; execution uses their finite float values.
"""
from __future__ import annotations

import json
import math
import numbers
import re
import weakref
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from zoneinfo import ZoneInfo

import pandas as pd
from pydantic import BaseModel, ConfigDict
from typing import Literal

from app.trading.candle_calendar import digest, require_approved_calendar, resolve_approved_calendar

IST = ZoneInfo("Asia/Kolkata")
CODES = {
    "CI_INPUT_MODE_INVALID": "Invalid execution input mode",
    "CI_INPUT_MODE_AMBIGUOUS": "Ambiguous execution input mode",
    "CI_DECLARATION_REQUIRED": "Dataset declaration is required",
    "CI_DECLARATION_INVALID": "Invalid dataset declaration",
    "CI_DECLARATION_MISMATCH": "Dataset declaration does not match execution",
    "CI_CALENDAR_UNTRUSTED": "Calendar authority is not trusted",
    "CI_CALENDAR_COVERAGE": "Calendar coverage is insufficient",
    "CI_TRADING_DATES_MISMATCH": "Trading dates do not match calendar",
    "CI_DATASET_EMPTY": "Candle dataset is empty",
    "CI_ROW_INVALID": "Invalid candle row",
    "CI_REQUIRED_FIELD_MISSING": "Required candle field is missing",
    "CI_ALIAS_CONFLICT": "Conflicting candle aliases",
    "CI_TIMESTAMP_INVALID": "Invalid certified candle timestamp",
    "CI_NUMERIC_INVALID": "Invalid candle numeric value",
    "CI_PRICE_NONPOSITIVE": "Candle price must be positive",
    "CI_OHLC_INCONSISTENT": "Inconsistent candle OHLC bounds",
    "CI_VOLUME_NEGATIVE": "Candle volume must be nonnegative",
    "CI_VOLUME_PROVENANCE_INCOMPATIBLE": "Volume provenance is incompatible",
    "CI_SESSION_VOLUME_NONPOSITIVE": "Session volume must be positive",
    "CI_UNDERLYING_MISMATCH": "Candle underlying does not match",
    "CI_DUPLICATE_TIMESTAMP": "Duplicate candle timestamp",
    "CI_NON_MONOTONIC_TIMESTAMPS": "Candle timestamps are not chronological",
    "CI_DATE_OUT_OF_COVERAGE": "Candle date is outside coverage",
    "CI_EXCLUDED_SESSION": "Candle belongs to an excluded session",
    "CI_NON_TRADING_SESSION": "Candle belongs to a nontrading session",
    "CI_BAR_GRID_INVALID": "Candle is outside the session grid",
    "CI_SESSION_MISSING": "A required session is missing",
    "CI_SESSION_INCOMPLETE": "A required session is incomplete",
    "CI_SNAPSHOT_REQUIRED": "An issued selection view is required",
    "CI_SNAPSHOT_INVALID": "Snapshot issuance or content is invalid",
    "CI_SNAPSHOT_SCOPE_MISMATCH": "Snapshot scope does not match execution",
}
FIELDS = ("timestamp", "open", "high", "low", "close", "volume", "underlying")


class CandleIntegrityError(ValueError):
    def __init__(self, code, *, row_index=None, field=None):
        self.code = code
        self.context = {}
        if type(row_index) is int and row_index >= 0:
            self.context["row_index"] = row_index
        if field in FIELDS:
            self.context["field"] = field
        super().__init__(CODES[code])

    def to_dict(self):
        return {"code": self.code, "message": CODES[self.code], "context": dict(self.context)}


class DatasetDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["candle-dataset-declaration-v1"]
    scope: Literal["full", "development"]
    underlying: str
    interval: Literal["5m"]
    timestamp_semantics: Literal["bar_open"]
    timezone: Literal["Asia/Kolkata"]
    coverage_start: str
    coverage_end: str
    volume_provenance: Literal["exchange_reported", "synthetic", "unavailable"]
    calendar_id: str


def declaration_value(value):
    if value is None:
        raise CandleIntegrityError("CI_DECLARATION_REQUIRED")
    try:
        result = DatasetDeclaration.model_validate(value)
        for day in (result.coverage_start, result.coverage_end):
            if date.fromisoformat(day).isoformat() != day:
                raise ValueError
        if (result.coverage_start > result.coverage_end or not result.underlying
                or result.underlying != result.underlying.upper() or not result.calendar_id):
            raise ValueError
        return result
    except ValueError:
        raise CandleIntegrityError("CI_DECLARATION_INVALID") from None


@dataclass(frozen=True)
class CandleRequirements:
    underlying: str
    needs_vwap: bool = False


def requirements_for(spec):
    rules = spec.rules_v2
    payload = rules.model_dump(mode="json") if rules else {}
    indicators = payload.get("indicators", [])
    entries = payload.get("intraday_entry", [])
    needs = any(x.get("name") == "vwap" for x in indicators) or any(x.get("kind") == "vwap_relation" for x in entries)
    return CandleRequirements(rules.instrument.underlying, needs)


def strict_timestamp(value, index=None):
    try:
        if isinstance(value, str):
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})", value):
                raise ValueError
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if not isinstance(value, datetime) or value is pd.NaT or value.utcoffset() is None:
            raise ValueError
        if value.second or value.microsecond or getattr(value, "nanosecond", 0):
            raise ValueError
        utc = value.astimezone(timezone.utc)
        if utc.second or utc.microsecond:
            raise ValueError
        return utc.isoformat(timespec="seconds").replace("+00:00", "Z")
    except (ValueError, TypeError, OverflowError):
        raise CandleIntegrityError("CI_TIMESTAMP_INVALID", row_index=index, field="timestamp") from None


def decimal_value(value, index, field):
    if isinstance(value, (bool, str)) or not isinstance(value, numbers.Real):
        raise CandleIntegrityError("CI_NUMERIC_INVALID", row_index=index, field=field)
    try:
        if not math.isfinite(float(value)):
            raise ValueError
        dec = Decimal(str(value))
        if not dec.is_finite() or (dec != 0 and float(dec) == 0):
            raise ValueError
        encoded = format(dec, "f")
        if "." in encoded:
            encoded = encoded.rstrip("0").rstrip(".")
        return "0" if dec == 0 else encoded
    except (ValueError, OverflowError, InvalidOperation):
        raise CandleIntegrityError("CI_NUMERIC_INVALID", row_index=index, field=field) from None


def canonical_rows(candles, expected_underlying=None):
    if isinstance(candles, pd.DataFrame):
        if not candles.columns.is_unique:
            raise CandleIntegrityError("CI_ROW_INVALID")
        candles = candles.to_dict(orient="records")
    try:
        records = list(candles)
    except TypeError:
        raise CandleIntegrityError("CI_ROW_INVALID") from None
    rows = []
    for i, row in enumerate(records):
        if not isinstance(row, Mapping):
            raise CandleIntegrityError("CI_ROW_INVALID", row_index=i)
        aliases = [row[k] for k in ("timestamp", "date", "datetime") if k in row]
        if any(value is None for value in aliases):
            raise CandleIntegrityError("CI_REQUIRED_FIELD_MISSING", row_index=i, field="timestamp")
        stamps = [strict_timestamp(value, i) for value in aliases]
        if not stamps:
            raise CandleIntegrityError("CI_REQUIRED_FIELD_MISSING", row_index=i, field="timestamp")
        if len(set(stamps)) != 1:
            raise CandleIntegrityError("CI_ALIAS_CONFLICT", row_index=i, field="timestamp")
        identities = [row[k] for k in ("underlying", "symbol") if k in row]
        if not identities or any(not isinstance(x, str) or not x for x in identities):
            raise CandleIntegrityError("CI_REQUIRED_FIELD_MISSING", row_index=i, field="underlying")
        if len(set(identities)) != 1:
            raise CandleIntegrityError("CI_ALIAS_CONFLICT", row_index=i, field="underlying")
        if expected_underlying is not None and identities[0] != expected_underlying:
            raise CandleIntegrityError("CI_UNDERLYING_MISMATCH", row_index=i, field="underlying")
        values = []
        for field in FIELDS[1:6]:
            if field not in row or row[field] is None:
                raise CandleIntegrityError("CI_REQUIRED_FIELD_MISSING", row_index=i, field=field)
            values.append(decimal_value(row[field], i, field))
        o, h, l, c, v = map(Decimal, values)
        if min(o, h, l, c) <= 0:
            raise CandleIntegrityError("CI_PRICE_NONPOSITIVE", row_index=i)
        if not l <= min(o, c) <= max(o, c) <= h:
            raise CandleIntegrityError("CI_OHLC_INCONSISTENT", row_index=i)
        if v < 0:
            raise CandleIntegrityError("CI_VOLUME_NEGATIVE", row_index=i, field="volume")
        rows.append((stamps[0], *values, identities[0]))
    stamps = [r[0] for r in rows]
    if len(set(stamps)) != len(stamps):
        raise CandleIntegrityError("CI_DUPLICATE_TIMESTAMP")
    if stamps != sorted(stamps):
        raise CandleIntegrityError("CI_NON_MONOTONIC_TIMESTAMPS")
    return tuple(rows)


@dataclass(frozen=True, eq=False)
class ValidatedCandleSnapshot(Sequence):
    _canonical: tuple
    _declaration_json: str
    _dates: tuple
    _requirements: CandleRequirements
    _calendar: object
    scope: str
    _selection_dates: tuple = ()

    def __len__(self):
        return len(self._canonical)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return tuple(self[i] for i in range(*index.indices(len(self))))
        raw = self._canonical[index]
        return MappingProxyType(dict(zip(FIELDS, (raw[0], *(float(x) for x in raw[1:6]), raw[6]))))

    @property
    def declaration(self):
        if self.scope == "is_view":
            raise AttributeError("Selection views have no dataset declaration")
        return json.loads(self._declaration_json)

    @property
    def receipt(self):
        if self.scope == "is_view":
            raise AttributeError("Selection views have no receipt")
        return _receipt(self)


_issued = weakref.WeakKeyDictionary()


def _binding(snapshot):
    return (snapshot._canonical, snapshot._declaration_json, snapshot._dates, snapshot._requirements,
            snapshot._calendar, snapshot.scope, snapshot._selection_dates)


def _issue(rows, declaration, days, requirements, calendar, scope, selection_dates=()):
    value = ValidatedCandleSnapshot(rows, json.dumps(declaration, sort_keys=True), tuple(days),
                                    requirements, calendar, scope, tuple(selection_dates))
    _issued[value] = _binding(value)
    return value


def _authentic(value):
    if not isinstance(value, ValidatedCandleSnapshot) or _issued.get(value) != _binding(value):
        raise CandleIntegrityError("CI_SNAPSHOT_INVALID")
    require_approved_calendar(value._calendar)
    return value


def eligible_dates(declaration, calendar):
    content = require_approved_calendar(calendar).content()
    if declaration.coverage_start < content["coverage_start"] or declaration.coverage_end > content["coverage_end"]:
        raise CandleIntegrityError("CI_CALENDAR_COVERAGE")
    return tuple(d for d in content["sessions"] if declaration.coverage_start <= d <= declaration.coverage_end)


def _requirements_check(value, requirements):
    declaration = json.loads(value._declaration_json)
    if requirements.underlying != declaration["underlying"]:
        raise CandleIntegrityError("CI_UNDERLYING_MISMATCH")
    if requirements.needs_vwap:
        if declaration["volume_provenance"] != "exchange_reported":
            raise CandleIntegrityError("CI_VOLUME_PROVENANCE_INCOMPATIBLE")
        positive_days = {local_day(r[0]) for r in value._canonical if Decimal(r[5]) > 0}
        if not set(value._dates) <= positive_days:
            raise CandleIntegrityError("CI_SESSION_VOLUME_NONPOSITIVE")


def local_day(stamp):
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(IST).date().isoformat()


def validate_candle_dataset(candles, *, declaration, calendar, requirements, trading_dates=None):
    declaration = declaration_value(declaration)
    require_approved_calendar(calendar)
    if declaration.calendar_id != calendar.calendar_id:
        raise CandleIntegrityError("CI_DECLARATION_MISMATCH")
    days = eligible_dates(declaration, calendar)
    if trading_dates is not None and tuple(trading_dates) != days:
        raise CandleIntegrityError("CI_TRADING_DATES_MISMATCH")
    if declaration.underlying != requirements.underlying:
        raise CandleIntegrityError("CI_UNDERLYING_MISMATCH")
    rows = canonical_rows(candles, declaration.underlying)
    if not rows:
        raise CandleIntegrityError("CI_DATASET_EMPTY")
    by_day = {d: set() for d in days}
    exclusions = {x["date"] for x in calendar.content()["exclusions"]}
    for i, row in enumerate(rows):
        stamp = datetime.fromisoformat(row[0].replace("Z", "+00:00")).astimezone(IST)
        day = stamp.date().isoformat()
        if not declaration.coverage_start <= day <= declaration.coverage_end:
            raise CandleIntegrityError("CI_DATE_OUT_OF_COVERAGE", row_index=i)
        if day in exclusions:
            raise CandleIntegrityError("CI_EXCLUDED_SESSION", row_index=i)
        if day not in by_day:
            raise CandleIntegrityError("CI_NON_TRADING_SESSION", row_index=i)
        minute = stamp.hour * 60 + stamp.minute
        if minute < 555 or minute > 925 or (minute - 555) % 5:
            raise CandleIntegrityError("CI_BAR_GRID_INVALID", row_index=i)
        by_day[day].add(minute)
    if any(not bars for bars in by_day.values()):
        raise CandleIntegrityError("CI_SESSION_MISSING")
    if any(len(bars) != 75 for bars in by_day.values()):
        raise CandleIntegrityError("CI_SESSION_INCOMPLETE")
    value = _issue(rows, declaration.model_dump(), days, requirements, calendar, declaration.scope)
    _requirements_check(value, requirements)
    return value


def require_issued_snapshot(value, *, requirements, expected_dates, permitted_scopes):
    _authentic(value)
    if value.scope not in permitted_scopes or value._dates != tuple(expected_dates):
        raise CandleIntegrityError("CI_SNAPSHOT_SCOPE_MISMATCH")
    _requirements_check(value, requirements)
    return value


def execution_snapshot(candles, *, declaration, requirements, trading_dates):
    if isinstance(candles, ValidatedCandleSnapshot):
        value = require_issued_snapshot(candles, requirements=requirements, expected_dates=trading_dates,
                                        permitted_scopes=("development",))
        if declaration is not None and declaration_value(declaration).model_dump() != value.declaration:
            raise CandleIntegrityError("CI_DECLARATION_MISMATCH")
        if value._requirements != requirements:
            value = _issue(value._canonical, value.declaration, value._dates, requirements,
                           value._calendar, "development")
        return value
    if len(candles) == 0:
        raise CandleIntegrityError("CI_DATASET_EMPTY")
    dec = declaration_value(declaration)
    if dec.scope != "development":
        raise CandleIntegrityError("CI_SNAPSHOT_SCOPE_MISMATCH")
    calendar = resolve_approved_calendar(dec.calendar_id)
    return validate_candle_dataset(candles, declaration=dec, calendar=calendar,
                                   requirements=requirements, trading_dates=trading_dates)


def derive_development_snapshot(full_snapshot, *, development_dates):
    _authentic(full_snapshot)
    if full_snapshot.scope != "full":
        raise CandleIntegrityError("CI_SNAPSHOT_SCOPE_MISMATCH")
    days = tuple(development_dates)
    if not days or days != full_snapshot._dates[:len(days)]:
        raise CandleIntegrityError("CI_SNAPSHOT_SCOPE_MISMATCH")
    original = full_snapshot.declaration
    declaration = {k: original[k] for k in ("schema_version", "underlying", "interval", "timestamp_semantics",
                                             "timezone", "volume_provenance", "calendar_id")}
    declaration.update(scope="development", coverage_start=days[0], coverage_end=days[-1])
    if eligible_dates(declaration_value(declaration), full_snapshot._calendar) != days:
        raise CandleIntegrityError("CI_TRADING_DATES_MISMATCH")
    rows = tuple(r for r in full_snapshot._canonical if local_day(r[0]) in days)
    return _issue(rows, declaration, days, full_snapshot._requirements, full_snapshot._calendar, "development")


def derive_is_snapshot(development_snapshot, *, fold):
    _authentic(development_snapshot)
    if development_snapshot.scope != "development" or fold.is_end_date not in development_snapshot._dates:
        raise CandleIntegrityError("CI_SNAPSHOT_SCOPE_MISMATCH")
    days = development_snapshot._dates[:development_snapshot._dates.index(fold.is_end_date) + 1]
    if not set(fold.is_dates) <= set(days) or not fold.is_dates or fold.is_dates[-1] != fold.is_end_date:
        raise CandleIntegrityError("CI_SNAPSHOT_SCOPE_MISMATCH")
    declaration = development_snapshot.declaration
    declaration["coverage_end"] = days[-1]
    rows = tuple(r for r in development_snapshot._canonical if local_day(r[0]) in days)
    return _issue(rows, declaration, days, development_snapshot._requirements,
                  development_snapshot._calendar, "is_view", fold.is_dates)


def require_is_view(value, *, fold, requirements):
    if not isinstance(value, ValidatedCandleSnapshot):
        raise CandleIntegrityError("CI_SNAPSHOT_REQUIRED")
    _authentic(value)
    if (value.scope != "is_view" or not value._dates or value._dates[-1] != fold.is_end_date
            or value._selection_dates != tuple(fold.is_dates)):
        raise CandleIntegrityError("CI_SNAPSHOT_SCOPE_MISMATCH")
    _requirements_check(value, requirements)
    return value


def _receipt(value):
    _authentic(value)
    dec = value.declaration
    receipt = {"schema_version": "candle-integrity-receipt-v1", "validator_version": "candle-integrity-v1",
               "canonicalization_version": "candle-utc-decimal-v1", "scope": value.scope,
               "integrity_status": "CERTIFIED", "underlying": dec["underlying"], "interval": dec["interval"],
               "timezone": dec["timezone"], "timestamp_semantics": dec["timestamp_semantics"],
               "coverage_start": dec["coverage_start"], "coverage_end": dec["coverage_end"],
               "row_count": len(value), "session_count": len(value._dates),
               "volume_provenance": dec["volume_provenance"], "calendar_id": dec["calendar_id"],
               "calendar_sha256": value._calendar.sha256,
               "declaration_sha256": digest("candle-declaration-v1", dec),
               "candles_sha256": digest("candle-rows-utc-v1", value._canonical),
               "session_dates_sha256": digest("candle-session-dates-v1", value._dates),
               "requirements_sha256": digest("candle-requirements-v1", asdict(value._requirements))}
    receipt["receipt_sha256"] = digest("candle-receipt-v1", receipt)
    return receipt
