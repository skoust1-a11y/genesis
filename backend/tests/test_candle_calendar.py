import copy
from dataclasses import replace

import pytest
from app.trading import candle_calendar as cc
from app.trading.candle_integrity import CandleIntegrityError, CandleRequirements, validate_candle_dataset
from tests.candle_integrity_fixtures import calendar_record
from tests.test_causal_walk_forward import _make_session_candles


def install(monkeypatch, record):
    monkeypatch.setattr(cc,"_load_registry",lambda:{"schema_version":"approved-candle-calendars-v1","calendars":[record]})


def test_C09_empty_registry():
    with pytest.raises(CandleIntegrityError) as exc: cc.resolve_approved_calendar("TEST-UNKNOWN")
    assert exc.value.code=="CI_CALENDAR_UNTRUSTED"


@pytest.mark.parametrize("change",["digest","approval","source","inventory"])
def test_C09_K02_bad_records(monkeypatch,change):
    record=calendar_record(["2026-11-05"])
    if change=="digest":record["sha256"]="0"*64
    if change=="approval":record["approval"]["status"]="pending"
    if change=="source":record["sources"]=[]
    if change=="inventory":record["closures"]=["2026-11-05"]
    install(monkeypatch,record)
    with pytest.raises(CandleIntegrityError):cc.resolve_approved_calendar(record["calendar_id"])


def test_K01_copies_and_forged_calendars(monkeypatch):
    record=calendar_record(["2026-11-05"]);install(monkeypatch,record)
    real=cc.resolve_approved_calendar(record["calendar_id"])
    assert cc.require_approved_calendar(real) is real
    for fake in (copy.copy(real),replace(real),cc.ApprovedCalendar(real.calendar_id,real.canonical,real.sha256)):
        with pytest.raises(CandleIntegrityError) as exc:cc.require_approved_calendar(fake)
        assert exc.value.code=="CI_CALENDAR_UNTRUSTED"


def test_K03_fixed_path(monkeypatch,tmp_path):
    monkeypatch.chdir(tmp_path);monkeypatch.setenv("DATA_DIR",str(tmp_path));monkeypatch.setenv("CANDLE_CALENDAR_REGISTRY",str(tmp_path/"fake"))
    assert cc.REGISTRY_PATH.name=="approved_calendars.json"
    assert cc._load_registry()=={"schema_version":"approved-candle-calendars-v1","calendars":[]}


def test_S04_S06_explicit_exclusion_and_weekend_opening(monkeypatch):
    days=["2026-11-05","2026-11-07"]
    record=calendar_record(days,exclusions=("2026-11-06",));install(monkeypatch,record)
    cal=cc.resolve_approved_calendar(record["calendar_id"])
    dec=dict(schema_version="candle-dataset-declaration-v1",scope="development",underlying="NIFTY",interval="5m",
             timestamp_semantics="bar_open",timezone="Asia/Kolkata",coverage_start=days[0],coverage_end=days[-1],
             volume_provenance="exchange_reported",calendar_id=cal.calendar_id)
    rows=[r for d in days for r in _make_session_candles(d)]
    assert len(validate_candle_dataset(rows,declaration=dec,calendar=cal,requirements=CandleRequirements("NIFTY"),trading_dates=days))==150
    with pytest.raises(CandleIntegrityError) as exc:
        validate_candle_dataset(_make_session_candles(days[0])+_make_session_candles("2026-11-06")+_make_session_candles(days[1]),
            declaration=dec,calendar=cal,requirements=CandleRequirements("NIFTY"),trading_dates=days)
    assert exc.value.code=="CI_EXCLUDED_SESSION"
