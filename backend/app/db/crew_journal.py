"""S1 crew journal: durable model-trade journal and Telegram outbox in SQLite.

Schema and database-enforced invariants only (S1-P1). Tables are prefixed ``crew_`` so they
never collide with the legacy ``live_and_forward_trades`` table that shares the file.

State changes follow a lockstep protocol inside one transaction: append the event
(``crew_events`` / ``crew_outbox_events``) with ``seq = version + 1``, then UPDATE the
trade/outbox row to ``version = seq``. Triggers reject any UPDATE without its matching event,
any illegal transition, and any UPDATE/DELETE of append-only rows.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1

_DAY = "GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"
_HEX64 = "length({c}) = 64 AND {c} NOT GLOB '*[^0-9a-f]*'"
_NO_TOKEN = "coalesce({c}, '') NOT GLOB '*bot[0-9]*:*'"
_OPEN = "('TRACKING','PARTIALLY_EXITED')"
_TERMINAL = "('CLOSED','EXPIRED_UNRESOLVED','QUARANTINED')"
_OUTBOX_TERMINAL = "('ACKED','FAILED_PERMANENT','SUPPRESSED','RESOLVED_DELIVERED','RESOLVED_NOT_DELIVERED')"
_OUTBOX_STATES = (
    "('PENDING','SENDING','ACKED','RETRY_WAIT','FAILED_PERMANENT','UNKNOWN','SUPPRESSED',"
    "'RESOLVED_DELIVERED','RESOLVED_NOT_DELIVERED')"
)
_FORBIDDEN_KEYS = (
    "('fill_price','broker_order_id','broker_qty','avg_price','pnl_rupees','realized_pnl',"
    "'pnl_inr','net_pnl_inr','realized_pnl_inr','fill_qty','filled_qty')"
)
_RESEARCH_REASONS = "('QUOTE_NOT_EXECUTABLE','CAPITAL_UNAVAILABLE','CASH_UNAVAILABLE','MAX_LOSS_UNVERIFIED')"
_OUTBOX_TRANSITIONS = (
    "('PENDING>SUPPRESSED','PENDING>SENDING','SENDING>ACKED','SENDING>RETRY_WAIT',"
    "'SENDING>FAILED_PERMANENT','SENDING>UNKNOWN','RETRY_WAIT>SENDING','RETRY_WAIT>SUPPRESSED',"
    "'UNKNOWN>RESOLVED_DELIVERED','UNKNOWN>RESOLVED_NOT_DELIVERED')"
)


def _no_update_delete(table: str) -> str:
    # Mirrors app.db.triggers.append_only_sql, but idempotent.
    return f"""
