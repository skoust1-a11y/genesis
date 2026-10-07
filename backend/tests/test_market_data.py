from __future__ import annotations

import random
import shutil
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

from app.config import get_settings
from app.engine.market import market_data_fingerprint
from app.market.feed import choose_expiry
from app.market.historical import HistoricalFeed, option_price
from app.market.models import IndexBar, OptionEod
from app.market.nse import (
    bhavcopy_layout,
    iv_rank_series,
    parse_daily_bars,
    parse_daily_closes,
    parse_fo_bhavcopy,
    read_text,
)
from app.market.source import HistoricalSource, SampleSource, open_market_source
from app.market.store import OptionQuote, _ready_engine, open_market_store
from scripts.import_nse_eod import classify, import_dir

FIXTURES = Path(__file__).parent / "fixtures" / "nse"
LEGACY = FIXTURES / "fo05JAN2024bhav.csv"
UDIFF = FIXTURES / "BhavCopy_NSE_FO_0_0_0_20240708_F_0000.csv"
VIX = FIXTURES / "india_vix_2024.csv"
INDEX = FIXTURES / "index_NIFTY_2024.csv"


@pytest.fixture
def store(market_db_url):
    s = open_market_store(get_settings())
    yield s
    s.close()


def test_legacy_bhavcopy_keeps_index_options_only():
    text = read_text(LEGACY)
    assert bhavcopy_layout(text) == "legacy"
    rows = parse_fo_bhavcopy(text, ["NIFTY"])
    assert [(r.underlying, r.expiry, r.strike, r.option_type) for r in rows] == [
        ("NIFTY", "2024-01-11", 21700.0, "CE"),
        ("NIFTY", "2024-01-11", 21700.0, "PE"),
        ("NIFTY", "2024-01-11", 22500.0, "CE"),
    ]
    assert {r.date for r in rows} == {"2024-01-05"}
    assert rows[0].close == 135.5 and rows[0].volume == 40000 and rows[0].lot_size is None
    assert {r.underlying for r in parse_fo_bhavcopy(text)} == {"NIFTY", "BANKNIFTY"}


def test_udiff_bhavcopy_reads_lot_size_and_underlying_price():
    text = read_text(UDIFF)
    assert bhavcopy_layout(text) == "udiff"
    rows = parse_fo_bhavcopy(text, ["NIFTY"])
    assert [(r.strike, r.option_type) for r in rows] == [(24300.0, "CE"), (24300.0, "PE"), (25000.0, "CE")]
    assert {(r.date, r.expiry, r.lot_size, r.underlying_price) for r in rows} == {
        ("2024-07-08", "2024-07-11", 25, 24320.55)
    }


def test_zip_bhavcopy_is_read(tmp_path):
    archive = shutil.make_archive(str(tmp_path / "fo05JAN2024bhav.csv"), "zip", FIXTURES, LEGACY.name)
    assert read_text(Path(archive)) == read_text(LEGACY)


def test_daily_closes_parse_nse_exports():
    vix = parse_daily_closes(read_text(VIX))
    assert vix[0] == ("2024-06-03", 20.1) and vix[-1] == ("2024-07-08", 14.6) and len(vix) == 26
    assert parse_daily_closes(read_text(INDEX))[-1] == ("2024-07-08", 24320.55)


def test_daily_bars_keep_open_high_low():
    first = parse_daily_bars(read_text(INDEX))[0]
    assert (first.date, first.open, first.high, first.low, first.close) == ("2024-07-04", 24300, 24401, 24281, 24302.15)
    closes_only = parse_daily_bars("Date,Close\n04-Jul-2024,24302.15\n")
    assert (closes_only[0].open, closes_only[0].high, closes_only[0].low) == (None, None, None)


def test_index_import_stores_the_day_range_and_a_closes_only_file_keeps_it(store, tmp_path):
    store.import_index_closes(INDEX, "NIFTY")
    bar = store.index_bar("NIFTY", "2024-07-04")
    assert (bar.open, bar.high, bar.low, bar.close) == (24300.0, 24401.0, 24281.0, 24302.15)
    closes = tmp_path / "closes.csv"
    closes.write_text("Date,Close\n04-Jul-2024,24310.00\n")
    store.import_index_closes(closes, "NIFTY")
    store.session.expire_all()
    bar = store.index_bar("NIFTY", "2024-07-04")
    assert (bar.high, bar.close) == (24401.0, 24310.0)


def test_existing_market_db_gains_new_columns(tmp_path):
    url = f"sqlite:///{tmp_path / 'old_market.db'}"
    old = create_engine(url)
    with old.begin() as conn:
        conn.execute(text(
            "CREATE TABLE index_bars (underlying VARCHAR(32) NOT NULL, date VARCHAR(10) NOT NULL, "
            "close FLOAT, vix FLOAT, iv_rank FLOAT, PRIMARY KEY (underlying, date))"
        ))  # fmt: skip
    old.dispose()
    engine = _ready_engine(url)
    assert {"open", "high", "low"} <= {c["name"] for c in inspect(engine).get_columns("index_bars")}
    engine.dispose()


