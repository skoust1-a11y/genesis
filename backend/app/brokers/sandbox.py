"""A simulated broker that behaves like a real one, to exercise live plumbing with no money at risk.

Unlike `PaperBroker`, orders are asynchronous: they rest at the exchange and fill only when
polled (`fill_after_polls`) and when marketable against a bid/ask spread, possibly in parts
(`partial_fill_ratio`, `stall_partials`). It rejects orders above the freeze quantity, can
reject on demand (`reject_next`, `reject_when`), supports cancels and stop-limit (`SL`) orders,
reports basket margin, and is a `QuoteSource` priced from a synthetic market (`SandboxMarket`)
that tests move by hand or that replays the bundled sample one bar per index quote.

With `state_path`, orders, fills and positions persist across processes, as at a real broker.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any

from app.agents.trade_crew.legs import tradingsymbol
from app.brokers.base import (
    TERMINAL_ORDER_STATUSES,
    ChargeReport,
    FillReport,
    Instrument,
    OrderAck,
    OrderRequest,
    OrderUpdate,
    Quote,
)
from app.db.hashing import money
from app.db.types import utcnow
from app.engine.costs import CostProfile, cost_breakdown
from app.engine.market import Bar, load_sample_bars
from app.market.feed import IST, synthetic_premium

TICK = Decimal("0.05")
_SYMBOL = re.compile(r"^([A-Z]+)(\d{2}[A-Z]{3}\d{2})(\d+(?:\.\d+)?)(CE|PE)$")


def to_tick(price: Decimal | float, up: bool) -> Decimal:
    steps = (Decimal(str(price)) / TICK).quantize(Decimal(1), rounding=ROUND_CEILING if up else ROUND_FLOOR)
    return max(steps * TICK, TICK)


@dataclass
class SandboxMarket:
    """The synthetic market: index level, IV rank and VIX now, and the expiry new positions trade."""

    underlying: str = "NIFTY"
    spot: float = 22000.0
    iv_rank: float = 50.0
    vix: float = 14.0
    dte: int = 14
    lot_size: int = 75
    strike_step: float = 50
    spread_pct: float = 1.0
    strikes_each_side: int = 40
    expiry: str | None = None  # fixed once set; derived from `dte` and the clock otherwise
    bars: list[Bar] = field(default_factory=list)  # when set, each index quote advances one bar
    position: int = 0


@dataclass
class _Order:
    request: dict[str, Any]
    broker_order_id: str
    status: str = "OPEN"
    filled_qty: int = 0
    avg_price: str | None = None
    polls: int = 0
    triggered: bool = False
    message: str | None = None
    updated_at: str = ""


class SandboxBroker:
    name = "sandbox"
    reconciles = True

    def __init__(
        self,
        mode: str = "paper",
        *,
        market: SandboxMarket | None = None,
        profile: CostProfile | None = None,
        clock: Callable[[], datetime] = utcnow,
        state_path: Path | None = None,
        fill_after_polls: int = 1,
        partial_fill_ratio: float | None = None,
        stall_partials: bool = False,
        freeze_quantity: int | None = 1800,
        available: Decimal | None = Decimal("1000000"),
        margin_per_short_lot: Decimal = Decimal("60000"),
        hedge_benefit_per_lot: Decimal = Decimal("45000"),
        reject_next: int = 0,
        reject_when: Callable[[OrderRequest], str | None] | None = None,
    ) -> None:
        self.mode = mode
        self.market = market or SandboxMarket()
        self.profile = profile
        self.clock = clock
        self.state_path = Path(state_path) if state_path else None
        self.fill_after_polls = fill_after_polls
        self.partial_fill_ratio = partial_fill_ratio
        self.stall_partials = stall_partials
        self.freeze_quantity = freeze_quantity
        self.available = available
        self.margin_per_short_lot = margin_per_short_lot
        self.hedge_benefit_per_lot = hedge_benefit_per_lot
        self.reject_next = reject_next
        self.reject_when = reject_when
        self.cancels: list[str] = []
        self._seq = 0
        self._orders: dict[str, _Order] = {}
        self._fills: list[dict[str, Any]] = []
        self._load()

    @classmethod
    def from_sample(cls, *, mode: str, profile: CostProfile, clock, state_path: Path | None, settings) -> SandboxBroker:
        market = SandboxMarket(
            lot_size=settings.lot_size("NIFTY") or 75,
            strike_step=settings.strike_step("NIFTY") or 50,
            bars=load_sample_bars(),
        )
        return cls(mode, market=market, profile=profile, clock=clock, state_path=state_path,
                   freeze_quantity=settings.freeze_quantity("NIFTY"))  # fmt: skip

    # Persistence

    def _load(self) -> None:
        if not self.state_path or not self.state_path.exists():
            return
        state = json.loads(self.state_path.read_text())
        self._seq = state["seq"]
        self._orders = {k: _Order(**v) for k, v in state["orders"].items()}
        self._fills = state["fills"]
        self.market.position = state.get("market_position", 0)
        self.market.expiry = state.get("expiry")

    def _save(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "seq": self._seq,
            "orders": {k: asdict(v) for k, v in self._orders.items()},
            "fills": self._fills,
            "market_position": self.market.position,
            "expiry": self.market.expiry,
        }
        self.state_path.write_text(json.dumps(state, default=str))

    # Market

    def _today(self) -> date:
        return self.clock().astimezone(IST).date()

    def _expiry(self) -> str:
        if self.market.expiry is None:
            self.market.expiry = (self._today() + timedelta(days=self.market.dte)).isoformat()
        return self.market.expiry

    def _parse(self, symbol: str) -> tuple[float, str, str] | None:
        m = _SYMBOL.match(symbol)
        if not m:
            return None
        expiry = datetime.strptime(m.group(2).title(), "%y%b%d").date().isoformat()
        return float(m.group(3)), m.group(4), expiry

    def _mid(self, symbol: str) -> float | None:
        parsed = self._parse(symbol)
        if parsed is None:
            return None
        strike, option_type, expiry = parsed
        dte = (date.fromisoformat(expiry) - self._today()).days
        return synthetic_premium(self.market.spot, strike, option_type, self.market.iv_rank, max(dte, 0))

    def _bid_ask(self, symbol: str) -> tuple[Decimal, Decimal] | None:
        mid = self._mid(symbol)
        if mid is None:
            return None
        half = mid * self.market.spread_pct / 200
        return to_tick(max(mid - half, 0.05), up=False), to_tick(mid + half, up=True)

    def iv_rank(self, underlying: str, vix: float | None, as_of: str) -> float | None:
        return self.market.iv_rank

    def instruments(self, underlying: str) -> list[Instrument]:
        m = self.market
        if underlying.upper() != m.underlying:
            return []
        expiry = self._expiry()
        atm = round(m.spot / m.strike_step) * m.strike_step
        held = {self._parse(o.request["tradingsymbol"]) for o in self._orders.values()}
        keys = {
            (float(atm + i * m.strike_step), t, expiry)
            for i in range(-m.strikes_each_side, m.strikes_each_side + 1)
            for t in ("CE", "PE")
        } | {k for k in held if k}
        return [
            Instrument(tradingsymbol(m.underlying, e, s, t), m.underlying, e, s, t, m.lot_size,
                       freeze_quantity=self.freeze_quantity)  # fmt: skip
            for s, t, e in sorted(keys)
        ]

    def quotes(self, tradingsymbols: list[str]) -> dict[str, Quote]:
        now = self.clock()
        out = {}
        for symbol in tradingsymbols:
            ba = self._bid_ask(symbol)
            if ba:
                bid, ask = ba
                out[symbol] = Quote(symbol, (bid + ask) / 2, bid, ask, now)
        return out

    def underlying_quote(self, underlying: str) -> Quote | None:
        m = self.market
        if underlying.upper() != m.underlying:
            return None
        if m.bars:
            if m.position >= len(m.bars):
                return None
            bar = m.bars[m.position]
            m.position += 1
            m.spot, m.iv_rank, m.dte = bar.close, bar.iv_rank, bar.dte
            m.expiry = (self._today() + timedelta(days=bar.dte)).isoformat()
            self._save()
        spot = Decimal(str(m.spot))
        return Quote(m.underlying, spot, spot, spot, self.clock())

    def vix_quote(self) -> Quote | None:
        vix = Decimal(str(self.market.vix))
        return Quote("INDIAVIX", vix, vix, vix, self.clock())

    # Orders

    def place_order(self, order: OrderRequest) -> OrderAck:
        reason = None
        if self.reject_next > 0:
            self.reject_next -= 1
            reason = "rejected by sandbox"
        elif self.reject_when and (custom := self.reject_when(order)):
            reason = custom
        elif order.quantity <= 0 or order.side not in ("BUY", "SELL"):
            reason = "invalid side or quantity"
        elif order.order_type not in ("LIMIT", "SL") or order.limit_price is None or order.limit_price <= 0:
            reason = "sandbox accepts priced LIMIT and SL orders only"
        elif order.order_type == "SL" and (order.trigger_price is None or order.trigger_price <= 0):
            reason = "SL orders need a trigger price"
        elif self.freeze_quantity and order.quantity > self.freeze_quantity:
            reason = f"quantity {order.quantity} exceeds the freeze limit {self.freeze_quantity}"
        elif self._parse(order.tradingsymbol) is None:
            reason = f"unknown instrument {order.tradingsymbol}"
        if reason:
            return OrderAck(order.client_order_id, "", False, reason)
        self._seq += 1
        broker_order_id = f"sbx-{self._seq}"
        self._orders[broker_order_id] = _Order(
            request=asdict(order) | {"limit_price": str(order.limit_price),
                                     "trigger_price": str(order.trigger_price) if order.trigger_price else None},
            broker_order_id=broker_order_id,
            status="TRIGGER_PENDING" if order.order_type == "SL" else "OPEN",
            updated_at=self.clock().isoformat(),
        )  # fmt: skip
        self._save()
        return OrderAck(order.client_order_id, broker_order_id, True)

    def cancel_order(self, broker_order_id: str) -> OrderAck:
        o = self._orders.get(broker_order_id)
        if o is None:
            return OrderAck("", broker_order_id, False, "unknown order")
        self._advance()
        if o.status in TERMINAL_ORDER_STATUSES:
            return OrderAck(o.request["client_order_id"], broker_order_id, False, f"order is {o.status}")
        o.status, o.message, o.updated_at = "CANCELLED", "cancelled by user", self.clock().isoformat()
        self.cancels.append(broker_order_id)
        self._save()
        return OrderAck(o.request["client_order_id"], broker_order_id, True)

    def _fill(self, o: _Order, quantity: int, price: Decimal) -> None:
        prev = Decimal(o.avg_price) * o.filled_qty if o.avg_price else Decimal(0)
        o.filled_qty += quantity
        o.avg_price = str(money((prev + price * quantity) / o.filled_qty))
        o.status = "COMPLETE" if o.filled_qty >= o.request["quantity"] else "OPEN"
        o.updated_at = self.clock().isoformat()
        self._fills.append({
            "broker_order_id": o.broker_order_id, "broker_trade_id": f"{o.broker_order_id}-{len(self._fills) + 1}",
            "quantity": quantity, "price": str(price), "filled_at": o.updated_at,
            "side": o.request["side"], "tradingsymbol": o.request["tradingsymbol"],
        })  # fmt: skip

    def _advance(self) -> None:
        """One exchange step: resting orders fill when marketable, stop orders trigger."""
        changed = False
        for o in self._orders.values():
            if o.status in TERMINAL_ORDER_STATUSES:
                continue
            ba = self._bid_ask(o.request["tradingsymbol"])
            if ba is None:
                continue
            bid, ask = ba
            side, limit = o.request["side"], Decimal(o.request["limit_price"])
            if o.status == "TRIGGER_PENDING":
                trigger = Decimal(o.request["trigger_price"])
                if (side == "BUY" and ask >= trigger) or (side == "SELL" and bid <= trigger):
                    o.status, o.triggered, changed = "OPEN", True, True
                else:
                    continue
            o.polls += 1
            if o.polls < self.fill_after_polls:
                continue
            if o.filled_qty and self.stall_partials:
                continue
            marketable = limit >= ask if side == "BUY" else limit <= bid
            if not marketable:
                continue
            remaining = o.request["quantity"] - o.filled_qty
            quantity = remaining
            if self.partial_fill_ratio is not None and not o.filled_qty:
                lot = self.market.lot_size
                quantity = max(lot, int(remaining * self.partial_fill_ratio) // lot * lot)
                quantity = min(quantity, remaining)
            price = min(limit, ask) if side == "BUY" else max(limit, bid)
            self._fill(o, quantity, price)
            changed = True
        if changed:
            self._save()

    def _update(self, o: _Order) -> OrderUpdate:
        return OrderUpdate(
            broker_order_id=o.broker_order_id,
            status=o.status,
            filled_qty=o.filled_qty,
            pending_qty=0 if o.status in TERMINAL_ORDER_STATUSES else o.request["quantity"] - o.filled_qty,
            avg_price=Decimal(o.avg_price) if o.avg_price else None,
            exchange_order_id=f"x-{o.broker_order_id}",
            event_at=datetime.fromisoformat(o.updated_at) if o.updated_at else self.clock(),
            message=o.message,
        )

    def get_order(self, broker_order_id: str) -> OrderUpdate | None:
        self._advance()
        o = self._orders.get(broker_order_id)
        return self._update(o) if o else None

    def order_updates(self, since: datetime | None = None) -> list[OrderUpdate]:
        self._advance()
        updates = [self._update(o) for o in self._orders.values()]
        return [u for u in updates if since is None or u.event_at > since]

    def fills(self, since: datetime | None = None) -> list[FillReport]:
        self._advance()
        out = [
            FillReport(f["broker_order_id"], f["broker_trade_id"], f["quantity"], Decimal(f["price"]),
                       datetime.fromisoformat(f["filled_at"]))  # fmt: skip
            for f in self._fills
        ]
        return [f for f in out if since is None or f.filled_at > since]

    def positions(self) -> list[dict[str, Any]]:
        self._advance()
        net: dict[str, int] = {}
        for f in self._fills:
            signed = f["quantity"] if f["side"] == "BUY" else -f["quantity"]
            net[f["tradingsymbol"]] = net.get(f["tradingsymbol"], 0) + signed
        return [{"tradingsymbol": s, "quantity": q} for s, q in sorted(net.items()) if q]

    def charges(self, trade_date: date) -> list[ChargeReport]:
        if self.profile is None:
            return []
        out = []
        for f in self._fills:
            filled = datetime.fromisoformat(f["filled_at"])
            if filled.date() != trade_date:
                continue
            breakdown = asdict(
                cost_breakdown(float(f["price"]) * f["quantity"], orders=1, profile=self.profile,
                               side=f["side"], trade_date=trade_date.isoformat())  # fmt: skip
            )
            breakdown.pop("slippage")
            components = {k: money(v) for k, v in breakdown.items()}
            out.append(ChargeReport(trade_date, f["broker_order_id"], "api", components,
                                    sum(components.values(), Decimal("0"))))  # fmt: skip
        return out

    def margin_required(self, orders: list[OrderRequest]) -> Decimal | None:
        lot = self.market.lot_size
        shorts = sum(o.quantity for o in orders if o.side == "SELL") // lot
        longs = sum(o.quantity for o in orders if o.side == "BUY") // lot
        hedged = min(shorts, longs)
        return max(self.margin_per_short_lot * shorts - self.hedge_benefit_per_lot * hedged, Decimal(0))

    def available_margin(self) -> Decimal | None:
        return self.available
