"""Canonical, read-only reconstruction of long-option paper cash, basis and realized risk.

No mark is invented. Fill prices already contain slippage. Unpriced/orphaned states do not
release quantity or basis. This module does not authorize trades or change financial evidence.
"""
from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from typing import Mapping, Sequence

from app.trading.option_quote import IST

PAISA = Decimal("0.01")
ZERO = Decimal("0.00")


class AccountingError(ValueError):
    """Persisted evidence cannot support a financial projection."""


def money(value: str | int | Decimal) -> Decimal:
    """Accept exact finite paisa amounts; never coerce a canonical float or silently round evidence."""
    try:
        if isinstance(value, (bool, float)):
            raise AccountingError("canonical money must be exact")
        amount = Decimal(value)
        with localcontext() as context:
            context.prec = 38
            rounded = amount.quantize(PAISA, rounding=ROUND_HALF_UP)
        if not amount.is_finite() or rounded != amount:
            raise AccountingError("canonical money must be finite and exact to paisa")
        return rounded
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise AccountingError("invalid canonical money") from exc


def stop_budget(effective_capital: Decimal, initial_capital: Decimal) -> Decimal:
    """Profits cannot compound the approved 2% initial-capital stop budget."""
    effective, initial = money(effective_capital), money(initial_capital)
    if initial <= ZERO:
        raise AccountingError("initial capital must be positive")
    with localcontext() as context:
        context.prec = 38
        return (max(ZERO, min(effective, initial)) * Decimal("0.02")).quantize(PAISA, rounding=ROUND_DOWN)


@dataclass(frozen=True)
class RealizedSlice:
    seq: int
    market_day: str
    net: Decimal
    allocated_basis: Decimal


@dataclass(frozen=True)
class TradeProjection:
    quantity: int
    entry_quantity: int
    cashflow: Decimal
    remaining_basis: Decimal
    realized: Decimal
    worst_case_reserved: Decimal
    charges: Decimal
    slippage: Decimal
    state: str
    slices: tuple[RealizedSlice, ...]
    charge_discrepancies: tuple[int, ...]
    fill_costs: tuple[tuple[str, Decimal, Decimal], ...]

    def gross_realized_loss(self, market_day: str | None = None) -> Decimal:
        return sum((max(ZERO, -s.net) for s in self.slices
                    if market_day is None or s.market_day == market_day), ZERO)


def project_trade(events: Sequence[Mapping], *, liquidation_cost: Decimal) -> TradeProjection:
    """Replay one entry and zero or more sales in sequence; neutral events have no monetary effect.

    liquidation_cost is the conservative fee reservation for the remaining exit, supplied from
    the trade's persisted cost policy, never today's defaults. It is retained in full while held.
    """
    with localcontext() as context:
        context.prec = 38
        return _project_trade(events, money(liquidation_cost))


