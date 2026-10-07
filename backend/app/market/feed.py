"""Market snapshots for the Trade Crew.

`ReplayFeed` replays `data/sample_nifty_options.csv` one bar per snapshot with synthetic option
quotes. It exists for paper runs and tests only; a real feed comes with a broker adapter.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Protocol

from app.db.types import utcnow
from app.engine.market import Bar, load_sample_bars

Contract = tuple[float, str, str]  # (strike, option_type, expiry)
EventLookup = Callable[[str, str], list[dict]]  # (underlying, as_of) -> calendar events near that date


@dataclass
class MarketSnapshot:
    underlying: str
    timestamp: datetime  # when the snapshot was observed
    as_of: str  # market date the values belong to
    spot: float
    iv_rank: float
    dte: int
    expiry: str  # the expiry new positions trade
    quotes: dict[Contract, float] = field(default_factory=dict)
    lot_size: int | None = None  # exchange lot size on this date, when the feed knows it
    stale: set[Contract] = field(default_factory=set)  # quotes from settlement prices, not trades
    events: list[dict] | None = None  # calendar events near `as_of`; None when no calendar is loaded

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
        }


class MarketFeed(Protocol):
    def snapshot(self, underlying: str, contracts: Iterable[Contract] = ()) -> MarketSnapshot | None:
        """`contracts` must be quoted in addition to the current expiry's chain, such as open positions."""
        ...


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
