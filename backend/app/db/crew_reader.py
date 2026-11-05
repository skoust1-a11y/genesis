"""S1-P7 read-only view of the crew journal, for the API and the research export.

Opens an existing crew-only journal with SQLite ``mode=ro`` and ``query_only``. It never creates or migrates
the schema, runs recovery, takes or renews a lease, or repairs anything, and imports nothing that fetches
quotes, publishes or trades. Each response is read inside one transaction, so it sees one WAL snapshot.
Filesystem paths, lease tokens, hosts and pids are never returned.

Statistics are model results on the underlying index (points and R). There is no option premium, fill,
rupee P&L, ROI or capital return here, and different strategy ids, versions or execution policies are never
combined.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from app.db import crew_journal as cj
from app.trading.option_quote import IST

logger = logging.getLogger("genesis.crew_reader")

MAX_LIMIT = 500
# ponytail: provisional display floor for win rate and average R (approved S1-P7); not a significance test.
MIN_SAMPLE = 20
INTERPRETATION = ("Descriptive research results over model trades. Not validated profitability, not a forecast"
                  f" and not a significance test; win rate and average R are withheld below {MIN_SAMPLE} closed"
                  " eligible trades.")
UNITS = {"points": "underlying index points", "r": "R: model points / initial index-point risk"}
NO_MONEY = "No option premium, broker fill, rupee P&L, ROI or capital return is computed or implied."
ELIGIBILITY = ("CLOSED model trade whose MODEL_ENTRY matches strategy_id, strategy_version and execution_policy,"
               " with finite model_points and model_r on its MODEL_EXIT")
APPLICABLE = "non-REJECTED admissions (ACTIONABLE or RESEARCH_ONLY) of the strategy_id and strategy_version"

_REPO = Path(__file__).resolve().parents[3]
PROTECTED_DIRS = tuple(p.resolve() for p in (
    _REPO / "data", _REPO / "trades", _REPO / "reports", _REPO / "backend" / "data", _REPO / "backend" / "trades"))
_TABLES = frozenset({"crew_meta", "crew_sessions", "crew_admissions", "crew_trades", "crew_events", "crew_outbox",
                     "crew_outbox_events", "crew_session_events", "crew_lease"})
_DROP = frozenset({"legacy_ref", "source_path", "token", "token_sha256", "pid", "host", "body", "chat_id",
                   "last_error", "response_excerpt"})
_PATH = re.compile(r"(?:[A-Za-z]:\\|~/|/)[^\s#'\"]*[/\\][^\s#'\"]*")
_BOT_TOKEN = re.compile(r"bot\d+:[A-Za-z0-9_-]+")
_ADMISSION_KEYS = ("instrument", "strategy_id", "strategy_version", "signal_candle", "direction", "setup_id")

# An open trade is OPEN only while its owner holds the live lease on its own market day.
_STATUS = """CASE WHEN t.state IN ('TRACKING','PARTIALLY_EXITED') THEN
    CASE WHEN t.market_day < :today THEN 'OPEN_PRIOR_DAY'
         WHEN t.owner_session_id IS :holder THEN 'OPEN' ELSE 'ORPHANED' END
    ELSE t.state END"""
_TRADE_SELECT = f"""SELECT t.trade_id, t.signal_id, t.admission_id, t.admission_class, t.market_day,
    t.owner_session_id, t.state, {_STATUS} AS status, t.strategy AS strategy_id, t.direction, t.contract, t.expiry,
    t.entry_spot, t.stop_spot, t.target_spot, t.model_lots_initial, t.model_lots_open, t.version, t.opened_at,
    t.closed_at, t.legacy_ref IS NOT NULL AS legacy,
    json_extract(e.payload, '$.strategy_version') AS strategy_version,
    json_extract(e.payload, '$.execution_policy') AS execution_policy,
    json_extract(x.payload, '$.model_points') AS model_points, json_extract(x.payload, '$.model_r') AS model_r,
    json_extract(x.payload, '$.reason') AS exit_reason
FROM crew_trades t
LEFT JOIN crew_events e ON e.trade_id = t.trade_id AND e.seq = 1 AND e.event_type = 'MODEL_ENTRY'
LEFT JOIN crew_events x ON x.trade_id = t.trade_id AND x.event_type = 'MODEL_EXIT'"""


class Unavailable(Exception):
    """The journal cannot be served: NOT_CONFIGURED, MISSING, INCOMPATIBLE or UNREADABLE."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code, self.detail = code, detail