CREATE TRIGGER IF NOT EXISTS {table}_no_update BEFORE UPDATE ON {table}
BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;
CREATE TRIGGER IF NOT EXISTS {table}_no_delete BEFORE DELETE ON {table}
BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;
"""


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS crew_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    set_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS crew_sessions (
    session_id TEXT PRIMARY KEY CHECK (session_id <> ''),
    market_day TEXT NOT NULL CHECK (market_day {_DAY} AND date(market_day) IS market_day),
    pid INTEGER,
    host TEXT,
    code_rev TEXT,
    started_at TEXT NOT NULL,
    heartbeat_at TEXT,
    ended_at TEXT,
    end_reason TEXT
);

CREATE TABLE IF NOT EXISTS crew_admissions (
    admission_id TEXT PRIMARY KEY CHECK (admission_id <> ''),
    signal_id TEXT NOT NULL CHECK ({_HEX64.format(c="signal_id")}),
    market_day TEXT NOT NULL CHECK (market_day {_DAY} AND date(market_day) IS market_day),
    session_id TEXT NOT NULL REFERENCES crew_sessions(session_id),
    class TEXT NOT NULL CHECK (class IN ('ACTIONABLE','RESEARCH_ONLY','REJECTED')),
    reasons TEXT NOT NULL CHECK (json_valid(reasons) AND json_type(reasons) = 'array'),
    inputs TEXT NOT NULL CHECK (json_valid(inputs) AND json_type(inputs) = 'object'),
    decided_at TEXT NOT NULL,
    CHECK ((class = 'ACTIONABLE') = (json_array_length(reasons) = 0)),
    UNIQUE (signal_id, market_day)
);

CREATE TABLE IF NOT EXISTS crew_trades (
    trade_id TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL UNIQUE CHECK ({_HEX64.format(c="signal_id")}),
    admission_id TEXT UNIQUE REFERENCES crew_admissions(admission_id),
    admission_class TEXT NOT NULL CHECK (admission_class IN ('ACTIONABLE','RESEARCH_ONLY')),
    market_day TEXT NOT NULL CHECK (market_day {_DAY} AND date(market_day) IS market_day),
    owner_session_id TEXT REFERENCES crew_sessions(session_id),
    state TEXT NOT NULL CHECK (state IN ('TRACKING','PARTIALLY_EXITED','CLOSED','EXPIRED_UNRESOLVED','QUARANTINED')),
    execution_state TEXT NOT NULL DEFAULT 'NONE' CHECK (execution_state = 'NONE'),
    strategy TEXT NOT NULL CHECK (strategy <> ''),
    direction TEXT NOT NULL CHECK (direction IN ('CALL','PUT')),
    contract TEXT,
    expiry TEXT,
    lot_size_ref INTEGER CHECK (lot_size_ref IS NULL OR (typeof(lot_size_ref) = 'integer' AND lot_size_ref > 0)),
    entry_spot REAL,
    stop_spot REAL,
    target_spot REAL,
    model_lots_initial INTEGER NOT NULL CHECK (typeof(model_lots_initial) = 'integer' AND model_lots_initial > 0),
    model_lots_open INTEGER NOT NULL
        CHECK (typeof(model_lots_open) = 'integer' AND model_lots_open BETWEEN 0 AND model_lots_initial),
    version INTEGER NOT NULL DEFAULT 1 CHECK (typeof(version) = 'integer' AND version >= 1),
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    legacy_ref TEXT,
    CHECK (state <> 'TRACKING' OR model_lots_open = model_lots_initial),
    CHECK (state <> 'PARTIALLY_EXITED' OR model_lots_open BETWEEN 1 AND model_lots_initial - 1),
    CHECK (state <> 'CLOSED' OR model_lots_open = 0),
    CHECK ((state IN {_OPEN}) = (closed_at IS NULL)),
    -- Only a legacy JSON import may lack an admission; it is quarantined and never publishable.
    CHECK (admission_id IS NOT NULL OR (legacy_ref IS NOT NULL AND state = 'QUARANTINED')),
    CHECK (legacy_ref IS NULL OR admission_class = 'RESEARCH_ONLY'),
    CHECK (legacy_ref IS NOT NULL
           OR trade_id = 'T_' || replace(market_day, '-', '') || '_' || substr(signal_id, 1, 12))
);
CREATE INDEX IF NOT EXISTS crew_trades_day_state ON crew_trades(market_day, state);

CREATE TABLE IF NOT EXISTS crew_events (
    event_id TEXT PRIMARY KEY CHECK (event_id <> ''),
    trade_id TEXT NOT NULL REFERENCES crew_trades(trade_id),
    seq INTEGER NOT NULL CHECK (typeof(seq) = 'integer' AND seq >= 1),
    event_type TEXT NOT NULL CHECK (event_type IN ('MODEL_ENTRY','LEGACY_IMPORT','MODEL_LEVELS_UPDATED',
        'MODEL_PARTIAL_EXIT','MODEL_EXIT','EXPIRED_UNRESOLVED','QUARANTINED')),
    from_state TEXT,
    to_state TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE CHECK (idempotency_key <> ''),
    session_id TEXT REFERENCES crew_sessions(session_id),
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{{}}' CHECK (json_valid(payload) AND json_type(payload) = 'object'),
    UNIQUE (trade_id, seq)
);
CREATE UNIQUE INDEX IF NOT EXISTS crew_events_one_terminal ON crew_events(trade_id)
    WHERE to_state IN {_TERMINAL};

CREATE TABLE IF NOT EXISTS crew_outbox (
    publication_id TEXT PRIMARY KEY CHECK ({_HEX64.format(c="publication_id")}),
    trade_id TEXT REFERENCES crew_trades(trade_id),
    parent_publication_id TEXT REFERENCES crew_outbox(publication_id),
    kind TEXT NOT NULL CHECK (kind IN ('RECOMMENDATION','MONITORING','OPS')),
    purpose TEXT NOT NULL CHECK (purpose <> ''),
    body TEXT NOT NULL,
    body_sha256 TEXT NOT NULL CHECK ({_HEX64.format(c="body_sha256")}),
    state TEXT NOT NULL CHECK (state IN {_OUTBOX_STATES}),
    version INTEGER NOT NULL DEFAULT 1 CHECK (typeof(version) = 'integer' AND version >= 1),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (typeof(attempts) = 'integer' AND attempts BETWEEN 0 AND 3),
    not_before TEXT,
    created_at TEXT NOT NULL,
    claimed_at TEXT,
    claimed_by_session TEXT REFERENCES crew_sessions(session_id),
    acked_at TEXT,
    telegram_message_id INTEGER,
    last_error TEXT CHECK ({_NO_TOKEN.format(c="last_error")}),
    CHECK ((kind = 'OPS') = (trade_id IS NULL)),
    CHECK ((kind = 'MONITORING') = (parent_publication_id IS NOT NULL)),
    CHECK ((state IN ('ACKED','RESOLVED_DELIVERED')) = (telegram_message_id IS NOT NULL)),
    CHECK (telegram_message_id IS NULL
           OR (typeof(telegram_message_id) = 'integer' AND telegram_message_id > 0)),
    CHECK (state <> 'ACKED' OR acked_at IS NOT NULL),
    CHECK (state <> 'SENDING' OR (claimed_at IS NOT NULL AND claimed_by_session IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS crew_outbox_kind_state ON crew_outbox(kind, state);

CREATE TABLE IF NOT EXISTS crew_outbox_events (
    id INTEGER PRIMARY KEY,
    publication_id TEXT NOT NULL REFERENCES crew_outbox(publication_id),
    seq INTEGER NOT NULL CHECK (typeof(seq) = 'integer' AND seq >= 1),
    from_state TEXT,
    to_state TEXT NOT NULL CHECK (to_state IN {_OUTBOX_STATES}),
    at TEXT NOT NULL,
    session_id TEXT REFERENCES crew_sessions(session_id),
    http_status INTEGER,
    telegram_ok INTEGER CHECK (telegram_ok IS NULL OR telegram_ok IN (0, 1)),
    message_id INTEGER,
    exc_class TEXT,
    latency_ms INTEGER,
    response_excerpt TEXT CHECK ({_NO_TOKEN.format(c="response_excerpt")}),
    operator TEXT,
    method TEXT,
    note TEXT CHECK ({_NO_TOKEN.format(c="note")}),
    CHECK ((operator IS NOT NULL) = (to_state IN ('RESOLVED_DELIVERED','RESOLVED_NOT_DELIVERED'))),
    CHECK (to_state <> 'SENDING' OR session_id IS NOT NULL),
    UNIQUE (publication_id, seq)
);

-- Admissions: append-only; INSERT OR REPLACE would delete without firing delete triggers.
CREATE TRIGGER IF NOT EXISTS crew_admissions_insert BEFORE INSERT ON crew_admissions
BEGIN
    SELECT RAISE(ABORT, 'crew_admissions: duplicate admission or signal for the day')
    WHERE EXISTS (SELECT 1 FROM crew_admissions WHERE admission_id = NEW.admission_id
                  OR (signal_id = NEW.signal_id AND market_day = NEW.market_day));
    SELECT RAISE(ABORT, 'crew_admissions: reasons must be strings')
    WHERE EXISTS (SELECT 1 FROM json_each(NEW.reasons) WHERE type <> 'text');
    SELECT RAISE(ABORT, 'crew_admissions: RESEARCH_ONLY allows only quote/capital/cash/max-loss reasons')
    WHERE NEW.class = 'RESEARCH_ONLY'
      AND EXISTS (SELECT 1 FROM json_each(NEW.reasons) WHERE value NOT IN {_RESEARCH_REASONS});
END;
{_no_update_delete("crew_admissions")}

CREATE TRIGGER IF NOT EXISTS crew_trades_insert BEFORE INSERT ON crew_trades
BEGIN
    SELECT RAISE(ABORT, 'crew_trades: duplicate trade, signal or admission')
    WHERE EXISTS (SELECT 1 FROM crew_trades WHERE trade_id = NEW.trade_id OR signal_id = NEW.signal_id
                  OR admission_id = NEW.admission_id);
    SELECT RAISE(ABORT, 'crew_trades: a trade starts TRACKING at version 1 (QUARANTINED for legacy import)')
    WHERE NEW.version IS NOT 1
       OR NOT ((NEW.legacy_ref IS NULL AND NEW.state = 'TRACKING')
               OR (NEW.legacy_ref IS NOT NULL AND NEW.state = 'QUARANTINED'));
    SELECT RAISE(ABORT, 'crew_trades: admission class, signal or day does not match the admission')
    WHERE NEW.admission_id IS NOT NULL AND NOT coalesce(
        (SELECT a.class = NEW.admission_class AND a.signal_id = NEW.signal_id AND a.market_day = NEW.market_day
         FROM crew_admissions a WHERE a.admission_id = NEW.admission_id), 0);
END;

CREATE TRIGGER IF NOT EXISTS crew_trades_update BEFORE UPDATE ON crew_trades
BEGIN
    SELECT RAISE(ABORT, 'crew_trades: terminal trades cannot change or reopen')
    WHERE OLD.state IN {_TERMINAL};
    SELECT RAISE(ABORT, 'crew_trades: identity columns are immutable')
    WHERE NEW.trade_id IS NOT OLD.trade_id OR NEW.signal_id IS NOT OLD.signal_id
       OR NEW.admission_id IS NOT OLD.admission_id OR NEW.admission_class IS NOT OLD.admission_class
       OR NEW.market_day IS NOT OLD.market_day OR NEW.strategy IS NOT OLD.strategy
       OR NEW.direction IS NOT OLD.direction OR NEW.contract IS NOT OLD.contract
       OR NEW.expiry IS NOT OLD.expiry OR NEW.lot_size_ref IS NOT OLD.lot_size_ref
       OR NEW.entry_spot IS NOT OLD.entry_spot OR NEW.model_lots_initial IS NOT OLD.model_lots_initial
       OR NEW.opened_at IS NOT OLD.opened_at OR NEW.legacy_ref IS NOT OLD.legacy_ref;
    SELECT RAISE(ABORT, 'crew_trades: update must match the next crew_events row')
    WHERE NOT coalesce(
        (SELECT NEW.version = OLD.version + 1 AND e.from_state = OLD.state AND e.to_state = NEW.state
                AND CASE e.event_type
                    WHEN 'MODEL_PARTIAL_EXIT' THEN NEW.model_lots_open < OLD.model_lots_open
                        AND json_extract(e.payload, '$.lots_before') IS OLD.model_lots_open
                        AND json_extract(e.payload, '$.lots_after') IS NEW.model_lots_open
                    WHEN 'MODEL_EXIT' THEN NEW.model_lots_open = 0
                    ELSE NEW.model_lots_open = OLD.model_lots_open END
         FROM crew_events e WHERE e.trade_id = OLD.trade_id AND e.seq = NEW.version), 0);
END;

CREATE TRIGGER IF NOT EXISTS crew_trades_no_delete BEFORE DELETE ON crew_trades
BEGIN SELECT RAISE(ABORT, 'crew_trades rows cannot be deleted'); END;

CREATE TRIGGER IF NOT EXISTS crew_events_insert BEFORE INSERT ON crew_events
BEGIN
    SELECT RAISE(ABORT, 'crew_events: duplicate event id, idempotency key or sequence')
    WHERE EXISTS (SELECT 1 FROM crew_events WHERE event_id = NEW.event_id
                  OR idempotency_key = NEW.idempotency_key OR (trade_id = NEW.trade_id AND seq = NEW.seq));
    SELECT RAISE(ABORT, 'crew_events: broker fill and rupee P&L keys are forbidden in S1')
    WHERE EXISTS (SELECT 1 FROM json_tree(NEW.payload) WHERE lower(key) IN {_FORBIDDEN_KEYS});
    SELECT RAISE(ABORT, 'crew_events: illegal model state transition')
    WHERE NOT coalesce(
        (NEW.seq = 1 AND NEW.from_state IS NULL
            AND ((NEW.event_type = 'MODEL_ENTRY' AND NEW.to_state = 'TRACKING')
                 OR (NEW.event_type = 'LEGACY_IMPORT' AND NEW.to_state = 'QUARANTINED')))
        OR (NEW.seq > 1 AND NEW.from_state IN {_OPEN}
            AND ((NEW.event_type = 'MODEL_LEVELS_UPDATED' AND NEW.to_state = NEW.from_state)
                 OR (NEW.event_type = 'MODEL_PARTIAL_EXIT' AND NEW.to_state = 'PARTIALLY_EXITED')
                 OR (NEW.event_type = 'MODEL_EXIT' AND NEW.to_state = 'CLOSED')
                 OR (NEW.event_type IN ('EXPIRED_UNRESOLVED','QUARANTINED') AND NEW.to_state = NEW.event_type))),
        0);
    SELECT RAISE(ABORT, 'crew_events: event does not follow the trade state and version')
    WHERE NOT coalesce(
        (SELECT CASE WHEN NEW.seq = 1
                THEN t.version = 1 AND t.state = NEW.to_state
                     AND (NEW.event_type = 'LEGACY_IMPORT') = (t.legacy_ref IS NOT NULL)
                ELSE t.version + 1 = NEW.seq AND t.state = NEW.from_state
                     AND EXISTS (SELECT 1 FROM crew_events p WHERE p.trade_id = NEW.trade_id AND p.seq = NEW.seq - 1)
                END
         FROM crew_trades t WHERE t.trade_id = NEW.trade_id), 0);
END;
{_no_update_delete("crew_events")}

CREATE TRIGGER IF NOT EXISTS crew_outbox_insert BEFORE INSERT ON crew_outbox
BEGIN
    SELECT RAISE(ABORT, 'crew_outbox: duplicate publication')
    WHERE EXISTS (SELECT 1 FROM crew_outbox WHERE publication_id = NEW.publication_id);
    SELECT RAISE(ABORT, 'crew_outbox: a publication starts PENDING or SUPPRESSED, unclaimed, at version 1')
    WHERE NEW.state NOT IN ('PENDING','SUPPRESSED') OR NEW.version IS NOT 1 OR NEW.attempts IS NOT 0
       OR NEW.claimed_at IS NOT NULL OR NEW.claimed_by_session IS NOT NULL OR NEW.acked_at IS NOT NULL;
    SELECT RAISE(ABORT, 'crew_outbox: RECOMMENDATION and MONITORING require an ACTIONABLE admission')
    WHERE NEW.kind IN ('RECOMMENDATION','MONITORING') AND NOT coalesce(
        (SELECT t.admission_class = 'ACTIONABLE' AND a.class = 'ACTIONABLE'
         FROM crew_trades t JOIN crew_admissions a ON a.admission_id = t.admission_id
         WHERE t.trade_id = NEW.trade_id), 0);
    SELECT RAISE(ABORT, 'crew_outbox: MONITORING parent must be the same trade''s RECOMMENDATION')
    WHERE NEW.kind = 'MONITORING' AND NOT coalesce(
        (SELECT p.kind = 'RECOMMENDATION' AND p.trade_id = NEW.trade_id
         FROM crew_outbox p WHERE p.publication_id = NEW.parent_publication_id), 0);
    SELECT RAISE(ABORT, 'crew_outbox: MONITORING must be SUPPRESSED while its parent is undelivered')
    WHERE NEW.kind = 'MONITORING' AND NEW.state <> 'SUPPRESSED' AND NOT EXISTS
        (SELECT 1 FROM crew_outbox p WHERE p.publication_id = NEW.parent_publication_id
         AND p.state IN ('ACKED','RESOLVED_DELIVERED'));
    SELECT RAISE(ABORT, 'crew_outbox: RECOMMENDATION must be SUPPRESSED while a prior one is UNKNOWN')
    WHERE NEW.kind = 'RECOMMENDATION' AND NEW.state <> 'SUPPRESSED' AND EXISTS
        (SELECT 1 FROM crew_outbox WHERE kind = 'RECOMMENDATION' AND state = 'UNKNOWN');
END;

CREATE TRIGGER IF NOT EXISTS crew_outbox_update BEFORE UPDATE ON crew_outbox
BEGIN
    SELECT RAISE(ABORT, 'crew_outbox: terminal publications are immutable')
    WHERE OLD.state IN {_OUTBOX_TERMINAL};
    SELECT RAISE(ABORT, 'crew_outbox: identity columns are immutable')
    WHERE NEW.publication_id IS NOT OLD.publication_id OR NEW.trade_id IS NOT OLD.trade_id
       OR NEW.parent_publication_id IS NOT OLD.parent_publication_id OR NEW.kind IS NOT OLD.kind
       OR NEW.purpose IS NOT OLD.purpose OR NEW.body IS NOT OLD.body
       OR NEW.body_sha256 IS NOT OLD.body_sha256 OR NEW.created_at IS NOT OLD.created_at;
    SELECT RAISE(ABORT, 'crew_outbox: update must match the next crew_outbox_events row')
    WHERE NOT coalesce(
        (SELECT NEW.version = OLD.version + 1 AND e.from_state = OLD.state AND e.to_state = NEW.state
                AND NEW.attempts = OLD.attempts + (NEW.state = 'SENDING')
                AND (NEW.state NOT IN ('ACKED','RESOLVED_DELIVERED') OR NEW.telegram_message_id = e.message_id)
                AND (NEW.state <> 'SENDING'
                     OR (NEW.claimed_by_session = e.session_id AND NEW.claimed_at = e.at))
         FROM crew_outbox_events e WHERE e.publication_id = OLD.publication_id AND e.seq = NEW.version), 0);
END;

CREATE TRIGGER IF NOT EXISTS crew_outbox_no_delete BEFORE DELETE ON crew_outbox
BEGIN SELECT RAISE(ABORT, 'crew_outbox rows cannot be deleted'); END;

CREATE TRIGGER IF NOT EXISTS crew_outbox_events_insert BEFORE INSERT ON crew_outbox_events
BEGIN
    SELECT RAISE(ABORT, 'crew_outbox_events: duplicate audit id or sequence')
    WHERE EXISTS (SELECT 1 FROM crew_outbox_events WHERE id = NEW.id
                  OR (publication_id = NEW.publication_id AND seq = NEW.seq));
    SELECT RAISE(ABORT, 'crew_outbox_events: audit row does not follow the publication state and version')
    WHERE NOT coalesce(
        (SELECT CASE WHEN NEW.seq = 1
                THEN o.version = 1 AND NEW.from_state IS NULL AND NEW.to_state = o.state
                ELSE o.version + 1 = NEW.seq AND NEW.from_state = o.state
                END
         FROM crew_outbox o WHERE o.publication_id = NEW.publication_id), 0);
    SELECT RAISE(ABORT, 'crew_outbox_events: illegal delivery state transition')
    WHERE NEW.seq > 1 AND NEW.from_state || '>' || NEW.to_state NOT IN {_OUTBOX_TRANSITIONS};
    SELECT RAISE(ABORT, 'crew_outbox_events: ACKED needs HTTP 200, ok:true and a message_id')
    WHERE NEW.to_state = 'ACKED' AND NOT coalesce(
        NEW.http_status = 200 AND NEW.telegram_ok = 1
        AND typeof(NEW.message_id) = 'integer' AND NEW.message_id > 0, 0);
    SELECT RAISE(ABORT, 'crew_outbox_events: operator resolution needs operator, method and note')
    WHERE NEW.to_state IN ('RESOLVED_DELIVERED','RESOLVED_NOT_DELIVERED') AND NOT coalesce(
        trim(NEW.operator) <> '' AND NEW.method = 'channel_inspection' AND trim(NEW.note) <> ''
        AND CASE NEW.to_state
            WHEN 'RESOLVED_DELIVERED' THEN typeof(NEW.message_id) = 'integer' AND NEW.message_id > 0
            ELSE NEW.message_id IS NULL END, 0);
    SELECT RAISE(ABORT, 'crew_outbox_events: claim limit reached')
    WHERE NEW.to_state = 'SENDING'
      AND (SELECT attempts FROM crew_outbox WHERE publication_id = NEW.publication_id) >= 3;
    SELECT RAISE(ABORT, 'crew_outbox_events: RECOMMENDATION claim blocked by an UNKNOWN recommendation')
    WHERE NEW.to_state = 'SENDING'
      AND (SELECT kind FROM crew_outbox WHERE publication_id = NEW.publication_id) = 'RECOMMENDATION'
      AND EXISTS (SELECT 1 FROM crew_outbox WHERE kind = 'RECOMMENDATION' AND state = 'UNKNOWN'
                  AND publication_id <> NEW.publication_id);
    SELECT RAISE(ABORT, 'crew_outbox_events: MONITORING claim needs a delivered parent')
    WHERE NEW.to_state = 'SENDING' AND EXISTS
        (SELECT 1 FROM crew_outbox o WHERE o.publication_id = NEW.publication_id AND o.kind = 'MONITORING'
         AND NOT EXISTS (SELECT 1 FROM crew_outbox p WHERE p.publication_id = o.parent_publication_id
                         AND p.state IN ('ACKED','RESOLVED_DELIVERED')));
END;
{_no_update_delete("crew_outbox_events")}

INSERT OR IGNORE INTO crew_meta (key, value, set_at)
VALUES ('schema_version', '{SCHEMA_VERSION}', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
"""


