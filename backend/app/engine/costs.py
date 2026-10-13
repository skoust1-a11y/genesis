from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, fields

from app.config import Settings
from app.strategies.schema import StrategyDocument

TICK = 0.05  # NSE index option tick size, INR


def rate_on(schedule: list[tuple[str, float]], trade_date: str | None, default: float) -> float:
    """The rate in force on `trade_date` from (effective_date, rate) pairs; `default` without a date."""
    if not schedule or not trade_date:
        return default
    rate = default
    for effective, value in schedule:
        if effective <= trade_date:
            rate = value
    return rate


@dataclass
class CostProfile:
    brokerage_per_order: float
    stt_pct: float
    exchange_txn_pct: float
    gst_pct: float
    sebi_per_crore: float
    stamp_duty_pct: float
    slippage_bps: float
    stt_schedule: list[tuple[str, float]] = field(default_factory=list)
    exchange_txn_schedule: list[tuple[str, float]] = field(default_factory=list)
    exercise_stt_pct: float = 0.00125
    slippage_min_ticks: float = 0  # per unit per fill; applies when the fill quantity is known

    def stt_rate(self, trade_date: str | None = None) -> float:
        return rate_on(self.stt_schedule, trade_date, self.stt_pct)

    def exchange_rate(self, trade_date: str | None = None) -> float:
        return rate_on(self.exchange_txn_schedule, trade_date, self.exchange_txn_pct)


@dataclass
class CostBreakdown:
    brokerage: float
    stt: float
    exchange_txn: float
    sebi: float
    stamp_duty: float
    gst: float
    slippage: float

    @property
    def total(self) -> float:
        return (
            self.brokerage
            + self.stt
            + self.exchange_txn
            + self.sebi
            + self.stamp_duty
            + self.gst
            + self.slippage
        )

    def __add__(self, other: CostBreakdown) -> CostBreakdown:
        return CostBreakdown(**{f.name: getattr(self, f.name) + getattr(other, f.name) for f in fields(self)})

    @classmethod
    def zero(cls) -> CostBreakdown:
        return cls(0, 0, 0, 0, 0, 0, 0)


def resolve_cost_profile(settings: Settings, strategy: StrategyDocument) -> CostProfile:
    """Settings, overridden per strategy by frontmatter keys. A flat override disables that rate's schedule."""
    fm = strategy.frontmatter
    return CostProfile(
        brokerage_per_order=float(fm.get("brokerage_per_order", settings.brokerage_per_order)),
        stt_pct=float(fm.get("stt_pct", settings.stt_pct)),
        exchange_txn_pct=float(fm.get("exchange_txn_pct", settings.exchange_txn_pct)),
        gst_pct=float(fm.get("gst_pct", settings.gst_pct)),
        sebi_per_crore=float(fm.get("sebi_per_crore", settings.sebi_per_crore)),
        stamp_duty_pct=float(fm.get("stamp_duty_pct", settings.stamp_duty_pct)),
        slippage_bps=float(fm.get("slippage_bps", settings.slippage_bps)),
        stt_schedule=[] if "stt_pct" in fm else settings.schedule(settings.stt_schedule),
        exchange_txn_schedule=[] if "exchange_txn_pct" in fm else settings.schedule(settings.exchange_txn_schedule),
        exercise_stt_pct=float(fm.get("exercise_stt_pct", settings.exercise_stt_pct)),
        slippage_min_ticks=float(fm.get("slippage_min_ticks", settings.slippage_min_ticks)),
    )


def cost_breakdown(
    notional: float, orders: int, profile: CostProfile, side: str | None = None, trade_date: str | None = None,
    quantity: int | None = None,
) -> CostBreakdown:
    """Charges on `notional` premium turnover.

    STT applies to sells and stamp duty to buys; without a side both apply (the conservative
    estimate used when one figure covers several legs). With `quantity`, slippage is at least
    `slippage_min_ticks` option ticks per unit.
    """
    turnover = abs(notional)
    brokerage = profile.brokerage_per_order * orders
    exchange = turnover * profile.exchange_rate(trade_date)
    sebi = turnover / 1e7 * profile.sebi_per_crore
    return CostBreakdown(
        brokerage=brokerage,
        stt=turnover * profile.stt_rate(trade_date) if side in (None, "SELL") else 0.0,
        exchange_txn=exchange,
        sebi=sebi,
        stamp_duty=turnover * profile.stamp_duty_pct if side in (None, "BUY") else 0.0,
        gst=(brokerage + exchange + sebi) * profile.gst_pct,
        slippage=max(turnover * profile.slippage_bps / 10_000, (quantity or 0) * profile.slippage_min_ticks * TICK),
    )


def leg_costs(fills: Iterable[tuple[str, float, int]], profile: CostProfile, trade_date: str | None = None) -> CostBreakdown:
    """Charges for one order per (side, price, quantity) fill."""
    total = CostBreakdown.zero()
    for side, price, quantity in fills:
        total += cost_breakdown(
            price * quantity, 1, profile, side=side, trade_date=trade_date, quantity=quantity
        )
    return total


def exercise_costs(exercised: Iterable[tuple[float, int]], profile: CostProfile) -> CostBreakdown:
    """STT on the intrinsic value of long in-the-money options exercised at expiry."""
    stt = sum(intrinsic * quantity for intrinsic, quantity in exercised) * profile.exercise_stt_pct
    return CostBreakdown(0, stt, 0, 0, 0, 0, 0)


def apply_trade_costs(notional: float, orders: int, profile: CostProfile) -> float:
    return cost_breakdown(notional, orders, profile).total