def now_ist() -> datetime:
    return datetime.now(IST)


@contextmanager
def snapshot(path: Path | str | None) -> Iterator[sqlite3.Connection]:
    """One read-only transaction on a validated crew-only journal. Never creates the file."""
    if not path or not str(path).strip():
        raise Unavailable("NOT_CONFIGURED", "the crew journal location is not configured (CREW_JOURNAL_PATH)")
    target = Path(path).expanduser()
    if not target.is_file():
        raise Unavailable("MISSING", "the crew journal does not exist")
    try:
        conn = cj.connect_readonly(target)
    except sqlite3.Error as e:
        raise Unavailable("UNREADABLE", "the crew journal could not be opened") from e
    try:
        conn.execute("BEGIN")
        _check_identity(conn)
        yield conn
    except sqlite3.DatabaseError as e:
        logger.warning("crew journal read failed: %s: %s", type(e).__name__, e)
        raise Unavailable("UNREADABLE", "the crew journal could not be read") from e
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()


def _check_identity(conn: sqlite3.Connection) -> None:
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not _TABLES <= names or any(not n.startswith(("crew_", "sqlite_")) for n in names):
        raise Unavailable("INCOMPATIBLE", "the file is not a crew-only journal")
    row = conn.execute("SELECT value FROM crew_meta WHERE key='schema_version'").fetchone()
    if row is None or str(row[0]) != str(cj.SCHEMA_VERSION):
        found = "missing" if row is None else str(row[0])[:16]
        raise Unavailable("INCOMPATIBLE", f"crew journal schema {found} is not {cj.SCHEMA_VERSION}")


def redact(value):
    """Drop sensitive keys and mask filesystem paths and bot tokens, recursively."""
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items() if str(k).lower() not in _DROP}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _BOT_TOKEN.sub("[redacted]", _PATH.sub("[path]", value))
    return value


def _clock(conn: sqlite3.Connection, now: datetime) -> dict:
    """``today`` (IST) and the session holding a live lease, or None."""
    stamp = now.astimezone(IST)
    lease = conn.execute("SELECT session_id, expires_at, released_at FROM crew_lease WHERE id=1").fetchone()
    live = (lease is not None and lease["released_at"] is None
            and datetime.fromisoformat(lease["expires_at"]) > stamp)
    return {"today": stamp.date().isoformat(), "holder": lease["session_id"] if live else None}


def _page(limit: int, offset: int) -> tuple[int, int]:
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT or type(offset) is not int or offset < 0:
        raise ValueError(f"limit must be 1..{MAX_LIMIT} and offset >= 0")
    return limit, offset


def _trade(row: sqlite3.Row) -> dict:
    out = dict(row)
    out["legacy"] = bool(out["legacy"])
    return out


def journal_info(conn: sqlite3.Connection) -> dict:
    return {"source": "crew_journal", "schema_version": cj.SCHEMA_VERSION,
            "access": "read-only snapshot", "execution_state": "NONE (model trades, not orders)"}


def trades(conn: sqlite3.Connection, *, now: datetime, market_day: str | None = None, state: str | None = None,
           strategy_id: str | None = None, strategy_version: str | None = None,
           execution_policy: str | None = None, limit: int = 100, offset: int = 0) -> dict:
    limit, offset = _page(limit, offset)
    where = (" WHERE (:day IS NULL OR t.market_day = :day) AND (:state IS NULL OR t.state = :state)"
             " AND (:sid IS NULL OR t.strategy = :sid)"
             " AND (:ver IS NULL OR json_extract(e.payload, '$.strategy_version') = :ver)"
             " AND (:pol IS NULL OR json_extract(e.payload, '$.execution_policy') = :pol)")
    params = {**_clock(conn, now), "day": market_day, "state": state, "sid": strategy_id, "ver": strategy_version,
              "pol": execution_policy, "limit": limit, "offset": offset}
    total = conn.execute(f"SELECT count(*) FROM ({_TRADE_SELECT}{where})", params).fetchone()[0]
    rows = conn.execute(f"{_TRADE_SELECT}{where} ORDER BY t.market_day DESC, t.opened_at DESC, t.trade_id DESC"
                        " LIMIT :limit OFFSET :offset", params).fetchall()
    return {**journal_info(conn), "total": total, "limit": limit, "offset": offset,
            "items": redact([_trade(r) for r in rows])}


