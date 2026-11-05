"""S1-P3 admission boundary. Synthetic quotes, temp journals. No broker, scanner, Telegram or network."""

from __future__ import annotations

import ast
import hashlib
import shutil
import socket
import sqlite3
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.config import get_settings
from app.db import crew_journal as cj
from app.db import crew_session as cs
from app.db import trade_store
from app.trading import admission as adm
from app.trading.option_quote import IST, ContractQuote

DAY = "2026-11-06"
NOW = datetime(2026, 11, 6, 10, 15, 5, tzinfo=IST)
SETTINGS = get_settings().model_copy(update={"max_spread_pct": 5})
CANDIDATE = adm.SignalCandidate(market_day=DAY, instrument="NIFTY", strategy_id="PDC Zone",
                                strategy_version="v0", signal_candle="2026-11-06T10:15:00+05:30",
                                direction="CALL", setup_id="OR-30")
QUOTE = ContractQuote(underlying="NIFTY", expiry="2026-11-10", strike=26100.0, option_type="CE",
                      provider_instrument_id="provider-1", tradingsymbol="NIFTY25N1026100CE", lot_size=75,
                      ltp=Decimal("120.5"), bid=None, ask=None, quote_time=None,
                      fetched_at=NOW - timedelta(seconds=4), source="chain", delta=0.42)
EXECUTABLE = replace(QUOTE, bid=Decimal("120"), ask=Decimal("121"), quote_time=NOW - timedelta(seconds=2))
PROD_DB = trade_store.DEFAULT_DB_PATH.resolve()
PROD_HASH = hashlib.sha256(PROD_DB.read_bytes()).hexdigest()
OCT30 = trade_store.ROOT_DIR / "trades" / ".active_trades_2026-10-30.json"
OCT30_HASH = hashlib.sha256(OCT30.read_bytes()).hexdigest()
MODULE = Path(adm.__file__)
RISK = [adm.CAPITAL_UNAVAILABLE, adm.CASH_UNAVAILABLE, adm.MAX_LOSS_UNVERIFIED]
LEASES: dict[Path, cs.Lease] = {}


def run(quote=QUOTE, candidate=CANDIDATE, now=NOW):
    return adm.evaluate(candidate, quote, now=now, settings=SETTINGS)


def ready(tmp_path):
    path = tmp_path / "journal.db"
    cj.migrate(path)
    LEASES[path] = cs.acquire(path, now=NOW, ttl_seconds=3600)
    return path


def own(path):
    """The session lease and an injected clock inside it."""
    return {"lease": LEASES[path], "now": NOW}


def counts(path):
    conn = cj.connect(path)
    try:
        return tuple(conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                     for t in ("crew_admissions", "crew_trades", "crew_events", "crew_outbox"))
    finally:
        conn.close()


def track(path, admission):
    persisted = adm.persist(path, admission, **own(path))
    return persisted, adm.open_research_trade(path, persisted, **own(path), model_lots=3, entry_spot=23100,
                                              stop_spot=23080, target_spot=23160)


def test_01_fresh_reference_is_research_only():
    result = run()
    assert result.admission_class == "RESEARCH_ONLY"
    assert list(result.reasons) == [adm.QUOTE_NOT_EXECUTABLE, *RISK]
    assert result.quote["grade"] == "FRESH_REFERENCE" and result.quote["age_seconds"] == 4
    assert result.signal_id == cj.signal_id_for(**{k: getattr(CANDIDATE, k) for k in (
        "market_day", "instrument", "strategy_id", "strategy_version", "signal_candle", "direction", "setup_id")})
    for field in ("market_day", "instrument", "strategy_id", "strategy_version", "signal_candle", "direction"):
        assert getattr(result, field)
    assert result.contract["tradingsymbol"] == "NIFTY25N1026100CE"
    assert result.capital == {"verified": False, "source": None, "reasons": RISK}
    assert result.evaluated_at == "2026-11-06T10:15:05+05:30"


def test_02_missing_quote_is_rejected():
    assert run(quote=None).reasons == (adm.QUOTE_MISSING,)


@pytest.mark.parametrize("ltp", [None, Decimal(0), Decimal("-1")])
def test_03_zero_or_negative_ltp_is_rejected(ltp):
    result = run(quote=replace(QUOTE, ltp=ltp))
    assert result.admission_class == "REJECTED" and result.reasons == (adm.LTP_INVALID,)


@pytest.mark.parametrize("quote", [
    replace(QUOTE, fetched_at=NOW - timedelta(seconds=10.5)),
    replace(QUOTE, quote_time=NOW - timedelta(seconds=11)),
])
def test_04_quote_older_than_ten_seconds_is_rejected(quote):
    assert run(quote=quote).reasons == (adm.QUOTE_STALE,)
    assert run(quote=replace(QUOTE, fetched_at=NOW - timedelta(seconds=10))).admission_class == "RESEARCH_ONLY"


