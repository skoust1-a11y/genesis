"""Market snapshots for the Trade Crew.

`ReplayFeed` replays `data/sample_nifty_options.csv` one bar per snapshot with synthetic option
quotes, for paper runs and tests. `BrokerFeed` builds snapshots from a broker's `QuoteSource`:
the chain around spot for the expiry a backtest would pick (`choose_expiry`) plus every held contract,
priced at the bid/ask mid.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

from app.db.types import utcnow
from app.engine.market import Bar, load_sample_bars

if TYPE_CHECKING:
    from app.brokers.base import Instrument, Quote, QuoteSource
    from app.config import Settings

IST = timezone(timedelta(hours=5, minutes=30))

Contract = tuple[float, str, str]  # (strike, option_type, expiry)
EventLookup = Callable[[str, str], list[dict]]  # (underlying, as_of) -> calendar events near that date


@dataclass
class MarketSnapshot:
    underlying: str
    timestamp: datetime  # when the snapshot was observed
    as_of: str  # market date the values belong to
    spot: float
    iv_rank: float | None  # None when it cannot be computed; entry checks on it then fail
    dte: int
    expiry: str  # the expiry new positions trade
    quotes: dict[Contract, float] = field(default_factory=dict)
    lot_size: int | None = None  # exchange lot size on this date, when the feed knows it
    stale: set[Contract] = field(default_factory=set)  # quotes from settlement prices, not trades
    events: list[dict] | None = None  # calendar events near `as_of`; None when no calendar is loaded
    vix: float | None = None  # India VIX on `as_of`, when the feed knows it
    day_open: float | None = None  # index open/high/low on `as_of`, when the feed knows them
    day_high: float | None = None
    day_low: float | None = None
    prev_high: float | None = None  # index high/low on the previous trading day
    prev_low: float | None = None

    @property
    def pct_from_prev_high(self) -> float | None:
        """Spot vs the previous day's high, in %: above 0 means spot broke above it."""
        return round((self.spot - self.prev_high) / self.prev_high * 100, 4) if self.prev_high else None

    @property
    def pct_from_prev_low(self) -> float | None:
        """Spot vs the previous day's low, in %: below 0 means spot broke below it."""
        return round((self.spot - self.prev_low) / self.prev_low * 100, 4) if self.prev_low else None

    def quote(self, strike: float, option_type: str, expiry: str | None = None) -> float | None:
        return self.quotes.get((float(strike), option_type, expiry or self.expiry))

    def days_to(self, expiry: str) -> int:
        return (date.fromisoformat(expiry) - date.fromisoformat(self.as_of)).days

    def summary(self) -> dict:
        return {
            "underlying": self.underlying,
            "timestamp": self.timestamp,
            "as_of": self.as_of,
            "spot": self.spot,
            "iv_rank": self.iv_rank,
            "dte": self.dte,
            "expiry": self.expiry,
            "vix": self.vix,
            "day_open": self.day_open,
            "day_high": self.day_high,
            "day_low": self.day_low,
            "prev_high": self.prev_high,
            "prev_low": self.prev_low,
        }


class MarketFeed(Protocol):
    def snapshot(self, underlying: str, contracts: Iterable[Contract] = ()) -> MarketSnapshot | None:
        """`contracts` must be quoted in addition to the current expiry's chain, such as open positions."""
        ...


def days_between(start: str, end: str) -> int:
    return (date.fromisoformat(end) - date.fromisoformat(start)).days


def choose_expiry(day: str, expiries: Iterable[str], dte_window: tuple[int, int] | None) -> str | None:
    """The nearest expiry inside the strategy's DTE window, else the nearest one after today."""
    ahead = sorted(e for e in expiries if days_between(day, e) >= 1)
    if not ahead:
        return None
    if dte_window:
        lo, hi = dte_window
        inside = [e for e in ahead if lo <= days_between(day, e) <= hi]
        if inside:
            return inside[0]
    return ahead[0]


def snapshot_age_seconds(snapshot: MarketSnapshot, now: datetime) -> float:
    return (now - snapshot.timestamp).total_seconds()


def intrinsic(spot: float, strike: float, option_type: str) -> float:
    return max((spot - strike) if option_type == "CE" else (strike - spot), 0.0)


