"""S1 crew journal: durable model-trade journal and Telegram outbox in SQLite.

P1 is the schema. P2 is the model-trade API: admission, entry, partial exit, full exit,
level updates, prior-day expiry, legacy quarantine, and read-only recovery checks.
Tables are prefixed ``crew_`` so they never collide with the legacy
``live_and_forward_trades`` table that shares the file.

State changes follow a lockstep protocol inside one transaction: append the event
(``crew_events`` / ``crew_outbox_events``) with ``seq = version + 1``, then UPDATE the
trade/outbox row to ``version = seq``. Triggers reject any UPDATE without its matching event,
any illegal transition, and any UPDATE/DELETE of append-only rows.

Every operational write (admission, trade, expiry, outbox claim/outcome/suppression/recovery) takes the
session's ``Lease`` and verifies it against ``crew_lease`` first, inside its own BEGIN IMMEDIATE transaction.
A stale lease records FENCED, commits that audit row, and raises ``LeaseLost``; nothing else is written.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import socket
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import UTC, date, datetime
from pathlib import Path

from app.trading.option_quote import IST

SCHEMA_VERSION = 6

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

# v2: a Telegram message_id is unique only within its chat, so a publication carries its destination.
# v1 rows keep chat_id NULL ("destination not recorded"): they cannot be claimed, and their message_ids
# count as clashes in every chat.
_V2_ADD_CHAT = (
    "ALTER TABLE crew_outbox ADD COLUMN chat_id TEXT"
    " CHECK (chat_id IS NULL OR (chat_id <> '' AND chat_id = trim(chat_id)));"
)
SCHEMA_V2 = """
CREATE UNIQUE INDEX IF NOT EXISTS crew_outbox_chat_message ON crew_outbox(chat_id, telegram_message_id)
    WHERE telegram_message_id IS NOT NULL;

CREATE TRIGGER IF NOT EXISTS crew_outbox_insert_chat BEFORE INSERT ON crew_outbox
BEGIN
    SELECT RAISE(ABORT, 'crew_outbox: a publication needs its destination chat_id')
    WHERE NEW.chat_id IS NULL;
    SELECT RAISE(ABORT, 'crew_outbox: MONITORING must go to its parent''s chat')
    WHERE NEW.kind = 'MONITORING' AND EXISTS (SELECT 1 FROM crew_outbox p
        WHERE p.publication_id = NEW.parent_publication_id AND p.chat_id IS NOT NEW.chat_id);
END;

CREATE TRIGGER IF NOT EXISTS crew_outbox_chat_immutable BEFORE UPDATE OF chat_id ON crew_outbox
BEGIN
    SELECT RAISE(ABORT, 'crew_outbox: identity columns are immutable') WHERE NEW.chat_id IS NOT OLD.chat_id;
END;

UPDATE crew_meta SET value = '2', set_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE key = 'schema_version' AND CAST(value AS INTEGER) < 2;
"""

# v3: one exclusive journal lease (crew_lease, a single row) fenced by a generation that only grows,
# and an append-only audit of every acquisition, release, refusal, fence and recovery.
# Ownership comes from the lease alone; pid and host are recorded for the audit only.
_V3_ADD_GENERATION = "ALTER TABLE crew_sessions ADD COLUMN generation INTEGER;"
SCHEMA_V3 = f"""
CREATE UNIQUE INDEX IF NOT EXISTS crew_sessions_generation ON crew_sessions(generation)
    WHERE generation IS NOT NULL;

CREATE TABLE IF NOT EXISTS crew_session_events (
    id INTEGER PRIMARY KEY,
    event_type TEXT NOT NULL CHECK (event_type IN ('ACQUIRED','TAKEOVER','RELEASED','REFUSED','FENCED','RECOVERED')),
    session_id TEXT REFERENCES crew_sessions(session_id),
    generation INTEGER,
    prior_session_id TEXT REFERENCES crew_sessions(session_id),
    prior_generation INTEGER,
    market_day TEXT NOT NULL CHECK (market_day {_DAY} AND date(market_day) IS market_day),
    at TEXT NOT NULL CHECK (julianday(at) IS NOT NULL),
    pid INTEGER,
    host TEXT,
    detail TEXT NOT NULL DEFAULT '{{}}' CHECK (json_valid(detail) AND json_type(detail) = 'object'),
    CHECK ((event_type = 'REFUSED') = (session_id IS NULL)),
    CHECK ((session_id IS NULL) = (generation IS NULL)),
    CHECK (event_type <> 'TAKEOVER' OR prior_session_id IS NOT NULL)
);
{_no_update_delete("crew_session_events")}

CREATE TABLE IF NOT EXISTS crew_lease (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    session_id TEXT NOT NULL REFERENCES crew_sessions(session_id),
    generation INTEGER NOT NULL CHECK (typeof(generation) = 'integer' AND generation >= 1),
    token_sha256 TEXT NOT NULL CHECK ({_HEX64.format(c="token_sha256")}),
    acquired_at TEXT NOT NULL CHECK (julianday(acquired_at) IS NOT NULL),
    heartbeat_at TEXT NOT NULL CHECK (julianday(heartbeat_at) IS NOT NULL),
    expires_at TEXT NOT NULL CHECK (julianday(expires_at) > julianday(heartbeat_at)),
    released_at TEXT
);

CREATE TRIGGER IF NOT EXISTS crew_lease_insert BEFORE INSERT ON crew_lease
BEGIN
    SELECT RAISE(ABORT, 'crew_lease: the first lease is generation 1, unreleased, with its ACQUIRED event')
    WHERE NEW.generation IS NOT 1 OR NEW.released_at IS NOT NULL OR NOT EXISTS
        (SELECT 1 FROM crew_session_events WHERE event_type = 'ACQUIRED'
         AND session_id = NEW.session_id AND generation = 1);
END;

CREATE TRIGGER IF NOT EXISTS crew_lease_update BEFORE UPDATE ON crew_lease
BEGIN
    SELECT RAISE(ABORT, 'crew_lease: a new owner needs generation + 1, a released or expired lease, and its event')
    WHERE NEW.generation IS NOT OLD.generation AND NOT coalesce(
        NEW.generation = OLD.generation + 1 AND NEW.released_at IS NULL
        AND (OLD.released_at IS NOT NULL OR julianday(OLD.expires_at) <= julianday(NEW.acquired_at))
        AND EXISTS (SELECT 1 FROM crew_session_events WHERE event_type IN ('ACQUIRED','TAKEOVER')
                    AND session_id = NEW.session_id AND generation = NEW.generation), 0);
    SELECT RAISE(ABORT, 'crew_lease: session, token and acquisition are fixed for a generation')
    WHERE NEW.generation IS OLD.generation
      AND (NEW.session_id IS NOT OLD.session_id OR NEW.token_sha256 IS NOT OLD.token_sha256
           OR NEW.acquired_at IS NOT OLD.acquired_at);
    SELECT RAISE(ABORT, 'crew_lease: a released lease cannot change')
    WHERE NEW.generation IS OLD.generation AND OLD.released_at IS NOT NULL;
    SELECT RAISE(ABORT, 'crew_lease: an expired lease cannot be renewed')
    WHERE NEW.generation IS OLD.generation AND NEW.heartbeat_at IS NOT OLD.heartbeat_at
      AND julianday(NEW.heartbeat_at) >= julianday(OLD.expires_at);
    SELECT RAISE(ABORT, 'crew_lease: release needs its RELEASED event')
    WHERE NEW.generation IS OLD.generation AND NEW.released_at IS NOT NULL AND NOT EXISTS
        (SELECT 1 FROM crew_session_events WHERE event_type = 'RELEASED'
         AND session_id = NEW.session_id AND generation = NEW.generation);
