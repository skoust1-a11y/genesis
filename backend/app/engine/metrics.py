"""Performance statistics of one simulated window, from its closed trades and daily mark-to-market curve.

Returns are daily changes in P&L over the fixed capital (no compounding). Ratios that need a loss,
a spread of returns or a drawdown to divide by are None when there is none, never infinite.
"""

from __future__ import annotations

import math
from itertools import pairwise

from app.live.opinion_eval import bootstrap_mean_ci

TRADING_DAYS = 252


def _r(value: float | None, digits: int = 4) -> float | None:
    return None if value is None or not math.isfinite(value) else round(value, digits)


def performance(nets: list[float], curve: list[dict], capital: float, days_in_market: int) -> dict:
    """`nets`: net P&L per closed trade in closing order; `curve`: [{"equity": P&L in INR}] one per day."""
    wins, losses = [n for n in nets if n > 0], [n for n in nets if n < 0]
    gross_profit, gross_loss = sum(wins), -sum(losses)
    streak = longest = 0
    for n in nets:
        streak = streak + 1 if n < 0 else 0
        longest = max(longest, streak)
    expectancy = sum(nets) / len(nets) if nets else None
    ci = bootstrap_mean_ci(nets) if len(nets) >= 2 else None

    equity = [p["equity"] for p in curve]
    returns = [(b - a) / capital for a, b in pairwise([0.0, *equity])] if capital else []
    n = len(returns)
    mean = sum(returns) / n if n else 0.0
    std = math.sqrt(sum((r - mean) ** 2 for r in returns) / (n - 1)) if n > 1 else 0.0
    downside = math.sqrt(sum(min(r, 0.0) ** 2 for r in returns) / n) if n else 0.0
    peak, max_dd = 0.0, 0.0  # from the starting capital, so a loss on day one counts
    for e in equity:
        peak = max(peak, e)
        max_dd = max(max_dd, peak - e)
    annual_pct = mean * TRADING_DAYS * 100
    max_dd_pct = max_dd / capital * 100 if capital else 0.0
    net = sum(nets)
    return {
        "trades": len(nets),
        "gross_profit": _r(gross_profit, 2),
        "gross_loss": _r(gross_loss, 2),
        "profit_factor": _r(gross_profit / gross_loss) if gross_loss else None,
        "expectancy": _r(expectancy, 2),
        "expectancy_ci95": [_r(ci[0], 2), _r(ci[1], 2)] if ci else None,
        "avg_win": _r(gross_profit / len(wins), 2) if wins else None,
        "avg_loss": _r(-gross_loss / len(losses), 2) if losses else None,
        "max_consecutive_losses": longest,
        "return_pct": _r(equity[-1] / capital * 100 if equity and capital else None),
        "annual_return_pct": _r(annual_pct) if n else None,
        "sharpe": _r(mean / std * math.sqrt(TRADING_DAYS)) if std else None,
        "sortino": _r(mean / downside * math.sqrt(TRADING_DAYS)) if downside else None,
        "max_drawdown_pct": _r(max_dd_pct),
        "calmar": _r(annual_pct / max_dd_pct) if max_dd_pct and n else None,
        "recovery_factor": _r(net / max_dd) if max_dd else None,
        "exposure_pct": _r(days_in_market / n * 100) if n else None,
    }