def trade_events(conn: sqlite3.Connection, trade_id: str, *, limit: int = 100, offset: int = 0) -> dict:
    """Raises LookupError for an unknown trade."""
    limit, offset = _page(limit, offset)
    if conn.execute("SELECT 1 FROM crew_trades WHERE trade_id=?", (trade_id,)).fetchone() is None:
        raise LookupError(trade_id)
    total = conn.execute("SELECT count(*) FROM crew_events WHERE trade_id=?", (trade_id,)).fetchone()[0]
    rows = conn.execute(
        "SELECT event_id, seq, event_type, from_state, to_state, session_id, occurred_at, recorded_at, payload"
        " FROM crew_events WHERE trade_id=? ORDER BY seq LIMIT ? OFFSET ?", (trade_id, limit, offset)).fetchall()
    items = [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]
    return {**journal_info(conn), "trade_id": trade_id, "total": total, "limit": limit, "offset": offset,
            "items": redact(items)}


def admissions(conn: sqlite3.Connection, *, market_day: str | None = None, admission_class: str | None = None,
               strategy_id: str | None = None, strategy_version: str | None = None,
               limit: int = 100, offset: int = 0) -> dict:
    limit, offset = _page(limit, offset)
    where = (" WHERE (:day IS NULL OR a.market_day = :day) AND (:cls IS NULL OR a.class = :cls)"
             " AND (:sid IS NULL OR json_extract(a.inputs, '$.strategy_id') = :sid)"
             " AND (:ver IS NULL OR json_extract(a.inputs, '$.strategy_version') = :ver)")
    params = {"day": market_day, "cls": admission_class, "sid": strategy_id, "ver": strategy_version,
              "limit": limit, "offset": offset}
    total = conn.execute(f"SELECT count(*) FROM crew_admissions a{where}", params).fetchone()[0]
    rows = conn.execute(
        "SELECT a.admission_id, a.signal_id, a.market_day, a.session_id, a.class, a.reasons, a.inputs, a.decided_at,"
        f" t.trade_id FROM crew_admissions a LEFT JOIN crew_trades t ON t.signal_id = a.signal_id{where}"
        " ORDER BY a.market_day DESC, a.decided_at DESC, a.admission_id DESC LIMIT :limit OFFSET :offset",
        params).fetchall()
    items = []
    for r in rows:
        inputs = json.loads(r["inputs"])
        # Only the signal identity: quote and capital inputs are evidence, not results.
        items.append({**{k: r[k] for k in ("admission_id", "signal_id", "market_day", "session_id", "decided_at",
                                           "trade_id")},
                      "admission_class": r["class"], "reasons": json.loads(r["reasons"]),
                      **{k: inputs.get(k) for k in _ADMISSION_KEYS}})
    return {**journal_info(conn), "total": total, "limit": limit, "offset": offset, "items": redact(items)}


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _findings(conn: sqlite3.Connection) -> list[dict]:
    return redact(cj._consistency_findings(conn))


