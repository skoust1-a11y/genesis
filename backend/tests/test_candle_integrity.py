import copy
from datetime import date, datetime, timezone
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from app.trading import candle_integrity as ci
from app.trading.candle_calendar import resolve_approved_calendar
from tests.candle_integrity_fixtures import declaration, snapshot, synthetic_calendar
from tests.test_causal_walk_forward import _make_session_candles

pytestmark = pytest.mark.usefixtures("synthetic_calendar")
DAYS = ["2026-11-05", "2026-11-06"]


def validate(rows, days=DAYS, *, vwap=False, provenance="exchange_reported"):
    dec = declaration(days, volume_provenance=provenance)
    return ci.validate_candle_dataset(rows, declaration=dec,
        calendar=resolve_approved_calendar(dec["calendar_id"]),
        requirements=ci.CandleRequirements("NIFTY", vwap), trading_dates=days)


def bars():
    return [r for d in DAYS for r in _make_session_candles(d)]


def test_C07_N02_equivalent_containers_and_aware_instants():
    rows = bars()
    expected = validate(rows)
    variants = [copy.deepcopy(rows) for _ in range(4)]
    for row in variants[0]:
        row["timestamp"] = datetime.fromisoformat(row["timestamp"]).astimezone(timezone.utc).isoformat()
        row["volume"] = 1500
    for row in variants[1]:
        row["timestamp"] = datetime.fromisoformat(row["timestamp"])
        row["volume"] = np.float64(1500)
    for row in variants[2]:
        row["timestamp"] = pd.Timestamp(row["timestamp"])
    variants[3] = pd.DataFrame(variants[3])
    for variant in variants:
        actual = validate(variant)
        assert actual._canonical == expected._canonical
        assert actual.receipt == expected.receipt
        assert actual[0]["timestamp"].endswith("Z")


@pytest.mark.parametrize("bad", ["2026-11-05T09:15:00", datetime(2026,11,5,9,15),
    "2026-11-05", date(2026,11,5), 1793850300, "05/11/2026 09:15", "bad",
    "2026-11-05T09:15:01+05:30", "2026-11-05T09:15:00.000+05:30",
    datetime.fromisoformat("2026-11-05T09:15:00.000001+05:30"),
    pd.Timestamp("2026-11-05T09:15:00.000000001+05:30"), pd.NaT])
def test_N03_strict_timestamps(bad):
    rows = bars(); rows[0]["timestamp"] = bad
    with pytest.raises(ci.CandleIntegrityError, match="Invalid certified") as exc:
        validate(rows)
    assert exc.value.code == "CI_TIMESTAMP_INVALID"


@pytest.mark.parametrize("field,value,code", [
    ("open", True, "CI_NUMERIC_INVALID"), ("volume", "1500", "CI_NUMERIC_INVALID"),
    ("close", float("nan"), "CI_NUMERIC_INVALID"), ("volume", float("inf"), "CI_NUMERIC_INVALID"),
    ("high", 10**400, "CI_NUMERIC_INVALID"), ("low", 0, "CI_PRICE_NONPOSITIVE"),
    ("high", 1, "CI_OHLC_INCONSISTENT"), ("volume", -1, "CI_VOLUME_NEGATIVE"),
    ("underlying", "BANKNIFTY", "CI_UNDERLYING_MISMATCH"),
    ("underlying", None, "CI_REQUIRED_FIELD_MISSING"),
])
def test_N01_C05_V01_invalid_fields(field, value, code):
    rows=bars(); rows[0][field]=value
    with pytest.raises(ci.CandleIntegrityError) as exc: validate(rows)
    assert exc.value.code == code
    assert str(value) not in str(exc.value.to_dict()) or value in (0,1,True,None)


@pytest.mark.parametrize("field", ["timestamp","open","high","low","close","volume","underlying"])
def test_C05_missing_fields(field):
    rows=bars(); del rows[0][field]
    with pytest.raises(ci.CandleIntegrityError) as exc: validate(rows)
    assert exc.value.code == "CI_REQUIRED_FIELD_MISSING"


@pytest.mark.parametrize("index", [0, 25, 74, 75, 149])
def test_S01_missing_bars(index):
    rows=bars(); rows.pop(index)
    with pytest.raises(ci.CandleIntegrityError) as exc: validate(rows)
    assert exc.value.code == "CI_SESSION_INCOMPLETE"


@pytest.mark.parametrize("missing", [0, 1, 2])
def test_S02_missing_declared_sessions(missing):
    days=["2026-11-04", *DAYS]
    rows=[r for i,d in enumerate(days) if i != missing for r in _make_session_candles(d)]
    with pytest.raises(ci.CandleIntegrityError) as exc: validate(rows, days)
    assert exc.value.code == "CI_SESSION_MISSING"


