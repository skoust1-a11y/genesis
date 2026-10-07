"""Where the simulator's market data comes from.

Imported NSE end-of-day data when the market database has any for the underlying; otherwise the
bundled sample CSV with synthetic option prices, so reviews still run without an import.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from app.config import Settings
from app.engine.market import load_sample_bars, sample_data_fingerprint
from app.market.feed import EventLookup, MarketFeed, ReplayFeed
from app.market.historical import HistoricalFeed
from app.market.store import open_market_store


def _sim_clock() -> datetime:
    return datetime(2000, 1, 1, tzinfo=UTC)


class MarketSource(Protocol):
    data_source: str

    def dates(self) -> list[str]: ...

    def feed(self, dates: list[str], dte_window: tuple[int, int] | None) -> MarketFeed: ...

    def fingerprint(self) -> dict: ...

    def close(self) -> None: ...


class SampleSource:
    data_source = "sample_synthetic"

    def __init__(self, underlying: str, settings: Settings, events: EventLookup | None = None) -> None:
        self.underlying = underlying.upper()
        self.settings = settings
        self.events = events
        self.bars = load_sample_bars()

    def dates(self) -> list[str]:
        return [b.date for b in self.bars]

    def feed(self, dates: list[str], dte_window: tuple[int, int] | None) -> MarketFeed:
        wanted = set(dates)
        return ReplayFeed(
            self.underlying,
            bars=[b for b in self.bars if b.date in wanted],
            strike_step=self.settings.strike_step(self.underlying) or 50,
            clock=_sim_clock,
            events=self.events,
        )

    def fingerprint(self) -> dict:
        return sample_data_fingerprint()

    def close(self) -> None:
        pass


class HistoricalSource:
    data_source = "nse_eod"

    def __init__(self, store, underlying: str, events: EventLookup | None = None) -> None:
        self.store = store
        self.underlying = underlying.upper()
        self.events = events

    def dates(self) -> list[str]:
        return self.store.trading_dates(self.underlying)

    def feed(self, dates: list[str], dte_window: tuple[int, int] | None) -> MarketFeed:
        return HistoricalFeed(
            self.store, self.underlying, dates=dates, dte_window=dte_window, clock=_sim_clock, events=self.events
        )

    def fingerprint(self) -> dict:
        return self.store.fingerprint(self.underlying)

    def close(self) -> None:
        self.store.close()


def open_market_source(underlying: str, settings: Settings, events: EventLookup | None = None) -> MarketSource:
    store = open_market_store(settings)
    if store.has_data(underlying):
        return HistoricalSource(store, underlying, events)
    store.close()
    return SampleSource(underlying, settings, events)
