"""Upstox read-only REST data. Prefer the free Analytics Token; never submit orders."""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

import httpx
from pydantic import SecretStr

from app.brokers.paper import PaperBroker
from app.db.types import utcnow
from app.trading.option_quote import ContractQuote

BASE = 'https://api.upstox.com'
# v3 has no full-depth quote, so depth still comes from v2 /market-quote/quotes. All are documented GETs.
PATHS = ('/v2/option/contract', '/v2/market-quote/quotes', '/v3/market-quote/option-greek', '/v3/market-quote/ltp')


def _number(value):
    if value is None:
        return None
    try:
        if isinstance(value, bool):
            raise ValueError()
        value = Decimal(str(value))
        if not value.is_finite():
            raise ValueError()
        return value
    except (ValueError, InvalidOperation, TypeError):
        raise ValueError('UPSTOX_INVALID_NUMBER') from None


def _timestamp(value):
    if value is None:
        return None
    try:
        text = str(value)
        if 'T' in text:
            stamp = datetime.fromisoformat(text.replace('Z', '+00:00'))
            if stamp.utcoffset() is None:
                raise ValueError()
            return stamp
        # Accept documented epoch milliseconds only, never guess seconds or refresh stale data.
        if not re.fullmatch(r'\d{13}', text):
            raise ValueError()
        return datetime.fromtimestamp(int(text) / 1000, timezone.utc)
    except (ValueError, OverflowError, OSError):
        raise ValueError('UPSTOX_INVALID_QUOTE_TIMESTAMP') from None


def _best(levels, *, bid):
    """(price, quantity) of the best non-empty level, or (None, None)."""
    found = []
    try:
        for row in levels:
            price, quantity = _number(row['price']), _number(row['quantity'])
            if price is None or quantity is None or price < 0 or quantity < 0 or quantity != quantity.to_integral_value():
                raise ValueError()
            if price > 0 and quantity > 0:
                found.append((price, int(quantity)))
    except (ValueError, TypeError, KeyError):
        raise ValueError('UPSTOX_INVALID_DEPTH') from None
    return (max(found) if bid else min(found)) if found else (None, None)


def check_underlying(key):
    if not isinstance(key, str) or not re.fullmatch(r'NSE_INDEX\|[A-Za-z0-9 _-]{1,80}', key):
        raise ValueError('UPSTOX_INVALID_UNDERLYING_KEY')
    return key


def match_instrument(data, key):
    """The single row of a per-instrument response whose instrument_token is exactly `key`."""
    if not isinstance(data, dict):
        raise ValueError('UPSTOX_INSTRUMENT_RESPONSE_INVALID')
    matching = [row for row in data.values() if isinstance(row, dict) and row.get('instrument_token') == key]
    if len(data) != 1 or len(matching) != 1:
        raise ValueError('UPSTOX_INSTRUMENT_RESPONSE_MISMATCH')
    return matching[0]


