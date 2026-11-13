"""Manual paper trading: charges on each order and positions netted from the order log."""

from __future__ import annotations

from dataclasses import asdict, replace

from app.config import Settings
from app.engine.costs import CostProfile, cost_breakdown

# ponytail: FY2025-26 rates. Futures: STT 0.02% on sells, NSE 0.00173%, stamp 0.002% on buys. Shares are
# charged as delivery (STT 0.1% on both sides, NSE 0.00297%, stamp 0.015% on buys), never as intraday.
# Move to Settings with schedules when these enter the backtester.
FUTURES_RATES = {"stt_pct": 0.0002, "exchange_txn_pct": 0.0000173, "stamp_duty_pct": 0.00002}
EQUITY_RATES = {"stt_pct": 0.001, "exchange_txn_pct": 0.0000297, "stamp_duty_pct": 0.00015}


def order_charges(settings: Settings, instrument: str, side: str, price: float, quantity: int, day: str) -> dict:
    profile = CostProfile(
        brokerage_per_order=settings.brokerage_per_order,
        stt_pct=settings.stt_pct,
        exchange_txn_pct=settings.exchange_txn_pct,
        gst_pct=settings.gst_pct,
        sebi_per_crore=settings.sebi_per_crore,
        stamp_duty_pct=settings.stamp_duty_pct,
        slippage_bps=0,  # the user fills at the price they typed
        stt_schedule=settings.schedule(settings.stt_schedule),
        exchange_txn_schedule=settings.schedule(settings.exchange_txn_schedule),
    )
    rates = {"FUT": FUTURES_RATES, "EQ": EQUITY_RATES}.get(instrument)
    if rates:
        profile = replace(profile, **rates, stt_schedule=[], exchange_txn_schedule=[])
    costs = cost_breakdown(price * quantity, 1, profile, side=side, trade_date=day)
    if instrument == "EQ" and side == "BUY":
        costs.stt = price * quantity * EQUITY_RATES["stt_pct"]
    return {k: round(v, 2) for k, v in asdict(costs).items()} | {"total": round(costs.total, 2)}


def net_positions(orders) -> list[dict]:
    """Average-cost netting per contract, in order of placement."""
    book: dict[tuple, dict] = {}
    for o in sorted(orders, key=lambda o: o.created_at):
        key = (o.underlying, o.instrument, o.strike, o.expiry)
        p = book.setdefault(key, {
            "underlying": o.underlying, "instrument": o.instrument, "strike": o.strike, "expiry": o.expiry,
            "lot_size": o.lot_size, "quantity": 0, "avg_price": 0.0, "realized_pnl": 0.0, "charges": 0.0,
        })  # fmt: skip
        qty = o.lots * o.lot_size * (1 if o.side == "BUY" else -1)
        p["charges"] += o.charges.get("total", 0.0)
        pos = p["quantity"]
        if pos == 0 or (pos > 0) == (qty > 0):
            p["avg_price"] = (p["avg_price"] * abs(pos) + o.price * abs(qty)) / (abs(pos) + abs(qty))
        else:
            closing = min(abs(qty), abs(pos))
            p["realized_pnl"] += closing * (o.price - p["avg_price"]) * (1 if pos > 0 else -1)
            if abs(qty) > abs(pos):
                p["avg_price"] = o.price
        p["quantity"] = pos + qty
        if p["quantity"] == 0:
            p["avg_price"] = 0.0
    return [p | {"avg_price": round(p["avg_price"], 2), "realized_pnl": round(p["realized_pnl"], 2),
                 "charges": round(p["charges"], 2)} for p in book.values()]  # fmt: skip