END;

CREATE TRIGGER IF NOT EXISTS crew_lease_no_delete BEFORE DELETE ON crew_lease
BEGIN SELECT RAISE(ABORT, 'crew_lease rows cannot be deleted'); END;

CREATE TRIGGER IF NOT EXISTS crew_sessions_update BEFORE UPDATE ON crew_sessions
BEGIN
    SELECT RAISE(ABORT, 'crew_sessions: identity columns are immutable')
    WHERE NEW.session_id IS NOT OLD.session_id OR NEW.market_day IS NOT OLD.market_day
       OR NEW.started_at IS NOT OLD.started_at OR NEW.generation IS NOT OLD.generation
       OR NEW.pid IS NOT OLD.pid OR NEW.host IS NOT OLD.host OR NEW.code_rev IS NOT OLD.code_rev;
    SELECT RAISE(ABORT, 'crew_sessions: an ended session cannot change') WHERE OLD.ended_at IS NOT NULL;
END;

CREATE TRIGGER IF NOT EXISTS crew_sessions_no_delete BEFORE DELETE ON crew_sessions
BEGIN SELECT RAISE(ABORT, 'crew_sessions rows cannot be deleted'); END;

UPDATE crew_meta SET value = '3', set_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE key = 'schema_version' AND CAST(value AS INTEGER) < 3;
"""

# v4: paper trading. Simulated option fills priced from an executable book, in rupees, kept apart from the
# model journal (whose events still forbid rupee keys). Cash is never stored: it is derived from the fills.
_PAPER_OPEN = "('OPEN','PARTIALLY_EXITED')"
_PAPER_TERMINAL = "('CLOSED','EXIT_UNPRICED','EXPIRED_UNRESOLVED')"
_MONEY = "({c} IS NULL OR (typeof({c}) = 'text' AND CAST({c} AS REAL) >= 0))"
SCHEMA_V4 = f"""
CREATE TABLE IF NOT EXISTS crew_paper_accounts (
    account_id TEXT PRIMARY KEY CHECK (account_id = 'SHARED' OR account_id GLOB 'IND:?*'),
    capital_inr TEXT NOT NULL CHECK (typeof(capital_inr) = 'text' AND CAST(capital_inr AS REAL) > 0),
    risk_policy_id TEXT NOT NULL CHECK (risk_policy_id <> ''),
    opened_at TEXT NOT NULL
);
{_no_update_delete("crew_paper_accounts")}

CREATE TABLE IF NOT EXISTS crew_paper_trades (
    paper_trade_id TEXT PRIMARY KEY CHECK (paper_trade_id GLOB 'P_[0-9]*_?*'),
    account_id TEXT NOT NULL REFERENCES crew_paper_accounts(account_id),
    admission_id TEXT NOT NULL REFERENCES crew_admissions(admission_id),
    model_trade_id TEXT NOT NULL REFERENCES crew_trades(trade_id),
    market_day TEXT NOT NULL CHECK (market_day {_DAY} AND date(market_day) IS market_day),
    strategy_id TEXT NOT NULL CHECK (strategy_id <> ''),
    family TEXT NOT NULL CHECK (family <> ''),
    strategy_version TEXT NOT NULL,
    registry_hash TEXT NOT NULL CHECK ({_HEX64.format(c="registry_hash")}),
    underlying TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('CALL','PUT')),
    contract TEXT NOT NULL CHECK (contract <> ''),
    expiry TEXT NOT NULL CHECK (expiry {_DAY} AND expiry >= market_day),
    strike REAL NOT NULL CHECK (strike > 0),
    option_type TEXT NOT NULL CHECK (option_type IN ('CE','PE')),
    lot_size INTEGER NOT NULL CHECK (typeof(lot_size) = 'integer' AND lot_size > 0),
    lots_initial INTEGER NOT NULL CHECK (typeof(lots_initial) = 'integer' AND lots_initial > 0),
    lots_open INTEGER NOT NULL CHECK (typeof(lots_open) = 'integer' AND lots_open BETWEEN 0 AND lots_initial),
    entry_spot REAL NOT NULL, stop_spot REAL NOT NULL, target_spot REAL NOT NULL,
    signal_candle TEXT NOT NULL,
    regime TEXT NOT NULL,
    capital_committed_inr TEXT NOT NULL CHECK ({_MONEY.format(c="capital_committed_inr")}),
    max_loss_inr TEXT NOT NULL CHECK ({_MONEY.format(c="max_loss_inr")}),
    state TEXT NOT NULL CHECK (state IN ('OPEN','PARTIALLY_EXITED','CLOSED','EXIT_UNPRICED','EXPIRED_UNRESOLVED')),
    version INTEGER NOT NULL DEFAULT 1 CHECK (typeof(version) = 'integer' AND version >= 1),
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    UNIQUE (account_id, admission_id),
    CHECK (state <> 'CLOSED' OR lots_open = 0),
    CHECK ((state IN {_PAPER_OPEN}) = (closed_at IS NULL))
);

CREATE TABLE IF NOT EXISTS crew_paper_events (
    event_id INTEGER PRIMARY KEY,
    paper_trade_id TEXT NOT NULL REFERENCES crew_paper_trades(paper_trade_id),
    seq INTEGER NOT NULL CHECK (typeof(seq) = 'integer' AND seq >= 1),
    event_type TEXT NOT NULL CHECK (event_type IN ('ENTRY','PARTIAL_EXIT','PARTIAL_SKIPPED','EXIT','EXIT_UNPRICED',
                                                   'EXPIRED_UNRESOLVED')),
    from_state TEXT,
    to_state TEXT NOT NULL,
    reason TEXT NOT NULL CHECK (reason <> ''),
    lots_after INTEGER NOT NULL CHECK (typeof(lots_after) = 'integer' AND lots_after >= 0),
    side TEXT CHECK (side IS NULL OR side IN ('BUY','SELL')),
    lots INTEGER, qty INTEGER,
    price_inr TEXT CHECK ({_MONEY.format(c="price_inr")}),
    bid TEXT, ask TEXT, ltp TEXT, mid TEXT, quote_time TEXT, fetched_at TEXT, quote_age_s REAL, quote_source TEXT,
    slippage_inr TEXT CHECK ({_MONEY.format(c="slippage_inr")}),
    charges TEXT CHECK (charges IS NULL OR json_valid(charges)),
    charges_inr TEXT CHECK ({_MONEY.format(c="charges_inr")}),
    index_spot REAL,
    session_id TEXT REFERENCES crew_sessions(session_id),
    occurred_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE CHECK (idempotency_key <> ''),
    UNIQUE (paper_trade_id, seq),
    -- A fill is exactly an ENTRY (BUY) or a priced exit (SELL), with its book and charges.
    CHECK ((event_type IN ('ENTRY','PARTIAL_EXIT','EXIT')) = (side IS NOT NULL)),
    CHECK (side IS NULL OR (price_inr IS NOT NULL AND CAST(price_inr AS REAL) > 0 AND bid IS NOT NULL
           AND ask IS NOT NULL AND quote_time IS NOT NULL AND charges_inr IS NOT NULL
           AND typeof(qty) = 'integer' AND qty > 0 AND typeof(lots) = 'integer' AND lots > 0)),
    CHECK ((side = 'BUY') = (event_type = 'ENTRY'))
);
CREATE UNIQUE INDEX IF NOT EXISTS crew_paper_events_one_terminal ON crew_paper_events(paper_trade_id)
    WHERE to_state IN {_PAPER_TERMINAL};

