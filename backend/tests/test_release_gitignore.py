"""Release ignore rules protect local artifacts without hiding source or lockfiles."""
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required to verify ignore semantics")
def test_release_ignore_boundaries(tmp_path):
    root = Path(__file__).resolve().parents[2]
    (tmp_path / ".gitignore").write_bytes((root / ".gitignore").read_bytes())
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    protected = [
        "broker.credentials.properties", "backend/private/broker.credentials.properties",
        ".containment_evidence_example/evidence.json", "logs/session.txt", "backend/logs/session.txt",
        "backend/research/out/result.json", "backend/data/strategies/private.md",
        "backend/data/reports/run.json", "backend/data/sandbox/run.json", "backend/data/logs/session.txt",
        "backend/data/intraday/session.csv", "backend/data/nse/raw/archive.zip",
        "DocumentsAndDiagrams/scanner_v01_phase4_replay/runs/session.json",
        "backend/local.sqlite", "backend/local.sqlite-wal", "backend/local.sqlite3",
        "backend/local.sqlite3-shm", "backend/.venv-dev/bin/python", "backend/session.log",
        "backend/server.pid", "trades/journal.json", "backend/.env", "backend/data/state.db-wal",
    ]
    source = [
        "backend/app/config.py", "backend/tests/test_paper_risk.py", "backend/uv.lock",
        "frontend/package-lock.json", "backend/data/holidays/nse.yaml", "backend/data/lot_sizes/nse.yaml",
        "backend/data/events/synthetic.yaml", "backend/scripts/generate_synthetic_fixtures.py",
        "docs/unpublished-review.md", "backend/research/strategies/liquidity-sweep.md",
    ]
    result = subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "core.excludesFile=/dev/null",
         "check-ignore", "--no-index", "--stdin"],
        input="\n".join(protected + source) + "\n", text=True, capture_output=True, check=True,
    )
    assert set(result.stdout.splitlines()) == set(protected)
