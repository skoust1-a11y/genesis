"""Print the protected-file manifest (sha256 and repo-relative path, sorted): one stable scope for every S1 gate.

    backend/.venv/bin/python backend/scripts/protected_manifest.py > /tmp/before.sha
    ...
    backend/.venv/bin/python backend/scripts/protected_manifest.py | diff /tmp/before.sha - && echo identical

Scope: every file under data/, trades/, reports/, backend/data/ and backend/trades/; JSON under logs/; every
*.db, *.db-wal, *.db-shm, *.sqlite* and *.xlsx elsewhere in the repo; and the frozen V0 sources. Dependency,
VCS, cache and graph folders are skipped. Paths have no "./" prefix, so each file is listed exactly once.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIRS = ("data/", "trades/", "reports/", "backend/data/", "backend/trades/")
FROZEN = ("backend/scripts/backtest_pdc_15ema.py", "backend/app/trading/crew/", "backend/app/trading/scanner_crew/",
          "backend/app/strategies/")
SKIP = {".git", "node_modules", "vendor", "graphify-out", ".venv", "venv", "__pycache__", ".next"}


def protected(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return (rel.startswith(DIRS + FROZEN) or (rel.startswith("logs/") and ".json" in name)
            or name.endswith((".db", ".db-wal", ".db-shm", ".xlsx")) or ".sqlite" in name)


def manifest() -> list[tuple[str, str]]:
    out = []
    for folder, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP]
        for name in names:
            path = Path(folder) / name
            rel = path.relative_to(ROOT).as_posix()
            if protected(rel) and path.is_file():
                out.append((hashlib.sha256(path.read_bytes()).hexdigest(), rel))
    return sorted(out, key=lambda row: row[1])


if __name__ == "__main__":
    for digest, rel in manifest():
        print(f"{digest}  {rel}")