class UpstoxMarketData:
    name = 'upstox_api'

    def __init__(self, access_token, *, authorized=False, client=None, clock=utcnow):
        if not isinstance(access_token, str) or not access_token or any(c in access_token for c in '\r\n'):
            raise ValueError('UPSTOX_ACCESS_TOKEN_REQUIRED')
        self._token = SecretStr(access_token)
        self.authorized, self._client, self.clock = authorized, client, clock

    @staticmethod
    def capabilities():
        return {'provider': 'upstox_api', 'forward_paper_ready': False, 'remote_orders_supported': False,
                'blockers': ['BOOK_OBSERVATION_TIME_UNDOCUMENTED', 'GREEK_OBSERVATION_TIME_UNDOCUMENTED']}

    def _get(self, path, params):
        return self.fetch(path, params)[1]

    def fetch(self, path, params):
        """(raw response text, parsed data) from one allowlisted GET."""
        if self.authorized is not True:
            raise ValueError('UPSTOX_MARKET_DATA_AUTHORIZATION_REQUIRED')
        if path not in PATHS:
            raise ValueError('UPSTOX_ENDPOINT_NOT_ALLOWED')
        headers = {'Accept': 'application/json', 'Authorization': 'Bearer ' + self._token.get_secret_value()}
        try:
            if self._client is None:
                with httpx.Client(timeout=15, follow_redirects=False, trust_env=False) as client:
                    response = client.get(BASE + path, params=params, headers=headers)
            else:
                response = self._client.get(BASE + path, params=params, headers=headers,
                                            timeout=15, follow_redirects=False)
        except httpx.HTTPError:
            raise ValueError('UPSTOX_DATA_REQUEST_FAILED') from None
        if response.status_code != 200:
            raise ValueError(f'UPSTOX_HTTP_{response.status_code}')
        try:
            body = response.json(parse_float=Decimal)
            if not isinstance(body, dict) or body.get('status') != 'success' or not isinstance(body.get('data'), (dict, list)):
                raise ValueError()
            return response.text, body['data']
        except (ValueError, TypeError):
            raise ValueError('UPSTOX_INVALID_OR_UNSUCCESSFUL_RESPONSE') from None

    def contracts(self, underlying_key):
        return self.parse_contracts(underlying_key, self._get(PATHS[0], {'instrument_key': check_underlying(underlying_key)}))

    @staticmethod
    def parse_contracts(underlying_key, rows):
        if not isinstance(rows, list):
            raise ValueError('UPSTOX_INVALID_CONTRACTS')
        result = {}
        try:
            for row in rows:
                key = row['instrument_key']
                lot, strike = int(str(row['lot_size'])), _number(row['strike_price'])
                if (row['exchange'] != 'NSE' or row['segment'] != 'NSE_FO'
                        or row['underlying_key'] != underlying_key or row['instrument_type'] not in ('CE', 'PE')
                        or not re.fullmatch(r'NSE_FO\|\d+', key) or key in result
                        or not isinstance(row['trading_symbol'], str) or not row['trading_symbol'].strip()
                        or not isinstance(row['underlying_symbol'], str)
                        or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,31}', row['underlying_symbol']) or lot <= 0
                        or strike is None or strike <= 0 or not math.isfinite(float(strike))):
                    raise ValueError()
                date.fromisoformat(row['expiry'])
                result[key] = {**row, 'lot_size': lot, 'strike_price': strike}
        except (ValueError, TypeError, KeyError):
            raise ValueError('UPSTOX_CONTRACT_IDENTITY_INVALID') from None
        return result

    def _instrument_data(self, path, key):
        return match_instrument(self._get(path, {'instrument_key': key}), key)

    def option_evidence(self, underlying_key, instrument_key):
        contract = self.contracts(underlying_key).get(instrument_key)
        if contract is None:
            raise ValueError('UPSTOX_CONTRACT_NOT_FOUND')
        quote = self._instrument_data(PATHS[1], instrument_key)
        greek = self._instrument_data(PATHS[2], instrument_key)
        depth = quote.get('depth') or {}
        if not isinstance(depth, dict):
            raise ValueError('UPSTOX_INVALID_DEPTH')
        bid, ask = _best(depth.get('buy', []), bid=True)[0], _best(depth.get('sell', []), bid=False)[0]
        if bid is not None and ask is not None and bid > ask:
            raise ValueError('UPSTOX_CROSSED_BOOK')
        delta = _number(greek.get('delta'))
        if delta is not None and not (-1 <= delta <= 1):
            raise ValueError('UPSTOX_INVALID_DELTA')
        _timestamp(quote.get('last_trade_time'))
        # quote_time stays None: last_trade_time is a trade and `timestamp` is response time; neither dates the book.
        return ContractQuote(contract['underlying_symbol'], contract['expiry'], float(contract['strike_price']),
            contract['instrument_type'], instrument_key, contract['trading_symbol'], contract['lot_size'],
            _number(quote.get('last_price')), bid, ask, None, self.clock(),
            self.name, float(delta) if delta is not None else None,
            delta_observed_at=None, delta_source=self.name, delta_contract=contract['trading_symbol'])


class UpstoxPaperBroker(PaperBroker):
    """Existing local paper fills plus an isolated GET-only reader. No remote orders."""
    name = 'upstox'

    def __init__(self, profile, access_token, *, authorized=False, clock=utcnow):
        super().__init__(profile, clock=clock)
        self.market_data = UpstoxMarketData(access_token, authorized=authorized, clock=clock)
