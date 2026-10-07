"""Sending orders to a broker and waiting for them, for the Trade Crew.

Legs go out hedge first: BUY legs before SELL legs, for opens and closes alike (a close buys back
the shorts before selling the wings), so the account is never short an option without its wing.
Each order is a LIMIT at the leg's quote. The manager polls it until it reaches a terminal status
or `ORDER_FILL_TIMEOUT_S` passes, then cancels what is left and re-prices toward the market, at
most `ORDER_REPRICE_STEPS` times and never more than `MAX_SLIPPAGE_BPS` from the quote. Orders
above the instrument's freeze quantity are split, `MAX_ORDER_VALUE_INR` caps a single order and
`MAX_ORDERS_PER_MINUTE` paces them. Every status change, fill and charge goes through `LiveStore`.

A leg that cannot be filled completely stops the run with an error; the caller decides whether
to unwind (entries) or halt (exits).
"""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.brokers.base import TERMINAL_ORDER_STATUSES, BrokerAdapter, OrderRequest, OrderUpdate
from app.brokers.sandbox import to_tick
from app.config import Settings
from app.db.hashing import money
from app.db.models import BrokerCharge, Order, TradeIntent
from app.engine.costs import CostProfile, cost_breakdown
from app.live.store import LiveStore

EXCHANGE = "NFO"
PRODUCT = "NRML"


class RateLimiter:
    """At most `per_minute` orders in any 60 seconds; waits (with `sleep`) when the budget is spent."""

    def __init__(self, per_minute: int, sleep: Callable[[float], None], monotonic: Callable[[], float] = time.monotonic):
        self.per_minute = per_minute
        self.sleep = sleep
        self.monotonic = monotonic
        self.sent: deque[float] = deque()

    def acquire(self) -> None:
        now = self.monotonic()
        while self.sent and now - self.sent[0] >= 60:
            self.sent.popleft()
        if len(self.sent) >= self.per_minute:
            self.sleep(60 - (now - self.sent[0]))
            self.sent.popleft()
        self.sent.append(self.monotonic())


def latest_update(adapter: BrokerAdapter, broker_order_id: str) -> OrderUpdate | None:
    """The order's current state: `get_order` when the adapter has it, else its last entry in `order_updates`."""
    get_order = getattr(adapter, "get_order", None)
    if get_order is not None:
        return get_order(broker_order_id)
    return next((u for u in reversed(list(adapter.order_updates())) if u.broker_order_id == broker_order_id), None)