def connect(db_path: Path | str, *, create: bool = False) -> sqlite3.Connection:
    """Open the journal: WAL, synchronous=FULL, foreign_keys=ON, autocommit (use ``transaction``).

    The path is mandatory. Without ``create`` a missing file raises instead of being created.
    """
    path = Path(db_path).resolve()
    conn = sqlite3.connect(
        f"{path.as_uri()}?mode={'rwc' if create else 'rw'}", uri=True, timeout=30.0, isolation_level=None
    )
    conn.row_factory = sqlite3.Row
    try:
        mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        if mode != "wal" or conn.execute("PRAGMA synchronous").fetchone()[0] != 2 \
                or conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise sqlite3.OperationalError(f"crew journal pragmas not applied on {path}")
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT; any exception (including a failed COMMIT) rolls back."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def migrate(db_path: Path | str) -> int:
    """Create the crew_* schema additively and idempotently in one transaction. Returns the schema version.

    Only CREATE ... IF NOT EXISTS statements; existing tables (including legacy ones) are never altered.
    """
    conn = connect(db_path, create=True)
    try:
        has_meta = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='crew_meta'").fetchone()
        if has_meta:
            row = conn.execute("SELECT value FROM crew_meta WHERE key='schema_version'").fetchone()
            if row and int(row[0]) > SCHEMA_VERSION:
                raise RuntimeError(f"crew journal schema {row[0]} is newer than this code ({SCHEMA_VERSION})")
        try:
            conn.executescript(f"BEGIN IMMEDIATE;\n{SCHEMA}\nCOMMIT;")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        return int(conn.execute("SELECT value FROM crew_meta WHERE key='schema_version'").fetchone()[0])
    finally:
        conn.close()
