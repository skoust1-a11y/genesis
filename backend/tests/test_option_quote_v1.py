"""option-quote-v1 observation layer. Synthetic Upstox payloads, not account evidence."""
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.brokers.upstox_api import PATHS, UpstoxMarketData
from app.market import option_quote_v1 as oq
from app.market.nse import LotChange

NOW = datetime.fromisoformat('2026-11-06T10:00:00+05:30')
KEY, UNDERLYING = 'NSE_FO|123', 'NSE_INDEX|Nifty 50'
LOTS = [LotChange('NIFTY', '2024-11-20', 75), LotChange('NIFTY', '2026-01-01', 65)]
CONTRACT = {'exchange': 'NSE', 'segment': 'NSE_FO', 'underlying_key': UNDERLYING, 'underlying_symbol': 'NIFTY',
            'instrument_key': KEY, 'exchange_token': '123', 'trading_symbol': 'NIFTY 24000 CE 10 NOV 26',
            'instrument_type': 'CE', 'expiry': '2026-11-10', 'lot_size': 65, 'strike_price': 24000}
BOOK = {'instrument_token': KEY, 'symbol': 'NIFTY26N1024000CE', 'last_price': 49.5, 'volume': 1000, 'oi': 500,
        'timestamp': NOW.isoformat(), 'last_trade_time': str(int((NOW - timedelta(seconds=2)).timestamp() * 1000)),
        'depth': {'buy': [{'price': 49, 'quantity': 130, 'orders': 2}, {'price': 48.5, 'quantity': 65, 'orders': 1}],
                  'sell': [{'price': 50, 'quantity': 65, 'orders': 1}]}}
GREEK = {'instrument_token': KEY, 'last_price': 49.5, 'ltq': 65, 'delta': 0.5, 'gamma': 0.001, 'theta': -9.1,
         'vega': 12.3, 'iv': 0.1599}
SPOT = {'instrument_token': UNDERLYING, 'last_price': 24010.5}


def normalize(*, contract=None, book=None, greek=None, spot=None, lots=LOTS, retrieved=None):
    return oq.normalize_upstox(
        underlying_key=UNDERLYING, instrument_key=KEY, contracts=[contract or CONTRACT],
        quote={'k': book or BOOK}, greek={'k': greek or GREEK}, underlying={'k': spot or SPOT},
        retrieved=retrieved or {'contracts': NOW, 'quote': NOW, 'greek': NOW + timedelta(seconds=1),
                                'underlying': NOW + timedelta(milliseconds=500)},
        lot_changes=lots, code_rev='abc+dirty', raw_ids={'quote': 1})


def test_timestamps_stay_separate_and_never_substitute():
    r = normalize()
    assert r['schema_version'] == 'option-quote-v1' and r['provider'] == 'upstox'
    assert r['book_time'] is None and r['greek_time'] is None and r['underlying_time'] is None
    assert r['source_response_time'] == NOW and r['retrieved_at'] == NOW
    assert r['last_trade_time'] == NOW - timedelta(seconds=2)
    assert r['greek_retrieved_at'] == NOW + timedelta(seconds=1)
    assert {r['missing_reasons'][k] for k in ('book_time', 'greek_time', 'underlying_time')} == {'NOT_DOCUMENTED'}
    assert {'BOOK_TIME_NOT_SUPPLIED', 'GREEK_TIME_NOT_SUPPLIED', 'GREEK_METHOD_UNDISCLOSED'} <= set(r['quality_flags'])
    assert not {'LAST_TRADE_STALE', 'RESPONSE_TIME_SKEW'} & set(r['quality_flags'])
    assert r['code_rev'] == 'abc+dirty' and r['worktree_dirty'] is True


def test_contract_identity_book_and_lot():
    r = normalize()
    assert (r['exchange'], r['segment'], r['exchange_token'], r['provider_instrument_id']) == ('NSE', 'NSE_FO', '123', KEY)
    assert (r['underlying'], r['expiry'], r['strike'], r['option_type']) == ('NIFTY', '2026-11-10', Decimal(24000), 'CE')
    assert r['exchange_trading_symbol'] is None and r['missing_reasons']['exchange_trading_symbol'] == 'NOT_DOCUMENTED'
    assert r['provider_trading_symbol'] == CONTRACT['trading_symbol']
    assert (r['bid'], r['bid_qty'], r['ask'], r['ask_qty']) == (Decimal(49), 130, Decimal(50), 65)
    assert (r['lot_size'], r['lot_effective_on'], r['provider_lot_size']) == (65, '2026-01-01', 65)
    assert r['underlying_price'] == Decimal('24010.5') and r['last_trade_qty'] == 65
    for bad in ({'contract': {**CONTRACT, 'exchange_token': '999'}},
                {'contract': {**CONTRACT, 'underlying_key': 'NSE_INDEX|Other'}},
                {'book': {**BOOK, 'instrument_token': 'NSE_FO|999'}},
                {'greek': {**GREEK, 'instrument_token': 'NSE_FO|999'}},
                {'spot': {**SPOT, 'instrument_token': 'NSE_INDEX|Nifty Bank'}}):
        with pytest.raises(ValueError):
            normalize(**bad)


