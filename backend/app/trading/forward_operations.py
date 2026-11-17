"""Read-only operational evidence alongside the paper journal, never a trading authority."""
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


def session_evidence(journal, day, observation):
    """Observed current-journal lifecycle; never manufacture a signal to satisfy verification."""
    result = {"market_day": day, "state": "AWAITING_MARKET_DATA", "verified": False,
              "scope": "Technical paper lifecycle only; not evidence of strategy profitability"}
    if not observation or observation.get("market_day") != day or observation.get("source") != "upstox_v3_full_live_snapshot":
        return result
    result.update(observation=observation, state="MARKET_DATA_OBSERVED")
    try:
        conn = sqlite3.connect(Path(journal).as_uri() + "?mode=ro", uri=True)
        try:
            opened = conn.execute("SELECT count(*) FROM crew_paper_trades WHERE market_day=?", (day,)).fetchone()[0]
            closed = conn.execute("SELECT count(*) FROM crew_paper_trades WHERE market_day=? AND state='CLOSED'", (day,)).fetchone()[0]
            delivered = conn.execute("""SELECT count(*) FROM crew_paper_trades t
                WHERE t.market_day=? AND t.state='CLOSED'
                AND EXISTS (SELECT 1 FROM crew_outbox o WHERE o.kind='OPS' AND o.state='ACKED'
                    AND o.purpose LIKE 'paper|PAPER_ENTRY_FILLED|' || t.paper_trade_id || '|%')
                AND EXISTS (SELECT 1 FROM crew_outbox o WHERE o.kind='OPS' AND o.state='ACKED' AND (
                    o.purpose LIKE 'paper|PAPER_FINAL_EXIT|' || t.paper_trade_id || '|%' OR
                    o.purpose LIKE 'paper|PAPER_STOP_LOSS|' || t.paper_trade_id || '|%' OR
                    o.purpose LIKE 'paper|PAPER_TARGET_HIT|' || t.paper_trade_id || '|%'))""", (day,)).fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        result["state"] = "JOURNAL_UNAVAILABLE"
        return result
    result.update(paper_entries=opened, paper_closures=closed, telegram_confirmed_lifecycles=delivered)
    if opened:
        result["state"] = "PAPER_FILL_OBSERVED"
    if closed and delivered:
        result.update(state="PAPER_LIFECYCLE_AND_TELEGRAM_OBSERVED", verified=True)
    return result


def report(journal: str, *, now: datetime | None = None) -> dict:
    if not journal:
        return {"runner": "NOT_CONFIGURED", "live_orders": False}
    root = Path(journal).parent
    result = {"live_orders": False, "runner": "NO_HEARTBEAT", "session_verified": False}
    for name in ("status", "reconciliation", "strategy-readiness", "upstox-check"):
        path = root / (name + ".json")
        try:
            value = json.loads(path.read_text())
            if isinstance(value, dict):
                result[name.replace("-", "_")] = value
        except (OSError, ValueError):
            continue
    status = result.get("status", {})
    result["session_verified"] = bool(status.get("session_evidence", {}).get("verified"))
    try:
        age = ((now or datetime.now(UTC)) - datetime.fromisoformat(status["updated_at"])).total_seconds()
        result["runner"] = status["state"] if 0 <= age <= 90 else "HEARTBEAT_STALE"
        result["heartbeat_age_seconds"] = round(age, 1)
    except (KeyError, ValueError, TypeError):
        pass
    return result
