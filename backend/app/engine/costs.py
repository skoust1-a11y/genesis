from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings
from app.strategies.schema import StrategyDocument


@dataclass
class CostProfile:
    brokerage_per_order: float
    stt_pct: float
    exchange_txn_pct: float
    gst_pct: float
    sebi_per_crore: float
    stamp_duty_pct: float
    slippage_bps: float


def resolve_cost_profile(settings: Settings, strategy: StrategyDocument) -> CostProfile:
    fm = strategy.frontmatter
    return CostProfile(
        brokerage_per_order=float(fm.get("brokerage_per_order", settings.brokerage_per_order)),
        stt_pct=float(fm.get("stt_pct", settings.stt_pct)),
        exchange_txn_pct=float(fm.get("exchange_txn_pct", settings.exchange_txn_pct)),
        gst_pct=float(fm.get("gst_pct", settings.gst_pct)),
        sebi_per_crore=float(fm.get("sebi_per_crore", settings.sebi_per_crore)),
        stamp_duty_pct=float(fm.get("stamp_duty_pct", settings.stamp_duty_pct)),
        slippage_bps=float(fm.get("slippage_bps", settings.slippage_bps)),
    )


def apply_trade_costs(notional: float, orders: int, profile: CostProfile) -> float:
    turnover = abs(notional)
    brokerage = profile.brokerage_per_order * orders
    stt = turnover * profile.stt_pct
    exchange = turnover * profile.exchange_txn_pct
    sebi = turnover / 1e7 * profile.sebi_per_crore
    stamp = turnover * profile.stamp_duty_pct
    gst = (brokerage + exchange + sebi) * profile.gst_pct
    slippage = turnover * (profile.slippage_bps / 10_000)
    return brokerage + stt + exchange + sebi + stamp + gst + slippage