def test_iv_rank_never_looks_ahead():
    rng = random.Random(7)
    values = [rng.uniform(10, 30) for _ in range(120)]
    full = iv_rank_series(values, lookback=60, min_history=20)
    for k in (20, 45, 90):
        assert iv_rank_series(values[:k], lookback=60, min_history=20) == full[:k]
    assert full[:19] == [None] * 19 and all(0 <= r <= 100 for r in full[19:])
    assert iv_rank_series([15.0] * 30, lookback=60, min_history=20)[-1] is None  # a flat window has no rank


def test_import_is_idempotent_and_fingerprinted(store):
    assert store.import_vix(VIX) == 26
    assert store.import_bhavcopy(UDIFF, ["NIFTY"]) == 3
    assert store.import_bhavcopy(UDIFF, ["NIFTY"]) == 0
    bar = store.index_bar("NIFTY", "2024-07-08")
    assert bar.close == 24320.55  # filled from the bhavcopy even though the VIX row came first
    assert bar.iv_rank is not None
    assert store.trading_dates("NIFTY") == ["2024-07-08"]
    fp = store.fingerprint("NIFTY")
    assert fp["source"] == "nse_eod" and fp["files"] == 2 and fp["option_rows"] == 3
    assert market_data_fingerprint("NIFTY")["source"] == "nse_eod"
    assert market_data_fingerprint("BANKNIFTY")["source"] == "sample_synthetic"


def test_later_vix_does_not_change_earlier_ranks(store, tmp_path):
    lines = read_text(VIX).splitlines()
    early = tmp_path / "vix_early.csv"
    early.write_text("\n".join(lines[:21]) + "\n")
    store.import_vix(early)
    before = {b.date: b.iv_rank for b in store.session.query(IndexBar).all()}
    store.import_vix(VIX)
    after = {b.date: b.iv_rank for b in store.session.query(IndexBar).all()}
    assert all(after[d] == r for d, r in before.items())
    assert len(after) > len(before)


def test_import_script_classifies_and_orders_files(market_db_url, tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    for f in (LEGACY, UDIFF, VIX, INDEX):
        shutil.copy(f, raw / f.name)
    (raw / "notes.txt").write_text("ignored")
    assert classify(raw / INDEX.name) == ("index", "NIFTY")
    assert classify(raw / VIX.name) == ("vix", "NIFTY")
    assert classify(raw / "notes.txt") is None
    assert import_dir(raw, ["NIFTY"]) == {"bhavcopy": 6, "index": 3, "vix": 26, "skipped": 0}
    assert import_dir(raw, ["NIFTY"])["skipped"] == 4


def test_quote_falls_back_to_settlement_and_marks_it_stale():
    assert option_price(OptionQuote(135.5, 135.0, 10, 75)) == (135.5, False)
    assert option_price(OptionQuote(4.1, 4.3, 0, 75)) == (4.3, True)
    assert option_price(OptionQuote(None, None, 0, 75)) == (None, False)


def test_expiry_choice_prefers_the_strategy_window():
    expiries = ["2024-01-04", "2024-01-11", "2024-01-18", "2024-01-25"]
    assert choose_expiry("2024-01-04", expiries, None) == "2024-01-11"
    assert choose_expiry("2024-01-04", expiries, (10, 21)) == "2024-01-18"
    assert choose_expiry("2024-01-04", expiries, (60, 90)) == "2024-01-11"
    assert choose_expiry("2024-01-30", expiries, None) is None


def test_feed_settles_expired_contracts_at_intrinsic(store):
    s = store.session
    for day, spot in (("2024-01-10", 21600.0), ("2024-01-11", 21800.0)):
        s.add(IndexBar(underlying="NIFTY", date=day, close=spot, vix=14.0, iv_rank=50.0))
        for expiry in ("2024-01-11", "2024-01-18"):
            s.add_all(
                OptionEod(
                    underlying="NIFTY", date=day, expiry=expiry, strike=21700.0, option_type=t,
                    close=90.0, settle=90.0, volume=0 if t == "PE" else 10, open_interest=1, lot_size=25,
                    underlying_price=None,
                )  # fmt: skip
                for t in ("CE", "PE")
            )
    s.commit()
    feed = HistoricalFeed(store, "NIFTY")
    held = [(21700.0, "CE", "2024-01-11"), (21700.0, "PE", "2024-01-11")]
    first = feed.snapshot("NIFTY", held)
    assert first.expiry == "2024-01-11" and first.dte == 1 and first.lot_size == 25
    assert first.quote(21700, "CE", "2024-01-11") == 90.0
    assert (21700.0, "PE", "2024-01-11") in first.stale
    expiry_day = feed.snapshot("NIFTY", held)
    assert expiry_day.expiry == "2024-01-18"
    assert expiry_day.quote(21700, "CE", "2024-01-11") == 100.0  # 21800 - 21700
    assert expiry_day.quote(21700, "PE", "2024-01-11") == 0.0
    assert feed.snapshot("NIFTY") is None and feed.exhausted


def test_sample_source_is_the_fallback(market_db_url):
    settings = get_settings()
    source = open_market_source("NIFTY", settings)
    assert isinstance(source, SampleSource) and source.data_source == "sample_synthetic"
    assert source.dates()
    source.close()
    store = open_market_store(settings)
    store.import_vix(VIX)
    store.import_bhavcopy(UDIFF, ["NIFTY"])
    store.close()
    source = open_market_source("NIFTY", settings)
    assert isinstance(source, HistoricalSource) and source.data_source == "nse_eod"
    assert source.dates() == ["2024-07-08"]
    source.close()
