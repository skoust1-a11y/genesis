"""Simulated option fills for paper trading. Pure functions: no clock, no database, no network, no broker.

A long option buys at the ask and sells at the bid, each plus ``ticks`` of slippage, and only from a quote that
``quote_decision`` grades executable (fresh book, sane spread, quote time, trading symbol). Last price is never a
fill: without an executable book the result is UNPRICED with a reason, and no premium is invented.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.config import Settings
from app.paper import order_charges
from app.trading.option_quote import ContractQuote, parse_chain_leg, quote_decision

TICK = Decimal("0.05")
PAISA = Decimal("0.01")

QUOTE_MISSING = "QUOTE_MISSING"
QUOTE_STALE = "QUOTE_STALE"
NO_BID_ASK = "NO_BID_ASK"
NO_QUOTE_TIME = "NO_QUOTE_TIME"
SPREAD_WIDE = "SPREAD_WIDE"
QUOTE_REJECTED = "QUOTE_REJECTED"
RISK_LIMIT = "RISK_LIMIT"
INSUFFICIENT_CAPITAL = "INSUFFICIENT_CAPITAL"
_REASONS = {
    "quote is stale": QUOTE_STALE,
    "spread is wide": SPREAD_WIDE,
    "bid or ask is missing": NO_BID_ASK,
    "last price is not an executable quote": NO_BID_ASK,
    "quote time is missing": NO_QUOTE_TIME,
}


@dataclass(frozen=True)
class Fill:
    side: str
    lots: int
    qty: int
    price: Decimal
    bid: Decimal
    ask: Decimal
    ltp: Decimal | None
    mid: Decimal
    quote_time: str
    fetched_at: str
    quote_age_s: float
    quote_source: str
    slippage_inr: Decimal  # cost against the mid, always >= 0
    charges: dict
    charges_inr: Decimal

    @property
    def value(self) -> Decimal:
        return self.price * self.qty


def executable(quote: ContractQuote | None, now: datetime, settings: Settings) -> tuple[ContractQuote | None, str | None]:
    """(quote, None) when the book can be traded now, else (None, UNPRICED reason)."""
    if quote is None:
        return None, QUOTE_MISSING
    decision = quote_decision(quote, now, settings)
    if decision.status == "executable":
        return quote, None
    return None, _REASONS.get(decision.reason or "", QUOTE_REJECTED)


def fill(quote: ContractQuote, side: str, lots: int, *, now: datetime, settings: Settings, ticks: int) -> Fill:
    """BUY at ask + ticks, SELL at bid - ticks (never below one tick). ``quote`` must be ``executable``."""
    qty = lots * quote.lot_size
    slip = TICK * ticks
    price = quote.ask + slip if side == "BUY" else max(quote.bid - slip, TICK)
    mid = (quote.bid + quote.ask) / 2
    charges = order_charges(settings, "OPT", side, float(price), qty, now.date().isoformat())
    stamp = quote.quote_time or quote.fetched_at
    return Fill(side=side, lots=lots, qty=qty, price=price, bid=quote.bid, ask=quote.ask, ltp=quote.ltp,
                mid=mid, quote_time=stamp.isoformat(), fetched_at=quote.fetched_at.isoformat(),
                quote_age_s=round((now - stamp).total_seconds(), 3), quote_source=quote.source,
                slippage_inr=(abs(price - mid) * qty).quantize(PAISA), charges=charges,
                charges_inr=Decimal(str(charges["total"])).quantize(PAISA))


def max_loss(entry: Fill, settings: Settings, day: str) -> Decimal:
    """Premium paid, entry charges, and the fixed charges of selling at zero: a long option's worst case."""
    exit_fixed = Decimal(str(order_charges(settings, "OPT", "SELL", 0.0, entry.qty, day)["total"]))
    return entry.value + entry.charges_inr + exit_fixed


def size(quote: ContractQuote, *, now: datetime, settings: Settings, ticks: int, max_lots: int,
         loss_cap: Decimal, funds: Decimal) -> tuple[Fill | None, str | None]:
    """The largest lot count whose max loss fits both ``loss_cap`` and ``funds``."""
    day = now.date().isoformat()
    reason = RISK_LIMIT
    for lots in range(max_lots, 0, -1):
        entry = fill(quote, "BUY", lots, now=now, settings=settings, ticks=ticks)
        loss = max_loss(entry, settings, day)
        if loss > loss_cap:
            continue
        if loss <= funds:  # the exit charges must be payable too, or a sale near zero leaves cash negative
            return entry, None
        reason = INSUFFICIENT_CAPITAL
    return None, reason


def contract_quote(chain: dict | None, *, strike: float, option_type: str, expiry: str) -> ContractQuote | None:
    """The chain row for one held contract, or None when the chain does not carry it."""
    if not chain:
        return None
    for row in chain.get("contracts") or []:
        quote = parse_chain_leg(row, option_type, chain.get("expiry"), chain.get("fetched_at"))
        if quote is not None and quote.strike == strike and quote.expiry == expiry:
            return quote
    return None


def regime(df_5m) -> str:
    """regime-v1, fixed at the signal: close vs the session's mean typical price, and the 20-bar slope."""
    if df_5m is None or len(df_5m) < 2:
        return "UNKNOWN"
    day = str(df_5m.iloc[-1].get("date") or "")[:10]
    session = df_5m[[str(d)[:10] == day for d in df_5m["date"]]] if "date" in df_5m else df_5m
    typical = ((session["high"] + session["low"] + session["close"]) / 3).mean()
    closes = list(df_5m["close"].astype(float))
    if math.isnan(typical):
        return "UNKNOWN"
    side = "ABOVE_VWAP" if closes[-1] >= typical else "BELOW_VWAP"
    slope = "RISING" if closes[-1] >= closes[-min(20, len(closes))] else "FALLING"
    return f"{side}/{slope}"


def time_bucket(candle: str) -> str:
    hhmm = re.search(r"T?(\d{2}:\d{2})", candle[10:]).group(1)
    for end, label in (("10:00", "09:15-10:00"), ("11:30", "10:00-11:30"), ("13:00", "11:30-13:00"),
                       ("14:30", "13:00-14:30")):
        if hhmm < end:
            return label
    return "14:30-15:30"
