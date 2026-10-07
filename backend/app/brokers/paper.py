"""A paper broker: fills every LIMIT order immediately at its limit price plus slippage."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.brokers.base import ChargeReport, FillReport, OrderAck, OrderRequest, OrderUpdate
from app.db.hashing import money
from app.db.types import utcnow
from app.engine.costs import CostProfile, cost_breakdown


class PaperBroker:
    name = "paper"
    mode = "paper"

    def __init__(self, profile: CostProfile, clock: Callable[[], datetime] = utcnow) -> None:
        self.profile = profile
        self.clock = clock
        self._seq = 0
        self._updates: list[OrderUpdate] = []
        self._fills: list[FillReport] = []
        self._charges: list[ChargeReport] = []
        self._positions: dict[str, int] = {}

    def place_order(self, order: OrderRequest) -> OrderAck:
        if order.order_type != "LIMIT" or order.limit_price is None or order.limit_price <= 0:
            return OrderAck(order.client_order_id, "", False, "paper broker only fills priced LIMIT orders")
        if order.quantity <= 0 or order.side not in ("BUY", "SELL"):
            return OrderAck(order.client_order_id, "", False, "invalid side or quantity")
        self._seq += 1
        broker_order_id = f"paper-{self._seq}"
        now = self.clock()
        slip = Decimal(str(self.profile.slippage_bps)) / Decimal(10_000)
        factor = (1 - slip) if order.side == "SELL" else (1 + slip)
        price = money(order.limit_price * factor)
        self._fills.append(FillReport(broker_order_id, f"{broker_order_id}-1", order.quantity, price, now))
        self._updates.append(
            OrderUpdate(broker_order_id, "COMPLETE", order.quantity, 0, price, f"x-{broker_order_id}", now)
        )
        breakdown = asdict(
            cost_breakdown(
                float(price) * order.quantity, orders=1, profile=self.profile,
                side=order.side, trade_date=now.date().isoformat(),
            )
        )  # fmt: skip
        breakdown.pop("slippage")  # already in the fill price
        components = {k: money(v) for k, v in breakdown.items()}
        self._charges.append(
            ChargeReport(now.date(), broker_order_id, "api", components, sum(components.values(), Decimal("0")))
        )
        signed = order.quantity if order.side == "BUY" else -order.quantity
        self._positions[order.tradingsymbol] = self._positions.get(order.tradingsymbol, 0) + signed
        return OrderAck(order.client_order_id, broker_order_id, True)

    def cancel_order(self, broker_order_id: str) -> OrderAck:
        return OrderAck("", broker_order_id, False, "paper orders fill immediately")

    def order_updates(self, since: datetime | None = None) -> list[OrderUpdate]:
        return [u for u in self._updates if since is None or u.event_at > since]

    def fills(self, since: datetime | None = None) -> list[FillReport]:
        return [f for f in self._fills if since is None or f.filled_at > since]

    def positions(self) -> list[dict[str, Any]]:
        return [{"tradingsymbol": s, "quantity": q} for s, q in self._positions.items() if q]

    def charges(self, trade_date: date) -> list[ChargeReport]:
        return [c for c in self._charges if c.trade_date == trade_date]