def synthetic_premium(spot: float, strike: float, option_type: str, iv_rank: float, dte: int) -> float:
    """Bachelier (normal-model) price with an IV mapped from IV rank. Synthetic, for paper runs only."""
    moneyness = (spot - strike) if option_type == "CE" else (strike - spot)
    if dte <= 0:
        return round(max(moneyness, 0.05), 2)
    iv = 0.10 + 0.20 * iv_rank / 100
    sd = spot * iv * math.sqrt(dte / 365)
    x = moneyness / sd
    cdf = 0.5 * (1 + math.erf(x / math.sqrt(2)))
    pdf = math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)
    return round(max(moneyness * cdf + sd * pdf, 0.05), 2)


class ReplayFeed:
    def __init__(
        self,
        underlying: str = "NIFTY",
        bars: list[Bar] | None = None,
        strike_step: float = 50,
        strikes_each_side: int = 40,
        clock: Callable[[], datetime] = utcnow,
        events: EventLookup | None = None,
    ) -> None:
        self.underlying = underlying.upper()
        self.bars = bars if bars is not None else load_sample_bars()
        self.strike_step = strike_step
        self.strikes_each_side = strikes_each_side
        self.clock = clock
        self.events = events
        self.position = 0

    @property
    def exhausted(self) -> bool:
        return self.position >= len(self.bars)

    def snapshot(self, underlying: str, contracts: Iterable[Contract] = ()) -> MarketSnapshot | None:
        if underlying.upper() != self.underlying or self.exhausted:
            return None
        bar = self.bars[self.position]
        self.position += 1
        today = date.fromisoformat(bar.date)
        expiry = (today + timedelta(days=bar.dte)).isoformat()
        atm = round(bar.close / self.strike_step) * self.strike_step
        wanted = {
            (float(atm + i * self.strike_step), option_type, expiry)
            for i in range(-self.strikes_each_side, self.strikes_each_side + 1)
            for option_type in ("CE", "PE")
        } | {(float(strike), option_type, exp) for strike, option_type, exp in contracts}
        quotes = {
            (strike, option_type, exp): synthetic_premium(
                bar.close, strike, option_type, bar.iv_rank, (date.fromisoformat(exp) - today).days
            )
            for strike, option_type, exp in wanted
        }
        return MarketSnapshot(
            underlying=self.underlying,
            timestamp=self.clock(),
            as_of=bar.date,
            spot=bar.close,
            iv_rank=bar.iv_rank,
            dte=bar.dte,
            expiry=expiry,
            quotes=quotes,
            events=self.events(self.underlying, bar.date) if self.events else None,
        )


IvRank = Callable[[str, float | None, str], float | None]  # (underlying, today's VIX, as_of) -> IV rank


def _num(value: Decimal | None) -> float | None:
    return float(value) if value is not None and value > 0 else None


def quote_price(quote: Quote | None, max_spread_pct: float) -> tuple[float | None, bool]:
    """(price, wide). The bid/ask mid when both sides exist and the spread is within `max_spread_pct`
    of the mid; otherwise the mid or last traded price, flagged as wide (stale)."""
    if quote is None:
        return None, False
    bid, ask, last = _num(quote.bid), _num(quote.ask), _num(quote.last)
    if bid and ask and ask >= bid:
        mid = (bid + ask) / 2
        return round(mid, 2), (ask - bid) / mid * 100 > max_spread_pct
    return (round(last, 2), True) if last else (None, False)


def vix_iv_rank(settings: Settings) -> IvRank:
    """IV rank of today's India VIX against the imported VIX history before today."""
    from sqlalchemy import select

    from app.market.models import IndexBar
    from app.market.nse import iv_rank_series
    from app.market.store import open_market_store

    def rank(underlying: str, vix: float | None, as_of: str) -> float | None:
        if vix is None:
            return None
        store = open_market_store(settings)
        try:
            history = list(
                store.session.scalars(
                    select(IndexBar.vix)
                    .where(IndexBar.underlying == underlying.upper(), IndexBar.date < as_of, IndexBar.vix.is_not(None))
                    .order_by(IndexBar.date)
                )
            )
        finally:
            store.close()
        return iv_rank_series([*history, vix], settings.iv_rank_lookback_days, settings.iv_rank_min_history)[-1]

    return rank


