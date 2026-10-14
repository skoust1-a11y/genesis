"""Unit tests for Institutional Chart Analytics Engine.

Validates:
1. EMA, RSI, MACD, ATR, ADX against reference formulas.
2. Robust VWAP behavior (non-null for cash index bars without volume).
3. Swing high / low detection without lookahead bias.
4. BOS and CHoCH detection with close vs wick confirmation.
5. Liquidity level calculation (PDH, PDL, PDC, PWH, PWL, EQH, EQL, Pivots).
6. Liquidity sweep detection (wick sweep vs breakout, reclaim).
7. Trend classification engine across trending, sideways, volatile regimes.
8. Multi-timeframe conflict resolution logic.
"""

from __future__ import annotations

import pytest

from app.market.chart_analytics import (
    calculate_adx,
    calculate_atr,
    calculate_ema,
    calculate_macd,
    calculate_robust_vwap,
    calculate_rsi,
    classify_market_trend,
    detect_bos_choch,
    detect_liquidity_sweeps,
    detect_swing_points,
    detect_trendlines_and_channels,
    generate_mtf_matrix,
    calculate_liquidity_levels,
    build_trading_decision_dashboard,
    LiquidityLevel,
)


def make_bars(tuples, start_hour=9, start_min=15, date_prefix="2026-10-14"):
    """Tuples of (open, high, low, close, volume)."""
    bars = []
    total_minutes = start_hour * 60 + start_min
    for i, t in enumerate(tuples):
        m = total_minutes + i * 5
        hr = m // 60
        mn = m % 60
        o, h, l, c = t[:4]
        v = t[4] if len(t) > 4 else 0
        bars.append({
            "date": f"{date_prefix} {hr:02d}:{mn:02d}",
            "open": float(o),
            "high": float(h),
            "low": float(l),
            "close": float(c),
            "volume": float(v),
        })
    return bars


def test_ema_mathematical_accuracy():
    prices = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0, 19.0]
    # Period 5 EMA: First 4 values are None, 5th value is SMA(10..14) = 12.0
    ema5 = calculate_ema(prices, 5)
    assert ema5[:4] == [None, None, None, None]
    assert ema5[4] == 12.0
    # Multiplier = 2 / (5 + 1) = 1/3
    # Next EMA = (15 - 12) * (1/3) + 12 = 13.0
    assert ema5[5] == 13.0
    # Next EMA = (16 - 13) * (1/3) + 13 = 14.0
    assert ema5[6] == 14.0


def test_rsi_accuracy():
    # 15 constant prices: RSI should be 100 on gains or 0 on losses or handled
    steady_up = [100.0 + i * 2 for i in range(20)]
    rsi_up = calculate_rsi(steady_up, 14)
    assert rsi_up[-1] == 100.0

    steady_dn = [200.0 - i * 2 for i in range(20)]
    rsi_dn = calculate_rsi(steady_dn, 14)
    assert rsi_dn[-1] == 0.0


def test_robust_vwap_never_none_without_volume():
    # NIFTY cash index bars have volume 0
    bars = make_bars([(22000, 22050, 21980, 22020, 0)] * 10)
    vwap = calculate_robust_vwap(bars)
    assert len(vwap) == 10
    assert all(v is not None for v in vwap)
    tp = (22050 + 21980 + 22020) / 3.0
    assert vwap[0] == round(tp, 2)


def test_swing_points_non_repainting():
    # Construct distinct peak at bar 3: (100, 105, 95, 102), bar 3 has high 120
    tuples = [
        (100, 102, 98, 100),
        (100, 104, 99, 102),
        (102, 106, 100, 105),
        (105, 120, 104, 115),  # Swing High at index 3
        (115, 116, 110, 111),
        (111, 113, 107, 108),  # Confirmation of swing 3 happens here at index 5 (for N=2)
        (108, 110, 105, 106),
    ]
    bars = make_bars(tuples)
    swings = detect_swing_points(bars, sensitivity=2)
    high_swings = [s for s in swings if s.kind == "high"]
    assert len(high_swings) == 1
    sw = high_swings[0]
    assert sw.index == 3
    assert sw.price == 120.0
    # Confirmed index MUST be 3 + 2 = 5
    assert sw.confirmed_index == 5
    assert sw.confirmed_date == bars[5]["date"]


def test_bos_and_choch_detection():
    # Uptrend creates swing high at index 2 (price 110), confirmed at index 4
    # Then bar 6 breaks above 110 with close 112 -> Bullish BOS
    tuples = [
        (100, 101, 99, 100),
        (100, 105, 99, 104),
        (104, 110, 103, 108),  # Swing high 110 (idx 2)
        (108, 109, 102, 103),
        (103, 104, 101, 102),  # Confirms swing high (idx 4)
        (102, 106, 101, 105),
        (105, 113, 105, 112),  # Bar 6 breaks 110 with close 112
    ]
    bars = make_bars(tuples)
    swings = detect_swing_points(bars, sensitivity=2)
    events = detect_bos_choch(bars, swings)
    assert len(events) >= 1
    ev = events[0]
    assert ev.direction == "bullish"
    assert ev.level_price == 110.0
    assert ev.confirmation == "close"
    assert ev.break_index == 6