def _project_trade(events: Sequence[Mapping], liquidation_cost: Decimal) -> TradeProjection:
    if not events or liquidation_cost < ZERO:
        raise AccountingError("missing entry or negative liquidation reserve")
    cashflow = basis = allocated = realized = charges = slippage = ZERO
    entry_qty = sold_qty = 0
    slices, discrepancies, fill_costs = [], [], []
    previous_state, previous_time = None, None
    trade_policy: tuple[str, str] | None = None
    for seq, event in enumerate(events, 1):
        if previous_state in ("CLOSED", "EXIT_UNPRICED", "EXPIRED_UNRESOLVED"):
            raise AccountingError("event follows a terminal outcome")
        if type(event["seq"]) is not int or event["seq"] != seq or event["from_state"] != previous_state:
            raise AccountingError("event sequence/state discontinuity")
        try:
            occurred = datetime.fromisoformat(event["occurred_at"])
            if occurred.utcoffset() is None or (previous_time is not None and occurred < previous_time):
                raise AccountingError("event clock is naive or moves backwards")
        except (TypeError, ValueError) as exc:
            raise AccountingError("invalid event clock") from exc
        previous_time, previous_state = occurred, event["to_state"]
        if previous_state not in ("OPEN", "PARTIALLY_EXITED", "CLOSED", "EXIT_UNPRICED", "EXPIRED_UNRESOLVED"):
            raise AccountingError("invalid paper state")
        side = event["side"]
        if side is None:
            if event["event_type"] not in ("PARTIAL_SKIPPED", "EXIT_UNPRICED", "EXPIRED_UNRESOLVED"):
                raise AccountingError("unknown neutral event")
            if seq == 1 or any(event.get(k) is not None for k in ("qty", "price_inr", "charges_inr", "slippage_inr")):
                raise AccountingError("non-fill event carries financial amounts")
            continue
        qty = event["qty"]
        if type(qty) is not int or qty <= 0:
            raise AccountingError("invalid fill quantity")
        price, fee = money(event["price_inr"]), money(event["charges_inr"])
        slip = money(event["slippage_inr"])
        if price <= ZERO or fee < ZERO or slip < ZERO:
            raise AccountingError("negative or zero fill amount")
        charges += fee
        slippage += slip
        fill_costs.append((occurred.astimezone(IST).date().isoformat(), fee, slip))
        # Older evidence used independently rounded floats. Expose discrepancies; do not rewrite it.
        raw = event["charges"]
        try:
            components = json.loads(raw) if isinstance(raw, str) else raw
            if "policy" in components:
                snapshot = components["policy"]
                if not isinstance(snapshot, dict) or not snapshot.get("policy_id"):
                    raise AccountingError("missing cost policy identity")
                digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":"),
                                                   allow_nan=False).encode()).hexdigest()
                if components.get("policy_hash") != digest:
                    raise AccountingError("cost policy hash mismatch")
                v_from = snapshot.get("verified_from")
                v_through = snapshot.get("verified_through")
                fill_day = occurred.astimezone(IST).date().isoformat()
                if v_from and v_through and not (str(v_from) <= fill_day <= str(v_through)):
                    raise AccountingError("cost policy date mismatch")
                current_policy = (str(snapshot["policy_id"]), digest)
                if trade_policy is None:
                    trade_policy = current_policy
                elif trade_policy != current_policy:
                    raise AccountingError("trade cost policy mismatch across fills")
            elif trade_policy is not None:
                raise AccountingError("missing cost policy on subsequent fill")
            parts = [money(str(components[k])) for k in
                     ("brokerage", "stt", "exchange_txn", "sebi", "stamp_duty", "gst", "slippage")]
            if "ipft" in components:
                parts.append(money(str(components["ipft"])))
            if any(p < ZERO for p in parts):
                raise AccountingError("negative charge component")
            total = sum(parts, ZERO)
            if total != fee or money(str(components["total"])) != fee:
                discrepancies.append(seq)
        except AccountingError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise AccountingError("invalid charge evidence") from exc
        value = price * qty
        if side == "BUY":
            if seq != 1 or entry_qty or event["event_type"] != "ENTRY":
                raise AccountingError("trade requires exactly one initial BUY")
            entry_qty, basis = qty, value + fee
            cashflow -= basis
        elif side == "SELL":
            if event["event_type"] not in ("PARTIAL_EXIT", "EXIT"):
                raise AccountingError("invalid sale event")
            if not entry_qty or sold_qty + qty > entry_qty:
                raise AccountingError("sale exceeds confirmed holdings")
            sold_qty += qty
            expected_state = "CLOSED" if sold_qty == entry_qty else "PARTIALLY_EXITED"
            if previous_state != expected_state:
                raise AccountingError("sale state disagrees with remaining quantity")
            cumulative_basis = (basis * sold_qty / entry_qty).quantize(PAISA, rounding=ROUND_HALF_UP)
            slice_basis = cumulative_basis - allocated
            allocated = cumulative_basis
            net = value - fee - slice_basis
            realized += net
            cashflow += value - fee
            slices.append(RealizedSlice(seq, occurred.astimezone(IST).date().isoformat(), net, slice_basis))
        else:
            raise AccountingError("invalid fill side")
    held = entry_qty - sold_qty
    remaining = basis - allocated
    if not entry_qty or (held == 0) != (previous_state == "CLOSED"):
        raise AccountingError("holdings and terminal state disagree")
    if cashflow != realized - remaining:
        raise AccountingError("cashflow/basis identity failed")
    return TradeProjection(held, entry_qty, cashflow, remaining, realized,
                           remaining + liquidation_cost if held else ZERO, charges, slippage,
                           previous_state, tuple(slices), tuple(discrepancies), tuple(fill_costs))