def stats(conn: sqlite3.Connection, *, strategy_version: str, execution_policy: str,
          strategy_ids: list[str] | None = None, now: datetime) -> dict:
    """One group per strategy_id, for exactly one strategy_version and execution_policy.

    Withholds every group when the journal fails its consistency check.
    """
    if not strategy_version or not execution_policy:
        raise ValueError("strategy_version and execution_policy are required")
    findings = _findings(conn)
    out = {**journal_info(conn), "strategy_version": strategy_version, "execution_policy": execution_policy,
           "units": UNITS, "monetary_metrics": NO_MONEY, "eligibility": ELIGIBILITY,
           "applicable_signal_rule": APPLICABLE, "min_sample": MIN_SAMPLE, "interpretation": INTERPRETATION,
           "as_of": now.astimezone(IST).isoformat(),
           "consistency": {"ok": not findings, "count": len(findings), "findings": findings[:100]}}
    if findings:
        return {**out, "statistics_withheld": True, "groups": []}
    params = {**_clock(conn, now), "ver": strategy_version}
    trade_rows = [dict(r) for r in conn.execute(
        f"{_TRADE_SELECT} WHERE json_extract(e.payload, '$.strategy_version') = :ver ORDER BY t.trade_id", params)]
    admission_rows = [dict(r) for r in conn.execute(
        "SELECT a.admission_id, a.signal_id, a.class, json_extract(a.inputs, '$.strategy_id') AS strategy_id,"
        " t.trade_id FROM crew_admissions a LEFT JOIN crew_trades t ON t.signal_id = a.signal_id"
        " WHERE json_extract(a.inputs, '$.strategy_version') = :ver ORDER BY a.admission_id", params)]
    ids = list(dict.fromkeys(strategy_ids)) if strategy_ids else sorted(
        {r["strategy_id"] for r in trade_rows} | {r["strategy_id"] for r in admission_rows if r["strategy_id"]})
    groups = [_group(sid, strategy_version, execution_policy,
                     [r for r in trade_rows if r["strategy_id"] == sid],
                     [r for r in admission_rows if r["strategy_id"] == sid]) for sid in ids]
    return {**out, "statistics_withheld": False, "groups": groups}


def _group(sid: str, version: str, policy: str, trade_rows: list[dict], admission_rows: list[dict]) -> dict:
    applicable = [a for a in admission_rows if a["class"] != "REJECTED"]
    missing = [a for a in applicable if a["trade_id"] is None]
    population = [t for t in trade_rows if t["execution_policy"] == policy]
    eligible, exclusions = [], Counter()
    for t in population:
        if t["status"] == "CLOSED" and _finite(t["model_points"]) and _finite(t["model_r"]):
            eligible.append(t)
        else:
            exclusions["CLOSED_WITHOUT_MODEL_RESULT" if t["status"] == "CLOSED" else t["status"]] += 1
    statuses = Counter(t["status"] for t in population)
    n = len(eligible)
    wins = sum(t["model_r"] > 0 for t in eligible)
    losses = sum(t["model_r"] < 0 for t in eligible)
    enough = n >= MIN_SAMPLE
    entered = len(applicable) - len(missing)

    def ratio(num: int, den: int) -> dict:
        return {"numerator": num, "denominator": den, "ratio": round(num / den, 4) if den else None}

    return {
        "strategy_id": sid, "strategy_version": version, "execution_policy": policy,
        "population": {
            "admission_scope": "strategy_id and strategy_version (admissions precede any execution policy)",
            "admissions_total": len(admission_rows),
            "admissions_by_class": {c: sum(a["class"] == c for a in admission_rows)
                                    for c in ("ACTIONABLE", "RESEARCH_ONLY", "REJECTED")},
            "unique_signals": len({a["signal_id"] for a in admission_rows}),
            "applicable_signals": len(applicable),
            "missing_model_entries": len(missing),
            "model_trades_opened": len(population),
            "open": statuses["OPEN"],
            "orphaned": statuses["ORPHANED"],
            "open_prior_day": statuses["OPEN_PRIOR_DAY"],
            "expired_unresolved": statuses["EXPIRED_UNRESOLVED"],
            "closed_eligible": n,
        },
        "exclusions": dict(sorted(exclusions.items())),
        "not_in_population": {
            "other_execution_policy": sum(t["execution_policy"] not in (None, policy) for t in trade_rows),
            "execution_policy_missing": sum(t["execution_policy"] is None for t in trade_rows),
        },
        "coverage": {"closed_eligible_of_opened": ratio(n, len(population)),
                     "entered_of_applicable_signals": ratio(entered, len(applicable))},
        "results": {
            "closed_eligible": n, "wins": wins, "losses": losses, "flat": n - wins - losses,
            "sum_points": round(sum(t["model_points"] for t in eligible), 2),
            "sum_r": round(sum(t["model_r"] for t in eligible), 2),
            "sufficient_sample": enough,
            "label": "DESCRIPTIVE_RESEARCH_NOT_VALIDATED" if enough else "INSUFFICIENT_SAMPLE",
            "win_rate_pct": round(wins / n * 100, 1) if enough else None,
            "avg_r": round(sum(t["model_r"] for t in eligible) / n, 3) if enough else None,
            "avg_points": round(sum(t["model_points"] for t in eligible) / n, 2) if enough else None,
            "denominator": "closed_eligible",
        },
    }