CREATE TABLE IF NOT EXISTS crew_paper_decisions (
    account_id TEXT NOT NULL REFERENCES crew_paper_accounts(account_id),
    admission_id TEXT NOT NULL REFERENCES crew_admissions(admission_id),
    market_day TEXT NOT NULL CHECK (market_day {_DAY} AND date(market_day) IS market_day),
    strategy_id TEXT NOT NULL CHECK (strategy_id <> ''),
    family TEXT,
    registry_hash TEXT NOT NULL CHECK ({_HEX64.format(c="registry_hash")}),
    outcome TEXT NOT NULL CHECK (outcome IN ('OPENED','REJECTED','UNPRICED')),
    reasons TEXT NOT NULL CHECK (json_valid(reasons) AND json_type(reasons) = 'array'),
    paper_trade_id TEXT REFERENCES crew_paper_trades(paper_trade_id),
    decided_at TEXT NOT NULL,
    PRIMARY KEY (account_id, admission_id),
    CHECK ((outcome = 'OPENED') = (paper_trade_id IS NOT NULL)),
    CHECK ((outcome = 'OPENED') = (json_array_length(reasons) = 0))
);
{_no_update_delete("crew_paper_decisions")}

CREATE TRIGGER IF NOT EXISTS crew_paper_decisions_insert BEFORE INSERT ON crew_paper_decisions
BEGIN
    SELECT RAISE(ABORT, 'crew_paper_decisions: an OPENED decision needs its trade in the same account')
    WHERE NEW.outcome = 'OPENED' AND NOT EXISTS (SELECT 1 FROM crew_paper_trades t
        WHERE t.paper_trade_id = NEW.paper_trade_id AND t.account_id = NEW.account_id
        AND t.admission_id = NEW.admission_id);
END;

CREATE TRIGGER IF NOT EXISTS crew_paper_trades_insert BEFORE INSERT ON crew_paper_trades
BEGIN
    SELECT RAISE(ABORT, 'crew_paper_trades: a paper trade starts OPEN at version 1')
    WHERE NEW.state IS NOT 'OPEN' OR NEW.version IS NOT 1 OR NEW.lots_open IS NOT NEW.lots_initial;
    SELECT RAISE(ABORT, 'crew_paper_trades: the model trade must come from the same research admission')
    WHERE NOT coalesce((SELECT m.admission_id = NEW.admission_id AND m.admission_class = 'RESEARCH_ONLY'
                        AND m.market_day = NEW.market_day AND m.strategy = NEW.strategy_id
                        AND m.direction = NEW.direction
                        FROM crew_trades m WHERE m.trade_id = NEW.model_trade_id), 0);
END;

CREATE TRIGGER IF NOT EXISTS crew_paper_trades_update BEFORE UPDATE ON crew_paper_trades
BEGIN
    SELECT RAISE(ABORT, 'crew_paper_trades: terminal paper trades cannot change')
    WHERE OLD.state IN {_PAPER_TERMINAL};
    SELECT RAISE(ABORT, 'crew_paper_trades: identity columns are immutable')
    WHERE NEW.paper_trade_id IS NOT OLD.paper_trade_id OR NEW.account_id IS NOT OLD.account_id
       OR NEW.admission_id IS NOT OLD.admission_id OR NEW.model_trade_id IS NOT OLD.model_trade_id
       OR NEW.strategy_id IS NOT OLD.strategy_id OR NEW.contract IS NOT OLD.contract
       OR NEW.lot_size IS NOT OLD.lot_size OR NEW.lots_initial IS NOT OLD.lots_initial
       OR NEW.capital_committed_inr IS NOT OLD.capital_committed_inr OR NEW.max_loss_inr IS NOT OLD.max_loss_inr
       OR NEW.opened_at IS NOT OLD.opened_at OR NEW.registry_hash IS NOT OLD.registry_hash;
    SELECT RAISE(ABORT, 'crew_paper_trades: update must match the next crew_paper_events row')
    WHERE NOT coalesce((SELECT NEW.version = OLD.version + 1 AND e.from_state = OLD.state
                        AND e.to_state = NEW.state AND e.lots_after = NEW.lots_open
                        FROM crew_paper_events e
                        WHERE e.paper_trade_id = OLD.paper_trade_id AND e.seq = NEW.version), 0);
END;

CREATE TRIGGER IF NOT EXISTS crew_paper_trades_no_delete BEFORE DELETE ON crew_paper_trades
BEGIN SELECT RAISE(ABORT, 'crew_paper_trades rows cannot be deleted'); END;

CREATE TRIGGER IF NOT EXISTS crew_paper_events_insert BEFORE INSERT ON crew_paper_events
BEGIN
    SELECT RAISE(ABORT, 'crew_paper_events: illegal paper state transition')
    WHERE NOT ((NEW.seq = 1 AND NEW.from_state IS NULL AND NEW.event_type = 'ENTRY' AND NEW.to_state = 'OPEN')
        OR (NEW.seq > 1 AND NEW.from_state IN {_PAPER_OPEN} AND (
               (NEW.event_type = 'PARTIAL_EXIT' AND NEW.to_state = 'PARTIALLY_EXITED')
            OR (NEW.event_type = 'PARTIAL_SKIPPED' AND NEW.to_state = NEW.from_state)
            OR (NEW.event_type = 'EXIT' AND NEW.to_state = 'CLOSED' AND NEW.lots_after = 0)
            OR (NEW.event_type IN ('EXIT_UNPRICED','EXPIRED_UNRESOLVED') AND NEW.to_state = NEW.event_type))));
    SELECT RAISE(ABORT, 'crew_paper_events: event does not follow the paper trade state and version')
    WHERE NOT coalesce((SELECT CASE WHEN NEW.seq = 1
                THEN t.version = 1 AND t.state = 'OPEN' AND NEW.lots_after = t.lots_initial
                     AND NEW.lots = t.lots_initial
                ELSE t.version + 1 = NEW.seq AND t.state = NEW.from_state
                     AND NEW.lots_after = t.lots_open - coalesce(NEW.lots, 0)
                END FROM crew_paper_trades t WHERE t.paper_trade_id = NEW.paper_trade_id), 0);
END;
{_no_update_delete("crew_paper_events")}

