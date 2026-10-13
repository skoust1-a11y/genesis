"""Groww's public option-chain pages as a `QuoteSource`: no login, every listed expiry, last traded prices.

Read-only market data, not a broker: nothing here places orders. A broker with an API (Kite, Dhan, ...)
is the same four `QuoteSource` methods, with bid/ask instead of last price.
"""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import httpx

from app.brokers.base import Instrument, Quote
from app.db.types import utcnow

# ponytail: reads the page's embedded Next.js JSON, which Groww can change without notice, and has no bid/ask, so
# every quote counts as wide (stale) in `BrokerFeed`. Use a broker's quote API for anything that trades.
PAGES = {"NIFTY": "nifty", "BANKNIFTY": "nifty-bank", "FINNIFTY": "nifty-financial-services"}
UA = {"User-Agent": "Mozilla/5.0"}
NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)


def parse_page(html: str) -> dict:
    """The page's `optionChain` and `company` (index) payload."""
    m = NEXT_DATA.search(html)
    if not m:
        raise ValueError("no option-chain data on the Groww page")
    return json.loads(m.group(1))["props"]["pageProps"]["data"]


def _dec(value) -> Decimal | None:
    return Decimal(str(value)) if value else None


class GrowwQuotes:
    name = "groww"

    def __init__(self, max_age_seconds: float = 10, timeout: float = 15) -> None:
        self.max_age = max_age_seconds
        self.timeout = timeout
        self._pages: dict[tuple[str, str | None], tuple[float, dict]] = {}
        self._where: dict[str, tuple[str, str | None]] = {}  # tradingsymbol -> the page that lists it

    def _page(self, underlying: str, expiry: str | None = None) -> dict:
        """One expiry's chain; None is the page's default, the nearest expiry. Reused for `max_age` seconds,
        since a feed snapshot reads the same page for the index, the instruments and the quotes."""
        key = (underlying.upper(), expiry)
        hit = self._pages.get(key)
        if hit and time.monotonic() - hit[0] < self.max_age:
            return hit[1]
        slug = PAGES.get(key[0])
        if slug is None:
            raise ValueError(f"no Groww option page for {underlying} (known: {', '.join(PAGES)})")
        r = httpx.get(
            f"https://groww.in/options/{slug}", params={"expiry": expiry} if expiry else None,
            headers=UA, timeout=self.timeout, follow_redirects=True,
        )  # fmt: skip
        r.raise_for_status()
        page = parse_page(r.text)
        self._pages[key] = (time.monotonic(), page)
        return page

    def instruments(self, underlying: str) -> list[Instrument]:
        first = self._page(underlying)
        agg = first["optionChain"]["aggregatedDetails"]
        later = [e for e in agg["expiryDates"] if e > agg["currentExpiry"]]
        with ThreadPoolExecutor(max_workers=8) as pool:
            pages = [(None, first), *zip(later, pool.map(lambda e: self._page(underlying, e), later))]
        out = []
        for expiry_param, page in pages:
            details = page["optionChain"]["aggregatedDetails"]
            for row in page["optionChain"]["optionContracts"]:
                for side in ("ce", "pe"):
                    if not (c := row.get(side)):
                        continue
                    self._where[c["growwContractId"]] = (underlying.upper(), expiry_param)
                    out.append(Instrument(
                        tradingsymbol=c["growwContractId"], underlying=underlying.upper(),
                        expiry=details["currentExpiry"], strike=row["strikePrice"] / 100, option_type=side.upper(),
                        lot_size=c.get("marketLot") or details["lotSize"], freeze_quantity=details.get("freezeQty"),
                    ))  # fmt: skip
        return out

    def quotes(self, tradingsymbols: list[str]) -> dict[str, Quote]:
        """Quotes for symbols from `instruments()`; others are skipped."""
        ltp: dict[str, Decimal | None] = {}
        for key in {self._where[s] for s in tradingsymbols if s in self._where}:
            for row in self._page(*key)["optionChain"]["optionContracts"]:
                for side in ("ce", "pe"):
                    if c := row.get(side):
                        ltp[c["growwContractId"]] = _dec(c["liveData"].get("ltp"))
        now = utcnow()
        return {s: Quote(s, ltp[s], None, None, now) for s in tradingsymbols if ltp.get(s)}

    def underlying_quote(self, underlying: str) -> Quote | None:
        last = _dec(self._page(underlying)["company"]["liveData"].get("ltp"))
        return Quote(underlying.upper(), last, None, None, utcnow()) if last else None

    def vix_quote(self) -> Quote | None:
        # ponytail: the option pages carry no India VIX, so a feed on Groww has no IV rank and the crew's entry
        # checks fail. Fine for quotes and P&L; add a VIX source before paper trading the crew on it.
        return None
