"""Offline-only higher-risk experiment; never imported by the paper runtime."""

from decimal import Decimal as D

from app.trading import paper_risk as risk


def projection(
    *,
    full_premium_loss,
    estimated_stop_risk,
    stop_budget,
    gross_daily_loss,
    existing_reservations,
    available_cash,
):
    full, stop, budget, lost, reserved, cash = map(
        D,
        (
            full_premium_loss,
            estimated_stop_risk,
            stop_budget,
            gross_daily_loss,
            existing_reservations,
            available_cash,
        ),
    )
    if (
        any(
            not x.is_finite() or x < 0
            for x in (full, stop, budget, lost, reserved, cash)
        )
        or full == 0
        or budget > 3000
    ):
        raise ValueError("Invalid research risk projection")
    if full > risk.FULL_PREMIUM_LIMIT:
        return "FULL_PREMIUM_LIMIT"
    if stop > budget:
        return "STOP_RISK_LIMIT"
    if lost + reserved + full > risk.DAILY_EXPOSURE_LIMIT:
        return "DAILY_EXPOSURE_LIMIT"
    if full > cash:
        return "INSUFFICIENT_CAPITAL"
    return None


def install():
    risk.MAX_LOTS = 4
    risk.exposure_rejection = projection
