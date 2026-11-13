"""option-quote-v1: observation-only option quotes in an isolated research database.

Nothing here feeds admission, sizing or paper fills. A null time stays null with a reason: response time,
last-trade time and retrieval time are each stored as themselves and never stand in for book or Greek time.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.brokers.upstox_api import PATHS, _best, _number, _timestamp, check_underlying, match_instrument, UpstoxMarketData
from app.market.nse import contract_lot_size, lot_size_on
from app.trading.option_quote import IST, MAX_QUOTE_AGE_SECONDS

SCHEMA_VERSION = 'option-quote-v1'
REPO = Path(__file__).resolve().parents[3]
PROTECTED_DIRS = (REPO / 'backend' / 'data', REPO / 'trades')
STALE = timedelta(seconds=MAX_QUOTE_AGE_SECONDS)
# Upstox documents `iv` without a unit. The docs example (0.336) and the 10 Nov 2026 probe (0.1599 beside
# Groww's 16.49 %) are fractions; the v2 option chain example (262.31) is a percent.
UPSTOX_GREEK_IV_UNIT = ('fraction', 'UNDOCUMENTED_INFERRED_FROM_DOC_EXAMPLE_AND_CROSS_SOURCE')


def code_revision(repo: Path = REPO) -> str | None:
    """HEAD, with +dirty when any tracked file differs from it. None when git cannot say."""
    try:
        out = subprocess.run(['git', '-C', str(repo), 'describe', '--always', '--dirty', '--abbrev=40'],
                             capture_output=True, text=True, timeout=5, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out.replace('-dirty', '+dirty') or None


def normalize_iv(raw, unit: str) -> tuple[Decimal | None, str | None]:
    """IV as a fraction (0.16 = 16 %). Returns (value, missing_reason)."""
    if raw is None:
        return None, 'NOT_SUPPLIED'
    try:
        value = Decimal(str(raw))
        fraction = {'fraction': value, 'percent': value / 100}[unit]
    except (InvalidOperation, KeyError):
        return None, 'INVALID'
    # ponytail: 500 % ceiling only rejects unit mix-ups; tighten if deep-OTM expiry-day IVs ever exceed it.
    return (fraction, None) if fraction.is_finite() and 0 < fraction <= 5 else (None, 'INVALID')


def _time(value):
    try:
        return _timestamp(value), None if value is not None else 'NOT_SUPPLIED'
    except ValueError:
        return None, 'INVALID'


def _num(value):
    try:
        return _number(value), None if value is not None else 'NOT_SUPPLIED'
    except ValueError:
        return None, 'INVALID'


def normalize_upstox(*, underlying_key, instrument_key, contracts, quote, greek, underlying, retrieved,
                     lot_changes, code_rev, raw_ids) -> dict:
    """One option-quote-v1 record from parsed Upstox `data` payloads. Raises ValueError on identity mismatch.

    `retrieved` maps 'contracts', 'quote', 'greek' and 'underlying' to the local clock after each response.
    """
    contract = UpstoxMarketData.parse_contracts(check_underlying(underlying_key), contracts).get(instrument_key)
    if contract is None:
        raise ValueError('UPSTOX_CONTRACT_NOT_FOUND')
    token = instrument_key.split('|', 1)[1]
    if contract.get('exchange_token') is not None and str(contract['exchange_token']) != token:
        raise ValueError('OPTION_QUOTE_EXCHANGE_TOKEN_MISMATCH')
    quote, greek = match_instrument(quote, instrument_key), match_instrument(greek, instrument_key)
    underlying = match_instrument(underlying, underlying_key)
    if any(t.utcoffset() is None for t in retrieved.values()):
        raise ValueError('OPTION_QUOTE_RETRIEVAL_TIME_NOT_AWARE')

    missing, flags = {}, set()
    at = retrieved['quote']
    day = at.astimezone(IST).date().isoformat()
    record = {
        'schema_version': SCHEMA_VERSION, 'provider': 'upstox', 'source': 'upstox_api',
        'source_endpoints': {'contract': PATHS[0], 'quote': PATHS[1], 'greek': PATHS[2], 'underlying': PATHS[3]},
        'code_rev': code_rev, 'worktree_dirty': None if code_rev is None else code_rev.endswith('+dirty'),
        'retrieved_at': at, 'contract_retrieved_at': retrieved['contracts'], 'greek_retrieved_at': retrieved['greek'],
        'underlying_retrieved_at': retrieved['underlying'],
        'exchange': contract['exchange'], 'segment': contract['segment'], 'exchange_token': token,
        'provider_instrument_id': instrument_key, 'exchange_trading_symbol': None,
        'provider_trading_symbol': contract['trading_symbol'], 'quote_symbol': quote.get('symbol'),
        'underlying': contract['underlying_symbol'], 'expiry': contract['expiry'],
        'strike': contract['strike_price'], 'option_type': contract['instrument_type'],
        'provider_lot_size': contract['lot_size'], 'book_time': None, 'greek_time': None, 'underlying_time': None,
        'underlying_key': underlying_key, 'greek_method': 'VENDOR_UNDISCLOSED', 'greek_source': 'upstox_api',
        'raw_ids': raw_ids,
    }
    missing.update(exchange_trading_symbol='NOT_DOCUMENTED', book_time='NOT_DOCUMENTED',
                   greek_time='NOT_DOCUMENTED', underlying_time='NOT_DOCUMENTED')
    flags |= {'BOOK_TIME_NOT_SUPPLIED', 'GREEK_TIME_NOT_SUPPLIED', 'UNDERLYING_TIME_NOT_SUPPLIED',
              'GREEK_METHOD_UNDISCLOSED', 'IV_UNIT_UNDOCUMENTED'}

    record['source_response_time'], missing['source_response_time'] = _time(quote.get('timestamp'))
    record['last_trade_time'], missing['last_trade_time'] = _time(quote.get('last_trade_time'))
    for name, row, field in (('last_price', quote, 'last_price'), ('volume', quote, 'volume'), ('oi', quote, 'oi'),
                             ('last_trade_qty', greek, 'ltq'), ('underlying_price', underlying, 'last_price'),
                             ('delta', greek, 'delta'), ('gamma', greek, 'gamma'), ('theta', greek, 'theta'),
                             ('vega', greek, 'vega')):
        record[name], missing[name] = _num(row.get(field))
    if record['delta'] is not None and not -1 <= record['delta'] <= 1:
        record['delta'], missing['delta'] = None, 'INVALID'
    record['iv_raw'] = greek.get('iv')
    record['iv_unit_raw'], record['iv_unit_basis'] = UPSTOX_GREEK_IV_UNIT
    record['iv'], missing['iv'] = normalize_iv(greek.get('iv'), UPSTOX_GREEK_IV_UNIT[0])

    depth = quote.get('depth') if isinstance(quote.get('depth'), dict) else {}
    try:
        (record['bid'], record['bid_qty']), (record['ask'], record['ask_qty']) = (
            _best(depth.get('buy') or [], bid=True), _best(depth.get('sell') or [], bid=False))
        for side in ('bid', 'ask'):
            if record[side] is None:
                missing[side] = missing[side + '_qty'] = 'NOT_SUPPLIED'
    except ValueError:
        record.update(bid=None, ask=None, bid_qty=None, ask_qty=None)
        missing.update(bid='INVALID', ask='INVALID', bid_qty='INVALID', ask_qty='INVALID')
    if record['bid'] is None and record['ask'] is None:
        flags.add('EMPTY_BOOK')
    elif record['bid'] is None or record['ask'] is None:
        flags.add('ONE_SIDED_BOOK')
    elif record['bid'] > record['ask']:
        flags.add('CROSSED_BOOK')

    record['lot_size'] = lot_size_on(lot_changes, record['underlying'], day)
    effective = [c.from_date for c in lot_changes if c.underlying == record['underlying'] and c.from_date <= day]
    record['lot_effective_on'] = max(effective) if effective else None
    if record['lot_size'] is None:
        missing['lot_size'] = missing['lot_effective_on'] = 'NOT_IN_DATED_TABLE'
        flags.add('LOT_SIZE_UNDATED')
    else:
        try:
            contract_lot_size(lot_changes, record['underlying'], day, record['provider_lot_size'])
        except ValueError:
            flags.add('LOT_SIZE_MISMATCH')
        if record['bid_qty'] and record['bid_qty'] < record['lot_size'] or record['ask_qty'] and record['ask_qty'] < record['lot_size']:
            flags.add('TOP_QTY_BELOW_ONE_LOT')

    if record['last_trade_time'] is not None:
        if record['last_trade_time'] > at:
            flags.add('LAST_TRADE_TIME_IN_FUTURE')
        elif at - record['last_trade_time'] > STALE:
            flags.add('LAST_TRADE_STALE')
    if record['source_response_time'] is not None and abs(record['source_response_time'] - at) > STALE:
        flags.add('RESPONSE_TIME_SKEW')
    if max(retrieved.values()) - min(retrieved[k] for k in ('quote', 'greek', 'underlying')) > STALE:
        flags.add('CROSS_ENDPOINT_RETRIEVAL_GAP')
    if date.fromisoformat(record['expiry']).isoformat() < day:
        flags.add('CONTRACT_EXPIRED')
    record['missing_reasons'] = {k: v for k, v in sorted(missing.items()) if v is not None and record.get(k) is None}
    record['quality_flags'] = sorted(flags)
    return record


def _json(value):
    return json.dumps(value, sort_keys=True, default=lambda v: v.isoformat() if isinstance(v, datetime) else str(v))


class ObservationStore:
    """Append-only SQLite file for raw responses and option-quote-v1 records. Refuses production paths."""

    TABLES = {'meta', 'raw_observation', 'option_quote_v1'}

    def __init__(self, path):
        path = Path(path).expanduser().resolve()
        if any(path.is_relative_to(d.resolve()) for d in PROTECTED_DIRS):
            raise ValueError('OBSERVATION_STORE_PROTECTED_PATH')
        existing = sqlite3.connect(f'file:{path}?mode=ro', uri=True) if path.exists() else None
        if existing is not None:
            names = {r[0] for r in existing.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            existing.close()
            if names - self.TABLES:
                raise ValueError('OBSERVATION_STORE_FOREIGN_DATABASE')
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(f"""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT OR IGNORE INTO meta VALUES ('schema_version', '{SCHEMA_VERSION}');
            CREATE TABLE IF NOT EXISTS raw_observation (
                id INTEGER PRIMARY KEY, provider TEXT NOT NULL, endpoint TEXT NOT NULL, params_json TEXT NOT NULL,
                retrieved_at TEXT NOT NULL, payload_sha256 TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS option_quote_v1 (
                id INTEGER PRIMARY KEY, schema_version TEXT NOT NULL, exchange_token TEXT NOT NULL,
                provider_instrument_id TEXT NOT NULL, retrieved_at TEXT NOT NULL, code_rev TEXT, record_json TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS oq_token_time ON option_quote_v1 (exchange_token, retrieved_at);
            CREATE TRIGGER IF NOT EXISTS raw_no_update BEFORE UPDATE ON raw_observation
                BEGIN SELECT RAISE(ABORT, 'append-only'); END;
            CREATE TRIGGER IF NOT EXISTS raw_no_delete BEFORE DELETE ON raw_observation
                BEGIN SELECT RAISE(ABORT, 'append-only'); END;
            CREATE TRIGGER IF NOT EXISTS oq_no_update BEFORE UPDATE ON option_quote_v1
                BEGIN SELECT RAISE(ABORT, 'append-only'); END;
            CREATE TRIGGER IF NOT EXISTS oq_no_delete BEFORE DELETE ON option_quote_v1
                BEGIN SELECT RAISE(ABORT, 'append-only'); END;
        """)
        if self.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] != SCHEMA_VERSION:
            raise ValueError('OBSERVATION_STORE_SCHEMA_MISMATCH')

    def raw(self, provider, endpoint, params, retrieved_at, payload: str) -> int:
        with self.db:
            return self.db.execute(
                'INSERT INTO raw_observation (provider, endpoint, params_json, retrieved_at, payload_sha256, payload)'
                ' VALUES (?, ?, ?, ?, ?, ?)', (provider, endpoint, _json(params), retrieved_at.isoformat(),
                hashlib.sha256(payload.encode()).hexdigest(), payload)).lastrowid

    def record(self, rec: dict) -> int:
        with self.db:
            return self.db.execute(
                'INSERT INTO option_quote_v1 (schema_version, exchange_token, provider_instrument_id, retrieved_at,'
                ' code_rev, record_json) VALUES (?, ?, ?, ?, ?, ?)',
                (rec['schema_version'], rec['exchange_token'], rec['provider_instrument_id'],
                 rec['retrieved_at'].isoformat(), rec['code_rev'], _json(rec))).lastrowid


def collect_upstox(reader, store: ObservationStore, underlying_key, instrument_keys, *, lot_changes, code_rev) -> list:
    """Fetch, keep every raw response, then normalise. One result per key: a record id or an error code."""
    def get(name, path, params):
        text, data = reader.fetch(path, params)
        at = reader.clock()
        raw_ids[name], retrieved[name], payloads[name] = store.raw('upstox', path, params, at, text), at, data

    raw_ids, retrieved, payloads, results = {}, {}, {}, []
    # ponytail: stores the full contract list (~830 KB for NIFTY) per run; pass expiry_date before polling every few seconds.
    get('contracts', PATHS[0], {'instrument_key': check_underlying(underlying_key)})
    get('underlying', PATHS[3], {'instrument_key': underlying_key})
    for key in instrument_keys:
        try:
            get('quote', PATHS[1], {'instrument_key': key})
            get('greek', PATHS[2], {'instrument_key': key})
            rec = normalize_upstox(underlying_key=underlying_key, instrument_key=key, contracts=payloads['contracts'],
                                   quote=payloads['quote'], greek=payloads['greek'], underlying=payloads['underlying'],
                                   retrieved=dict(retrieved), lot_changes=lot_changes, code_rev=code_rev,
                                   raw_ids=dict(raw_ids))
            results.append({'instrument_key': key, 'record_id': store.record(rec), 'flags': rec['quality_flags']})
        except ValueError as error:
            results.append({'instrument_key': key, 'error': str(error)})
    return results