def test_lot_size_mismatch_and_undated():
    r = normalize(contract={**CONTRACT, 'lot_size': 50})
    assert 'LOT_SIZE_MISMATCH' in r['quality_flags'] and r['lot_size'] == 65 and r['provider_lot_size'] == 50
    # The old lot is allowed only inside the transition window around a change.
    assert 'LOT_SIZE_MISMATCH' not in normalize(contract={**CONTRACT, 'lot_size': 75},
        lots=LOTS + [LotChange('NIFTY', '2026-12-01', 75)])['quality_flags']
    r = normalize(lots=[])
    assert r['lot_size'] is None and r['missing_reasons']['lot_size'] == 'NOT_IN_DATED_TABLE'
    assert 'LOT_SIZE_UNDATED' in r['quality_flags']
    assert 'TOP_QTY_BELOW_ONE_LOT' in normalize(book={**BOOK, 'depth': {
        'buy': [{'price': 49, 'quantity': 10}], 'sell': [{'price': 50, 'quantity': 65}]}})['quality_flags']


def test_missing_and_invalid_fields_carry_reasons():
    book = {k: v for k, v in BOOK.items() if k not in ('timestamp', 'last_trade_time', 'oi')}
    r = normalize(book={**book, 'depth': {'buy': [], 'sell': [{'price': 50, 'quantity': 65}]}},
                  greek={'instrument_token': KEY, 'delta': 1.5})
    reasons = r['missing_reasons']
    assert reasons['source_response_time'] == reasons['last_trade_time'] == reasons['oi'] == 'NOT_SUPPLIED'
    assert reasons['bid'] == reasons['bid_qty'] == reasons['iv'] == reasons['gamma'] == 'NOT_SUPPLIED'
    assert r['delta'] is None and reasons['delta'] == 'INVALID'
    assert 'ONE_SIDED_BOOK' in r['quality_flags'] and r['ask'] == Decimal(50)
    r = normalize(book={**BOOK, 'last_trade_time': '1746156600', 'depth': {'buy': [{'price': -1, 'quantity': 1}]}})
    assert r['last_trade_time'] is None and r['missing_reasons']['last_trade_time'] == 'INVALID'
    assert r['missing_reasons']['bid'] == 'INVALID' and 'EMPTY_BOOK' in r['quality_flags']
    crossed = normalize(book={**BOOK, 'depth': {'buy': [{'price': 51, 'quantity': 65}], 'sell': [{'price': 50, 'quantity': 65}]}})
    assert 'CROSSED_BOOK' in crossed['quality_flags']


def test_iv_normalization_keeps_raw_unit_and_rejects_mixups():
    assert oq.normalize_iv(Decimal('0.1599'), 'fraction') == (Decimal('0.1599'), None)
    assert oq.normalize_iv(Decimal('16.49'), 'percent') == (Decimal('0.1649'), None)
    assert oq.normalize_iv(None, 'fraction') == (None, 'NOT_SUPPLIED')
    for raw, unit in ((Decimal('16.49'), 'fraction'), (0, 'fraction'), (-0.2, 'percent'), ('x', 'percent'), (1, 'bp')):
        assert oq.normalize_iv(raw, unit) == (None, 'INVALID')
    r = normalize()
    assert (r['iv'], r['iv_raw'], r['iv_unit_raw']) == (Decimal('0.1599'), 0.1599, 'fraction')
    assert r['iv_unit_basis'].startswith('UNDOCUMENTED') and 'IV_UNIT_UNDOCUMENTED' in r['quality_flags']
    r = normalize(greek={**GREEK, 'iv': 16.49})
    assert r['iv'] is None and r['missing_reasons']['iv'] == 'INVALID' and r['iv_raw'] == 16.49


