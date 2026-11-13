from __future__ import annotations

import hashlib
import sqlite3

import pytest
from sqlalchemy import create_engine

from app import alerts
from app.config import get_settings
from app.db import trade_store
from app.trading.crew.exit_agent import ExitDecision
from app.trading.crew.journal_agent import JournalAgent
from tests.conftest import BACKEND, PROTECTED_DB_FILES, _production_fingerprint

PROD_TRADES_DB = trade_store.ROOT_DIR / "data" / "trades.db"
TRADE = {"date": "2026-11-04", "time": "10:15", "strategy": "Liquidity Sweep", "side": "LONG", "entry": 100.0}


def test_guard_blocks_production_db(production_db_opens, monkeypatch):
    genesis_db = BACKEND / "data" / "genesis.db"
    with pytest.raises(RuntimeError, match="production database"):
        sqlite3.connect(str(PROD_TRADES_DB))
    with pytest.raises(RuntimeError, match="production database"):
        sqlite3.connect(f"{PROD_TRADES_DB.as_uri()}?mode=ro", uri=True)
    with pytest.raises(RuntimeError, match="production database"):
        sqlite3.Connection(str(PROD_TRADES_DB) + "-wal")
    with pytest.raises(RuntimeError, match="production database"):
        create_engine(f"sqlite:///{genesis_db}").connect()
    monkeypatch.chdir(BACKEND)  # the settings default URL is relative to the working directory
    with pytest.raises(RuntimeError, match="production database"):
        create_engine("sqlite:///data/genesis_live.db").connect()
    assert len(production_db_opens) == 5
    production_db_opens.clear()


def test_guard_blocks_and_fingerprints_protected_root_and_trades_db(production_db_opens, monkeypatch):
    expected_paths = (
        BACKEND.resolve() / "genesis.db",
        BACKEND.resolve() / "live.db",
        trade_store.ROOT_DIR.resolve() / "trades" / "trades.db",
    )
    assert PROTECTED_DB_FILES == expected_paths
    fingerprint = _production_fingerprint()
    for db_path in expected_paths:
        if db_path.exists():
            assert str(db_path) in fingerprint
        with pytest.raises(RuntimeError, match="production database"):
            sqlite3.connect(str(db_path))
        with pytest.raises(RuntimeError, match="production database"):
            sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
        with pytest.raises(RuntimeError, match="production database"):
            sqlite3.Connection(str(db_path) + "-wal")
        with pytest.raises(RuntimeError, match="production database"):
            create_engine(f"sqlite:///{db_path}").connect()
    monkeypatch.chdir(BACKEND)
    for rel_name in ("genesis.db", "live.db", "../trades/trades.db"):
        with pytest.raises(RuntimeError, match="production database"):
            sqlite3.connect(rel_name)
    assert len(production_db_opens) == len(expected_paths) * 4 + 3
    production_db_opens.clear()


def test_guard_records_opens_that_the_code_swallows(production_db_opens, monkeypatch):
    assert alerts.record(title="t", message="m") is None  # the default temporary DATABASE_URL has no schema
    assert production_db_opens == []
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{BACKEND / 'data' / 'genesis.db'}")
    get_settings.cache_clear()
    assert alerts.record(title="t", message="m") is None  # record() never raises
    assert production_db_opens == [str(BACKEND / "data" / "genesis.db")]
    production_db_opens.clear()


def test_default_path_is_temporary(tmp_path):
    assert trade_store.DEFAULT_DB_PATH == tmp_path / "trades.db"


def test_journal_agent_writes_to_injected_path(tmp_path):
    db = tmp_path / "injected.db"
    ja = JournalAgent(trades_dir=tmp_path / "trades", db_path=db)
    ja.save_active_trades({"T1": {"trade_id": "T1", **TRADE, "status": "OPEN"}}, date_str="2026-11-04")
    decision = ExitDecision(
        action="TARGET_HIT", exit_price=120.0, realized_points=20.0, realized_r=1.0,
        exit_reason="target", is_fully_closed=True,
    )  # fmt: skip
    assert ja.record_exit("T1", decision, date_str="2026-11-04") is not None
    assert [t["trade_id"] for t in trade_store.get_all_trades(since="2026-01-01", db_path=db)] == ["T1"]
    assert not trade_store.DEFAULT_DB_PATH.exists()


def test_read_does_not_create_or_mutate(tmp_path):
    db = tmp_path / "ro.db"
    assert trade_store.get_all_trades(db_path=db) == []
    assert not db.exists()

    trade_store.upsert_trade(TRADE, db)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    trade_store.get_all_trades(since="2026-01-01", db_path=db)
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        trade_store.get_readonly_connection(db).execute("CREATE TABLE x (a)")


def test_init_db_keeps_legacy_check_table(tmp_path):
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE live_and_forward_trades (trade_id TEXT PRIMARY KEY, date TEXT, status TEXT,"
        " strategy TEXT CHECK(strategy IN ('A')))"
    )
    conn.execute("INSERT INTO live_and_forward_trades VALUES ('keep', '2026-11-04', 'OPEN', 'A')")
    conn.commit()
    conn.close()
    trade_store.init_db(db)
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT trade_id FROM live_and_forward_trades").fetchall() == [("keep",)]
    conn.close()