UPDATE crew_meta SET value = '4', set_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE key = 'schema_version' AND CAST(value AS INTEGER) < 4;
"""


SCHEMA_V5 = f"""
CREATE TABLE IF NOT EXISTS crew_paper_account_identity (
    account_id TEXT PRIMARY KEY REFERENCES crew_paper_accounts(account_id),
    registry_hash TEXT NOT NULL CHECK (length(registry_hash) = 64),
    strategy_version TEXT NOT NULL,
    bound_at TEXT NOT NULL,
    provenance TEXT NOT NULL
);
{_no_update_delete("crew_paper_account_identity")}
UPDATE crew_meta SET value = '5', set_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE key = 'schema_version' AND CAST(value AS INTEGER) < 5;
"""


SCHEMA_V6 = f"""
CREATE TABLE IF NOT EXISTS crew_paper_risk_decisions (
    account_id TEXT NOT NULL,
    admission_id TEXT NOT NULL,
    risk_policy_id TEXT NOT NULL CHECK (risk_policy_id = 'paper-risk-v2'),
    evidence TEXT NOT NULL CHECK (json_valid(evidence) AND json_type(evidence) = 'object'),
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (account_id, admission_id),
    FOREIGN KEY (account_id, admission_id) REFERENCES crew_paper_decisions(account_id, admission_id)
);
{_no_update_delete("crew_paper_risk_decisions")}
UPDATE crew_meta SET value = '6', set_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE key = 'schema_version' AND CAST(value AS INTEGER) < 6;
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

    Only CREATE ... IF NOT EXISTS statements, plus v2's nullable ``crew_outbox.chat_id`` and v3's nullable
    ``crew_sessions.generation`` columns; existing rows and legacy tables are never rewritten.
    """
    conn = connect(db_path, create=True)
    try:
        has_meta = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='crew_meta'").fetchone()
        if has_meta:
            row = conn.execute("SELECT value FROM crew_meta WHERE key='schema_version'").fetchone()
            if row and int(row[0]) > SCHEMA_VERSION:
                raise RuntimeError(f"crew journal schema {row[0]} is newer than this code ({SCHEMA_VERSION})")
        has_chat = any(r[1] == "chat_id" for r in conn.execute("PRAGMA table_info(crew_outbox)"))
        has_gen = any(r[1] == "generation" for r in conn.execute("PRAGMA table_info(crew_sessions)"))
        try:
            # A concurrent migrator can win the ALTER; this one then rolls back whole and a rerun is a no-op.
            conn.executescript(
                f"BEGIN IMMEDIATE;\n{SCHEMA}\n{'' if has_chat else _V2_ADD_CHAT}\n{SCHEMA_V2}\n"
                f"{'' if has_gen else _V3_ADD_GENERATION}\n{SCHEMA_V3}\n{SCHEMA_V4}\n{SCHEMA_V5}\n{SCHEMA_V6}\nCOMMIT;"
            )
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        return int(conn.execute("SELECT value FROM crew_meta WHERE key='schema_version'").fetchone()[0])
    finally:
        conn.close()


# --- P2 journal API ---------------------------------------------------------------------------

_FORBIDDEN = frozenset({
    "fill_price", "broker_order_id", "broker_qty", "avg_price", "pnl_rupees", "realized_pnl",
    "pnl_inr", "net_pnl_inr", "realized_pnl_inr", "fill_qty", "filled_qty",
})
_LEVEL_KEYS = frozenset({"spot", "stop", "target", "entry"})
_OPEN_STATES = frozenset({"TRACKING", "PARTIALLY_EXITED"})
_ENTRY_TYPES = frozenset({"MODEL_ENTRY", "LEGACY_IMPORT"})
# ponytail: legacy imports use strategy_version "legacy" until a real version is known.
_LEGACY_VERSION = "legacy"


class JournalError(Exception):
    pass


class VersionConflict(JournalError):
    pass


class InvalidTransition(JournalError):
    pass


class TradeIdCollision(JournalError):
    pass


class AdmissionConflict(JournalError):
    pass


class ConsistencyError(JournalError):
    def __init__(self, findings: list[dict]):
        super().__init__(f"{len(findings)} journal inconsistencies")
        self.findings = findings


class LeaseHeld(JournalError):
    """Another session holds an unexpired lease."""


class LeaseLost(JournalError):
    """This session no longer holds the lease: released, expired, taken over, or the market day ended."""


@dataclass(frozen=True)
class Lease:
    """Issued only by ``crew_session.acquire``; the journal stores only the token's SHA-256."""
    session_id: str
    market_day: str
    generation: int
    token: str = dc_field(repr=False)


def canonical_signal_fingerprint(
    *, market_day: str, instrument: str, strategy_id: str, strategy_version: str,
    signal_candle: str, direction: str, setup_id: str,
) -> str:
    """UTF-8 identity hashed by ``signal_id_for``. No clock, pid, or random id.

    One ``name=value`` line, in this order: market_day, instrument, strategy_id,
    strategy_version, signal_candle, direction, setup_id.
    """
    day = _market_day(market_day)
    candle = _candle(signal_candle)
    if candle[:10] != day:
        raise InvalidTransition("signal candle is not on the market day")
    fields = {
        "market_day": day,
        "instrument": _text(instrument).upper(),
        "strategy_id": _text(strategy_id),
        "strategy_version": _text(strategy_version),
        "signal_candle": candle,
        "direction": _text(direction).upper(),
        "setup_id": _text(setup_id),
    }
    if fields["direction"] not in ("CALL", "PUT"):
        raise InvalidTransition("direction must be CALL or PUT")
    if not fields["instrument"] or not fields["strategy_id"] or not fields["setup_id"]:
        raise InvalidTransition("instrument, strategy_id and setup_id are required")
    return "\n".join(f"{name}={fields[name]}" for name in fields)


def signal_id_for(**fields: str) -> str:
    """Lowercase SHA-256 hex of the canonical fingerprint."""
    return hashlib.sha256(canonical_signal_fingerprint(**fields).encode("utf-8")).hexdigest()


def trade_id_for(market_day: str, signal_id: str) -> str:
    day = _market_day(market_day)
    if len(signal_id) != 64 or any(c not in "0123456789abcdef" for c in signal_id):
        raise InvalidTransition("signal_id must be 64 lowercase hex characters")
    return f"T_{day.replace('-', '')}_{signal_id[:12]}"


def connect_readonly(db_path: Path | str) -> sqlite3.Connection:
    """Read-only connection. Does not create the file and cannot write it."""
    path = Path(db_path).resolve()
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def record_admission(
    db_path: Path | str, *, lease: Lease, admission_class: str, reasons: list[str] | tuple[str, ...],
    market_day: str, instrument: str, strategy_id: str, strategy_version: str, signal_candle: str,
    direction: str, setup_id: str, inputs: dict | None = None, decided_at: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Insert one admission for the lease's session, or return it when the same signal is replayed."""
    if admission_class not in ("ACTIONABLE", "RESEARCH_ONLY", "REJECTED"):
        raise InvalidTransition("admission class is not ACTIONABLE, RESEARCH_ONLY or REJECTED")
    cleaned = _reason_list(reasons)
    parts, sid = _identity(
        market_day=market_day, instrument=instrument, strategy_id=strategy_id,
        strategy_version=strategy_version, signal_candle=signal_candle, direction=direction, setup_id=setup_id,
    )
    decided = parts["signal_candle"] if decided_at is None else _candle(decided_at)
    if decided[:10] != parts["market_day"]:
        raise InvalidTransition("decided_at is not on the market day")
    extra = dict(inputs or {})
    for key, value in extra.items():
        if key in parts and value != parts[key]:
            raise InvalidTransition(f"inputs[{key}] disagrees with the signal identity")
    _reject_forbidden(extra)
    stored = {**extra, **parts}
    aid = f"A_{parts['market_day'].replace('-', '')}_{sid[:12]}"
    session_id = getattr(lease, "session_id", None)

    def op(conn: sqlite3.Connection) -> dict:
        if parts["market_day"] != lease.market_day:
            raise InvalidTransition(f"admission market day {parts['market_day']} is not the lease's {lease.market_day}")
        row = conn.execute(
            "SELECT * FROM crew_admissions WHERE signal_id=? AND market_day=?", (sid, parts["market_day"])
        ).fetchone()
        if row is not None:
            if not _admission_matches(row, session_id=session_id, admission_class=admission_class,
                                      reasons=cleaned, stored=stored, decided=decided):
                raise AdmissionConflict(f"admission {row['admission_id']} already records this signal")
            return {"replayed": True, "admission": _public_admission(row)}
        clash = conn.execute("SELECT signal_id FROM crew_admissions WHERE admission_id=?", (aid,)).fetchone()
        if clash is not None:
            raise TradeIdCollision(f"admission id {aid} belongs to signal {clash['signal_id']}")
        conn.execute(
            "INSERT INTO crew_admissions (admission_id, signal_id, market_day, session_id, class, reasons,"
            " inputs, decided_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (aid, sid, parts["market_day"], session_id, admission_class,
             json.dumps(cleaned, separators=(",", ":")),
             json.dumps(stored, sort_keys=True, separators=(",", ":")), decided),
        )
        saved = conn.execute("SELECT * FROM crew_admissions WHERE admission_id=?", (aid,)).fetchone()
        return {"replayed": False, "admission": _public_admission(saved)}

    return _fenced(db_path, lease, now, op)