def test_stale_market_data_is_flagged_not_refreshed():
    monday = NOW - timedelta(days=3)
    # Holiday shape: Monday's last trade, response stamped "now".
    r = normalize(book={**BOOK, 'last_trade_time': str(int(monday.timestamp() * 1000))})
    assert r['last_trade_time'] == monday and 'LAST_TRADE_STALE' in r['quality_flags'] and r['book_time'] is None
    r = normalize(book={**BOOK, 'timestamp': (NOW - timedelta(minutes=5)).isoformat()})
    assert 'RESPONSE_TIME_SKEW' in r['quality_flags']
    r = normalize(retrieved={'contracts': NOW, 'quote': NOW, 'greek': NOW + timedelta(seconds=30), 'underlying': NOW})
    assert 'CROSS_ENDPOINT_RETRIEVAL_GAP' in r['quality_flags']
    r = normalize(book={**BOOK, 'last_trade_time': str(int((NOW + timedelta(seconds=5)).timestamp() * 1000))})
    assert 'LAST_TRADE_TIME_IN_FUTURE' in r['quality_flags']
    expired = normalize(retrieved={k: NOW + timedelta(days=5) for k in ('contracts', 'quote', 'greek', 'underlying')})
    assert 'CONTRACT_EXPIRED' in expired['quality_flags']
    with pytest.raises(ValueError, match='NOT_AWARE'):
        normalize(retrieved={k: NOW.replace(tzinfo=None) for k in ('contracts', 'quote', 'greek', 'underlying')})


def _reader():
    requests = []
    def respond(request):
        requests.append(request)
        data = {PATHS[0]: [CONTRACT], PATHS[1]: {'k': BOOK}, PATHS[2]: {'k': GREEK}, PATHS[3]: {'k': SPOT}}[request.url.path]
        return httpx.Response(200, json={'status': 'success', 'data': data})
    return UpstoxMarketData('private-fixture-token', authorized=True,
                            client=httpx.Client(transport=httpx.MockTransport(respond)), clock=lambda: NOW), requests


def test_collect_is_get_only_keeps_raw_and_store_is_append_only(tmp_path):
    reader, requests = _reader()
    store = oq.ObservationStore(tmp_path / 'obs.sqlite')
    results = oq.collect_upstox(reader, store, UNDERLYING, [KEY, 'NSE_FO|999'], lot_changes=LOTS, code_rev='abc')
    assert results[0]['record_id'] == 1 and results[1]['error'] == 'UPSTOX_CONTRACT_NOT_FOUND'
    assert all(r.method == 'GET' and r.url.path in PATHS for r in requests)
    assert all('private-fixture-token' not in str(r.url) for r in requests)
    raws = store.db.execute('SELECT endpoint, payload_sha256, payload FROM raw_observation').fetchall()
    assert [r[0] for r in raws] == [PATHS[0], PATHS[3], PATHS[1], PATHS[2], PATHS[1], PATHS[2]]
    assert all(hashlib.sha256(p.encode()).hexdigest() == h for _, h, p in raws)
    assert all('private-fixture-token' not in p for _, _, p in raws)
    rec = json.loads(store.db.execute('SELECT record_json FROM option_quote_v1').fetchone()[0])
    assert rec['raw_ids'] == {'contracts': 1, 'underlying': 2, 'quote': 3, 'greek': 4} and rec['book_time'] is None
    for sql in ('UPDATE option_quote_v1 SET code_rev = NULL', 'DELETE FROM raw_observation'):
        with pytest.raises(sqlite3.IntegrityError, match='append-only'):
            store.db.execute(sql)
    store.db.rollback()
    oq.ObservationStore(tmp_path / 'obs.sqlite')  # reopening its own file is allowed


def test_store_refuses_production_paths_and_foreign_databases(tmp_path):
    for protected in (oq.REPO / 'backend' / 'data' / 'genesis.db', oq.REPO / 'trades' / 'x.sqlite'):
        with pytest.raises(ValueError, match='PROTECTED_PATH'):
            oq.ObservationStore(protected)
    foreign = tmp_path / 'journal.db'
    sqlite3.connect(foreign).execute('CREATE TABLE trades (id INTEGER)').connection.commit()
    with pytest.raises(ValueError, match='FOREIGN_DATABASE'):
        oq.ObservationStore(foreign)


def test_read_only_and_isolated_from_orders_and_admission():
    reader, requests = _reader()
    for path in ('/v2/order/place', '/v3/order/place', '/v2/market-quote/quotes/../../order/place'):
        with pytest.raises(ValueError, match='ENDPOINT_NOT_ALLOWED'):
            reader.fetch(path, {})
    assert not requests and not any('order' in name.lower() for name in dir(reader))
    assert all(p.startswith(('/v2/option/contract', '/v2/market-quote/', '/v3/market-quote/')) for p in PATHS)
    app = Path(oq.__file__).resolve().parents[1]
    users = [p for p in app.rglob('*.py') if 'option_quote_v1' in p.read_text() and p.name != 'option_quote_v1.py']
    assert users == []
    assert 'order' not in Path(oq.__file__).read_text().lower().replace('ordered', '')