PrevRange = Callable[[str, str], tuple[float, float] | None]  # (underlying, as_of) -> previous day's (high, low)


def stored_prev_range(settings: Settings) -> PrevRange:
    """The previous trading day's high and low from the imported index history."""
    from app.market.store import open_market_store

    def prev_range(underlying: str, as_of: str) -> tuple[float, float] | None:
        store = open_market_store(settings)
        try:
            return store.previous_range(underlying, as_of)
        finally:
            store.close()

    return prev_range


class BrokerFeed:
    """Snapshots from a broker's quotes. Never exhausted; a missing index quote means no snapshot."""

    exhausted = False

    def __init__(
        self,
        source: QuoteSource,
        settings: Settings,
        clock: Callable[[], datetime] = utcnow,
        events: EventLookup | None = None,
        iv_rank: IvRank | None = None,
        strikes_each_side: int = 40,
        prev_range: PrevRange | None = None,
        dte_window: tuple[int, int] | None = None,
    ) -> None:
        self.source = source
        self.settings = settings
        self.clock = clock
        self.events = events
        self.iv_rank = iv_rank or vix_iv_rank(settings)
        self.prev_range = prev_range or stored_prev_range(settings)
        self.strikes_each_side = strikes_each_side
        self.dte_window = dte_window
        self._chains: dict[str, tuple[str, list[Instrument]]] = {}

    def _chain(self, underlying: str, as_of: str) -> list[Instrument]:
        cached = self._chains.get(underlying)
        if cached is None or cached[0] != as_of:
            self._chains[underlying] = (as_of, self.source.instruments(underlying))
        return self._chains[underlying][1]

    def snapshot(self, underlying: str, contracts: Iterable[Contract] = ()) -> MarketSnapshot | None:
        underlying = underlying.upper()
        now = self.clock()
        as_of = now.astimezone(IST).date().isoformat()
        spot_quote = self.source.underlying_quote(underlying)
        spot, _ = quote_price(spot_quote, float("inf"))
        if spot is None or spot_quote is None:
            return None
        chain = [i for i in self._chain(underlying, as_of) if i.expiry >= as_of]
        expiry = choose_expiry(as_of, {i.expiry for i in chain}, self.dte_window)
        if expiry is None:
            return None
        step = self.settings.strike_step(underlying) or 50
        near = [i for i in chain if i.expiry == expiry and abs(i.strike - spot) <= self.strikes_each_side * step]
        by_key = {(float(i.strike), i.option_type, i.expiry): i for i in chain}
        held = [by_key[k] for k in ((float(s), t, e) for s, t, e in contracts) if k in by_key]
        wanted = {i.tradingsymbol: i for i in [*near, *held]}
        raw = self.source.quotes(list(wanted))
        quotes: dict[Contract, float] = {}
        stale: set[Contract] = set()
        for symbol, inst in wanted.items():
            price, wide = quote_price(raw.get(symbol), self.settings.max_spread_pct)
            if price is None:
                continue
            key = (float(inst.strike), inst.option_type, inst.expiry)
            quotes[key] = price
            if wide:
                stale.add(key)
        vix, _ = quote_price(self.source.vix_quote(), float("inf"))
        prev_high, prev_low = self.prev_range(underlying, as_of) or (None, None)
        return MarketSnapshot(
            underlying=underlying,
            timestamp=spot_quote.timestamp,
            as_of=as_of,
            spot=spot,
            iv_rank=self.iv_rank(underlying, vix, as_of),
            dte=days_between(as_of, expiry),
            expiry=expiry,
            quotes=quotes,
            lot_size=next((i.lot_size for i in near), None),
            stale=stale,
            events=self.events(underlying, as_of) if self.events else None,
            vix=vix,
            prev_high=prev_high,
            prev_low=prev_low,
        )

    def instrument(self, underlying: str, strike: float, option_type: str, expiry: str) -> Instrument | None:
        as_of = self.clock().astimezone(IST).date().isoformat()
        return next(
            (
                i for i in self._chain(underlying.upper(), as_of)
                if float(i.strike) == float(strike) and i.option_type == option_type and i.expiry == expiry
            ),
            None,
        )  # fmt: skip
