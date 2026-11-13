"""Versioned Decimal accrual for NSE index-option paper orders, not broker settlement.

Each simulated executed order is one accrual unit. STT taxable value and tax round
to rupees; other components round to paisa before summing. Partial exit orders
incur separate brokerage. Expiry/exercise and broker auto-square-off are excluded.
Historical fills must retain their persisted charges, never be repriced here.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP, localcontext

from app.trading.paper_accounting import money

POLICY_ID = "nse-index-option-groww-paper-2026-11-06-v1"
# Exact 2026-11-06 live/paper policy identity preserved unchanged.
VERIFIED_FROM = VERIFIED_THROUGH = "2026-11-06"
RATES = {
    "brokerage_per_order": "20.00",
    "stt_sell": "0.0015",
    "exchange": "0.000355299",
    "ipft": "0.000000001",
    "sebi": "0.000001",
    "stamp_buy": "0.00003",
    "gst": "0.18",
}
SOURCES = (
    "https://nsearchives.nseindia.com/content/circulars/FA73061.pdf",
    "https://nsearchives.nseindia.com/content/circulars/FATAX73524.pdf",
    "https://groww.in/pricing/futures-and-options",
    "https://www.nseindia.com/static/invest/first-time-investor-sebi-turnover-fees-stt-other-levies",
    "https://www.sebi.gov.in/sebi_data/attachdocs/apr-2017/1492087548939.pdf",
)

HISTORICAL_BACKTEST_POLICY_Oct2024 = "nse-index-option-historical-statutory-2024-10-01-to-2026-02-28-v1"
HISTORICAL_BACKTEST_POLICY_Mar2026 = "nse-index-option-historical-statutory-2026-03-01-to-2026-03-31-v1"
HISTORICAL_BACKTEST_POLICY_Apr2026 = "nse-index-option-historical-statutory-2026-04-01-to-2026-11-05-v1"
HISTORICAL_BROKERAGE_PROVENANCE = "SIMULATED_ASSUMPTION_RS20_PER_ORDER_STATUTORY_VERIFIED"


@dataclass(frozen=True)
class FeePolicySpec:
    policy_id: str
    verified_from: str
    verified_through: str
    rates: dict[str, str]
    sources: tuple[str, ...]
    brokerage_provenance: str | None = None

    def snapshot(self) -> dict:
        snap = {
            "policy_id": self.policy_id,
            "verified_from": self.verified_from,
            "verified_through": self.verified_through,
            "rates": dict(self.rates),
            "sources": list(self.sources),
            "rounding": "HALF_UP; STT value and tax rupees; other components paisa",
            "accrual_unit": "one simulated executed order",
            "gst_basis": ["brokerage", "exchange_txn", "ipft", "sebi"],
        }
        if self.brokerage_provenance is not None:
            snap["brokerage_provenance"] = self.brokerage_provenance
        return snap


LIVE_PAPER_POLICY_2026_11_06 = FeePolicySpec(
    policy_id=POLICY_ID,
    verified_from=VERIFIED_FROM,
    verified_through=VERIFIED_THROUGH,
    rates=dict(RATES),
    sources=SOURCES,
    brokerage_provenance=None,  # Exact byte-identical snapshot preserved for 2026-11-06-v1
)

# Historical backtest/replay policies: NSE/SEBI statutory schedules verified against primary circulars;
# Rs 20/order brokerage is explicitly tagged as a simulated flat-brokerage assumption because Groww's
# public pricing page is not a dated historical tariff archive. Pre-2024-10-01 dates fail closed
# because the complete NSE option exchange-transaction + IPFT circular boundary before 2024-10-01 is
# not verified in local primary-source evidence.
HISTORICAL_POLICY_SPECS: tuple[FeePolicySpec, ...] = (
    FeePolicySpec(
        policy_id=HISTORICAL_BACKTEST_POLICY_Oct2024,
        verified_from="2024-10-01",
        verified_through="2026-02-28",
        rates={
            "brokerage_per_order": "20.00",
            "stt_sell": "0.001",
            "exchange": "0.0003503",
            "ipft": "0.000005",
            "sebi": "0.000001",
            "stamp_buy": "0.00003",
            "gst": "0.18",
        },
        sources=(
            "https://nsearchives.nseindia.com/content/circulars/FA73061.pdf",
            "https://nsearchives.nseindia.com/content/circulars/FATAX63809.pdf",
            "https://www.nseindia.com/static/invest/first-time-investor-sebi-turnover-fees-stt-other-levies",
            "https://www.sebi.gov.in/sebi_data/attachdocs/apr-2017/1492087548939.pdf",
            "https://groww.in/pricing/futures-and-options",
        ),
        brokerage_provenance="SIMULATED_ASSUMPTION_RS20_PER_ORDER_STATUTORY_VERIFIED",
    ),
    FeePolicySpec(
        policy_id=HISTORICAL_BACKTEST_POLICY_Mar2026,
        verified_from="2026-03-01",
        verified_through="2026-03-31",
        rates={
            "brokerage_per_order": "20.00",
            "stt_sell": "0.001",
            "exchange": "0.000355299",
            "ipft": "0.000000001",
            "sebi": "0.000001",
            "stamp_buy": "0.00003",
            "gst": "0.18",
        },
        sources=(
            "https://nsearchives.nseindia.com/content/circulars/FA73061.pdf",
            "https://nsearchives.nseindia.com/content/circulars/FATAX63809.pdf",
            "https://www.nseindia.com/static/invest/first-time-investor-sebi-turnover-fees-stt-other-levies",
            "https://www.sebi.gov.in/sebi_data/attachdocs/apr-2017/1492087548939.pdf",
            "https://groww.in/pricing/futures-and-options",
        ),
        brokerage_provenance="SIMULATED_ASSUMPTION_RS20_PER_ORDER_STATUTORY_VERIFIED",
    ),
    FeePolicySpec(
        policy_id=HISTORICAL_BACKTEST_POLICY_Apr2026,
        verified_from="2026-04-01",
        verified_through="2026-11-05",
        rates=dict(RATES),
        sources=SOURCES,
        brokerage_provenance="SIMULATED_ASSUMPTION_RS20_PER_ORDER_STATUTORY_VERIFIED",
    ),
)

ALL_POLICY_SPECS: tuple[FeePolicySpec, ...] = (*HISTORICAL_POLICY_SPECS, LIVE_PAPER_POLICY_2026_11_06)
POLICIES_BY_ID: dict[str, FeePolicySpec] = {spec.policy_id: spec for spec in ALL_POLICY_SPECS}
KNOWN_POLICY_IDS: frozenset[str] = frozenset(POLICIES_BY_ID)


class FeePolicyUnavailable(ValueError):
    """No verified complete fee schedule for this market date or policy ID."""


def is_known_policy_id(policy_id: str | None) -> bool:
    return isinstance(policy_id, str) and policy_id in POLICIES_BY_ID


def resolve_policy(
    market_day: str,
    *,
    policy_id: str | None = None,
    allow_historical_brokerage_assumption: bool = False,
) -> FeePolicySpec:
    """Resolve the immutable FeePolicySpec for `market_day` and optional `policy_id`."""
    if not isinstance(market_day, str) or date.fromisoformat(market_day).isoformat() != market_day:
        raise ValueError("market_day must be an ISO date")
    if policy_id is not None:
        spec = POLICIES_BY_ID.get(policy_id)
        if spec is None:
            raise FeePolicyUnavailable("COST_POLICY_UNKNOWN")
        if not (spec.verified_from <= market_day <= spec.verified_through):
            raise FeePolicyUnavailable("COST_POLICY_UNVERIFIED_DATE")
        if spec.brokerage_provenance is not None and not allow_historical_brokerage_assumption:
            raise FeePolicyUnavailable("COST_POLICY_BROKERAGE_ASSUMPTION_NOT_ENABLED")
        return spec
    if VERIFIED_FROM <= market_day <= VERIFIED_THROUGH:
        return LIVE_PAPER_POLICY_2026_11_06
    for spec in HISTORICAL_POLICY_SPECS:
        if spec.verified_from <= market_day <= spec.verified_through:
            if not allow_historical_brokerage_assumption:
                raise FeePolicyUnavailable("COST_POLICY_UNVERIFIED_DATE")
            return spec
    raise FeePolicyUnavailable("COST_POLICY_UNVERIFIED_DATE")


def order_charges(
    *,
    price: Decimal,
    quantity: int,
    side: str,
    market_day: str,
    policy_id: str | None = None,
    allow_historical_brokerage_assumption: bool = False,
) -> dict:
    """Return JSON-safe exact components and a complete immutable policy snapshot.

    Zero premium is allowed for conservative liquidation-cost estimation; this
    function alone cannot authorize a zero-price fill or an exercise settlement.
    """
    spec = resolve_policy(
        market_day,
        policy_id=policy_id,
        allow_historical_brokerage_assumption=allow_historical_brokerage_assumption,
    )
    if side not in ("BUY", "SELL") or type(quantity) is not int or quantity <= 0:
        raise ValueError("invalid order side or quantity")
    price = money(price)
    if price < 0:
        raise ValueError("negative premium")
    with localcontext() as ctx:
        ctx.prec = 38
        turnover = price * quantity
        rate = {k: Decimal(v) for k, v in spec.rates.items()}
        rounded = lambda value: value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        components = {
            "brokerage": rate["brokerage_per_order"],
            "stt": rounded(
                (
                    turnover.quantize(Decimal("1"), rounding=ROUND_HALF_UP) * rate["stt_sell"]
                ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            )
            if side == "SELL"
            else Decimal("0.00"),
            "exchange_txn": rounded(turnover * rate["exchange"]),
            "ipft": rounded(turnover * rate["ipft"]),
            "sebi": rounded(turnover * rate["sebi"]),
            "stamp_duty": rounded(turnover * rate["stamp_buy"]) if side == "BUY" else Decimal("0.00"),
        }
        components["gst"] = rounded(
            sum(components[k] for k in ("brokerage", "exchange_txn", "ipft", "sebi")) * rate["gst"]
        )
        snapshot = spec.snapshot()
        digest = hashlib.sha256(
            json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return {
            "components": {k: str(v) for k, v in components.items()},
            "total": str(sum(components.values())),
            "turnover": str(turnover),
            "market_day": market_day,
            "side": side,
            "quantity": quantity,
            "policy": snapshot,
            "policy_hash": digest,
        }
