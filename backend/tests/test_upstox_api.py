"""Synthetic Upstox responses, not authorized account/session evidence."""
from dataclasses import replace
from datetime import datetime, timedelta

import httpx
import pytest

from app.brokers import registry, credentials
from app.brokers.upstox_api import UpstoxMarketData, PATHS
from app.config import Settings
from app.trading import preflight, walk_forward as wf
from app.trading.option_quote import quote_decision
from tests.test_walk_forward_and_ablation import _make_candidate_event, _wf_cfg

NOW = datetime.fromisoformat('2026-11-06T10:00:00+05:30')
KEY, UNDERLYING = 'NSE_FO|123', 'NSE_INDEX|Nifty 50'
CONTRACT = {'exchange': 'NSE', 'segment': 'NSE_FO', 'underlying_key': UNDERLYING,
    'underlying_symbol': 'NIFTY', 'instrument_key': KEY, 'trading_symbol': 'FIXTURE OPTION',
    'instrument_type': 'CE', 'expiry': '2026-11-10', 'lot_size': 75, 'strike_price': 24000}
BOOK = {'instrument_token': KEY, 'symbol': 'FIXTURE26N1024000CE', 'last_price': 49.5,
        'timestamp': (NOW + timedelta(hours=5)).isoformat(),
        'last_trade_time': str(int(NOW.timestamp() * 1000)), 'depth': {'buy': [{'price': 49, 'quantity': 75}],
                                               'sell': [{'price': 50, 'quantity': 75}]}}


def reader(*, book=None, contracts=None, greek=None, code=200, authorized=True):
    requests = []
    def respond(request):
        requests.append(request)
        if code != 200:
            return httpx.Response(code, text='PRIVATE_PROVIDER_ERROR')
        if request.url.path == PATHS[0]:
            data = [CONTRACT] if contracts is None else contracts
        elif request.url.path == PATHS[1]:
            data = {'display-key': BOOK if book is None else book}
        else:
            data = {'display-key': {'instrument_token': KEY, 'delta': 0.5} if greek is None else greek}
        return httpx.Response(200, json={'status': 'success', 'data': data})
    return UpstoxMarketData('private-fixture-token', authorized=authorized,
        client=httpx.Client(transport=httpx.MockTransport(respond)), clock=lambda: NOW), requests


def test_read_only_exact_join_without_book_or_greek_time():
    data, requests = reader()
    q = data.option_evidence(UNDERLYING, KEY)
    assert q.provider_instrument_id == KEY and q.tradingsymbol == CONTRACT['trading_symbol']
    assert (q.strike, q.lot_size, q.option_type) == (24000, 75, 'CE')
    # Neither the fresh last trade nor the response `timestamp` may date the book.
    assert q.quote_time is None and q.delta == 0.5 and q.delta_observed_at is None
    assert quote_decision(q, NOW, Settings()).status == 'reference'
    assert not data.capabilities()['forward_paper_ready']
    event = replace(_make_candidate_event(1, '2026-11-06'), expiry=q.expiry, entry_quote=q)
    config = replace(_wf_cfg(), trusted_delta_sources=frozenset({'upstox_api'}))
    with pytest.raises(wf.MissingOptionDataError, match='TIER_1_UNEXECUTABLE_QUOTE'):
        wf.simulate_option_execution(event, config=config)
    assert len(requests) == 3 and all(r.method == 'GET' for r in requests)
    assert all(r.headers['Authorization'] == 'Bearer private-fixture-token' for r in requests)
    assert all('private-fixture-token' not in str(r.url) for r in requests)


def test_disabled_authorization_redirects_and_order_endpoints():
    data, requests = reader(authorized=False)
    with pytest.raises(ValueError, match='AUTHORIZATION_REQUIRED'):
        data.contracts(UNDERLYING)
    assert not requests
    data.authorized = True
    with pytest.raises(ValueError, match='ENDPOINT_NOT_ALLOWED'):
        data._get('/v2/order/place', {})
    assert not requests and not hasattr(data, 'place_order')
    for code in (302, 401, 429, 500):
        data, requests = reader(code=code)
        with pytest.raises(ValueError, match=f'UPSTOX_HTTP_{code}') as error:
            data.contracts(UNDERLYING)
        assert len(requests) == 1 and 'PRIVATE_PROVIDER_ERROR' not in str(error.value)
        assert 'private-fixture-token' not in str(error.value)


def test_contract_mismatch_and_ambiguous_quote_timestamps_reject():
    for contracts in ([{**CONTRACT, 'underlying_key': 'NSE_INDEX|Other'}], [CONTRACT, CONTRACT]):
        data, _ = reader(contracts=contracts)
        with pytest.raises(ValueError, match='CONTRACT_IDENTITY_INVALID'):
            data.option_evidence(UNDERLYING, KEY)
    data, _ = reader(book={**BOOK, 'instrument_token': 'NSE_FO|999'})
    with pytest.raises(ValueError, match='INSTRUMENT_RESPONSE_MISMATCH'):
        data.option_evidence(UNDERLYING, KEY)
    for stamp in ('1746156600', '2026-11-06T10:00:00'):
        data, _ = reader(book={**BOOK, 'last_trade_time': stamp})
        with pytest.raises(ValueError, match='INVALID_QUOTE_TIMESTAMP'):
            data.option_evidence(UNDERLYING, KEY)
    data, _ = reader(book={**BOOK, 'last_trade_time': str(int((NOW - timedelta(seconds=11)).timestamp() * 1000))})
    assert quote_decision(data.option_evidence(UNDERLYING, KEY), NOW, Settings()).status != 'executable'
    data, _ = reader(book={**BOOK, 'last_trade_time': None})
    assert data.option_evidence(UNDERLYING, KEY).quote_time is None


def test_preflight_selects_upstox_and_never_reuses_groww_fee_policy(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('BROKER_UPSTOX_ACCESS_TOKEN', 'private-fixture-token')
    credentials._dotenv.cache_clear()
    report = preflight.report(market_day='2026-11-06', settings=Settings(upstox_market_data_authorized=True),
                              now=NOW, provider='upstox')
    assert report['provider'] == 'upstox' and report['credentials'][0]['configured']
    assert report['fee_policy'] is None and 'FORWARD_FEE_DATE_UNVERIFIED' in report['blockers']
    assert 'GREEK_OBSERVATION_TIME_UNDOCUMENTED' in report['blockers']
    assert 'BOOK_OBSERVATION_TIME_UNDOCUMENTED' in report['blockers']
    assert not report['forward_ready'] and 'private-fixture-token' not in str(report)


def test_registry_is_paper_only_and_cannot_start_executable_feed():
    spec = registry.get_spec('upstox')
    assert spec.modes(Settings()) == ('paper',) and not spec.has_quotes
    assert credentials.missing_fields(spec, {}) == ['access_token']
    broker = spec.factory({'access_token': 'fixture-token'}, Settings(upstox_market_data_authorized=False),
                          None, lambda: NOW, None)
    assert not broker.market_data.authorized
    with pytest.raises(ValueError, match='provides no quotes'):
        registry.get_feed('broker', broker, 'NIFTY', Settings(), lambda: NOW, None)