def open_trade(
    db_path: Path | str, *, lease: Lease, admission_id: str, model_lots: int, entry_spot: float,
    stop_spot: float, target_spot: float, contract: str | None = None, expiry: str | None = None,
    lot_size_ref: int | None = None, execution_policy: str | None = None, now: datetime | None = None,
) -> dict:
    """Open one TRACKING trade and its MODEL_ENTRY from this session's admission, or return the existing trade."""
    lots = _pos_int("model_lots", model_lots)
    entry_spot, stop_spot, target_spot = _number("entry_spot", entry_spot), _number("stop_spot", stop_spot), _number("target_spot", target_spot)
    if lot_size_ref is not None:
        lot_size_ref = _pos_int("lot_size_ref", lot_size_ref)

    def op(conn: sqlite3.Connection) -> dict:
        admission = conn.execute("SELECT * FROM crew_admissions WHERE admission_id=?", (admission_id,)).fetchone()
        if admission is None:
            raise InvalidTransition(f"unknown admission {admission_id}")
        if admission["class"] not in ("ACTIONABLE", "RESEARCH_ONLY"):
            raise InvalidTransition("a rejected admission is not a trade")
        if admission["session_id"] != lease.session_id:
            raise InvalidTransition(f"admission {admission_id} belongs to session {admission['session_id']},"
                                    f" not {lease.session_id}")
        parts = json.loads(admission["inputs"])
        sid, day = admission["signal_id"], admission["market_day"]
        tid = trade_id_for(day, sid)
        existing = conn.execute("SELECT * FROM crew_trades WHERE signal_id=?", (sid,)).fetchone()
        if existing is not None:
            if existing["legacy_ref"] or existing["admission_id"] != admission_id:
                raise TradeIdCollision(f"signal {sid} is already recorded as {existing['trade_id']}")
            if not _same_open(existing, lots=lots, entry=entry_spot, stop=stop_spot, target=target_spot,
                              contract=contract, expiry=expiry, lot_size_ref=lot_size_ref):
                raise InvalidTransition("open_trade does not match the stored trade")
            event = _event_by_key(conn, f"{existing['trade_id']}:entry")
            if event is None:
                raise ConsistencyError([{"kind": "missing_entry", "trade_id": existing["trade_id"]}])
            return {"replayed": True, "trade": dict(existing), "event": _public_event(event)}
        clash = conn.execute("SELECT signal_id FROM crew_trades WHERE trade_id=?", (tid,)).fetchone()
        if clash is not None:
            raise TradeIdCollision(f"{tid} already belongs to signal {clash['signal_id']}")
        payload = {
            "direction": parts["direction"], "entry_spot": entry_spot, "instrument": parts["instrument"],
            "model_lots": lots, "setup_id": parts["setup_id"], "signal_candle": parts["signal_candle"],
            "stop_spot": stop_spot, "strategy_id": parts["strategy_id"],
            "strategy_version": parts["strategy_version"], "target_spot": target_spot,
        }
        if contract is not None:
            payload["contract"] = contract
        if expiry is not None:
            payload["expiry"] = expiry
        if execution_policy is not None:
            payload["execution_policy"] = _text(execution_policy)
        _reject_forbidden(payload)
        conn.execute(
            "INSERT INTO crew_trades (trade_id, signal_id, admission_id, admission_class, market_day,"
            " owner_session_id, state, strategy, direction, contract, expiry, lot_size_ref, entry_spot,"
            " stop_spot, target_spot, model_lots_initial, model_lots_open, opened_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'TRACKING', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tid, sid, admission_id, admission["class"], day, admission["session_id"], parts["strategy_id"],
             parts["direction"], contract, expiry, lot_size_ref, entry_spot, stop_spot, target_spot,
             lots, lots, parts["signal_candle"]),
        )
        # Event follows the insert: seq 1 must observe the trade already at TRACKING / version 1.
        key = f"{tid}:entry"
        _insert_event(conn, key=key, trade_id=tid, seq=1, event_type="MODEL_ENTRY", from_state=None,
                      to_state="TRACKING", session_id=admission["session_id"], occurred_at=parts["signal_candle"],
                      payload=payload)
        return {"replayed": False, "trade": dict(_trade(conn, tid)), "event": _public_event(_event_by_key(conn, key))}

    return _fenced(db_path, lease, now, op)


def partial_exit(
    db_path: Path | str, *, lease: Lease, trade_id: str, expected_version: int, reason: str, lots_after: int,
    signal_candle: str, index_levels: dict, idempotency_key: str | None = None, now: datetime | None = None,
) -> dict:
    """Reduce hypothetical model lots once per idempotency key."""
    version = _version(expected_version)
    why = _text(reason)
    if not why:
        raise InvalidTransition("partial exit needs a reason")
    if type(lots_after) is not int or lots_after < 1:
        raise InvalidTransition("lots_after must leave at least one model lot")
    candle = _candle(signal_candle)
    levels = _levels(index_levels)
    key = idempotency_key or f"{trade_id}:partial:{candle}:{lots_after}:{why}"

    def op(conn: sqlite3.Connection) -> dict:
        prior = _event_by_key(conn, key)
        if prior is not None:
            return _replay_partial(conn, prior, trade_id=trade_id, reason=why, lots_after=lots_after,
                                   candle=candle, levels=levels)
        trade = _require_open(conn, trade_id, version, lease)
        before = trade["model_lots_open"]
        if lots_after >= before:
            raise InvalidTransition("partial exit must reduce model lots")
        payload = {"index_levels": levels, "lots_after": lots_after, "lots_before": before,
                   "reason": why, "signal_candle": candle}
        return _project(conn, trade, event_type="MODEL_PARTIAL_EXIT", to_state="PARTIALLY_EXITED", key=key,
                        occurred_at=candle, payload=payload, lots=lots_after, stop=trade["stop_spot"],
                        target=trade["target_spot"], closed_at=None)

    return _fenced(db_path, lease, now, op)


def exit_trade(
    db_path: Path | str, *, lease: Lease, trade_id: str, expected_version: int, reason: str, signal_candle: str,
    index_levels: dict, model_points: float | None = None, model_r: float | None = None,
    now: datetime | None = None,
) -> dict:
    """Close the model once. A second request returns that exit and writes nothing."""
    version = _version(expected_version)
    why = _text(reason)
    if not why:
        raise InvalidTransition("exit needs a reason")
    candle = _candle(signal_candle)
    levels = _levels(index_levels)
    points = None if model_points is None else _number("model_points", model_points)
    r_multiple = None if model_r is None else _number("model_r", model_r)
    key = f"{trade_id}:EXIT"

    def op(conn: sqlite3.Connection) -> dict:
        prior = _event_by_key(conn, key)
        if prior is not None:
            if prior["trade_id"] != trade_id or prior["event_type"] != "MODEL_EXIT":
                raise InvalidTransition(f"exit key {key} belongs to another event")
            return {"replayed": True, "trade": dict(_trade(conn, trade_id)), "event": _public_event(prior)}
        trade = _require_open(conn, trade_id, version, lease)
        payload = {"index_levels": levels, "reason": why, "signal_candle": candle}
        if points is not None:
            payload["model_points"] = points
        if r_multiple is not None:
            payload["model_r"] = r_multiple
        return _project(conn, trade, event_type="MODEL_EXIT", to_state="CLOSED", key=key, occurred_at=candle,
                        payload=payload, lots=0, stop=trade["stop_spot"], target=trade["target_spot"],
                        closed_at=candle)

    return _fenced(db_path, lease, now, op)


