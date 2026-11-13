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
from decimal import Decimal, localcontext

from app.config import Settings
from app.paper import order_charges
from app.trading import paper_fees, regime as reg_mod
from app.trading.option_quote import IST, ContractQuote, _strike, parse_chain_leg, quote_decision

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
        with localcontext() as context:
            context.prec = 38
            return self.price * self.qty


def executable(quote: ContractQuote | None, now: datetime, settings: Settings) -> tuple[ContractQuote | None, str | None]:
    """(quote, None) when the book can be traded now, else (None, UNPRICED reason)."""
    if quote is None:
        return None, QUOTE_MISSING
    decision = quote_decision(quote, now, settings)
    if decision.status == "executable":
        return quote, None
    return None, _REASONS.get(decision.reason or "", QUOTE_REJECTED)


def fill(quote: ContractQuote, side: str, lots: int, *, now: datetime, settings: Settings, ticks: int,
         cost_policy_id: str | None = None,
         allow_historical_brokerage_assumption: bool = False) -> Fill:
    """BUY at ask + ticks, SELL at bid - ticks (never below one tick). ``quote`` must be ``executable``."""
    with localcontext() as context:
        context.prec = 38
        qty = lots * quote.lot_size
        slip = TICK * ticks
        price = quote.ask + slip if side == "BUY" else max(quote.bid - slip, TICK)
        mid = (quote.bid + quote.ask) / 2
        charges = _charges(
            price, qty, side, now.astimezone(IST).date().isoformat(), settings, cost_policy_id,
            allow_historical_brokerage_assumption=allow_historical_brokerage_assumption,
        )
        stamp = quote.quote_time or quote.fetched_at
        return Fill(side=side, lots=lots, qty=qty, price=price, bid=quote.bid, ask=quote.ask, ltp=quote.ltp,
                    mid=mid, quote_time=stamp.isoformat(), fetched_at=quote.fetched_at.isoformat(),
                    quote_age_s=round((now - stamp).total_seconds(), 3), quote_source=quote.source,
                    slippage_inr=(abs(price - mid) * qty).quantize(PAISA), charges=charges,
                    charges_inr=Decimal(str(charges["total"])).quantize(PAISA))


def max_loss(entry: Fill, settings: Settings, day: str) -> Decimal:
    """Premium paid, entry charges, and the fixed charges of selling at zero: a long option's worst case."""
    with localcontext() as context:
        context.prec = 38
        policy_snap = entry.charges.get("policy") or {}
        policy = policy_snap.get("policy_id")
        historical = bool(policy_snap.get("brokerage_provenance"))
        exit_fixed = Decimal(str(
            _charges(
                Decimal("0"), entry.qty, "SELL", day, settings, policy,
                allow_historical_brokerage_assumption=historical,
            )["total"]
        ))
        return entry.value + entry.charges_inr + exit_fixed


def _charges(price, qty, side, day, settings, policy, *, allow_historical_brokerage_assumption: bool = False):
    if policy is None and not allow_historical_brokerage_assumption:
        return order_charges(settings, "OPT", side, float(price), qty, day)
    if policy is not None and not paper_fees.is_known_policy_id(policy):
        raise paper_fees.FeePolicyUnavailable("COST_POLICY_UNKNOWN")
    receipt = paper_fees.order_charges(
        price=price, quantity=qty, side=side, market_day=day, policy_id=policy,
        allow_historical_brokerage_assumption=allow_historical_brokerage_assumption,
    )
    return {**receipt.pop("components"), **receipt, "slippage": "0.00"}


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


def contract_quote(chain: dict | None, *, strike: float, option_type: str, expiry: str,
                   underlying: str | None = None) -> ContractQuote | None:
    """Require one row for the requested slot, including malformed/conflicting duplicates."""
    if not chain or option_type not in ("CE", "PE"):
        return None
    candidates = [row for row in chain.get("contracts") or []
                  if isinstance(row, dict) and option_type.lower() in row
                  and any(_strike({key: row[key]}) == strike for key in ("strike", "strikePrice") if key in row)]
    if len(candidates) != 1:
        return None
    quote = parse_chain_leg(candidates[0], option_type, chain.get("expiry"), chain.get("fetched_at"))
    if (quote is None or quote.expiry != expiry or not quote.underlying
            or (underlying is not None and quote.underlying != underlying)):
        return None
    return quote


def regime(df_5m) -> str:
    """regime-v1, fixed at the signal: close vs the session's mean typical price, and the 20-bar slope."""
    return reg_mod.legacy_regime_v1(df_5m)


def regime_v2(df_5m, *, now: datetime, bars_15m=None, bars_1d=None, levels=None, vix_quote=None) -> reg_mod.MarketRegimeVector:
    """Deterministic causal regime-v2 vector over validated closed bars."""
    return reg_mod.classify_regime(
        df_5m, now=now, bars_15m=bars_15m, bars_1d=bars_1d, levels=levels, vix_quote=vix_quote,
    )


def time_bucket(candle: str) -> str:
    hhmm = re.search(r"T?(\d{2}:\d{2})", candle[10:]).group(1)
    for end, label in (("10:00", "09:15-10:00"), ("11:30", "10:00-11:30"), ("13:00", "11:30-13:00"),
                       ("14:30", "13:00-14:30")):
        if hhmm < end:
            return label
    return "14:30-15:30"