@dataclass(frozen=True)
class AccountProjection:
    cash: Decimal
    committed: Decimal
    realized: Decimal
    daily_realized: Decimal
    daily_charges: Decimal
    daily_slippage: Decimal
    total_charges: Decimal
    total_slippage: Decimal
    gross_daily_loss: Decimal
    reserved: Decimal
    equity: Decimal | None
    unrealized: Decimal | None
    capital_lower_bound: Decimal
    effective_capital: Decimal
    stop_risk_budget: Decimal
    unpriced: tuple[str, ...]
    charge_discrepancies: tuple[tuple[str, int], ...]

    @property
    def spendable_cash(self) -> Decimal:
        """Cash after reserving unpaid liquidation fees; premium basis is already paid."""
        return max(ZERO, self.cash - (self.reserved - self.committed))


def project_account(capital: Decimal, trades: Mapping[str, TradeProjection], *, market_day: str,
                    net_liquidation_values: Mapping[str, Decimal] | None = None) -> AccountProjection:
    """Values, when provided, are verified net proceeds for the full remaining quantity.

    Price/quote validation belongs to the caller; no missing value is treated as a zero P&L mark.
    The conservative capital bound is separate from equity and may assume zero recovery.
    """
    date.fromisoformat(market_day)
    with localcontext() as context:
        context.prec = 38
        initial = money(capital)
        if initial <= ZERO:
            raise AccountingError("initial capital must be positive")
        cash = initial + sum((t.cashflow for t in trades.values()), ZERO)
        committed = sum((t.remaining_basis for t in trades.values()), ZERO)
        realized = sum((t.realized for t in trades.values()), ZERO)
        if cash != initial + realized - committed or cash < ZERO:
            raise AccountingError("account cash reconciliation failed")
        reserved = sum((t.worst_case_reserved for t in trades.values()), ZERO)
        losses = sum((t.gross_realized_loss(market_day) for t in trades.values()), ZERO)
        held = {key: t for key, t in trades.items() if t.quantity}
        marks = net_liquidation_values or {}
        unpriced = tuple(sorted(set(held) - set(marks)))
        priced_value = sum((money(marks[key]) for key in held if key in marks), ZERO)
        # If worth zero, exit fees may still be payable. Keep those in the lower bound.
        unpriced_exit_costs = sum((held[key].worst_case_reserved - held[key].remaining_basis
                                  for key in unpriced), ZERO)
        bound = cash + priced_value - unpriced_exit_costs
        equity = None if unpriced else cash + priced_value
        unrealized = None if equity is None else equity - initial - realized
        effective = max(ZERO, min(initial, bound))
        return AccountProjection(
            cash=cash, committed=committed, realized=realized,
            daily_realized=sum((s.net for trade in trades.values() for s in trade.slices
                                if s.market_day == market_day), ZERO),
            daily_charges=sum((fee for trade in trades.values() for day, fee, _ in trade.fill_costs
                               if day == market_day), ZERO),
            daily_slippage=sum((slip for trade in trades.values() for day, _, slip in trade.fill_costs
                                if day == market_day), ZERO),
            total_charges=sum((t.charges for t in trades.values()), ZERO),
            total_slippage=sum((t.slippage for t in trades.values()), ZERO),
            gross_daily_loss=losses, reserved=reserved, equity=equity, unrealized=unrealized,
            capital_lower_bound=bound, effective_capital=effective, stop_risk_budget=stop_budget(effective, initial),
            unpriced=unpriced, charge_discrepancies=tuple((key, seq) for key, trade in sorted(trades.items())
                                                       for seq in trade.charge_discrepancies))