def update_levels(
    db_path: Path | str, *, lease: Lease, trade_id: str, expected_version: int, signal_candle: str,
    stop_spot: float | None = None, target_spot: float | None = None, reason: str | None = None,
    idempotency_key: str | None = None, now: datetime | None = None,
) -> dict:
    """Record a stop or target move without changing model state or lots."""
    version = _version(expected_version)
    if stop_spot is None and target_spot is None:
        raise InvalidTransition("update_levels needs a stop or a target")
    candle = _candle(signal_candle)
    new_stop = None if stop_spot is None else _number("stop_spot", stop_spot)
    new_target = None if target_spot is None else _number("target_spot", target_spot)
    why = _text(reason or "LEVELS")

    def op(conn: sqlite3.Connection) -> dict:
        trade = _trade(conn, trade_id)
        if trade is None:
            raise InvalidTransition(f"unknown trade {trade_id}")
        stop = trade["stop_spot"] if new_stop is None else new_stop
        target = trade["target_spot"] if new_target is None else new_target
        key = idempotency_key or f"{trade_id}:levels:{candle}:{_num(stop)}:{_num(target)}"
        prior = _event_by_key(conn, key)
        if prior is not None:
            stored = json.loads(prior["payload"])
            if (prior["trade_id"] != trade_id or stored.get("signal_candle") != candle
                    or not _same_number(stored.get("stop_spot"), stop)
                    or not _same_number(stored.get("target_spot"), target)):
                raise InvalidTransition("idempotency key reused with different levels")
            return {"replayed": True, "trade": dict(trade), "event": _public_event(prior)}
        _owned_by(trade, lease)
        if trade["state"] not in _OPEN_STATES:
            raise InvalidTransition(f"{trade_id} is {trade['state']} and cannot change")
        if trade["version"] != version:
            raise VersionConflict(f"{trade_id} is at version {trade['version']}, not {version}")
        payload = {"reason": why, "signal_candle": candle, "stop_spot": stop, "target_spot": target}
        return _project(conn, trade, event_type="MODEL_LEVELS_UPDATED", to_state=trade["state"], key=key,
                        occurred_at=candle, payload=payload, lots=trade["model_lots_open"], stop=stop,
                        target=target, closed_at=None)

    return _fenced(db_path, lease, now, op)


def expire_unresolved(db_path: Path | str, *, lease: Lease, at: str, now: datetime | None = None) -> list[dict]:
    """Mark trades open before the lease's market day EXPIRED_UNRESOLVED. Does not invent a price or P&L."""
    stamp = _candle(at)
    if not isinstance(lease, Lease) or stamp[:10] != lease.market_day:
        raise InvalidTransition("expiry time must fall on the lease's market day")
    return _fenced(db_path, lease, now, lambda conn: _expire_unresolved(conn, lease.market_day, stamp))


def _expire_unresolved(conn: sqlite3.Connection, day: str, stamp: str) -> list[dict]:
    pending = conn.execute(
        "SELECT * FROM crew_trades WHERE state IN ('TRACKING','PARTIALLY_EXITED') AND market_day < ?"
        " ORDER BY trade_id", (day,)
    ).fetchall()
    results = []
    seen = set()
    for trade in pending:
        key = f"{trade['trade_id']}:EXPIRED_UNRESOLVED"
        prior = _event_by_key(conn, key)
        if prior is not None:
            if trade["state"] != "EXPIRED_UNRESOLVED":
                raise ConsistencyError([{"kind": "projection_mismatch", "trade_id": trade["trade_id"]}])
            results.append({"replayed": True, "trade": dict(trade), "event": _public_event(prior)})
        else:
            payload = {"as_of_day": day, "market_day": trade["market_day"], "reason": "PRIOR_MARKET_DAY"}
            results.append(_project(
                conn, trade, event_type="EXPIRED_UNRESOLVED", to_state="EXPIRED_UNRESOLVED", key=key,
                occurred_at=stamp, payload=payload, lots=trade["model_lots_open"], stop=trade["stop_spot"],
                target=trade["target_spot"], closed_at=stamp,
            ))
        seen.add(trade["trade_id"])
    already = conn.execute(
        "SELECT * FROM crew_trades WHERE state='EXPIRED_UNRESOLVED' AND market_day < ? ORDER BY trade_id",
        (day,),
    ).fetchall()
    for trade in already:
        if trade["trade_id"] in seen:
            continue
        event = conn.execute(
            "SELECT * FROM crew_events WHERE trade_id=? AND event_type='EXPIRED_UNRESOLVED' ORDER BY seq DESC LIMIT 1",
            (trade["trade_id"],),
        ).fetchone()
        results.append({"replayed": True, "trade": dict(trade), "event": _public_event(event)})
    return results


def quarantine_legacy_reference(db_path: Path | str, source_path: Path | str) -> list[dict]:
    """Record OPEN rows from a legacy JSON file. The file is only read."""
    path = Path(source_path)
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        book = json.loads(raw)
    except json.JSONDecodeError as e:
        raise InvalidTransition(f"legacy journal is not JSON: {path}") from e
    if not isinstance(book, dict):
        raise InvalidTransition("legacy journal must be an object of trades")
    opened = [(str(key), book[key]) for key in sorted(book)
              if isinstance(book[key], dict) and book[key].get("status") == "OPEN"]
    resolved = str(path.resolve())

    def op(conn: sqlite3.Connection) -> list[dict]:
        results = []
        for key, rec in opened:
            for field in ("symbol", "strategy", "direction", "timestamp", "lots"):
                if rec.get(field) in (None, ""):
                    raise InvalidTransition(f"{key} is missing {field}")
            lots = rec["lots"]
            if type(lots) is not int or lots < 1:
                raise InvalidTransition(f"{key} lots must be a positive integer")
            parts, sid = _identity(
                market_day=_candle(rec["timestamp"])[:10], instrument=rec["symbol"], strategy_id=rec["strategy"],
                strategy_version=_LEGACY_VERSION, signal_candle=rec["timestamp"], direction=rec["direction"],
                setup_id=rec.get("suggested_strike") or key,
            )
            legacy_ref = f"{resolved}#{digest[:8]}:{key}"
            existing = conn.execute("SELECT * FROM crew_trades WHERE trade_id=?", (key,)).fetchone()
            if existing is not None:
                if existing["legacy_ref"] != legacy_ref or existing["signal_id"] != sid or existing["state"] != "QUARANTINED":
                    raise TradeIdCollision(f"legacy trade {key} does not match the stored quarantine")
                event = _event_by_key(conn, f"{key}:legacy")
                results.append({"replayed": True, "trade": dict(existing), "event": _public_event(event)})
                continue
            clash = conn.execute("SELECT trade_id FROM crew_trades WHERE signal_id=?", (sid,)).fetchone()
            if clash is not None:
                raise TradeIdCollision(f"signal {sid} is already recorded as {clash['trade_id']}")
            payload = {"legacy_ref": legacy_ref, "source_sha256": digest, "source_trade_id": key}
            _reject_forbidden(payload)
            # closed_at is the source bar: QUARANTINED is not an open state, and this is not an exit price.
            conn.execute(
                "INSERT INTO crew_trades (trade_id, signal_id, admission_class, market_day, state, strategy,"
                " direction, entry_spot, stop_spot, target_spot, model_lots_initial, model_lots_open, opened_at,"
                " closed_at, legacy_ref) VALUES (?, ?, 'RESEARCH_ONLY', ?, 'QUARANTINED', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (key, sid, parts["market_day"], parts["strategy_id"], parts["direction"], rec.get("entry_price"),
                 rec.get("stop_loss"), rec.get("target_price"), lots, lots, parts["signal_candle"],
                 parts["signal_candle"], legacy_ref),
            )
            event_key = f"{key}:legacy"
            _insert_event(conn, key=event_key, trade_id=key, seq=1, event_type="LEGACY_IMPORT", from_state=None,
                          to_state="QUARANTINED", session_id=None, occurred_at=parts["signal_candle"], payload=payload)
            results.append({"replayed": False, "trade": dict(_trade(conn, key)),
                            "event": _public_event(_event_by_key(conn, event_key))})
        return results

    return _immediate(db_path, op)


