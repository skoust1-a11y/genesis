"""What a planned option buy makes or loses when the index reaches its stop or targets.

Premiums are Black-76 prices at the IV implied by the option's own quote and the forward implied by put-call
parity, so any `MarketSnapshot` works: a broker's quotes, Groww's, or stored end-of-day data. IV is held
constant, which flatters calls on a rally (IV usually falls) and understates puts on a sell-off.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time

from app.config import Settings
from app.market.feed import IST, MarketSnapshot
from app.paper import order_charges


def _cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def black76(forward: float, strike: float, years: float, iv: float, option_type: str) -> float:
    """Undiscounted: over a weekly or monthly expiry, rates move a premium by well under 1%."""
    if years <= 0 or iv <= 0:
        return max(forward - strike if option_type == "CE" else strike - forward, 0.0)
    sd = iv * math.sqrt(years)
    d1 = math.log(forward / strike) / sd + sd / 2
    d2 = d1 - sd
    if option_type == "CE":
        return forward * _cdf(d1) - strike * _cdf(d2)
    return strike * _cdf(-d2) - forward * _cdf(-d1)


def implied_vol(price: float, forward: float, strike: float, years: float, option_type: str) -> float | None:
    """None when no volatility between 0.01% and 500% gives `price` (e.g. a quote below intrinsic)."""
    lo, hi = 1e-4, 5.0
    if not black76(forward, strike, years, lo, option_type) <= price <= black76(forward, strike, years, hi, option_type):
        return None
    for _ in range(100):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if black76(forward, strike, years, mid, option_type) < price else (lo, mid)
    return (lo + hi) / 2


def implied_basis(snapshot: MarketSnapshot, strikes: int = 5) -> float:
    """Forward minus spot from put-call parity (F = K + C - P), averaged over the strikes nearest spot."""
    q, e = snapshot.quotes, snapshot.expiry
    near = sorted(
        (k for k, t, x in q if t == "CE" and x == e and (k, "PE", e) in q), key=lambda k: abs(k - snapshot.spot)
    )[:strikes]
    if not near:
        raise ValueError("no strike near spot has both a call and a put quote")
    return sum(k + q[(k, "CE", e)] - q[(k, "PE", e)] - snapshot.spot for k in near) / len(near)


def plan_pnl(
    snapshot: MarketSnapshot,
    settings: Settings,
    option_type: str,
    strike: float,
    entry_spot: float,
    exits: dict[str, float],
    lots: int = 1,
    entry_after_days: float = 0.0,
    hold_days: float = 0.15,
) -> dict:
    """Premium and net P&L (after charges) for buying `lots` of the option when the index is at `entry_spot`
    `entry_after_days` from now, and selling it `hold_days` later at each index level in `exits`."""
    price = snapshot.quotes.get((float(strike), option_type, snapshot.expiry))
    if price is None:
        raise ValueError(f"no quote for {strike:g} {option_type} {snapshot.expiry}")
    basis = implied_basis(snapshot)
    expires = datetime.combine(date.fromisoformat(snapshot.expiry), time.fromisoformat(settings.market_close), IST)
    days_left = (expires - snapshot.timestamp).total_seconds() / 86400

    def years(days_from_now: float) -> float:
        return max(days_left - days_from_now, 0) / 365

    iv = implied_vol(price, snapshot.spot + basis, strike, years(0), option_type)
    if iv is None:
        raise ValueError(f"{strike:g} {option_type} at {price} implies no volatility; the quote looks stale")
    lot_size = snapshot.lot_size or settings.lot_size(snapshot.underlying)
    if not lot_size:
        raise ValueError(f"no lot size for {snapshot.underlying}")
    qty = lots * lot_size
    entry = black76(entry_spot + basis, strike, years(entry_after_days), iv, option_type)
    buy_charges = order_charges(settings, option_type, "BUY", entry, qty, snapshot.as_of)["total"]
    rows = []
    for name, spot in exits.items():
        premium = black76(spot + basis, strike, years(entry_after_days + hold_days), iv, option_type)
        charges = buy_charges + order_charges(settings, option_type, "SELL", premium, qty, snapshot.as_of)["total"]
        rows.append({
            "exit": name, "spot": spot, "premium": round(premium, 2),
            "pnl": round((premium - entry) * qty - charges, 2), "charges": round(charges, 2),
        })  # fmt: skip
    return {
        "underlying": snapshot.underlying, "expiry": snapshot.expiry, "spot": snapshot.spot,
        "quote_time": snapshot.timestamp.isoformat(), "option": f"{strike:g} {option_type}", "quote": price,
        "iv_pct": round(iv * 100, 2), "forward_basis": round(basis, 2), "lots": lots, "quantity": qty,
        "entry_spot": entry_spot, "entry_premium": round(entry, 2), "entry_value": round(entry * qty, 2),
        "exits": rows,
    }  # fmt: skip
