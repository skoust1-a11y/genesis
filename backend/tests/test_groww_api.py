"""Synthetic HTTP fixtures, never Groww session evidence or real credentials."""
from datetime import datetime
from decimal import Decimal

import httpx
import pytest

from app.brokers import registry
from app.brokers.groww_api import API, MASTER, GrowwMarketData
from app.config import Settings
from app.trading.option_quote import quote_decision

NOW = datetime.fromisoformat('2026-11-06T10:00:00+05:30')
SYMBOL = 'SYNTHETIC_NIFTY_CE'
CSV = ('exchange,exchange_token,trading_symbol,underlying_symbol,instrument_type,segment,expiry_date,strike_price,lot_size,tick_size\n'
       f'NSE,fixture-token,{SYMBOL},NIFTY,CE,FNO,2026-11-10,24000,75,0.05\n')
CHAIN = {'strikes': {'24000': {'CE': {'trading_symbol': SYMBOL, 'greeks': {'delta': 0.5}, 'ltp': 99}}}}
BOOK = {'bid_price': 100, 'offer_price': 101, 'last_price': 100.5,
        'last_trade_time': int(NOW.timestamp() * 1000)}


def reader(*, authorized=True, chain=None, book=None, response_code=200):
    requests = []
    def respond(request):
        requests.append(request)
        if response_code != 200:
            return httpx.Response(response_code, text='DO_NOT_REFLECT_PROVIDER_BODY')
        if str(request.url) == MASTER:
            return httpx.Response(200, text=CSV)
        if request.url.path == '/v1/live-data/quote':
            payload = BOOK if book is None else book
        else:
            payload = CHAIN if chain is None else chain
        return httpx.Response(200, json={'status': 'SUCCESS', 'payload': payload})
    client = httpx.Client(transport=httpx.MockTransport(respond))
    return GrowwMarketData('fixture-access-value', authorized=authorized, client=client, clock=lambda: NOW), requests


def test_official_paths_contract_join_and_unknown_timestamps():
    data, requests = reader()
    quote = data.option_evidence('NIFTY', SYMBOL)
    assert (quote.expiry, quote.strike, quote.lot_size, quote.provider_instrument_id) == ('2026-11-10', 24000, 75, 'fixture-token')
    assert quote.bid == Decimal('100') and quote.ask == Decimal('101')
    assert quote.delta == 0.5 and quote.delta_contract == SYMBOL
    assert quote.quote_time is None and quote.delta_observed_at is None
    assert quote.fetched_at == NOW
    assert quote_decision(quote, NOW, Settings()).status != 'executable'
    assert not data.capabilities()['forward_paper_ready']
    assert len(requests) == 3 and all(r.method == 'GET' for r in requests)
    assert 'authorization' not in requests[0].headers
    assert requests[1].headers['Authorization'] == 'Bearer fixture-access-value'
    assert requests[2].url.params['segment'] == 'FNO'
    assert all('fixture-access-value' not in str(r.url) for r in requests)


def test_authorization_disabled_and_endpoints_restricted():
    data, requests = reader(authorized=False)
    with pytest.raises(ValueError, match='AUTHORIZATION_REQUIRED'):
        data.instruments('NIFTY')
    assert not requests
    data.authorized = True
    with pytest.raises(ValueError, match='ENDPOINT_NOT_ALLOWED'):
        data._get(API + '/order/create')
    with pytest.raises(ValueError, match='INVALID_UNDERLYING'):
        data.option_chain('../orders', '2026-11-10')
    assert not requests and not hasattr(data, 'place_order')


def test_redirect_and_http_errors_are_not_retried_or_reflected():
    for code in (302, 401, 429, 500):
        data, requests = reader(response_code=code)
        with pytest.raises(ValueError, match=f'GROWW_HTTP_{code}') as error:
            data.instruments('NIFTY')
        assert 'fixture-access-value' not in str(error.value)
        assert 'DO_NOT_REFLECT' not in str(error.value)
        assert len(requests) == 1


def test_contract_mismatch_missing_book_and_invalid_numbers_fail_closed():
    data, _ = reader(chain={'strikes': {'24000': {'CE': {'trading_symbol': 'OTHER'}}}})
    with pytest.raises(ValueError, match='CHAIN_CONTRACT_MISMATCH'):
        data.option_evidence('NIFTY', SYMBOL)
    data, _ = reader(book={'last_price': 100})
    quote = data.option_evidence('NIFTY', SYMBOL)
    assert quote.bid is None and quote.ask is None
    assert quote_decision(quote, NOW, Settings()).status != 'executable'
    for book in ({'bid_price': 102, 'offer_price': 101}, {'bid_price': 'NaN'}, {'bid_price': True}):
        data, _ = reader(book=book)
        with pytest.raises(ValueError):
            data.option_evidence('NIFTY', SYMBOL)


def test_catalog_cache_does_not_reuse_quote_or_greek_freshness():
    data, requests = reader()
    data.option_evidence('NIFTY', SYMBOL)
    data.option_evidence('NIFTY', SYMBOL)
    assert sum(str(r.url) == MASTER for r in requests) == 1
    assert len(requests) == 5
    with pytest.raises(ValueError, match='CONTRACT_NOT_IN_MASTER'):
        data.option_evidence('NIFTY', 'GUESSED_CONTRACT')


def test_registry_is_paper_only_and_does_not_claim_executable_feed():
    spec = registry.get_spec('groww')
    assert spec.modes(Settings()) == ('paper',) and not spec.has_quotes
    assert registry.credentials.missing_fields(spec, {}) == ['access_token']
    broker = spec.factory({'access_token': 'fixture-access-value'}, Settings(), None, lambda: NOW, None)
    assert broker.mode == 'paper' and not broker.market_data.authorized
    from app.brokers.base import QuoteSource
    assert not isinstance(broker, QuoteSource)
    with pytest.raises(ValueError, match='provides no quotes'):
        registry.get_feed('broker', broker, 'NIFTY', Settings(), lambda: NOW, None)