@pytest.mark.parametrize("change", [
    {"expiry": "2026-11-05"}, {"expiry": "10-11-2026"}, {"expiry": None}, {"option_type": "PE"},
    {"underlying": "BANKNIFTY"}, {"lot_size": 0}, {"lot_size": 75.0}, {"strike": 0.0}, {"strike": float("nan")},
])
def test_05_invalid_contract_or_expiry_is_rejected(change):
    assert run(quote=replace(QUOTE, **change)).reasons == (adm.CONTRACT_INVALID,)


@pytest.mark.parametrize("code", [adm.CAPITAL_UNAVAILABLE, adm.CASH_UNAVAILABLE, adm.MAX_LOSS_UNVERIFIED])
def test_06_to_08_missing_capital_cash_or_max_loss_is_never_actionable(code):
    result = run(quote=EXECUTABLE)
    assert result.quote["grade"] == "EXECUTABLE"
    assert code in result.reasons and result.admission_class == "RESEARCH_ONLY"


def test_09_missing_bid_ask_is_never_actionable():
    for quote in (QUOTE, replace(EXECUTABLE, ask=None), replace(EXECUTABLE, bid=Decimal("10"))):
        result = run(quote=quote)
        assert adm.QUOTE_NOT_EXECUTABLE in result.reasons and result.admission_class == "RESEARCH_ONLY"


def test_10_reference_quote_with_incomplete_execution_evidence_is_research_only(tmp_path):
    path = ready(tmp_path)
    for setup, quote in (("ref", QUOTE), ("exe", EXECUTABLE)):
        result = run(quote=quote, candidate=replace(CANDIDATE, setup_id=setup))
        stored = adm.persist(path, result, **own(path))["admission"]
        assert stored["class"] == "RESEARCH_ONLY" and stored["reasons"] == list(result.reasons)
        assert stored["inputs"]["quote"]["grade"] == result.quote["grade"]
    conn = cj.connect(path)
    assert conn.execute("SELECT count(*) FROM crew_admissions WHERE class='ACTIONABLE'").fetchone()[0] == 0
    conn.close()


def test_11_duplicate_signal_keeps_one_admission(tmp_path):
    path = ready(tmp_path)
    first = adm.persist(path, run(), **own(path))
    same = adm.persist(path, run(), **own(path))
    later = adm.persist(path, run(quote=replace(QUOTE, ltp=None), now=NOW + timedelta(seconds=1)),
                        **own(path))
    assert first["replayed"] is False and same["replayed"] is True and later["replayed"] is True
    assert later["admission"]["class"] == "RESEARCH_ONLY"
    assert later["admission"]["admission_id"] == first["admission"]["admission_id"]
    assert counts(path)[0] == 1


def test_12_signal_identity_is_deterministic_across_restart():
    script = ("from datetime import datetime, timedelta, timezone; from decimal import Decimal;"
              "from app.config import get_settings; from app.trading import admission as a;"
              "from app.trading.option_quote import ContractQuote, IST;"
              "now = datetime(2026, 11, 6, 10, 15, 5, tzinfo=IST);"
              "c = a.SignalCandidate('2026-11-06', 'NIFTY', 'PDC Zone', 'v0', '2026-11-06T10:15:00+05:30', 'CALL', 'OR-30');"
              "q = ContractQuote('NIFTY', '2026-11-10', 26100.0, 'CE', 'provider-1', 'NIFTY25N1026100CE', 75,"
              " Decimal('120.5'), None, None, None, now - timedelta(seconds=4), 'chain', 0.42);"
              "print(a.evaluate(c, q, now=now, settings=get_settings()).signal_id)")
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                         cwd=MODULE.parents[2], check=True)
    assert out.stdout.strip() == run().signal_id
    assert run(candidate=replace(CANDIDATE, instrument=" nifty ", direction="call")).signal_id == run().signal_id


def test_13_research_only_trade_has_no_recommendation(tmp_path):
    path = ready(tmp_path)
    _, opened = track(path, run())
    trade = opened["trade"]
    assert trade["admission_class"] == "RESEARCH_ONLY" and trade["execution_state"] == "NONE"
    assert trade["contract"] == "NIFTY25N1026100CE" and trade["expiry"] == "2026-11-10"
    assert trade["lot_size_ref"] == 75
    conn = cj.connect(path)
    with pytest.raises(sqlite3.IntegrityError, match="ACTIONABLE"):
        conn.execute(
            "INSERT INTO crew_outbox (publication_id, trade_id, kind, purpose, body, body_sha256, state, created_at,"
            " chat_id) VALUES (?, ?, 'RECOMMENDATION', 'entry', 'b', ?, 'PENDING', ?, '-100')",
            (hashlib.sha256(b"p").hexdigest(), trade["trade_id"], hashlib.sha256(b"b").hexdigest(), DAY),
        )
    conn.close()
    assert counts(path) == (1, 1, 1, 0)


