"""S1-P7: read-only crew API, research statistics, legacy separation and the research export."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import openpyxl
import pytest

from app.api import crew as crew_api
from app.auth import hash_token
from app.config import get_settings
from app.db import crew_journal as cj
from app.db import crew_reader, trade_store
from app.db import crew_session as cs
from app.main import app
from app.reports import crew_export

DAY = "2026-11-06"
NOW = datetime.fromisoformat(f"{DAY}T10:15:00+05:30")
VER, POLICY = "scanner-v0", "research-exec-v1-monotonic-stop"
TOKEN = "crew-reader-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
BACKEND = Path(__file__).resolve().parents[1]
ROUTES = ("/api/live/crew/trades", "/api/live/crew/admissions", "/api/live/crew/exceptions",
          "/api/live/crew/sessions", f"/api/live/crew/stats?strategy_version={VER}&execution_policy={POLICY}")
MONEY_KEYS = ("pnl", "premium", "roi", "capital", "inr", "fill", "rupee")


class Journal:
    def __init__(self, path: Path):
        self.path = path
        cj.migrate(path)
        self.lease = cs.acquire(path, now=NOW, ttl_seconds=3600)
        self.n = 0

    def trade(self, *, strategy="Sweep", version=VER, policy=POLICY, cls="RESEARCH_ONLY", r=None, opened=True):
        self.n += 1
        reasons = {"ACTIONABLE": [], "RESEARCH_ONLY": ["CAPITAL_UNAVAILABLE"], "REJECTED": ["QUOTE_MISSING"]}[cls]
        candle = f"{DAY}T{9 + self.n // 60:02d}:{self.n % 60:02d}:00+05:30"
        a = cj.record_admission(self.path, lease=self.lease, now=NOW, admission_class=cls, reasons=reasons,
                                market_day=DAY, instrument="NIFTY", strategy_id=strategy, strategy_version=version,
                                signal_candle=candle, direction="CALL", setup_id=f"setup-{self.n}")
        if cls == "REJECTED" or not opened:
            return None
        trade = cj.open_trade(self.path, lease=self.lease, now=NOW, admission_id=a["admission"]["admission_id"],
                              model_lots=3, entry_spot=23100, stop_spot=23080, target_spot=23160,
                              execution_policy=policy)["trade"]
        if r is not None:
            cj.exit_trade(self.path, lease=self.lease, now=NOW, trade_id=trade["trade_id"], expected_version=1,
                          reason="TARGET", signal_candle=f"{DAY}T11:00:00+05:30", index_levels={"spot": 23140.0},
                          model_points=r * 20, model_r=r)
        return trade["trade_id"]

    def new_session(self):
        cs.release(self.path, self.lease, now=NOW)
        self.lease = cs.acquire(self.path, now=NOW, ttl_seconds=3600)


def build(tmp_path: Path) -> Journal:
    folder = tmp_path / "crew"
    folder.mkdir(exist_ok=True)
    j = Journal(folder / "journal.db")
    for r in (2.0, -1.0, 0.0):
        j.trade(r=r)
    j.orphan = j.trade()                          # open, owner session ends below
    j.trade(opened=False)                         # admission with no model entry
    j.trade(cls="REJECTED")
    j.trade(policy="research-exec-v0-other", r=5.0)
    j.trade(version="scanner-v1", r=9.0)
    j.trade(strategy="Breakout", r=3.0)
    j.new_session()
    j.live = j.trade(strategy="Breakout")         # open, owned by the lease holder
    return j


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def keys(value) -> set[str]:
    if isinstance(value, dict):
        return {str(k) for k in value} | {k for v in value.values() for k in keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in keys(v)}
    return set()


@pytest.fixture
def auth_on(monkeypatch):
    monkeypatch.setenv("API_TOKENS", f"reader:{hash_token(TOKEN)}")
    get_settings.cache_clear()
    yield AUTH
    get_settings.cache_clear()


@pytest.fixture
def journal(tmp_path, monkeypatch):
    j = build(tmp_path)
    monkeypatch.setenv("CREW_JOURNAL_PATH", str(j.path))
    monkeypatch.setattr(crew_reader, "now_ist", lambda: NOW)
    get_settings.cache_clear()
    return j


@pytest.fixture
def api(client, journal, auth_on):
    def get(url, **params):
        response = client.get(url, params=params or None, headers=AUTH)  # httpx drops the URL query for {}
        assert response.status_code == 200, response.text
        return response.json()
    return get


def group(body, sid):
    return next(g for g in body["groups"] if g["strategy_id"] == sid)


# 1, 2: authentication


def test_01_every_crew_route_needs_a_valid_token(client, journal, auth_on):
    for url in ROUTES + (f"/api/live/crew/trades/{journal.orphan}/events",):
        assert client.get(url).status_code == 401, url
        assert client.get(url, headers={"Authorization": "Bearer wrong"}).status_code == 401, url
        assert client.get(url, headers={"Authorization": f"Basic {TOKEN}"}).status_code == 401, url
        assert client.get(url, headers=AUTH).status_code == 200, url
    # Malformed parameters are still refused for authentication first.
    assert client.get("/api/live/crew/trades?limit=-5&state=BOGUS").status_code == 401


def test_02_unconfigured_authentication_fails_closed(client, journal):
    assert not get_settings().token_hashes
    for url in ROUTES:
        response = client.get(url)
        assert response.status_code == 403, url
        assert "authentication" in response.json()["detail"]


def test_02b_auth_is_a_router_dependency_and_routes_are_get_only():
    paths = {p: set(ops) for p, ops in app.openapi()["paths"].items() if p.startswith("/api/live/crew")}
    assert paths == {p: {"get"} for p in ("/api/live/crew/trades", "/api/live/crew/trades/{trade_id}/events",
                                          "/api/live/crew/admissions", "/api/live/crew/stats",
                                          "/api/live/crew/exceptions", "/api/live/crew/sessions",
                                          "/api/live/crew/paper/stats")}
    assert [d.dependency.__name__ for d in crew_api.router.dependencies] == ["require_auth"]
    for route in crew_api.router.routes:
        assert route.methods == {"GET"}
        assert any(d.call.__name__ == "require_auth" for d in route.dependant.dependencies)


# 3, 4, 5: read-only, missing and incompatible journals


def test_03_reader_never_writes_migrates_recovers_or_leases(api, journal, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("the reader must not write")
    for module, name in ((cj, "connect"), (cj, "migrate"), (cj, "transaction"), (cs, "acquire"),
                         (cs, "recover"), (cs, "heartbeat"), (cs, "release")):
        monkeypatch.setattr(module, name, forbidden)
    before = sha(journal.path)
    wal = journal.path.with_name("journal.db-wal")
    wal_before = wal.read_bytes() if wal.exists() else b""
    files = set(os.listdir(journal.path.parent))
    for url in ROUTES:
        api(url)
    api(f"/api/live/crew/trades/{journal.orphan}/events")
    assert sha(journal.path) == before
    assert (wal.read_bytes() if wal.exists() else b"") == wal_before  # no checkpoint, no appended frames
    assert set(os.listdir(journal.path.parent)) <= files | {"journal.db-wal", "journal.db-shm"}
    with crew_reader.snapshot(journal.path) as conn, pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO crew_meta (key, value, set_at) VALUES ('x', 'y', 'z')")


def _crashed_writer(path: Path) -> None:
    """Commit one row into the WAL with autocheckpoint off, then die without closing (no checkpoint)."""
    code = (f"import sqlite3, os; c = sqlite3.connect({str(path)!r}); c.execute('PRAGMA wal_autocheckpoint=0');"
            "c.execute(\"INSERT INTO crew_meta (key, value, set_at) VALUES ('probe', '1', 'x')\"); c.commit();"
            " os._exit(0)")
    subprocess.run([sys.executable, "-c", code], check=True)


@pytest.mark.parametrize("sidecars", ["none", "wal_and_shm", "wal_without_shm"])
def test_03b_wal_sidecars_never_touch_the_journal_or_its_wal(tmp_path, sidecars):
    # Documented limit: SQLite mode=ro may create an empty -wal/-shm and rewrites the -shm wal-index while
    # reading. The journal and every committed WAL frame stay byte-identical: no checkpoint, recovery or write.
    j = Journal(tmp_path / "journal.db")
    j.trade(r=1.0)
    cs.release(j.path, j.lease, now=NOW)
    wal, shm = j.path.with_name("journal.db-wal"), j.path.with_name("journal.db-shm")
    if sidecars == "none":
        assert not wal.exists() and not shm.exists()
    else:
        _crashed_writer(j.path)
        if sidecars == "wal_without_shm":
            shm.unlink()
    db_before, wal_before = sha(j.path), (wal.read_bytes() if wal.exists() else b"")
    events_before = None
    with crew_reader.snapshot(j.path) as conn:
        probe = conn.execute("SELECT count(*) FROM crew_meta WHERE key='probe'").fetchone()[0]
        events_before = conn.execute("SELECT count(*) FROM crew_session_events").fetchone()[0]
        crew_reader.stats(conn, strategy_version=VER, execution_policy=POLICY, now=NOW)
        crew_reader.exceptions(conn, now=NOW)
    assert probe == (0 if sidecars == "none" else 1)  # committed WAL frames are visible
    assert sha(j.path) == db_before
    assert (wal.read_bytes() if wal.exists() else b"") == wal_before
    assert set(os.listdir(tmp_path)) <= {"journal.db", "journal.db-wal", "journal.db-shm"}
    with crew_reader.snapshot(j.path) as conn:
        assert conn.execute("SELECT count(*) FROM crew_session_events").fetchone()[0] == events_before


def test_04_missing_or_unconfigured_journal_is_503_and_never_created(client, auth_on, tmp_path, monkeypatch):
    for configured in ("", str(tmp_path / "nowhere" / "journal.db")):
        monkeypatch.setenv("CREW_JOURNAL_PATH", configured)
        get_settings.cache_clear()
        for url in ROUTES:
            response = client.get(url, headers=AUTH)
            assert response.status_code == 503, url
            assert response.json()["detail"]["status"] == ("NOT_CONFIGURED" if not configured else "MISSING")
            assert str(tmp_path) not in response.text
    assert not (tmp_path / "nowhere").exists()


def test_05_incompatible_or_corrupt_files_are_refused_unchanged(client, auth_on, tmp_path, monkeypatch):
    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.execute("CREATE TABLE live_and_forward_trades (trade_id TEXT)")
    conn.close()
    shared = Journal(tmp_path / "shared.db").path
    conn = sqlite3.connect(shared)
    conn.execute("CREATE TABLE live_and_forward_trades (trade_id TEXT)")
    conn.close()
    old = Journal(tmp_path / "old.db").path
    conn = sqlite3.connect(old)
    conn.execute("UPDATE crew_meta SET value='2' WHERE key='schema_version'")
    conn.commit()
    conn.close()
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"not a database" * 100)
    for path, status in ((legacy, "INCOMPATIBLE"), (shared, "INCOMPATIBLE"), (old, "INCOMPATIBLE"),
                         (garbage, "UNREADABLE")):
        before = sha(path)
        monkeypatch.setenv("CREW_JOURNAL_PATH", str(path))
        get_settings.cache_clear()
        response = client.get("/api/live/crew/trades", headers=AUTH)
        assert response.status_code == 503 and response.json()["detail"]["status"] == status, path
        assert str(tmp_path) not in response.text
        assert sha(path) == before


# 6: inconsistent journal


def test_06_inconsistent_journal_withholds_statistics(api, journal):
    conn = cj.connect(journal.path)
    conn.execute("DROP TRIGGER IF EXISTS crew_events_no_delete")
    conn.execute("DELETE FROM crew_events WHERE trade_id=?", (journal.orphan,))
    conn.close()
    body = api("/api/live/crew/stats", strategy_version=VER, execution_policy=POLICY)
    assert body["statistics_withheld"] is True and body["groups"] == []
    assert body["consistency"]["ok"] is False
    assert [f["kind"] for f in body["consistency"]["findings"]] == ["missing_entry"]
    exc = api("/api/live/crew/exceptions")
    assert exc["consistency"]["ok"] is False and exc["consistency"]["count"] == 1


# 7, 8, 9, 10, 11: population integrity


def test_07_strategy_ids_stay_separate(api):
    body = api("/api/live/crew/stats", strategy_version=VER, execution_policy=POLICY)
    assert [g["strategy_id"] for g in body["groups"]] == ["Breakout", "Sweep"]
    assert group(body, "Breakout")["results"]["sum_r"] == 3.0
    assert group(body, "Sweep")["results"]["sum_r"] == 1.0
    requested = api("/api/live/crew/stats", strategy_version=VER, execution_policy=POLICY,
                    strategy_id=["Sweep", "Breakout", "Sweep"])
    assert [g["strategy_id"] for g in requested["groups"]] == ["Sweep", "Breakout"]
    unknown = api("/api/live/crew/stats", strategy_version=VER, execution_policy=POLICY, strategy_id=["Nope"])
    assert unknown["groups"][0]["population"]["admissions_total"] == 0


def test_08_strategy_versions_stay_separate(api):
    v1 = group(api("/api/live/crew/stats", strategy_version="scanner-v1", execution_policy=POLICY), "Sweep")
    assert v1["results"]["closed_eligible"] == 1 and v1["results"]["sum_r"] == 9.0
    assert v1["population"]["admissions_total"] == 1
    v0 = group(api("/api/live/crew/stats", strategy_version=VER, execution_policy=POLICY), "Sweep")
    assert 9.0 not in [v0["results"]["sum_r"]] and v0["results"]["closed_eligible"] == 3


def test_09_execution_policies_stay_separate(api):
    other = group(api("/api/live/crew/stats", strategy_version=VER, execution_policy="research-exec-v0-other"),
                  "Sweep")
    assert other["results"]["closed_eligible"] == 1 and other["results"]["sum_r"] == 5.0
    main = group(api("/api/live/crew/stats", strategy_version=VER, execution_policy=POLICY), "Sweep")
    assert main["not_in_population"] == {"other_execution_policy": 1, "execution_policy_missing": 0}
    assert main["results"]["sum_r"] == 1.0


def test_10_orphans_and_missing_entries_are_excluded(api, journal):
    sweep = group(api("/api/live/crew/stats", strategy_version=VER, execution_policy=POLICY), "Sweep")
    assert sweep["exclusions"] == {"ORPHANED": 1}
    assert sweep["population"]["orphaned"] == 1 and sweep["population"]["missing_model_entries"] == 1
    breakout = group(api("/api/live/crew/stats", strategy_version=VER, execution_policy=POLICY), "Breakout")
    assert breakout["exclusions"] == {"OPEN": 1} and breakout["population"]["open"] == 1
    exc = api("/api/live/crew/exceptions")
    assert [t["trade_id"] for t in exc["orphaned_trades"]["items"]] == [journal.orphan]
    assert exc["missing_model_entries"]["count"] == 1
    assert exc["lease_holder"] == journal.lease.session_id
    assert exc["unknown_publications"] == {"count": 0, "items": []}


def test_11_population_denominators_and_coverage(api):
    sweep = group(api("/api/live/crew/stats", strategy_version=VER, execution_policy=POLICY), "Sweep")
    assert sweep["population"] == {
        "admission_scope": "strategy_id and strategy_version (admissions precede any execution policy)",
        "admissions_total": 7, "admissions_by_class": {"ACTIONABLE": 0, "RESEARCH_ONLY": 6, "REJECTED": 1},
        "unique_signals": 7, "applicable_signals": 6, "missing_model_entries": 1, "model_trades_opened": 4,
        "open": 0, "orphaned": 1, "open_prior_day": 0, "expired_unresolved": 0, "closed_eligible": 3,
    }
    assert sweep["coverage"] == {
        "closed_eligible_of_opened": {"numerator": 3, "denominator": 4, "ratio": 0.75},
        "entered_of_applicable_signals": {"numerator": 5, "denominator": 6, "ratio": 0.8333},
    }
    results = sweep["results"]
    assert (results["wins"], results["losses"], results["flat"], results["sum_points"]) == (1, 1, 1, 20.0)
    assert results["sufficient_sample"] is False and results["label"] == "INSUFFICIENT_SAMPLE"
    assert results["win_rate_pct"] is None and results["avg_r"] is None and results["avg_points"] is None


def test_11b_win_rate_needs_the_minimum_sample(tmp_path):
    assert crew_reader.MIN_SAMPLE == 20
    j = Journal(tmp_path / "big.db")
    for i in range(crew_reader.MIN_SAMPLE - 1):
        j.trade(r=1.0 if i % 4 else -1.0)
    with crew_reader.snapshot(j.path) as conn:
        below = crew_reader.stats(conn, strategy_version=VER, execution_policy=POLICY, now=NOW)["groups"][0]
    assert below["results"]["closed_eligible"] == 19 and below["results"]["win_rate_pct"] is None
    assert below["results"]["avg_r"] is None and below["results"]["label"] == "INSUFFICIENT_SAMPLE"
    j.trade(r=1.0)
    with crew_reader.snapshot(j.path) as conn:
        body = crew_reader.stats(conn, strategy_version=VER, execution_policy=POLICY, now=NOW)
    results = body["groups"][0]["results"]
    assert results["sufficient_sample"] is True and results["closed_eligible"] == 20
    assert results["win_rate_pct"] == 75.0 and results["avg_r"] == 0.5
    assert results["label"] == "DESCRIPTIVE_RESEARCH_NOT_VALIDATED"
    assert "Not validated profitability" in body["interpretation"]


def test_11c_statistics_disclose_identity_units_and_rules(api):
    body = api("/api/live/crew/stats", strategy_version=VER, execution_policy=POLICY)
    assert body["source"] == "crew_journal" and body["strategy_version"] == VER
    assert body["execution_policy"] == POLICY and body["units"] == crew_reader.UNITS
    assert body["eligibility"] == crew_reader.ELIGIBILITY and body["monetary_metrics"] == crew_reader.NO_MONEY
    for g in body["groups"]:
        assert (g["strategy_version"], g["execution_policy"]) == (VER, POLICY)


def test_11d_version_and_policy_are_required(api, client):
    assert client.get("/api/live/crew/stats", headers=AUTH).status_code == 422
    assert client.get(f"/api/live/crew/stats?strategy_version={VER}", headers=AUTH).status_code == 422
    assert client.get(f"/api/live/crew/stats?execution_policy={POLICY}", headers=AUTH).status_code == 422


# 12: no fabricated money


def test_12_no_monetary_fields_anywhere(api, journal):
    bodies = [api(url) for url in ROUTES] + [api(f"/api/live/crew/trades/{journal.orphan}/events")]
    found = {k for k in keys(bodies) if any(m in k.lower() for m in MONEY_KEYS)}
    assert found == set()


# 13: pagination and ordering


def test_13_pagination_is_deterministic_and_bounded(api, client, journal):
    full = api("/api/live/crew/trades", limit=500)["items"]
    assert len(full) == api("/api/live/crew/trades")["total"] == 8
    order = [(t["market_day"], t["opened_at"], t["trade_id"]) for t in full]
    assert order == sorted(order, reverse=True)
    pages = [api("/api/live/crew/trades", limit=3, offset=o)["items"] for o in (0, 3, 6)]
    assert [len(p) for p in pages] == [3, 3, 2]
    assert [t["trade_id"] for p in pages for t in p] == [t["trade_id"] for t in full]
    assert api("/api/live/crew/trades", limit=3, offset=3) == api("/api/live/crew/trades", limit=3, offset=3)
    assert api("/api/live/crew/trades", offset=9999)["items"] == []
    adm = api("/api/live/crew/admissions", limit=3)
    assert adm["total"] == 10 and len(adm["items"]) == 3
    for bad in ("limit=0", "limit=501", "limit=100000", "offset=-1", "limit=abc", "state=BOGUS",
                "market_day=yesterday", "market_day=2026-11-06%27%3B--", "strategy_id=" + "x" * 129):
        assert client.get(f"/api/live/crew/trades?{bad}", headers=AUTH).status_code == 422, bad
    assert client.get("/api/live/crew/trades/T_missing/events", headers=AUTH).status_code == 404
    assert client.get("/api/live/crew/trades/..%2F..%2Fetc/events", headers=AUTH).status_code in (404, 422)
    with pytest.raises(ValueError), crew_reader.snapshot(journal.path) as conn:
        crew_reader.trades(conn, now=NOW, limit=crew_reader.MAX_LIMIT + 1)


def test_13b_filters_and_events(api, journal):
    sweep = api("/api/live/crew/trades", strategy_id="Sweep", strategy_version=VER, execution_policy=POLICY)
    assert sweep["total"] == 4
    closed = api("/api/live/crew/trades", state="CLOSED", strategy_id="Breakout")
    assert [t["model_r"] for t in closed["items"]] == [3.0]
    events = api(f"/api/live/crew/trades/{journal.orphan}/events")
    assert [e["event_type"] for e in events["items"]] == ["MODEL_ENTRY"]
    assert events["items"][0]["payload"]["execution_policy"] == POLICY


# 14: no secrets or paths


def test_14_no_tokens_hosts_or_paths_leak(api, journal, tmp_path):
    legacy = tmp_path / "legacy_book.json"
    legacy.write_text(json.dumps({"TRADE_X": {"status": "OPEN", "symbol": "NIFTY", "strategy": "Legacy",
                                              "direction": "CALL", "timestamp": f"{DAY}T09:30:00+05:30",
                                              "lots": 2, "entry_price": 23000}}))
    cj.quarantine_legacy_reference(journal.path, legacy)
    bodies = [api(url) for url in ROUTES] + [api("/api/live/crew/trades/TRADE_X/events")]
    blob = json.dumps(bodies)
    conn = sqlite3.connect(journal.path)
    token_hash = conn.execute("SELECT token_sha256 FROM crew_lease").fetchone()[0]
    conn.close()
    assert str(tmp_path) not in blob and "/Users/" not in blob and str(Path.home()) not in blob
    assert token_hash not in blob and journal.lease.token not in blob
    assert not keys(bodies) & {"token", "token_sha256", "legacy_ref", "pid", "host", "body", "chat_id"}
    quarantined = api("/api/live/crew/exceptions")["quarantined_trades"]
    assert quarantined["count"] == 1 and quarantined["items"][0]["legacy"] is True
    assert crew_reader.redact({"note": "bot123:ABC_def at /Users/x/genesis/data/trades.db"}) == \
        {"note": "[redacted] at [path]"}


# 15: no scanner, market feed, Telegram or broker code


def test_15_no_scanner_feed_telegram_or_broker_imports():
    code = ("import sys, app.api.crew, app.db.crew_reader, app.reports.crew_export;"
            "bad = ('app.brokers', 'app.telegram', 'app.market', 'app.trading.scanner', 'app.trading.research',"
            " 'app.trading.crew', 'app.db.crew_outbox', 'app.db.crew_session', 'app.liquidity', 'yfinance', 'growwapi');"
            "print([m for m in sys.modules if m.startswith(bad)])")
    out = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, capture_output=True, text=True, check=True,
                         env={**os.environ, "PYTHONPATH": str(BACKEND)})
    assert out.stdout.strip() == "[]", out.stdout + out.stderr


def test_15b_sessions_show_generations_takeover_and_recovery(api, journal):
    later = datetime.fromisoformat(f"{DAY}T12:30:00+05:30")
    takeover = cs.acquire(journal.path, now=later, ttl_seconds=3600)
    cs.recover(journal.path, takeover, now=later)
    body = api("/api/live/crew/sessions")
    assert body["lease"]["generation"] == 3 and "token_sha256" not in body["lease"]
    assert [e["event_type"] for e in body["events"]["items"]][:2] == ["RECOVERED", "TAKEOVER"]
    orphaned = {t["trade_id"] for t in body["events"]["items"][0]["detail"]["orphaned_open"]}
    assert orphaned == {journal.orphan, journal.live}
    assert body["sessions"]["total"] == 3
    assert api("/api/live/crew/sessions", limit=1, events_offset=1)["events"]["items"][0]["event_type"] == "TAKEOVER"


# 16: legacy endpoint contract


LEGACY_READS = ("/api/trades/forward-tested", "/api/journal", "/api/reports")
WIN = {"date": "2026-11-02", "time": "10:00", "strategy": "Liquidity Sweep", "side": "LONG", "status": "WIN",
       "entry": 23000, "stop": 22980, "target": 23040, "points": 40, "r_multiple": 2.0, "entry_premium": 100,
       "exit_premium": 130, "lots": 2, "est_pnl_inr": 3900, "capital_deployed": 13000, "exit_value": 16900}


def test_16_forward_tested_contract_is_kept_with_a_legacy_notice(client, auth_on):
    trade_store.upsert_trade(WIN)
    response = client.get("/api/trades/forward-tested", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert {"metrics", "strategy_metrics", "trades"} <= set(body)
    assert body["metrics"]["total_trades"] >= 1 and body["metrics"]["net_pnl_inr"] == 3900
    assert body["source"] == "legacy_trade_store" and body["verified_option_pnl"] is False
    assert body["data_source"] == "sqlite" and body["source_warnings"] == []
    assert "not broker fills or realized returns" in body["warning"]
    assert "estimated or unverified" in body["warning"]


def test_16a_legacy_reads_need_a_token_once_auth_is_enabled(client, auth_on):
    for url in LEGACY_READS:
        assert client.get(url).status_code == 401, url
        assert client.get(url, headers={"Authorization": "Bearer wrong"}).status_code == 401, url
        assert client.get(url, headers=AUTH).status_code == 200, url
    assert client.get("/api/reports/nope", headers=AUTH).status_code == 404
    assert client.get("/api/reports/nope").status_code == 401
    assert client.get("/api/health").status_code == 200  # unrelated public reads are unchanged


def test_16a2_legacy_reads_stay_public_while_auth_is_not_configured(client):
    # Rollout step: nothing changes until a token or login exists, so no current client breaks.
    assert not get_settings().token_hashes
    for url in LEGACY_READS:
        assert client.get(url).status_code == 200, url


def test_16b_missing_pnl_is_unknown_and_never_answered_from_excel(client, monkeypatch):
    trade_store.upsert_trade(WIN)
    # The writer stores NULL P&L for index-point-only trades; this used to raise and silently switch to Excel.
    trade_store.upsert_trade({**WIN, "time": "11:00", "strategy": "Trendline", "pnl_method": "index_points_not_priced"})
    import openpyxl
    monkeypatch.setattr(openpyxl, "load_workbook", lambda *a, **k: pytest.fail("Excel must not be read"))
    body = client.get("/api/trades/forward-tested").json()
    assert body["data_source"] == "sqlite"
    m = body["metrics"]
    assert m["missing_values"] == {"est_pnl_inr": 1}
    assert m["net_pnl_inr"] is None and m["total_roi_pct"] is None and m["profit_factor"] is None
    assert body["strategy_metrics"]["Trendline Reversal"]["net_pnl_inr"] is None
    assert body["strategy_metrics"]["Liquidity Sweep"]["net_pnl_inr"] == 3900
    assert m["total_trades"] == 2 and m["wins"] == 2 and m["total_r"] == 4.0
    unpriced = next(t for t in body["trades"] if t["strategy"] == "Trendline Reversal")
    assert unpriced["est_pnl_inr"] is None and unpriced["roi_pct"] is None


def test_16c_a_failed_sqlite_read_is_reported_not_replaced_by_excel(client, monkeypatch):
    import openpyxl
    monkeypatch.setattr(trade_store, "get_all_trades", lambda **k: (_ for _ in ()).throw(sqlite3.OperationalError("x")))
    monkeypatch.setattr(openpyxl, "load_workbook", lambda *a, **k: pytest.fail("Excel must not be read"))
    response = client.get("/api/trades/forward-tested")
    assert response.status_code == 200
    body = response.json()
    assert body["data_source"] == "unavailable" and body["source_error"] == "OperationalError"
    assert (body["metrics"], body["strategy_metrics"], body["trades"]) == ({}, {}, [])
    assert body["source_warnings"] and body["verified_option_pnl"] is False


def test_16d_compute_metrics_keeps_missing_values_unknown():
    rows = [{**WIN, "strategy": "A"}, {**WIN, "strategy": "A", "r_multiple": -1.0, "est_pnl_inr": -1300},
            {**WIN, "strategy": "B", "r_multiple": None, "points": None, "est_pnl_inr": None}]
    out = trade_store.compute_metrics(rows)
    m = out["metrics"]
    assert (m["total_trades"], m["wins"], m["losses"], m["win_rate"]) == (3, 1, 1, 50.0)
    assert m["net_pnl_inr"] is None and m["total_r"] is None and m["total_points"] is None
    assert m["profit_factor"] is None and m["total_roi_pct"] is None
    assert m["total_capital_deployed"] == 39000  # every row has it
    assert m["missing_values"] == {"r_multiple": 1, "points": 1, "est_pnl_inr": 1}
    a, b = out["strategy_metrics"]["A"], out["strategy_metrics"]["B"]
    assert (a["net_pnl_inr"], a["total_r"], a["roi_pct"]) == (2600, 1.0, 10.0)
    assert (b["net_pnl_inr"], b["total_r"], b["roi_pct"], b["win_rate"]) == (None, None, None, 0.0)
    complete = trade_store.compute_metrics(rows[:2])["metrics"]
    assert complete["missing_values"] == {} and complete["net_pnl_inr"] == 2600
    assert complete["profit_factor"] == 3.0


# 17, 18: export location and protected files


def protected_hashes() -> dict[str, str]:
    out = {}
    for d in crew_reader.PROTECTED_DIRS:
        for f in sorted(d.rglob("*")) if d.exists() else []:
            if f.is_file() and not f.name.endswith(("-wal", "-shm")):
                out[str(f)] = sha(f)
    return out


def test_17_export_writes_only_a_new_file_outside_protected_dirs(journal, tmp_path):
    before_protected = protected_hashes()
    before = sha(journal.path)
    out = crew_export.export(journal.path, tmp_path / "crew.xlsx", strategy_version=VER, execution_policy=POLICY,
                             now=NOW)
    wb = openpyxl.load_workbook(out)
    assert wb.sheetnames == ["Stats", "Model Trades", "Exceptions"]
    rows = list(wb["Stats"].iter_rows(values_only=True))
    start = rows.index(("strategy_id", "section", "metric", "value"))
    # One block per strategy_id: every metric row names exactly one strategy, none is an aggregate.
    assert {r[0] for r in rows[start + 1:]} == {"Breakout", "Sweep"}
    sum_r = {r[0]: r[3] for r in rows[start + 1:] if r[1:3] == ("results", "sum_r")}
    assert sum_r == {"Breakout": 3.0, "Sweep": 1.0}
    header = [c.value for c in wb["Model Trades"][1]]
    assert not [h for h in header if any(m in h.lower() for m in MONEY_KEYS)]
    assert wb["Model Trades"].max_row == 1 + 6  # Sweep x4 and Breakout x2 under scanner-v0 / this policy
    assert sha(journal.path) == before
    existing = tmp_path / "existing.xlsx"
    existing.write_bytes(b"keep me")
    link = tmp_path / "link"
    link.symlink_to(trade_store.ROOT_DIR / "trades", target_is_directory=True)
    refused = [trade_store.ROOT_DIR / "trades" / "crew.xlsx", trade_store.ROOT_DIR / "data" / "crew.xlsx",
               trade_store.ROOT_DIR / "reports" / "crew.xlsx", BACKEND / "data" / "crew.xlsx",
               link / "crew.xlsx", existing, out, tmp_path / "crew.csv", tmp_path / "missing" / "crew.xlsx"]
    for target in refused:
        with pytest.raises(crew_export.ExportRefused):
            crew_export.export(journal.path, target, strategy_version=VER, execution_policy=POLICY, now=NOW)
    assert existing.read_bytes() == b"keep me"
    assert not (trade_store.ROOT_DIR / "trades" / "crew.xlsx").exists()
    assert protected_hashes() == before_protected


def test_18_cli_refuses_a_missing_journal_without_creating_it(tmp_path, capsys):
    code = crew_export.main(["--journal", str(tmp_path / "none.db"), "--out", str(tmp_path / "x.xlsx"),
                             "--strategy-version", VER, "--execution-policy", POLICY])
    assert code == 2 and "does not exist" in capsys.readouterr().out
    assert not (tmp_path / "none.db").exists() and not (tmp_path / "x.xlsx").exists()