def hedge_first(legs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(legs, key=lambda leg: 0 if leg["side"] == "BUY" else 1)


def split_quantity(quantity: int, freeze: int | None, lot_size: int | None) -> list[int]:
    """Chunks no larger than the freeze quantity, each a whole number of lots when the lot size is known."""
    if not freeze or quantity <= freeze:
        return [quantity]
    chunk = freeze // lot_size * lot_size if lot_size and freeze >= lot_size else freeze
    full, rest = divmod(quantity, chunk)
    return [chunk] * full + ([rest] if rest else [])


def reprice(side: str, quote: Decimal, step: int, steps: int, max_slippage_bps: float) -> Decimal:
    """The limit price for attempt `step` (0 = the quote itself), moving toward the market."""
    if step == 0 or steps == 0:
        return money(quote)
    bps = Decimal(str(max_slippage_bps)) * step / steps / Decimal(10_000)
    return to_tick(quote * (1 + bps), up=True) if side == "BUY" else to_tick(quote * (1 - bps), up=False)


class OrderManager:
    def __init__(
        self,
        live: Session,
        adapter: BrokerAdapter,
        settings: Settings,
        profile: CostProfile,
        *,
        broker_account_id: str,
        sebi_algo_id: str | None,
        clock: Callable[[], datetime],
        sleep: Callable[[float], None] = time.sleep,
        limiter: RateLimiter | None = None,
        freeze_quantity: Callable[[dict[str, Any]], int | None] | None = None,
        lot_size: int | None = None,
    ) -> None:
        self.live = live
        self.store = LiveStore(live)
        self.adapter = adapter
        self.settings = settings
        self.profile = profile
        self.broker_account_id = broker_account_id
        self.sebi_algo_id = sebi_algo_id
        self.clock = clock
        self.sleep = sleep
        self.limiter = limiter or RateLimiter(settings.max_orders_per_minute, sleep)
        self.freeze_quantity = freeze_quantity or (lambda leg: None)
        self.lot_size = lot_size

    # Sending

    def execute(self, intent: TradeIntent, legs: list[dict[str, Any]], retries: int = 0) -> tuple[list[Order], str | None]:
        """Send every leg, hedges first. Returns the orders placed and the first error, if any;
        a rejection is retried `retries` times before it counts as an error."""
        orders: list[Order] = []
        for leg in hedge_first(legs):
            leg_orders, error = self._fill_leg(intent, leg, retries)
            orders += leg_orders
            if error:
                return orders, error
        return orders, None

    def _fill_leg(self, intent: TradeIntent, leg: dict[str, Any], retries: int) -> tuple[list[Order], str | None]:
        remaining, attempt, rejections = int(leg["quantity"]), 0, 0
        quote = Decimal(str(leg["quote"]))
        steps, orders = self.settings.order_reprice_steps, []
        while remaining > 0:
            price = reprice(leg["side"], quote, attempt, steps, self.settings.max_slippage_bps)
            for quantity in split_quantity(remaining, self.freeze_quantity(leg), self.lot_size):
                order, status, filled, message = self._place_and_wait(intent, leg, quantity, price)
                if order is not None:
                    orders.append(order)
                remaining -= filled
                if status == "REJECTED":
                    rejections += 1
                    if order is None or rejections > retries:
                        return orders, f"order for {leg['tradingsymbol']} rejected: {message}"
                    break
                if filled < quantity:
                    break
            else:
                continue
            if status != "REJECTED":
                attempt += 1
                if attempt > steps:
                    done = int(leg["quantity"]) - remaining
                    return orders, (
                        f"order for {leg['tradingsymbol']} not filled after {steps} re-prices "
                        f"({done} of {leg['quantity']} filled)"
                    )
        return orders, None

    def _place_and_wait(
        self, intent: TradeIntent, leg: dict[str, Any], quantity: int, price: Decimal
    ) -> tuple[Order | None, str, int, str | None]:
        """(order, final status, quantity filled, message). Order is None when nothing was sent."""
        cap = self.settings.max_order_value_inr
        if cap and price * quantity > Decimal(str(cap)):
            return None, "REJECTED", 0, f"order value {price * quantity:.0f} exceeds MAX_ORDER_VALUE_INR {cap:.0f}"
        request = OrderRequest(
            client_order_id=self._client_order_id(intent),
            exchange=EXCHANGE,
            tradingsymbol=leg["tradingsymbol"],
            side=leg["side"],
            quantity=quantity,
            order_type="LIMIT",
            product=PRODUCT,
            limit_price=price,
            sebi_algo_id=self.sebi_algo_id,
        )
        order, ack = self._send(intent, leg, request)
        if not ack.accepted:
            return order, "REJECTED", 0, ack.message
        polls = max(1, math.ceil(self.settings.order_fill_timeout_s / self.settings.order_poll_interval_s))
        update = None
        for i in range(polls):
            update = self.sync(order, leg)
            if update is not None and update.status in TERMINAL_ORDER_STATUSES:
                break
            if i < polls - 1:
                self.sleep(self.settings.order_poll_interval_s)
        else:
            self.adapter.cancel_order(order.broker_order_id)
            update = self.sync(order, leg)
        filled = sum(f.quantity for f in self.store.fills_for_order(order.id))
        self._charges(order)
        self.live.commit()
        status = self.store.order_status(order.id) or "OPEN"
        return order, status, filled, update.message if update else None

    def _client_order_id(self, intent: TradeIntent) -> str:
        return f"tc-{intent.id}-{len(self.store.orders_for_intent(intent.id)):02d}"

    def _send(self, intent: TradeIntent, leg: dict[str, Any], request: OrderRequest):
        self.limiter.acquire()
        now = self.clock()
        ack = self.adapter.place_order(request)
        order = self.store.place_order(
            trade_intent_id=intent.id,
            client_order_id=request.client_order_id,
            broker_order_id=ack.broker_order_id or None,
            sebi_algo_id=request.sebi_algo_id,
            exchange=request.exchange,
            tradingsymbol=request.tradingsymbol,
            expiry=leg.get("expiry"),
            strike=money(leg["strike"]) if leg.get("strike") is not None else None,
            option_type=leg.get("option_type"),
            side=request.side,
            quantity=request.quantity,
            order_type=request.order_type,
            product=request.product,
            limit_price=request.limit_price,
            trigger_price=request.trigger_price,
            placed_at=now,
        )
        if not ack.accepted:
            self.store.add_order_event(order_id=order.id, status="REJECTED", message=ack.message, raw=ack.raw,
                                       event_at=now)  # fmt: skip
        self.live.commit()
        return order, ack

    def place_stop(
        self, intent: TradeIntent, leg: dict[str, Any], quantity: int, trigger: Decimal, limit: Decimal
    ) -> tuple[Order, str | None]:
        """A resting stop-limit order (no waiting). Returns the order and an error if it was refused."""
        request = OrderRequest(
            client_order_id=self._client_order_id(intent),
            exchange=EXCHANGE,
            tradingsymbol=leg["tradingsymbol"],
            side=leg["side"],
            quantity=quantity,
            order_type="SL",
            product=PRODUCT,
            limit_price=limit,
            trigger_price=trigger,
            sebi_algo_id=self.sebi_algo_id,
        )
        order, ack = self._send(intent, leg, request)
        if not ack.accepted:
            return order, f"protective stop for {leg['tradingsymbol']} rejected: {ack.message}"
        self.sync(order, leg)
        self.live.commit()
        return order, None

    # Tracking

    def sync(self, order: Order, leg: dict[str, Any] | None = None) -> OrderUpdate | None:
        """Record the order's latest status and any new fills. Does not commit."""
        if not order.broker_order_id:
            return None
        update = latest_update(self.adapter, order.broker_order_id)
        if update is None:
            return None
        events = self.store.order_events(order.id)
        last = events[-1] if events else None
        if last is None or (last.status, last.filled_qty) != (update.status, update.filled_qty):
            self.store.add_order_event(
                order_id=order.id, status=update.status, filled_qty=update.filled_qty, pending_qty=update.pending_qty,
                avg_price=update.avg_price, message=update.message, raw=update.raw, event_at=update.event_at,
            )  # fmt: skip
        seen = {f.broker_trade_id for f in self.store.fills_for_order(order.id)}
        quote = float((leg or {}).get("quote") or order.limit_price)
        for f in self.adapter.fills():
            if f.broker_order_id != order.broker_order_id or f.broker_trade_id in seen:
                continue
            est = asdict(
                cost_breakdown(
                    quote * f.quantity, orders=1, profile=self.profile,
                    side=order.side, trade_date=f.filled_at.date().isoformat(),
                )
            )  # fmt: skip
            self.store.record_fill(
                order_id=order.id, broker_trade_id=f.broker_trade_id, quantity=f.quantity, price=f.price,
                est_cost_breakdown={k: money(v) for k, v in est.items()}, filled_at=f.filled_at,
            )  # fmt: skip
        return update

    def _charges(self, order: Order) -> None:
        if not order.broker_order_id:
            return
        if self.live.scalar(select(BrokerCharge.id).where(BrokerCharge.order_id == order.id).limit(1)):
            return
        for c in self.adapter.charges(self.clock().date()):
            if c.broker_order_id == order.broker_order_id:
                self.store.record_broker_charge(
                    broker_account_id=self.broker_account_id, trade_date=c.trade_date.isoformat(),
                    order_id=order.id, source=c.source, breakdown=c.breakdown, total=c.total, raw=c.raw,
                )  # fmt: skip

    def cancel(self, order: Order) -> OrderUpdate | None:
        """Cancel a working order and record where it ended up. Commits."""
        if order.broker_order_id and self.store.order_status(order.id) not in TERMINAL_ORDER_STATUSES:
            ack = self.adapter.cancel_order(order.broker_order_id)
            if not ack.accepted:
                self.store.add_order_event(order_id=order.id, status=self.store.order_status(order.id) or "OPEN",
                                           message=f"cancel refused: {ack.message}", raw=ack.raw,
                                           event_at=self.clock())  # fmt: skip
        update = self.sync(order)
        self._charges(order)
        self.live.commit()
        return update
