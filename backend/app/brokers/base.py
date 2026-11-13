"""Interface a broker integration (Zerodha, Upstox, Dhan, Angel One, ...) must implement.

`PaperBroker` and `SandboxBroker` ship; a real broker is one module that implements
`BrokerAdapter` (and `QuoteSource`, to drive `BrokerFeed`) plus an entry in
`app.brokers.registry`. An adapter only talks to the broker; persisting intents, orders,
order events, fills and charges goes through ``app.live.store.LiveStore`` so every
step lands in the append-only tables and the audit chain.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable


@dataclass
class OrderRequest:
    client_order_id: str
    exchange: str
    tradingsymbol: str
    side: str  # "BUY" | "SELL"
    quantity: int
    order_type: str  # "MARKET" | "LIMIT" | "SL" | "SL-M"
    product: str  # "NRML" | "MIS"
    limit_price: Decimal | None = None
    trigger_price: Decimal | None = None
    sebi_algo_id: str | None = None


@dataclass
class OrderAck:
    client_order_id: str
    broker_order_id: str
    accepted: bool
    message: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class OrderUpdate:
    broker_order_id: str
    status: str
    filled_qty: int
    pending_qty: int
    avg_price: Decimal | None
    exchange_order_id: str | None
    event_at: datetime
    message: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class FillReport:
    broker_order_id: str
    broker_trade_id: str
    quantity: int
    price: Decimal
    filled_at: datetime
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChargeReport:
    trade_date: date
    broker_order_id: str | None
    source: str  # "contract_note" | "api"
    breakdown: dict[str, Decimal]
    total: Decimal
    raw: dict[str, Any] = field(default_factory=dict)


TERMINAL_ORDER_STATUSES = ("COMPLETE", "REJECTED", "CANCELLED")


@dataclass
class Instrument:
    tradingsymbol: str
    underlying: str
    expiry: str  # ISO date
    strike: float
    option_type: str  # "CE" | "PE"
    lot_size: int
    tick_size: Decimal = Decimal("0.05")
    freeze_quantity: int | None = None
    exchange: str = "NFO"


@dataclass
class Quote:
    tradingsymbol: str
    last: Decimal | None
    bid: Decimal | None
    ask: Decimal | None
    timestamp: datetime
    high: Decimal | None = None  # the day's high and low so far, for index quotes when the broker sends them
    low: Decimal | None = None


@runtime_checkable
class QuoteSource(Protocol):
    """Market data a broker exposes: the option instrument list, option quotes, the index and India VIX."""

    def instruments(self, underlying: str) -> list[Instrument]: ...

    def quotes(self, tradingsymbols: list[str]) -> dict[str, Quote]: ...

    def underlying_quote(self, underlying: str) -> Quote | None: ...

    def vix_quote(self) -> Quote | None: ...


@runtime_checkable
class BrokerAdapter(Protocol):
    name: str
    mode: str  # "paper" | "live"

    def place_order(self, order: OrderRequest) -> OrderAck: ...

    def cancel_order(self, broker_order_id: str) -> OrderAck: ...

    def order_updates(self, since: datetime | None = None) -> Iterable[OrderUpdate]: ...

    def fills(self, since: datetime | None = None) -> Iterable[FillReport]: ...

    def positions(self) -> list[dict[str, Any]]: ...

    def charges(self, trade_date: date) -> list[ChargeReport]: ...


@runtime_checkable
class LiveBrokerAdapter(BrokerAdapter, Protocol):
    """What a live adapter must add: order polling, positions worth reconciling and margin.
    The crew falls back to `order_updates` and skips margin and reconciliation for adapters without it."""

    # False when the adapter's positions do not survive a restart (paper), so reconciling them is meaningless.
    reconciles: bool

    def get_order(self, broker_order_id: str) -> OrderUpdate | None: ...

    def margin_required(self, orders: list[OrderRequest]) -> Decimal | None:
        """Margin for the whole basket (multi-leg benefit included); None when the broker cannot say."""
        ...

    def available_margin(self) -> Decimal | None:
        """None means margin is not checked (paper)."""
        ...
