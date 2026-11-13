"""`HistoricalFeed` replays imported NSE end-of-day data one trading day per snapshot.

It implements `MarketFeed`, so the Trade Crew and the simulator read it the same way.

Quote policy:
- the close when the contract traded that day, otherwise the settlement price (marked stale);
- no row at all means no quote, which blocks an entry through the `market.quotes` risk check;
- a contract whose expiry is on or before the snapshot date settles at intrinsic value against
  the index close.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from datetime import datetime

from app.db.types import utcnow
from app.market.feed import Contract, EventLookup, MarketSnapshot, choose_expiry, days_between, intrinsic
from app.market.store import MarketStore, OptionQuote


def option_price(row: OptionQuote) -> tuple[float | None, bool]:
    """(price, stale) for one end-of-day row."""
    if row.volume and row.close and row.close > 0:
        return row.close, False
    if row.settle and row.settle > 0:
        return row.settle, True
    return None, False


class HistoricalFeed:
    def __init__(
        self,
        store: MarketStore,
        underlying: str,
        dates: list[str] | None = None,
        dte_window: tuple[int, int] | None = None,
        clock: Callable[[], datetime] = utcnow,
        events: EventLookup | None = None,
    ) -> None:
        self.store = store
        self.underlying = underlying.upper()
        self.dates = dates if dates is not None else store.trading_dates(self.underlying)
        self.dte_window = dte_window
        self.clock = clock
        self.events = events
        self.position = 0

    @property
    def exhausted(self) -> bool:
        return self.position >= len(self.dates)

    def snapshot(self, underlying: str, contracts: Iterable[Contract] = ()) -> MarketSnapshot | None:
        if underlying.upper() != self.underlying or self.exhausted:
            return None
        day = self.dates[self.position]
        self.position += 1
        bar = self.store.index_bar(self.underlying, day)
        if bar is None or bar.close is None or bar.iv_rank is None:
            return None
        expiry = choose_expiry(day, self.store.expiries(self.underlying, day), self.dte_window)
        if expiry is None:
            return None

        quotes: dict[Contract, float] = {}
        stale: set[Contract] = set()
        lots: Counter[int] = Counter()
        for (strike, option_type), row in self.store.chain(self.underlying, day, expiry).items():
            price, is_stale = option_price(row)
            if row.lot_size:
                lots[row.lot_size] += 1
            if price is None:
                continue
            key = (strike, option_type, expiry)
            quotes[key] = price
            if is_stale:
                stale.add(key)
        for strike, option_type, exp in contracts:
            key = (float(strike), option_type, exp)
            if exp <= day:
                quotes[key] = round(intrinsic(bar.close, float(strike), option_type), 2)
                stale.discard(key)
                continue
            if key in quotes:
                continue
            row = self.store.option(self.underlying, day, exp, float(strike), option_type)
            price, is_stale = option_price(row) if row else (None, False)
            if price is not None:
                quotes[key] = price
                if is_stale:
                    stale.add(key)

        prev_high, prev_low = self.store.previous_range(self.underlying, day) or (None, None)
        return MarketSnapshot(
            underlying=self.underlying,
            timestamp=self.clock(),
            as_of=day,
            spot=bar.close,
            iv_rank=bar.iv_rank,
            dte=days_between(day, expiry),
            expiry=expiry,
            quotes=quotes,
            lot_size=lots.most_common(1)[0][0] if lots else None,
            stale=stale,
            events=self.events(self.underlying, day) if self.events else None,
            vix=bar.vix,
            day_open=bar.open,
            day_high=bar.high,
            day_low=bar.low,
            prev_high=prev_high,
            prev_low=prev_low,
        )