def test_14_rejected_candidate_cannot_become_a_trade(tmp_path):
    path = ready(tmp_path)
    rejected = run(quote=replace(QUOTE, fetched_at=NOW - timedelta(seconds=30)))
    persisted = adm.persist(path, rejected, **own(path))
    assert persisted["admission"]["class"] == "REJECTED"
    assert persisted["admission"]["reasons"] == [adm.QUOTE_STALE]
    with pytest.raises(cj.InvalidTransition, match="REJECTED"):
        adm.open_research_trade(path, persisted, **own(path), model_lots=3, entry_spot=1, stop_spot=1,
                                target_spot=1)
    with pytest.raises(cj.InvalidTransition, match="rejected"):
        cj.open_trade(path, **own(path), admission_id=persisted["admission"]["admission_id"], model_lots=3,
                      entry_spot=1, stop_spot=1, target_spot=1)
    assert counts(path) == (1, 0, 0, 0)
    assert adm.persist(path, run(candidate=replace(CANDIDATE, direction="LONG")), **own(path)) is None
    assert counts(path) == (1, 0, 0, 0)


@pytest.mark.parametrize("quote,now", [
    (replace(QUOTE, ltp=Decimal("NaN")), NOW),
    (replace(QUOTE, ltp=120.5), NOW),
    (replace(QUOTE, fetched_at=NOW.replace(tzinfo=None)), NOW),
    (replace(QUOTE, fetched_at=NOW + timedelta(seconds=1)), NOW),
    (replace(QUOTE, strike="26100"), NOW),
    ({"ltp": 120}, NOW),
    (QUOTE, NOW.replace(tzinfo=None)),
    (QUOTE, datetime(2026, 11, 6, 10, 14, tzinfo=IST)),
])
def test_15_malformed_market_data_fails_closed(quote, now):
    result = run(quote=quote, now=now)
    assert result.admission_class == "REJECTED"
    assert result.reasons[0] in (adm.MARKET_DATA_CORRUPT, adm.CONTRACT_INVALID)
    assert run(now=datetime(2026, 11, 7, 10, 0, tzinfo=IST)).reasons == (adm.SIGNAL_STALE,)


def test_16_admission_persistence_failure_opens_no_trade(tmp_path, monkeypatch):
    path = ready(tmp_path)

    def boom(*_, **__):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(cj, "record_admission", boom)
    with pytest.raises(sqlite3.OperationalError):
        track(path, run())
    assert counts(path) == (0, 0, 0, 0)
    monkeypatch.undo()
    with pytest.raises(cj.InvalidTransition, match="unknown admission"):
        cj.open_trade(path, **own(path), admission_id=f"A_20261106_{run().signal_id[:12]}", model_lots=3,
                      entry_spot=1, stop_spot=1, target_spot=1)
    assert counts(path) == (0, 0, 0, 0)


def test_17_unsupported_research_reason_is_refused_by_schema(tmp_path):
    path = ready(tmp_path)
    bogus = replace(run(), reasons=("LOOKS_GOOD",))
    with pytest.raises(cj.InvalidTransition, match="RESEARCH_ONLY allows only"):
        adm.persist(path, bogus, **own(path))
    with pytest.raises(cj.InvalidTransition):
        adm.persist(path, replace(run(), admission_class="ACTIONABLE", reasons=("LOOKS_GOOD",)), **own(path))
    assert counts(path)[0] == 0


def test_18_no_telegram_broker_or_network(tmp_path, monkeypatch):
    imported = set()
    for node in ast.walk(ast.parse(MODULE.read_text())):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert not any(name.startswith(("app.alerts", "app.brokers", "app.trading.crew", "app.trading.scanner",
                                    "httpx", "requests", "urllib", "telegram")) for name in imported)

    def blocked(*_, **__):
        raise AssertionError("network call during admission")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    track(ready(tmp_path), run())


def test_19_production_files_are_unchanged(production_db_opens):
    assert hashlib.sha256(PROD_DB.read_bytes()).hexdigest() == PROD_HASH
    assert hashlib.sha256(OCT30.read_bytes()).hexdigest() == OCT30_HASH
    with pytest.raises(RuntimeError, match="production database"):
        adm.persist(PROD_DB, run(), lease=cs.Lease("S1", DAY, 1, "unused"), now=NOW)
    production_db_opens.clear()


def test_20_frozen_v0_strategy_sources_are_untouched():
    repo = MODULE.parents[3]
    if shutil.which("git") is None or not (repo / ".git").exists():
        pytest.skip("git checkout not available")
    frozen = ["backend/app/trading/crew", "backend/app/trading/scanner_crew", "backend/app/strategies"]
    diff = subprocess.run(["git", "diff", "--quiet", "HEAD", "--", *frozen], cwd=repo, check=False)
    assert diff.returncode == 0
