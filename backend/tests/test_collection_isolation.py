"""Collection guards fail closed before protected files or SQLite can be opened."""
import sys
from types import SimpleNamespace

import conftest as isolation
import pytest


@pytest.mark.parametrize("relative", [
    "data/trades.db", "data/trades.db-wal", "backend/data/market.db",
    "backend/genesis.db", "backend/live.db-shm", "trades/trades.db",
    "trades/daily_risk_config.json", "trades/.active_trades_2026-10-30.json",
    "trades/crew_journal.db", "backend/trades/parameter_proposals.jsonl",
])
def test_collection_blocks_protected_file_opens(monkeypatch, relative):
    attempts = []
    monkeypatch.setattr(isolation, "_collection_active", True)
    monkeypatch.setattr(isolation, "_protected_reads", attempts)
    path = isolation.REPO / relative
    with pytest.raises(RuntimeError, match="Collection attempted"):
        isolation._guard_collection_state("open", (str(path), "r", 0))
    assert attempts == [str(path.resolve())]
    # Swallowing an open failure must still fail collection.
    with pytest.raises(RuntimeError, match="Protected-state access during collection"):
        isolation.pytest_collection_finish(None)


def test_collection_allows_temporary_state(monkeypatch, tmp_path):
    monkeypatch.setattr(isolation, "_collection_active", True)
    path = tmp_path / "journal.db"
    path.write_bytes(b"synthetic")
    assert path.read_bytes() == b"synthetic"


def test_protected_hashing_requires_explicit_option():
    request = SimpleNamespace(config=SimpleNamespace(getoption=lambda name: False))
    with pytest.raises(pytest.skip.Exception, match="--verify-protected-state"):
        next(isolation.protected_file_hashes.__wrapped__(request))


def test_protected_hashing_detects_changes(monkeypatch, tmp_path):
    monkeypatch.setattr(isolation, "REPO", tmp_path)
    for relative in ("data/trades.db", "trades/.active_trades_2026-10-30.json"):
        path = tmp_path / relative
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"synthetic baseline")
    request = SimpleNamespace(config=SimpleNamespace(getoption=lambda name: True))
    verification = isolation.protected_file_hashes.__wrapped__(request)
    baseline = next(verification)
    assert len(baseline) == 2
    (tmp_path / "data/trades.db").write_bytes(b"changed")
    with pytest.raises(AssertionError):
        next(verification)


def test_conftest_imports_share_audit_hooks():
    assert sys.modules["tests.conftest"] is isolation
