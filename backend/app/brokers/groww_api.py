"""Official Groww REST research reader. No orders; missing observation times stay unknown."""
from __future__ import annotations

import csv
import io
import re
from datetime import date
from decimal import Decimal, InvalidOperation

import httpx
from pydantic import SecretStr

from app.brokers.base import Instrument
from app.brokers.paper import PaperBroker
from app.db.types import utcnow
from app.trading.option_quote import ContractQuote, IST

API = 'https://api.groww.in/v1'
MASTER = 'https://growwapi-assets.groww.in/instruments/instrument.csv'


def _decimal(value, *, positive=False):
    if value is None:
        return None
    try:
        if isinstance(value, bool):
            raise ValueError()
        number = Decimal(str(value))
        if not number.is_finite() or (positive and number <= 0):
            raise ValueError()
        return number
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('GROWW_INVALID_NUMBER') from None


def _underlying(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,31}', value):
        raise ValueError('GROWW_INVALID_UNDERLYING')
    return value


class GrowwMarketData:
    """GET-only allowlisted endpoints; authorization must be explicitly configured.

    REST docs do not establish book/Greek observation times. Neither receive time nor
    last_trade_time is substituted for them. This reader cannot authorize paper entries.
    """
    name = 'groww_api'

    def __init__(self, access_token, *, authorized=False, client=None, clock=utcnow):
        if not access_token or not isinstance(access_token, str) or any(c in access_token for c in '\r\n'):
            raise ValueError('GROWW_ACCESS_TOKEN_REQUIRED')
        self._token = SecretStr(access_token)
        self.authorized = authorized
        self._client = client
        self.clock = clock
        self._contracts = {}
        self._master_day = None

    @staticmethod
    def capabilities():
        return {'provider': 'groww_api', 'remote_orders_supported': False,
            'forward_paper_ready': False, 'access_token_required': True,
            'blockers': ['BOOK_OBSERVATION_TIME_UNDOCUMENTED', 'GREEK_OBSERVATION_TIME_UNDOCUMENTED']}

    def _get(self, url, *, params=None):
        if self.authorized is not True:
            raise ValueError('GROWW_MARKET_DATA_AUTHORIZATION_REQUIRED')
        if url not in (MASTER, API + '/live-data/quote') and not re.fullmatch(
                re.escape(API) + r'/option-chain/exchange/NSE/underlying/[A-Z][A-Z0-9_]{0,31}', url):
            raise ValueError('GROWW_ENDPOINT_NOT_ALLOWED')
        headers = {'Accept': 'text/csv' if url == MASTER else 'application/json'}
        if url != MASTER:
            headers.update({'Authorization': 'Bearer ' + self._token.get_secret_value(), 'X-API-VERSION': '1.0'})
        try:
            if self._client is not None:
                response = self._client.get(url, params=params, headers=headers, follow_redirects=False, timeout=15)
            else:
                with httpx.Client(follow_redirects=False, timeout=15, trust_env=False) as client:
                    response = client.get(url, params=params, headers=headers)
            if response.status_code != 200:
                raise ValueError(f'GROWW_HTTP_{response.status_code}')
            return response
        except httpx.HTTPError:
            raise ValueError('GROWW_DATA_REQUEST_FAILED') from None

    def _payload(self, url, *, params=None):
        response = self._get(url, params=params)
        try:
            body = response.json(parse_float=Decimal)
            if not isinstance(body, dict) or body.get('status') != 'SUCCESS' or not isinstance(body.get('payload'), dict):
                raise ValueError()
            return body['payload']
        except (ValueError, TypeError):
            # Never reflect provider response bodies or request headers into errors.
            raise ValueError('GROWW_INVALID_OR_UNSUCCESSFUL_RESPONSE') from None

    def instruments(self, underlying):
        underlying = _underlying(underlying)
        today = self.clock().astimezone(IST).date()
        if self._master_day != today:
            rows = csv.DictReader(io.StringIO(self._get(MASTER).text))
            required = {'exchange', 'exchange_token', 'trading_symbol', 'underlying_symbol',
                'instrument_type', 'segment', 'expiry_date', 'strike_price', 'lot_size', 'tick_size'}
            if not required.issubset(rows.fieldnames or []):
                raise ValueError('GROWW_INSTRUMENT_SCHEMA_MISMATCH')
            contracts = {}
            try:
                for row in rows:
                    if row['exchange'] != 'NSE' or row['segment'] != 'FNO' or row['instrument_type'] not in ('CE', 'PE'):
                        continue
                    symbol, token = row['trading_symbol'], row['exchange_token']
                    expiry = date.fromisoformat(row['expiry_date'])
                    strike = _decimal(row['strike_price'], positive=True)
                    tick = _decimal(row['tick_size'], positive=True)
                    lot = int(row['lot_size'])
                    if not symbol or not token or lot <= 0 or symbol in contracts:
                        raise ValueError()
                    instrument = Instrument(symbol, _underlying(row['underlying_symbol']), expiry.isoformat(),
                        float(strike), row['instrument_type'], lot, tick_size=tick, exchange='NSE')
                    contracts[symbol] = (instrument, token)
            except (ValueError, TypeError, KeyError):
                raise ValueError('GROWW_INVALID_INSTRUMENT_MASTER') from None
            self._contracts, self._master_day = contracts, today
        return sorted([i for i, _ in self._contracts.values()
            if i.underlying == underlying and date.fromisoformat(i.expiry) >= today],
            key=lambda i: (i.expiry, i.strike, i.option_type))

    def option_chain(self, underlying, expiry):
        underlying = _underlying(underlying)
        try:
            expiry = date.fromisoformat(expiry).isoformat()
        except (ValueError, TypeError):
            raise ValueError('GROWW_INVALID_EXPIRY') from None
        return self._payload(API + '/option-chain/exchange/NSE/underlying/' + underlying,
                             params={'expiry_date': expiry})

    def option_evidence(self, underlying, symbol):
        """Join actual master/chain/book evidence without inventing contract metadata or freshness."""
        instruments = {i.tradingsymbol: i for i in self.instruments(underlying)}
        if symbol not in instruments:
            raise ValueError('GROWW_CONTRACT_NOT_IN_MASTER')
        instrument = instruments[symbol]
        chain = self.option_chain(underlying, instrument.expiry)
        leg = None
        try:
            for strike, sides in chain['strikes'].items():
                if _decimal(strike) == Decimal(str(instrument.strike)):
                    leg = sides.get(instrument.option_type)
                    break
            if not isinstance(leg, dict) or leg.get('trading_symbol') != symbol:
                raise ValueError()
            delta = _decimal((leg.get('greeks') or {}).get('delta'))
            if delta is not None and not (-1 <= delta <= 1):
                raise ValueError()
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ValueError('GROWW_CHAIN_CONTRACT_MISMATCH') from None
        book = self._payload(API + '/live-data/quote',
            params={'exchange': 'NSE', 'segment': 'FNO', 'trading_symbol': symbol})
        bid = _decimal(book.get('bid_price'), positive=True)
        ask = _decimal(book.get('offer_price'), positive=True)
        if bid is not None and ask is not None and bid > ask:
            raise ValueError('GROWW_CROSSED_BOOK')
        return ContractQuote(instrument.underlying, instrument.expiry, instrument.strike,
            instrument.option_type, self._contracts[symbol][1], symbol, instrument.lot_size,
            _decimal(book.get('last_price'), positive=True), bid, ask, None, self.clock(),
            self.name, float(delta) if delta is not None else None,
            delta_observed_at=None, delta_source=self.name, delta_contract=symbol)


class GrowwPaperBroker(PaperBroker):
    """Local paper fills only; the separate data reader exposes no remote-order methods."""
    name = 'groww'

    def __init__(self, profile, access_token, *, authorized=False, clock=utcnow):
        super().__init__(profile, clock=clock)
        self.market_data = GrowwMarketData(access_token, authorized=authorized, clock=clock)
