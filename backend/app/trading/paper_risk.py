"""Deterministic V2 risk estimates. Estimates never become execution prices."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, localcontext

from app.trading.paper_accounting import money
from app.config import Settings
from app.trading import paper_exec as px, paper_fees
from app.trading.option_quote import ContractQuote, IST

POLICY_ID = "paper-risk-v2"
FULL_PREMIUM_LIMIT = Decimal("5000.00")
DAILY_EXPOSURE_LIMIT = Decimal("5000.00")
DELTA_MAX_AGE_SECONDS = 10
MAX_LOTS = 3
SLIPPAGE_TICKS = 1


class StopRiskUnverifiable(ValueError):
    def __init__(self):
        super().__init__("STOP_RISK_UNVERIFIABLE")


@dataclass(frozen=True)
class DeltaEvidence:
    value: Decimal
    observed_at: datetime
    contract: str
    source: str


def stop_risk(*, evidence: DeltaEvidence | None, trusted_sources: frozenset[str], contract: str,
              option_type: str, now: datetime, underlying_entry: Decimal, underlying_stop: Decimal,
              quantity: int, entry_price: Decimal, liquidation_bid: Decimal,
              entry_charges: Decimal, conservative_exit_charges: Decimal, full_premium_loss: Decimal) -> Decimal:
    """Linear delta loss plus current spread/slippage and conservative round-trip fees.

    liquidation_bid already includes configured adverse exit slippage. The fee
    caller must reserve sale charges at the entry premium (an upper bound while
    premium falls), not at an invented stop price. Full-premium exposure remains
    separately enforced. Sources are trusted only by the validated adapter caller;
    a raw chain's un-timestamped delta is insufficient.
    """
    if (evidence is None or not evidence.source or evidence.source not in trusted_sources
            or not contract or evidence.contract != contract or option_type not in ("CE", "PE")
            or type(quantity) is not int or quantity <= 0):
        raise StopRiskUnverifiable()
    if (not isinstance(now, datetime) or now.utcoffset() is None
            or not isinstance(evidence.observed_at, datetime) or evidence.observed_at.utcoffset() is None):
        raise StopRiskUnverifiable()
    age = (now - evidence.observed_at).total_seconds()
    delta = evidence.value
    if (not isinstance(delta, Decimal) or not delta.is_finite() or not 0 <= age <= DELTA_MAX_AGE_SECONDS
            or not (Decimal("0") < delta <= 1 if option_type == "CE" else -1 <= delta < 0)):
        raise StopRiskUnverifiable()
    try:
        spot, stop, price, bid, buy_fee, exit_fee, full = map(money, (
            underlying_entry, underlying_stop, entry_price, liquidation_bid,
            entry_charges, conservative_exit_charges, full_premium_loss))
    except ValueError as exc:
        raise StopRiskUnverifiable() from exc
    if (min(spot, stop, price, full) <= 0 or min(bid, buy_fee, exit_fee) < 0 or bid > price
            or (stop >= spot if option_type == "CE" else stop <= spot)
            or full < price * quantity + buy_fee):
        raise StopRiskUnverifiable()
    with localcontext() as ctx:
        ctx.prec = 38
        estimated = (abs(delta) * abs(spot - stop) + price - bid) * quantity + buy_fee + exit_fee
        return min(full, estimated.quantize(Decimal("0.01"), rounding=ROUND_CEILING))


def exposure_rejection(*, full_premium_loss: Decimal, estimated_stop_risk: Decimal, stop_budget: Decimal,
                       gross_daily_loss: Decimal, existing_reservations: Decimal, available_cash: Decimal) -> str | None:
    """Pure monetary gates, after identity, position-count, session and quote gates.

    Existing reservations include unresolved/prior-day holdings. Profits are not
    an input and cannot replenish the gross daily loss budget.
    """
    full, stop, budget, lost, reserved, cash = map(money, (
        full_premium_loss, estimated_stop_risk, stop_budget,
        gross_daily_loss, existing_reservations, available_cash))
    if min(full, stop, budget, lost, reserved, cash) < 0 or full == 0 or budget > Decimal("1000"):
        raise ValueError("invalid risk projection")
    if full > FULL_PREMIUM_LIMIT:
        return "FULL_PREMIUM_LIMIT"
    if stop > budget:
        return "STOP_RISK_LIMIT"
    with localcontext() as context:
        context.prec = 38
        if lost + reserved + full > DAILY_EXPOSURE_LIMIT:
            return "DAILY_EXPOSURE_LIMIT"
    if full > cash:
        return "INSUFFICIENT_CAPITAL"
    return None


@dataclass(frozen=True)
class SizingDecision:
    entry: px.Fill | None
    full_premium_loss: Decimal | None
    estimated_stop_risk: Decimal | None
    reason: str | None


def size(*, quote: ContractQuote | None, evidence: DeltaEvidence | None, trusted_sources: frozenset[str],
         now: datetime, settings: Settings, underlying_entry: Decimal, underlying_stop: Decimal,
         stop_budget: Decimal, gross_daily_loss: Decimal, existing_reservations: Decimal,
         available_cash: Decimal, max_lots: int = MAX_LOTS,
         cost_policy_id: str | None = None,
         allow_historical_brokerage_assumption: bool = False) -> SizingDecision:
    """Largest of 1..min(max_lots, MAX_LOTS) lots satisfying V2 monetary gates; no ledger writes.

    ``max_lots`` is the strategy's own ceiling; it can only lower the policy maximum.
    Caller still enforces identity, eligibility, session, duplicate and position
    gates in the same transaction as entry. No legacy fee fallback is allowed.
    """
    if type(max_lots) is not int or max_lots < 1:
        return SizingDecision(None, None, None, "STRATEGY_LOT_LIMIT_INVALID")
    priced, why = px.executable(quote, now, settings)
    if priced is None:
        return SizingDecision(None, None, None, why)
    day = now.astimezone(IST).date().isoformat()
    try:
        resolved_policy = paper_fees.resolve_policy(
            day,
            policy_id=cost_policy_id,
            allow_historical_brokerage_assumption=allow_historical_brokerage_assumption,
        )
    except paper_fees.FeePolicyUnavailable as exc:
        return SizingDecision(None, None, None, str(exc))
    reason = "FULL_PREMIUM_LIMIT"
    for lots in range(min(max_lots, MAX_LOTS), 0, -1):
        try:
            entry = px.fill(
                priced, "BUY", lots, now=now, settings=settings,
                ticks=SLIPPAGE_TICKS, cost_policy_id=resolved_policy.policy_id,
                allow_historical_brokerage_assumption=allow_historical_brokerage_assumption,
            )
            full = px.max_loss(entry, settings, day)
            exit_cost = Decimal(paper_fees.order_charges(
                price=entry.price, quantity=entry.qty, side="SELL", market_day=day,
                policy_id=resolved_policy.policy_id,
                allow_historical_brokerage_assumption=allow_historical_brokerage_assumption,
            )["total"])
            estimated = stop_risk(
                evidence=evidence, trusted_sources=trusted_sources, contract=priced.tradingsymbol,
                option_type=priced.option_type, now=now, underlying_entry=underlying_entry,
                underlying_stop=underlying_stop, quantity=entry.qty, entry_price=entry.price,
                liquidation_bid=max(Decimal("0"), priced.bid - px.TICK * SLIPPAGE_TICKS),
                entry_charges=entry.charges_inr, conservative_exit_charges=exit_cost, full_premium_loss=full)
        except (StopRiskUnverifiable, paper_fees.FeePolicyUnavailable) as exc:
            return SizingDecision(None, None, None, str(exc))
        reason = exposure_rejection(full_premium_loss=full, estimated_stop_risk=estimated, stop_budget=stop_budget,
                                    gross_daily_loss=gross_daily_loss, existing_reservations=existing_reservations,
                                    available_cash=available_cash)
        if reason is None:
            return SizingDecision(entry, full, estimated, None)
    return SizingDecision(None, None, None, reason)
