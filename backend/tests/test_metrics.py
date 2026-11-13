"""Hand-calculated checks of the performance statistics and of one fill's charges."""

from __future__ import annotations

import math
import statistics

import pytest

from app.engine.costs import CostProfile, leg_costs
from app.engine.metrics import performance


def test_performance_matches_hand_calculation():
    nets = [100.0, -50.0, -30.0, 200.0]
    curve = [{"equity": e} for e in (100.0, 50.0, 20.0, 220.0)]
    s = performance(nets, curve, capital=1000, days_in_market=3)

    returns = [0.1, -0.05, -0.03, 0.2]  # daily P&L change over capital
    mean = statistics.mean(returns)
    assert (s["gross_profit"], s["gross_loss"], s["profit_factor"]) == (300, 80, 3.75)
    assert (s["expectancy"], s["avg_win"], s["avg_loss"]) == (55, 150, -40)
    assert s["max_consecutive_losses"] == 2
    assert s["sharpe"] == pytest.approx(mean / statistics.stdev(returns) * math.sqrt(252), abs=1e-4)
    assert s["sortino"] == pytest.approx(mean / math.sqrt((0.05**2 + 0.03**2) / 4) * math.sqrt(252), abs=1e-4)
    assert s["max_drawdown_pct"] == 8.0  # peak 100, trough 20, on 1,000 capital
    assert s["calmar"] == pytest.approx(mean * 252 * 100 / 8.0, abs=1e-3)
    assert (s["recovery_factor"], s["return_pct"], s["exposure_pct"]) == (2.75, 22.0, 75.0)
    lo, hi = s["expectancy_ci95"]
    assert lo <= 55 <= hi


def test_ratios_are_none_not_infinite_without_losses_or_spread():
    s = performance([10.0, 10.0], [{"equity": 10.0}, {"equity": 20.0}], capital=1000, days_in_market=2)
    assert s["profit_factor"] is None and s["sortino"] is None and s["calmar"] is None
    assert performance([], [], capital=1000, days_in_market=0)["sharpe"] is None


def test_one_sell_fill_charges_by_hand():
    profile = CostProfile(
        brokerage_per_order=20, stt_pct=0.001, exchange_txn_pct=0.0003503, gst_pct=0.18, sebi_per_crore=10,
        stamp_duty_pct=0.00003, slippage_bps=5, slippage_min_ticks=1,
    )  # fmt: skip
    c = leg_costs([("SELL", 20.0, 150)], profile)  # turnover 3,000
    assert (c.brokerage, c.stt, c.stamp_duty) == (20, pytest.approx(3.0), 0)
    assert c.exchange_txn == pytest.approx(1.0509) and c.sebi == pytest.approx(0.003)
    assert c.gst == pytest.approx(0.18 * (20 + 1.0509 + 0.003))
    assert c.slippage == pytest.approx(7.5)  # one 0.05 tick x 150 units beats 5 bps of 3,000 (1.50)
    assert leg_costs([("BUY", 400.0, 150)], profile).slippage == pytest.approx(30.0)  # 5 bps of 60,000 wins
