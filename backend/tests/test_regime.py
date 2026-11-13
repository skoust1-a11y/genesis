"""Deterministic, causal unit tests for `backend/app/trading/regime.py` (`regime-v2`)."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest

from app.trading import paper_exec as px
from app.trading.regime import (
    REGIME_VERSION,
    MarketRegimeVector,
    VixObservation,
    classify_market_regime,
    classify_regime,
    classify_regime_v2,
)

DAY = "2026-11-06"


def _make_5m_bars(
    closes: list[float],
    *,
    day: str = DAY,
    start: str = "09:15",
    half_range: float = 8.0,
    volume: float = 1000.0,
) -> pd.DataFrame:
    t0 = datetime.fromisoformat(f"{day}T{start}:00")
    rows = []
    for i, c in enumerate(closes):
        ts = (t0 + timedelta(minutes=5 * i)).strftime("%Y-%m-%d %H:%M")
        o = closes[i - 1] if i > 0 else c
        hi = max(o, c) + half_range
        lo = min(o, c) - half_range
        rows.append(
            {
                "date": ts,
                "open": float(o),
                "high": float(hi),
                "low": float(lo),
                "close": float(c),
                "volume": float(volume),
            }
        )
    return pd.DataFrame(rows)


def test_aliases_and_backward_compatibility_with_paper_exec_regime():
    assert classify_regime is classify_regime_v2
    assert classify_regime is classify_market_regime
    df = _make_5m_bars([24000.0 + i * 5.0 for i in range(16)])
    now = datetime.fromisoformat(f"{DAY}T10:35:00+05:30")
    vec = classify_regime(df, now=now)
    assert isinstance(vec, MarketRegimeVector)
    assert vec.version == REGIME_VERSION
    assert vec.legacy_regime == px.regime(df)
    assert px.regime(df) == "ABOVE_VWAP/RISING"
    assert px.regime_v2(df, now=now) == vec


def test_insufficient_data_under_14_closed_bars():
    df = _make_5m_bars([24000.0 + i * 4.0 for i in range(13)])
    now = datetime.fromisoformat(f"{DAY}T10:20:00+05:30")
    vec = classify_regime(df, now=now)
    assert vec.closed_bars_5m == 13
    assert vec.structure_regime == "INSUFFICIENT_DATA"
    assert vec.vol_regime == "UNKNOWN_VOL"
    assert vec.gap_regime == "INSUFFICIENT_DATA"


def test_causal_filtering_excludes_forming_and_future_bars():
    # 16 bars starting 09:15 (09:15 .. 10:30 starts; 16th bar closes at 10:35)
    base_closes = [24000.0 + i * 10.0 for i in range(16)]
    df_causal = _make_5m_bars(base_closes)
    # Add forming (10:35 start, closes 10:40) and future (10:40 start, closes 10:45) extreme crash bars
    df_with_future = _make_5m_bars(base_closes + [22000.0, 21000.0])
    now = datetime.fromisoformat(f"{DAY}T10:36:00+05:30")
    vec_causal = classify_regime(df_causal, now=now)
    vec_with_future = classify_regime(df_with_future, now=now)
    assert vec_causal.closed_bars_5m == 16
    assert vec_with_future.closed_bars_5m == 16
    assert vec_with_future == vec_causal


def test_trend_up_trend_down_compression_chop_and_range_rotation():
    now = datetime.fromisoformat(f"{DAY}T10:45:00+05:30")
    up_df = _make_5m_bars([24000.0 + i * 12.0 for i in range(18)], half_range=3.0)
    up_vec = classify_regime(up_df, now=now)
    assert up_vec.structure_regime == "TREND_UP"
    assert up_vec.adx_14 is not None and up_vec.adx_14 >= 20.0

    down_df = _make_5m_bars([24200.0 - i * 12.0 for i in range(18)], half_range=3.0)
    down_vec = classify_regime(down_df, now=now)
    assert down_vec.structure_regime == "TREND_DOWN"
    assert down_vec.adx_14 is not None and down_vec.adx_14 >= 20.0

    # Tight alternating oscillation -> high choppiness / narrow 6-bar span -> COMPRESSION_CHOP
    chop_closes = [24000.0 + (2.0 if i % 2 == 0 else -2.0) for i in range(18)]
    chop_df = _make_5m_bars(chop_closes, half_range=2.0)
    chop_vec = classify_regime(chop_df, now=now)
    assert chop_vec.structure_regime == "COMPRESSION_CHOP"

    # Moderate drift with wide bars so choppiness <= 61.8, ADX >= 20, but close near VWAP (< 0.3% from VWAP)
    rot_closes = [24000.0 + i * 3.5 for i in range(14)] + [24045.0, 24035.0, 24030.0, 24026.0]
    rot_df = _make_5m_bars(rot_closes, half_range=4.0)
    rot_vec = classify_regime(rot_df, now=now)
    assert rot_vec.structure_regime == "RANGE_ROTATION"


def test_volatility_regime_bands_and_staleness_guard():
    now = datetime.fromisoformat(f"{DAY}T10:35:00+05:30")
    df = _make_5m_bars([24000.0 + i * 5.0 for i in range(16)])
    assert classify_regime(df, now=now, vix_quote=VixObservation(11.5, now)).vol_regime == "LOW_VOL"
    assert classify_regime(df, now=now, vix_quote=VixObservation(15.0, now)).vol_regime == "NORMAL_VOL"
    assert classify_regime(df, now=now, vix_quote=VixObservation(19.5, now)).vol_regime == "HIGH_VOL"
    assert classify_regime(df, now=now, vix_quote=VixObservation(24.0, now)).vol_regime == "EXTREME_VOL"
    # Stale (> 60s), future, non-positive, or naive VIX observations fail closed to UNKNOWN_VOL
    assert (
        classify_regime(df, now=now, vix_quote=VixObservation(15.0, now - timedelta(seconds=61))).vol_regime
        == "UNKNOWN_VOL"
    )
    assert (
        classify_regime(df, now=now, vix_quote=VixObservation(15.0, now + timedelta(seconds=1))).vol_regime
        == "UNKNOWN_VOL"
    )
    assert classify_regime(df, now=now, vix_quote=VixObservation(0.0, now)).vol_regime == "UNKNOWN_VOL"
    assert (
        classify_regime(df, now=now, vix_quote=VixObservation(15.0, now.replace(tzinfo=None))).vol_regime
        == "UNKNOWN_VOL"
    )
    # Bare scalar float/Decimal, missing observed_at, or daily/EOD VIX sources fail closed to UNKNOWN_VOL
    assert classify_regime(df, now=now, vix_quote=15.0).vol_regime == "UNKNOWN_VOL"
    assert classify_regime(df, now=now, vix_quote=VixObservation(15.0, None)).vol_regime == "UNKNOWN_VOL"
    assert classify_regime(df, now=now, vix_quote=VixObservation(15.0, now, source="1d")).vol_regime == "UNKNOWN_VOL"
    assert (
        classify_regime(
            df, now=now, vix_quote={"value": 15.0, "observed_at": now.isoformat(), "timeframe": "1d"}
        ).vol_regime
        == "UNKNOWN_VOL"
    )
    assert (
        classify_regime(
            df, now=now, vix_quote={"value": 15.0, "observed_at": now.isoformat(), "source": "intraday_quote"}
        ).vol_regime
        == "NORMAL_VOL"
    )


def test_gap_regime_and_or30_availability_boundaries():
    df = _make_5m_bars([24100.0 + i * 2.0 for i in range(16)])
    before_or30 = datetime.fromisoformat(f"{DAY}T09:44:00+05:30")
    at_or30 = datetime.fromisoformat(f"{DAY}T09:45:00+05:30")

    v_before = classify_regime(df, now=before_or30, levels=SimpleNamespace(pdc=24000.0))
    assert v_before.gap_regime == "GAP_UP_LARGE"
    assert v_before.or30_ready is False and v_before.or30_high is None and v_before.or30_low is None

    v_after = classify_regime(df, now=at_or30, levels=SimpleNamespace(pdc=24000.0))
    assert v_after.or30_ready is True
    assert v_after.or30_high is not None and v_after.or30_low is not None

    assert classify_regime(df, now=at_or30, levels={"pdc": 24050.0}).gap_regime == "GAP_UP_MODERATE"
    assert classify_regime(df, now=at_or30, levels={"pdc": 24090.0}).gap_regime == "FLAT_OPEN"
    assert classify_regime(df, now=at_or30, levels={"pdc": 24150.0}).gap_regime == "GAP_DOWN_MODERATE"
    assert classify_regime(df, now=at_or30, levels={"pdc": 24250.0}).gap_regime == "GAP_DOWN_LARGE"
    assert classify_regime(df, now=at_or30, levels=None).gap_regime == "INSUFFICIENT_DATA"


@pytest.mark.parametrize(
    "hhmm,expected",
    [
        ("09:10", "OUTSIDE_SESSION"),
        ("09:20", "OPENING_DISCOVERY"),
        ("10:15", "MORNING_PRIME"),
        ("12:00", "MIDDAY_LULL"),
        ("14:00", "AFTERNOON_TREND"),
        ("15:15", "CLOSE_ONLY_WINDOW"),
        ("15:35", "OUTSIDE_SESSION"),
    ],
)
def test_session_phase_boundaries(hhmm: str, expected: str):
    now = datetime.fromisoformat(f"{DAY}T{hhmm}:00+05:30")
    vec = classify_regime(None, now=now)
    assert vec.session_phase == expected


def test_malformed_or_naive_inputs_fail_closed():
    now = datetime.fromisoformat(f"{DAY}T10:35:00+05:30")
    with pytest.raises(ValueError, match="timezone-aware"):
        classify_regime(None, now=datetime(2026, 11, 6, 10, 35))
    bad_df = _make_5m_bars([24000.0 + i for i in range(16)])
    bad_df.loc[3, "high"] = 100.0  # inconsistent OHLC (high < low)
    vec = classify_regime(bad_df, now=now)
    assert vec.structure_regime == "INSUFFICIENT_DATA" and vec.closed_bars_5m == 0