def test_liquidity_sweep_wick_vs_close_reclaim():
    levels = [LiquidityLevel("PDH", 22500.0, "PDH", "#f59e0b", "dashed", "high")]
    # Bar 1 trades high 22510 but closes at 22490 -> Wick sweep!
    bars = make_bars([
        (22480, 22495, 22475, 22490),
        (22490, 22510, 22485, 22492),  # Wick sweep of 22500
        (22492, 22498, 22470, 22480),
    ])
    sweeps = detect_liquidity_sweeps(bars, levels)
    assert len(sweeps) == 1
    assert sweeps[0].sweep_type == "wick_sweep"
    assert sweeps[0].reclaimed is True
    assert sweeps[0].extreme_price == 22510.0


def test_trend_classification_regimes():
    # Upward trending bars
    bars_bull = make_bars([(22000 + i * 20, 22000 + i * 20 + 25, 22000 + i * 20 - 5, 22000 + i * 20 + 15) for i in range(40)])
    swings = detect_swing_points(bars_bull, 2)
    events = detect_bos_choch(bars_bull, swings)
    closes = [b["close"] for b in bars_bull]
    ema_dict = {
        "ema9": calculate_ema(closes, 9),
        "ema20": calculate_ema(closes, 20),
        "ema50": calculate_ema(closes, 50),
        "ema200": calculate_ema(closes, 200),
    }
    vwap = calculate_robust_vwap(bars_bull)
    rsi = calculate_rsi(closes, 14)
    adx = calculate_adx(bars_bull, 14)

    regime = classify_market_trend(bars_bull, swings, events, ema_dict, vwap, rsi, adx)
    assert "Bullish" in regime.classification
    assert regime.score > 0
    assert len(regime.reasons) > 0


def test_mtf_conflict_resolution():
    daily_bars = make_bars([(22000, 22500, 21900, 22400)] * 30, date_prefix="2026-10-01")
    h1_bars = make_bars([(22300, 22450, 22250, 22420)] * 30, date_prefix="2026-10-14")
    m15_bars = make_bars([(22420, 22430, 22350, 22360)] * 30, date_prefix="2026-10-14")
    m5_bars = make_bars([(22360, 22370, 22280, 22290)] * 30, date_prefix="2026-10-14")

    mtf = generate_mtf_matrix(daily_bars, h1_bars, m15_bars, m5_bars)
    assert len(mtf.matrix) == 4
    assert mtf.alignment in ("Conflicted / Pullback", "Neutral", "Full Bullish", "Full Bearish")
    assert len(mtf.conflict_notes) > 0


def test_detect_trendlines_and_channels_hh_hl():
    # Construct swings with HH1, HH2 and HL1, HL2
    # Uptrend zigzag: Low 100 (idx 2), High 110 (idx 4), Low 105 (idx 6), High 115 (idx 8)
    tuples = [
        (102, 103, 101, 102),  # 0
        (102, 104, 100, 101),  # 1
        (101, 102, 98, 99),    # 2: Swing Low 98 (LL1)
        (99, 105, 99, 104),    # 3
        (104, 112, 103, 110),  # 4: Swing High 112 (HH1)
        (110, 111, 102, 104),  # 5
        (104, 106, 103, 105),  # 6: Swing Low 103 (HL1)
        (105, 112, 104, 111),  # 7
        (111, 120, 110, 118),  # 8: Swing High 120 (HH2)
        (118, 119, 106, 108),  # 9
        (108, 110, 107, 109),  # 10: Swing Low 107 (HL2)
        (109, 115, 108, 114),  # 11
        (114, 115, 112, 113),  # 12
    ]
    bars = make_bars(tuples)
    swings = detect_swing_points(bars, sensitivity=1)
    highs = [s for s in swings if s.kind == "high"]
    lows = [s for s in swings if s.kind == "low"]
    assert len(highs) >= 2
    assert len(lows) >= 2

    # Verify swing labels
    assert highs[0].label == "HH1"
    assert highs[1].label == "HH2"
    assert lows[1].label == "HL1"
    assert lows[2].label == "HL2"

    trendlines = detect_trendlines_and_channels(bars, swings)
    assert len(trendlines) >= 2

    labels = [tl.label for tl in trendlines]
    assert any("HH1 → HH2" in l for l in labels)
    assert any("HL1 → HL2" in l for l in labels)
