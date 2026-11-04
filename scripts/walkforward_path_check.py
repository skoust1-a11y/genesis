#!/usr/bin/env python3
"""Walk-forward the path check on closed trades that have a real R.

Each trade may see only 5-minute bars from before that day. The verdict is logged.
It does not veto. Blank rows with no real R are ignored.
"""

from __future__ import annotations

import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from app.trading.odds import fetch_and_store_nifty_5m, load_nifty_5m_sessions, path_check  # noqa: E402


def _real_r(row: sqlite3.Row) -> bool:
    r = row["r_multiple"]
    entry = row["entry_spot"] or 0
    if r is None or entry <= 0:
        return False
    # Blank shells are stored at ₹0 with no exit. A real close has an exit reason or a non-zero R.
    reason = (row["exit_reason"] or "").strip()
    return bool(reason) or float(r) != 0.0


def _bucket(strategy: str) -> str | None:
    name = strategy.lower()
    if "sweep" in name:
        return "sweep"
    if "trendline" in name:
        return "trendline"
    if "retest" in name or "breakout" in name:
        return "breakout"
    return None


def main() -> None:
    db = ROOT / "data" / "trades.db"
    stored = fetch_and_store_nifty_5m()
    print(f"Stored 5-minute bars this fetch: {stored}")
    if not db.exists():
        print("No trades database. The veto is not wired.")
        return
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "live_and_forward_trades" not in tables:
        print("No live trade table. The veto is not wired.")
        conn.close()
        return
    rows = list(conn.execute("SELECT * FROM live_and_forward_trades"))
    conn.close()
    real = [r for r in rows if _real_r(r)]
    groups: dict[str, list] = defaultdict(list)
    for row in real:
        bucket = _bucket(row["strategy"] or "")
        if bucket:
            groups[bucket].append(row)
    if not groups["sweep"]:
        print("No sweep rows with a real R.")
    scores: dict[str, list[float]] = {"leans to target": [], "leans to stop": []}
    for bucket, items in groups.items():
        print(f"{bucket}: {len(items)} rows with a real R")
        for row in items:
            day = str(row["date"])[:10]
            direction = "CALL" if str(row["side"]).upper() == "LONG" else "PUT"
            sessions = load_nifty_5m_sessions(day)
            result = path_check(
                entry=float(row["entry_spot"]),
                stop=float(row["stop_spot"]),
                target=float(row["target_spot"]),
                direction=direction,
                clock=str(row["time"] or "10:00")[-5:],
                as_of=day,
                sessions=sessions,
            )
            if result["verdict"] in scores:
                scores[result["verdict"]].append(float(row["r_multiple"]))
    def _avg(vals: list[float]) -> float | None:
        return sum(vals) / len(vals) if vals else None

    target_avg = _avg(scores["leans to target"])
    stop_avg = _avg(scores["leans to stop"])
    print(f"leans to target: n={len(scores['leans to target'])} avg R={target_avg}")
    print(f"leans to stop: n={len(scores['leans to stop'])} avg R={stop_avg}")
    if target_avg is None or stop_avg is None:
        print("Not enough of both buckets to compare. The closed book has no VIX, so odds_for has no break-even win rate and every verdict is break-even.")
    elif target_avg > stop_avg:
        print("The leans-to-target bucket beat the leans-to-stop bucket.")
    else:
        print("The leans-to-target bucket did not beat the leans-to-stop bucket.")
    sessions_on_file = load_nifty_5m_sessions("2099-01-01")
    print(f"Stored sessions available: {len(sessions_on_file)}.")
    print("The veto is not wired.")


if __name__ == "__main__":
    main()