def load_open_trades(db_path: Path | str, *, market_day: str | None = None) -> list[dict]:
    """Return TRACKING and PARTIALLY_EXITED projections. Read-only."""
    day = None if market_day is None else _market_day(market_day)
    conn = connect_readonly(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM crew_trades WHERE state IN ('TRACKING','PARTIALLY_EXITED')"
            " AND (? IS NULL OR market_day=?) ORDER BY market_day, opened_at, trade_id",
            (day, day),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_trade_events(db_path: Path | str, trade_id: str) -> list[dict]:
    conn = connect_readonly(db_path)
    try:
        rows = conn.execute("SELECT * FROM crew_events WHERE trade_id=? ORDER BY seq", (trade_id,)).fetchall()
        return [_public_event(row) for row in rows]
    finally:
        conn.close()


def check_consistency(db_path: Path | str) -> list[dict]:
    """Report orphan events, missing entries, sequence gaps and projection mismatches.

    Reads only. Never repairs.
    """
    conn = connect_readonly(db_path)
    try:
        return _consistency_findings(conn)
    finally:
        conn.close()


def _consistency_findings(conn: sqlite3.Connection) -> list[dict]:
    trades = [dict(row) for row in conn.execute("SELECT * FROM crew_trades ORDER BY trade_id")]
    events = [dict(row) for row in conn.execute("SELECT * FROM crew_events ORDER BY trade_id, seq")]
    trade_ids = {trade["trade_id"] for trade in trades}
    grouped: dict[str, list[dict]] = {}
    findings = []
    for event in events:
        if event["trade_id"] not in trade_ids:
            findings.append({"kind": "orphan_event", "event_id": event["event_id"], "trade_id": event["trade_id"]})
            continue
        grouped.setdefault(event["trade_id"], []).append(event)
    for trade in trades:
        evs = grouped.get(trade["trade_id"], [])
        if not evs or evs[0]["seq"] != 1 or evs[0]["event_type"] not in _ENTRY_TYPES:
            findings.append({"kind": "missing_entry", "trade_id": trade["trade_id"]})
        seqs = [event["seq"] for event in evs]
        if seqs != list(range(1, len(seqs) + 1)):
            findings.append({"kind": "sequence_gap", "trade_id": trade["trade_id"], "seqs": seqs})
        if evs and trade["version"] != evs[-1]["seq"]:
            findings.append({"kind": "version_mismatch", "trade_id": trade["trade_id"],
                             "version": trade["version"], "last_seq": evs[-1]["seq"]})
        if evs and _projection_disagrees(trade, evs):
            projected = _projected_lots(trade["model_lots_initial"], evs)
            findings.append({"kind": "projection_mismatch", "trade_id": trade["trade_id"],
                             "state": trade["state"], "event_state": evs[-1]["to_state"],
                             "model_lots_open": trade["model_lots_open"], "projected_lots": projected})
    return findings


def _immediate(db_path: Path | str, fn):
    """Run ``fn`` in one BEGIN IMMEDIATE transaction. A ``JournalError`` that ``fn`` *returns* is a refusal
    whose audit row must survive: the transaction commits first, then it is raised."""
    conn = connect(db_path)
    try:
        with transaction(conn):
            result = fn(conn)
    except sqlite3.IntegrityError as e:
        if "duplicate trade" in str(e):
            raise TradeIdCollision(str(e)) from e
        raise InvalidTransition(str(e)) from e
    finally:
        conn.close()
    if isinstance(result, JournalError):
        raise result
    return result


def _fenced(db_path: Path | str, lease: Lease, now: datetime | None, fn):
    """``_immediate`` for operational writes: the lease is verified inside the same transaction, before ``fn``."""
    if not isinstance(lease, Lease):
        raise InvalidTransition("operational writes need the session Lease from crew_session.acquire")
    stamp = _ist(now)
    return _immediate(db_path, lambda conn: _fence(conn, lease, stamp) or fn(conn))


def _fence(conn, lease: Lease, stamp: datetime, *, allow_expired: bool = False) -> LeaseLost | None:
    """Return (and audit) why ``lease`` may not act at ``stamp``, or None if it may."""
    held = _lease_row(conn)
    if not _mine(held, lease):
        why = "lease belongs to another generation"
    elif held["released_at"] is not None:
        why = "lease was released"
    elif not allow_expired and datetime.fromisoformat(held["expires_at"]) <= stamp:
        why = f"lease expired at {held['expires_at']}"
    elif not allow_expired and stamp.date().isoformat() != lease.market_day:
        why = f"market day {lease.market_day} has ended"
    else:
        return None
    day = stamp.date().isoformat()
    if conn.execute("SELECT 1 FROM crew_sessions WHERE session_id=?", (lease.session_id,)).fetchone():
        _session_event(conn, "FENCED", day=day, at=stamp.isoformat(), session_id=lease.session_id,
                       generation=lease.generation, prior=held, detail={"reason": why})
    else:  # a forged session id cannot be a FENCED row (FK), so its refusal is audited anonymously
        _session_event(conn, "REFUSED", day=day, at=stamp.isoformat(), prior=held,
                       detail={"reason": why, "claimed_session_id": str(lease.session_id)})
    return LeaseLost(f"{lease.session_id}: {why}")


def _mine(held, lease: Lease) -> bool:
    return (held is not None and held["generation"] == lease.generation
            and held["session_id"] == lease.session_id and held["token_sha256"] == _sha(lease.token))


def _session_event(conn, event_type, *, day, at, session_id=None, generation=None, prior=None, detail) -> None:
    conn.execute(
        "INSERT INTO crew_session_events (event_type, session_id, generation, prior_session_id, prior_generation,"
        " market_day, at, pid, host, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (event_type, session_id, generation, prior["session_id"] if prior else None,
         prior["generation"] if prior else None, day, at, os.getpid(), socket.gethostname(),
         json.dumps(detail, sort_keys=True, separators=(",", ":"))),
    )


def _lease_row(conn):
    return conn.execute("SELECT * FROM crew_lease WHERE id=1").fetchone()