def exceptions(conn: sqlite3.Connection, *, now: datetime, limit: int = 100) -> dict:
    """Unresolved conditions, read-only. Nothing here repairs, adopts, resends, resolves or quarantines."""
    limit, _ = _page(limit, 0)
    params = {**_clock(conn, now), "limit": limit}

    def listed(sql: str, order: str, row=dict) -> dict:
        total = conn.execute(f"SELECT count(*) FROM ({sql})", params).fetchone()[0]
        items = [row(r) for r in conn.execute(f"SELECT * FROM ({sql}) ORDER BY {order} LIMIT :limit", params)]
        return {"count": total, "items": redact(items)}

    def by_status(status: str) -> dict:
        return listed(f"SELECT * FROM ({_TRADE_SELECT}) WHERE status = '{status}'",  # status is a constant
                      "market_day DESC, opened_at DESC, trade_id DESC", _trade)

    findings = _findings(conn)
    return {
        **journal_info(conn), "as_of": now.astimezone(IST).isoformat(), "lease_holder": params["holder"],
        "consistency": {"ok": not findings, "count": len(findings), "findings": findings[:limit]},
        "missing_model_entries": listed(
            "SELECT a.admission_id, a.signal_id, a.market_day, a.session_id, a.class AS admission_class,"
            " a.decided_at, json_extract(a.inputs, '$.strategy_id') AS strategy_id,"
            " json_extract(a.inputs, '$.strategy_version') AS strategy_version FROM crew_admissions a"
            " LEFT JOIN crew_trades t ON t.signal_id = a.signal_id WHERE a.class <> 'REJECTED' AND t.trade_id IS NULL",
            "decided_at DESC, admission_id DESC"),
        "orphaned_trades": by_status("ORPHANED"),
        "open_prior_day_trades": by_status("OPEN_PRIOR_DAY"),
        "expired_unresolved_trades": by_status("EXPIRED_UNRESOLVED"),
        "quarantined_trades": by_status("QUARANTINED"),
        "unknown_publications": listed(
            "SELECT publication_id, trade_id, kind, purpose, attempts, created_at, claimed_at, claimed_by_session"
            " FROM crew_outbox WHERE state = 'UNKNOWN'", "created_at DESC, publication_id DESC"),
    }


def sessions(conn: sqlite3.Connection, *, now: datetime, limit: int = 100, offset: int = 0,
             events_offset: int = 0) -> dict:
    """Lease generation, sessions, and the session audit (acquisition, takeover, refusal, fence, recovery)."""
    limit, offset = _page(limit, offset)
    _, events_offset = _page(limit, events_offset)
    stamp = now.astimezone(IST)
    lease = conn.execute("SELECT session_id, generation, acquired_at, heartbeat_at, expires_at, released_at"
                         " FROM crew_lease WHERE id=1").fetchone()
    if lease is not None:
        lease = dict(lease)
        lease["status"] = ("RELEASED" if lease["released_at"] else
                           "EXPIRED" if datetime.fromisoformat(lease["expires_at"]) <= stamp else "HELD")
    session_rows = conn.execute(
        "SELECT session_id, market_day, generation, code_rev, started_at, heartbeat_at, ended_at, end_reason"
        " FROM crew_sessions ORDER BY started_at DESC, session_id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
    event_rows = conn.execute(
        "SELECT id, event_type, session_id, generation, prior_session_id, prior_generation, market_day, at, detail"
        " FROM crew_session_events ORDER BY id DESC LIMIT ? OFFSET ?", (limit, events_offset)).fetchall()
    return {
        **journal_info(conn), "as_of": stamp.isoformat(), "lease": lease,
        "sessions": {"total": conn.execute("SELECT count(*) FROM crew_sessions").fetchone()[0], "limit": limit,
                     "offset": offset, "items": redact([dict(r) for r in session_rows])},
        "events": {"total": conn.execute("SELECT count(*) FROM crew_session_events").fetchone()[0], "limit": limit,
                   "offset": events_offset,
                   "items": redact([{**dict(r), "detail": json.loads(r["detail"])} for r in event_rows])},
    }
