"""Deterministic numerical regressions; all candles and quotes are synthetic."""
import math
from datetime import datetime, timedelta

import pytest

from app.config import get_settings
from app.liquidity_signals import option_bars
from app.trading import option_pnl
from app.trading.option_pnl import black76


@pytest.mark.parametrize("kind,forward,strike", [("CE", 108., 125.), ("PE", 125., 108.)])
@pytest.mark.parametrize("minute,expected", [
    (15, 4.41213051733379e-17),
    (20, 4.311320271428754e-17),
    (25, 4.212686212691063e-17),
])
def test_otm_tail_retains_positive_premium(kind, forward, strike, minute, expected):
    # CI's failing 10:15/20/25 candles; the close is five minutes later.
    years = (datetime(2026, 10, 19, 15, 30) -
             datetime(2026, 10, 14, 10, minute) - timedelta(minutes=5)).total_seconds() / 86400 / 365
    premium = black76(forward, strike, years, .15, kind)
    # Independent 100-digit Decimal normal-CDF series reference values.
    assert premium == pytest.approx(expected, rel=1e-10, abs=0.)


@pytest.mark.parametrize("kind", ["CE", "PE"])
@pytest.mark.parametrize("years,iv", [(0., .15), (-1., .15), (.1, 0.)])
@pytest.mark.parametrize("forward", [80., 100., 120.])
def test_expiry_and_zero_volatility_intrinsic(kind, years, iv, forward):
    expected = max(forward - 100 if kind == "CE" else 100 - forward, 0.)
    assert black76(forward, 100., years, iv, kind) == expected


@pytest.mark.parametrize("kind,forward,strike,expected", [
    ("CE", 100., 100., 7.965567455405804),
    ("PE", 100., 100., 7.965567455405804),
    ("CE", 120., 100., 22.147298810578146),
    ("PE", 100., 120., 22.147298810578146),
])
def test_normal_prices(kind, forward, strike, expected):
    assert black76(forward, strike, 1., .2, kind) == pytest.approx(expected, rel=2e-14)


@pytest.mark.parametrize("kind", ["CE", "PE"])
def test_positive_intrinsic_boundary(kind):
    forward, strike = (120., 100.) if kind == "CE" else (100., 120.)
    assert black76(forward, strike, 1e-12, .15, kind) == 20.


@pytest.mark.parametrize("index", range(4))
@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_nonfinite_inputs_rejected_even_at_expiry(index, value):
    args = [100., 100., 0., .2, "CE"]
    args[index] = value
    with pytest.raises(ValueError, match="finite"):
        black76(*args)


@pytest.mark.parametrize("args", [
    (0., 100., .1, .2, "CE"), (-1., 100., .1, .2, "CE"),
    (100., 0., .1, .2, "PE"), (100., -1., .1, .2, "PE"),
    (100., 100., .1, -.2, "CE"), (100., 100., 0., .2, "call"),
    (100., 100., 1e308, 1e308, "CE"),
])
def test_invalid_inputs_rejected(args):
    with pytest.raises(ValueError):
        black76(*args)


@pytest.mark.parametrize("kind", ["CE", "PE"])
@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, -1e-6])
def test_invalid_calculated_premium_is_not_clamped(monkeypatch, kind, bad):
    values = iter([bad, 0.])
    monkeypatch.setattr(option_pnl, "_cdf", lambda _: next(values))
    with pytest.raises(ValueError, match="intrinsic-value"):
        black76(100., 100., .1, .2, kind)


@pytest.mark.parametrize("kind", ["CE", "PE"])
def test_only_roundoff_below_intrinsic_is_repaired(monkeypatch, kind):
    forward, strike = (120., 100.) if kind == "CE" else (100., 120.)
    values = iter([1. - math.ulp(1.), 1.])
    monkeypatch.setattr(option_pnl, "_cdf", lambda _: next(values))
    assert black76(forward, strike, .1, .2, kind) == 20.


@pytest.mark.parametrize("kind", ["CE", "PE"])
@pytest.mark.parametrize("premium", [-1e-15, 1e-30])
def test_zero_boundary_repairs_roundoff_and_retains_positive_prices(monkeypatch, kind, premium):
    values = iter([premium / 100., 0.])
    monkeypatch.setattr(option_pnl, "_cdf", lambda _: next(values))
    assert black76(100., 100., .1, .2, kind) == max(premium, 0.)


@pytest.mark.parametrize("kind", ["CE", "PE"])
def test_option_ohlc_bounds_without_tolerance(kind):
    from tests.test_liquidity_signals import bars

    plan = {"option": f"125 {kind}", "expiry": "2026-10-19", "iv_pct": 15., "forward_basis": 0.}
    # Original CI fixture plus its reflection about strike for OTM put tails.
    candles = bars()
    if kind == "PE":
        candles = [{"date": b["date"], "open": 250 - b["open"], "close": 250 - b["close"],
                    "low": 250 - b["high"], "high": 250 - b["low"]} for b in candles]
    for b in option_bars(candles, plan, get_settings()):
        assert 0 <= b["low"] <= min(b["open"], b["close"]) <= max(b["open"], b["close"]) <= b["high"]


@pytest.mark.parametrize("kind", ["CE", "PE"])
def test_platform_erf_tail_rounding_cannot_affect_prices(monkeypatch, kind):
    # glibc's rounded tail values made 108 CE / 125 PE negative. Pricing must
    # no longer subtract a rounded erf from one on any platform.
    def refuse(_):
        raise AssertionError("erf loses the normal tail")
    monkeypatch.setattr(math, "erf", refuse)
    forward, strike = (108., 125.) if kind == "CE" else (125., 108.)
    assert 0 < black76(forward, strike, 5. / 365, .15, kind) < 1e-15