def _sha(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def _ist(now: datetime | None) -> datetime:
    value = datetime.now(IST) if now is None else now
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise InvalidTransition("timestamps must be timezone-aware")
    return value.astimezone(IST)


def _owned_by(trade, lease: Lease) -> None:
    # Ownership never transfers: a current-day orphan stays untouched until a later day expires it.
    if trade["owner_session_id"] != lease.session_id:
        raise InvalidTransition(f"{trade['trade_id']} belongs to session {trade['owner_session_id']},"
                                f" not {lease.session_id}")


def _project(conn, trade, *, event_type, to_state, key, occurred_at, payload, lots, stop, target, closed_at) -> dict:
    # The UPDATE trigger requires this event to exist while the trade is still at the old version.
    seq = trade["version"] + 1
    _insert_event(conn, key=key, trade_id=trade["trade_id"], seq=seq, event_type=event_type,
                  from_state=trade["state"], to_state=to_state, session_id=trade["owner_session_id"],
                  occurred_at=occurred_at, payload=payload)
    cur = conn.execute(
        "UPDATE crew_trades SET state=?, model_lots_open=?, stop_spot=?, target_spot=?, version=?, closed_at=?"
        " WHERE trade_id=? AND version=?",
        (to_state, lots, stop, target, seq, closed_at, trade["trade_id"], trade["version"]),
    )
    if cur.rowcount != 1:
        raise VersionConflict(trade["trade_id"])
    return {"replayed": False, "trade": dict(_trade(conn, trade["trade_id"])), "event": _public_event(_event_by_key(conn, key))}


# ponytail: tests set this to "event" to fail after an earlier write in the same transaction.
_failpoint = None


def _insert_event(conn, *, key, trade_id, seq, event_type, from_state, to_state, session_id, occurred_at, payload):
    if _failpoint == "event":
        raise sqlite3.IntegrityError("injected event failure")
    _reject_forbidden(payload)
    conn.execute(
        "INSERT INTO crew_events (event_id, trade_id, seq, event_type, from_state, to_state, idempotency_key,"
        " session_id, occurred_at, recorded_at, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (key, trade_id, seq, event_type, from_state, to_state, key, session_id, occurred_at, _recorded_at(),
         json.dumps(payload, sort_keys=True, separators=(",", ":"))),
    )


def _require_open(conn, trade_id: str, version: int, lease: Lease):
    trade = _trade(conn, trade_id)
    if trade is None:
        raise InvalidTransition(f"unknown trade {trade_id}")
    _owned_by(trade, lease)
    if trade["state"] not in _OPEN_STATES:
        raise InvalidTransition(f"{trade_id} is {trade['state']} and cannot change")
    if trade["version"] != version:
        raise VersionConflict(f"{trade_id} is at version {trade['version']}, not {version}")
    return trade


def _replay_partial(conn, prior, *, trade_id, reason, lots_after, candle, levels) -> dict:
    stored = json.loads(prior["payload"])
    if (prior["trade_id"] != trade_id or prior["event_type"] != "MODEL_PARTIAL_EXIT"
            or stored.get("reason") != reason or stored.get("lots_after") != lots_after
            or stored.get("signal_candle") != candle or stored.get("index_levels") != levels):
        raise InvalidTransition("idempotency key reused with a different partial exit")
    return {"replayed": True, "trade": dict(_trade(conn, trade_id)), "event": _public_event(prior)}


def _projection_disagrees(trade: dict, events: list[dict]) -> bool:
    if trade["state"] != events[-1]["to_state"]:
        return True
    return _projected_lots(trade["model_lots_initial"], events) != trade["model_lots_open"]


def _projected_lots(initial: int, events: list[dict]):
    lots = initial
    for event in events:
        if event["event_type"] == "MODEL_EXIT":
            lots = 0
        elif event["event_type"] == "MODEL_PARTIAL_EXIT":
            try:
                payload = json.loads(event["payload"])
            except json.JSONDecodeError:
                return None
            after = payload.get("lots_after")
            if payload.get("lots_before") != lots or type(after) is not int:
                return None
            lots = after
    return lots


def _identity(**fields: str) -> tuple[dict[str, str], str]:
    text = canonical_signal_fingerprint(**fields)
    parts = dict(line.split("=", 1) for line in text.split("\n"))
    return parts, hashlib.sha256(text.encode("utf-8")).hexdigest()


def _admission_matches(row, *, session_id, admission_class, reasons, stored, decided) -> bool:
    return (row["session_id"] == session_id and row["class"] == admission_class
            and json.loads(row["reasons"]) == reasons and json.loads(row["inputs"]) == stored
            and row["decided_at"] == decided)


def _same_open(trade, *, lots, entry, stop, target, contract, expiry, lot_size_ref) -> bool:
    return (trade["model_lots_initial"] == lots and _same_number(trade["entry_spot"], entry)
            and _same_number(trade["stop_spot"], stop) and _same_number(trade["target_spot"], target)
            and trade["contract"] == contract and trade["expiry"] == expiry
            and trade["lot_size_ref"] == lot_size_ref)


def _public_admission(row) -> dict:
    body = dict(row)
    body["reasons"] = json.loads(body["reasons"])
    body["inputs"] = json.loads(body["inputs"])
    return body


def _public_event(row) -> dict:
    body = dict(row)
    body["payload"] = json.loads(body["payload"])
    return body


def _trade(conn, trade_id: str):
    return conn.execute("SELECT * FROM crew_trades WHERE trade_id=?", (trade_id,)).fetchone()


def _event_by_key(conn, key: str):
    return conn.execute("SELECT * FROM crew_events WHERE idempotency_key=?", (key,)).fetchone()


def _reject_forbidden(value) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in _FORBIDDEN:
                raise InvalidTransition(f"forbidden payload key {key}")
            _reject_forbidden(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_forbidden(item)


def _levels(levels: dict) -> dict:
    if not isinstance(levels, dict) or not levels:
        raise InvalidTransition("index levels are required")
    _reject_forbidden(levels)
    cleaned = {}
    for key, value in levels.items():
        if key not in _LEVEL_KEYS:
            raise InvalidTransition(f"unknown index level {key}")
        cleaned[key] = _number(key, value)
    return cleaned


def _reason_list(reasons) -> list[str]:
    if isinstance(reasons, str) or not isinstance(reasons, (list, tuple)):
        raise InvalidTransition("reasons must be a list of strings")
    cleaned = []
    for reason in reasons:
        if type(reason) is not str or not reason.strip():
            raise InvalidTransition("reasons must be a list of strings")
        cleaned.append(reason.strip())
    return cleaned


def _market_day(value: str) -> str:
    day = _text(value)
    try:
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError
    except ValueError as e:
        raise InvalidTransition("market day must be YYYY-MM-DD") from e
    return day


def _candle(value: str) -> str:
    text = str(value).strip().replace(" ", "T")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text).replace(microsecond=0)
    except ValueError as e:
        raise InvalidTransition(f"not a timestamp: {value}") from e
    return parsed.isoformat()


def _text(value: str) -> str:
    return " ".join(str(value).strip().split())


def _number(name: str, value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise InvalidTransition(f"{name} must be a finite number")
    return value


def _pos_int(name: str, value) -> int:
    if type(value) is not int or value < 1:
        raise InvalidTransition(f"{name} must be a positive integer")
    return value


def _version(value: int) -> int:
    if type(value) is not int or value < 1:
        raise InvalidTransition("expected_version must be a positive integer")
    return value


def _same_number(left, right) -> bool:
    if left is None or right is None:
        return left is right
    return float(left) == float(right)


def _num(value) -> str:
    if value is None:
        return ""
    number = float(value)
    return str(int(number)) if number.is_integer() else format(number, ".10g")


def _recorded_at() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