@pytest.mark.parametrize("kind,code", [("duplicate","CI_DUPLICATE_TIMESTAMP"),
    ("reverse","CI_NON_MONOTONIC_TIMESTAMPS"), ("grid","CI_BAR_GRID_INVALID"),
    ("outside","CI_DATE_OUT_OF_COVERAGE")])
def test_S03_order_grid_and_coverage(kind,code):
    rows=bars()
    if kind=="duplicate": rows.insert(40,copy.deepcopy(rows[0]))
    if kind=="reverse": rows[0],rows[1]=rows[1],rows[0]
    if kind=="grid": rows[0]["timestamp"]="2026-11-05T09:16:00+05:30"
    if kind=="outside": rows=_make_session_candles("2026-11-04")+rows
    original=copy.deepcopy(rows)
    with pytest.raises(ci.CandleIntegrityError) as exc: validate(rows)
    assert exc.value.code==code
    assert rows==original


def test_N04_aliases_and_C08_closed_receipt():
    rows=bars(); rows[0]["date"]="2026-11-05T03:45:00Z"; rows[0]["symbol"]="NIFTY"
    value=validate(rows)
    assert set(value.receipt)==set('schema_version validator_version canonicalization_version scope integrity_status underlying interval timezone timestamp_semantics coverage_start coverage_end row_count session_count volume_provenance calendar_id calendar_sha256 declaration_sha256 candles_sha256 session_dates_sha256 requirements_sha256 receipt_sha256'.split())
    rows[0]["date"]="2026-11-05T03:50:00Z"
    with pytest.raises(ci.CandleIntegrityError) as exc: validate(rows)
    assert exc.value.code=="CI_ALIAS_CONFLICT"
    rows[0]["date"]="2026-11-05"
    with pytest.raises(ci.CandleIntegrityError) as exc: validate(rows)
    assert exc.value.code=="CI_TIMESTAMP_INVALID"


@pytest.mark.parametrize("provenance", ["exchange_reported","synthetic","unavailable"])
@pytest.mark.parametrize("vwap", [False,True])
def test_C01_volume_provenance(provenance,vwap):
    if vwap and provenance!="exchange_reported":
        with pytest.raises(ci.CandleIntegrityError) as exc: validate(bars(),vwap=vwap,provenance=provenance)
        assert exc.value.code=="CI_VOLUME_PROVENANCE_INCOMPATIBLE"
    else: assert len(validate(bars(),vwap=vwap,provenance=provenance))==150


def test_V02_zero_session_volume_and_D07_mutation():
    rows=bars(); value=validate(rows,vwap=True); receipt=value.receipt
    rows[0]["volume"]=999999; rows[0]["nested"]={"secret":123}
    assert value.receipt==receipt and value[0]["volume"]==1500
    with pytest.raises(TypeError): value[0]["volume"]=0
    for r in rows[75:]: r["volume"]=0
    with pytest.raises(ci.CandleIntegrityError) as exc: validate(rows,vwap=True)
    assert exc.value.code=="CI_SESSION_VOLUME_NONPOSITIVE"
    assert len(validate(rows))==150


def test_C02_development_has_no_parent_or_holdout():
    full=snapshot(bars(),DAYS,scope="full")
    dev=ci.derive_development_snapshot(full,development_dates=DAYS[:1])
    assert dev.declaration["coverage_start"]==dev.declaration["coverage_end"]==DAYS[0]
    encoded=str(dev.declaration)+str(dev.receipt)+str(vars(dev))
    assert DAYS[1] not in str(dev.declaration)+str(dev.receipt)
    assert full.receipt["candles_sha256"] not in encoded
    assert all(x is not full for x in vars(dev).values())
    with pytest.raises(ci.CandleIntegrityError):
        ci.require_issued_snapshot(copy.copy(dev),requirements=ci.CandleRequirements("NIFTY"),expected_dates=DAYS[:1],permitted_scopes=("development",))


@pytest.mark.parametrize("change",["missing_provenance","unknown_field","bad_scope","partial_window"])
def test_C01_C08_declaration_is_closed(change):
    dec=declaration(DAYS)
    if change=="missing_provenance":del dec["volume_provenance"]
    if change=="unknown_field":dec["caller_receipt"]={"integrity_status":"CERTIFIED"}
    if change=="bad_scope":dec["scope"]="is_view"
    if change=="partial_window":dec["coverage_start"]="2026-11-05T10:00:00+05:30"
    with pytest.raises(ci.CandleIntegrityError) as exc:ci.declaration_value(dec)
    assert exc.value.code=="CI_DECLARATION_INVALID"


def test_N02_offset_aware_minute_precision():
    rows=bars(); value=validate(rows)
    for row in rows:
        row["timestamp"]=row["timestamp"].replace(":00+05:30","+0530")
    assert validate(rows).receipt==value.receipt
