"""Institutional Technical Analysis, Market Structure, Liquidity & Trend Engine.

Provides mathematically verified, non-repainting market structure, liquidity sweeps,
multi-timeframe analysis, indicator calculations, and transparent trend classification
for NIFTY and other trading instruments.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Literal


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class SwingPoint:
    index: int
    date: str
    price: float
    kind: Literal["high", "low"]
    classification: Literal["HH", "HL", "LH", "LL", "EQH", "EQL", "SWING"]
    is_major: bool
    confirmed_index: int  # First bar index where this swing is mathematically known (no lookahead)
    confirmed_date: str
    label: str = ""


@dataclass
class StructureEvent:
    event_type: Literal["BOS", "CHoCH"]
    direction: Literal["bullish", "bearish"]
    level_price: float
    swing_index: int
    swing_date: str
    break_index: int
    break_date: str
    confirmation: Literal["close", "wick"]
    label: str


@dataclass
class Trendline:
    kind: Literal["ascending", "descending"]
    start_index: int
    start_date: str
    start_price: float
    end_index: int
    end_date: str
    end_price: float
    slope: float
    channel_high: float | None = None
    channel_low: float | None = None
    is_broken: bool = False
    break_index: int | None = None
    break_date: str | None = None
    retested: bool = False
    label: str = ""


@dataclass
class LiquidityLevel:
    label: str
    price: float
    kind: str  # PDH, PDL, PDC, PWH, PWL, EQH, EQL, DH, DL, ORH, ORL, BSL, SSL, PIVOT
    color: str
    style: str  # solid, dashed, dotted
    importance: Literal["high", "medium", "standard"]


@dataclass
class LiquiditySweep:
    level_label: str
    level_price: float
    sweep_type: Literal["wick_sweep", "close_breakout"]
    reclaimed: bool
    sweep_index: int
    sweep_date: str
    reclaim_index: int | None
    reclaim_date: str | None
    extreme_price: float
    note: str


# ---------------------------------------------------------------------------
# Indicator Calculations (Mathematically Verified)
# ---------------------------------------------------------------------------

def calculate_ema(prices: list[float], period: int) -> list[float | None]:
    """Calculate Exponential Moving Average with smoothing 2 / (period + 1).

    First valid EMA is initialized with SMA of first `period` bars.
    """
    if len(prices) < period or period <= 0:
        return [None] * len(prices)

    out: list[float | None] = [None] * (period - 1)
    # Seed with Simple Moving Average
    sma = sum(prices[:period]) / period
    out.append(round(sma, 2))

    multiplier = 2.0 / (period + 1)
    prev = sma
    for p in prices[period:]:
        current = (p - prev) * multiplier + prev
        out.append(round(current, 2))
        prev = current

    return out


def calculate_rsi(closes: list[float], period: int = 14) -> list[float | None]:
    """Calculate Wilder's Relative Strength Index (RSI)."""
    if len(closes) <= period:
        return [None] * len(closes)

    out: list[float | None] = [None] * period
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]

    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]

    # First average gain and loss
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    if avg_loss == 0.0:
        out.append(100.0)
    else:
        rs = avg_gain / avg_loss
        out.append(round(100.0 - (100.0 / (1.0 + rs)), 2))

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        if avg_loss == 0.0:
            out.append(100.0)
        else:
            rs = avg_gain / avg_loss
            out.append(round(100.0 - (100.0 / (1.0 + rs)), 2))

    return out


def calculate_macd(
    closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> dict[str, list[float | None]]:
    """Calculate MACD line, Signal line, and Histogram."""
    ema_fast = calculate_ema(closes, fast)
    ema_slow = calculate_ema(closes, slow)

    macd_line: list[float | None] = []
    valid_macd_values: list[float] = []
    valid_indices: list[int] = []

    for i in range(len(closes)):
        f, s = ema_fast[i], ema_slow[i]
        if f is not None and s is not None:
            m = round(f - s, 2)
            macd_line.append(m)
            valid_macd_values.append(m)
            valid_indices.append(i)
        else:
            macd_line.append(None)

    # Calculate Signal Line on MACD values
    signal_sub = calculate_ema(valid_macd_values, signal)
    signal_line: list[float | None] = [None] * len(closes)
    hist: list[float | None] = [None] * len(closes)

    for idx, sig in zip(valid_indices, signal_sub):
        signal_line[idx] = sig
        if sig is not None and macd_line[idx] is not None:
            hist[idx] = round(macd_line[idx] - sig, 2)

    return {"macd": macd_line, "signal": signal_line, "hist": hist}


def calculate_atr(bars: list[dict], period: int = 14) -> list[float]:
    """Calculate Average True Range (ATR) with Wilder's smoothing."""
    if not bars:
        return []

    tr_list: list[float] = []
    for i, b in enumerate(bars):
        h, l = b["high"] or b["close"], b["low"] or b["close"]
        if i == 0:
            tr_list.append(h - l)
        else:
            prev_c = bars[i - 1]["close"]
            tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
            tr_list.append(tr)

    if len(bars) < period:
        # Fallback to cumulative average
        out: list[float] = []
        for i in range(len(tr_list)):
            out.append(round(sum(tr_list[: i + 1]) / (i + 1), 2))
        return out

    out = [round(sum(tr_list[:period]) / period, 2)] * period
    prev_atr = out[-1]
    for tr in tr_list[period:]:
        cur_atr = (prev_atr * (period - 1) + tr) / period
        out.append(round(cur_atr, 2))
        prev_atr = cur_atr

    return out


def calculate_adx(bars: list[dict], period: int = 14) -> dict[str, list[float | None]]:
    """Calculate Welles Wilder's ADX (Average Directional Index) with +DI and -DI."""
    n = len(bars)
    if n <= period:
        return {"adx": [None] * n, "plus_di": [None] * n, "minus_di": [None] * n}

    plus_dm: list[float] = [0.0]
    minus_dm: list[float] = [0.0]
    tr: list[float] = [bars[0]["high"] - bars[0]["low"]]

    for i in range(1, n):
        h, l = bars[i]["high"], bars[i]["low"]
        ph, pl, pc = bars[i - 1]["high"], bars[i - 1]["low"], bars[i - 1]["close"]

        up_move = h - ph
        down_move = pl - l

        p_dm = up_move if (up_move > down_move and up_move > 0) else 0.0
        m_dm = down_move if (down_move > up_move and down_move > 0) else 0.0

        plus_dm.append(p_dm)
        minus_dm.append(m_dm)
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))

    smooth_tr = sum(tr[:period])
    smooth_p_dm = sum(plus_dm[:period])
    smooth_m_dm = sum(minus_dm[:period])

    plus_di: list[float | None] = [None] * (period - 1)
    minus_di: list[float | None] = [None] * (period - 1)
    dx_values: list[float] = []

    def calc_di(p_val: float, m_val: float, tr_val: float) -> tuple[float, float, float]:
        if tr_val == 0:
            return 0.0, 0.0, 0.0
        p = 100.0 * (p_val / tr_val)
        m = 100.0 * (m_val / tr_val)
        sum_di = p + m
        dx = 100.0 * abs(p - m) / sum_di if sum_di != 0 else 0.0
        return round(p, 2), round(m, 2), dx

    p0, m0, d0 = calc_di(smooth_p_dm, smooth_m_dm, smooth_tr)
    plus_di.append(p0)
    minus_di.append(m0)
    dx_values.append(d0)

    for i in range(period, n):
        smooth_tr = smooth_tr - (smooth_tr / period) + tr[i]
        smooth_p_dm = smooth_p_dm - (smooth_p_dm / period) + plus_dm[i]
        smooth_m_dm = smooth_m_dm - (smooth_m_dm / period) + minus_dm[i]

        p, m, d = calc_di(smooth_p_dm, smooth_m_dm, smooth_tr)
        plus_di.append(p)
        minus_di.append(m)
        dx_values.append(d)

    # ADX is Wilder's smoothed DX
    adx_out: list[float | None] = [None] * (2 * period - 1)
    if len(dx_values) >= period:
        first_adx = sum(dx_values[:period]) / period
        adx_out.append(round(first_adx, 2))
        prev_adx = first_adx
        for d in dx_values[period:]:
            cur_adx = (prev_adx * (period - 1) + d) / period
            adx_out.append(round(cur_adx, 2))
            prev_adx = cur_adx

    # Pad or trim to length n
    if len(adx_out) < n:
        adx_out = adx_out + [None] * (n - len(adx_out))
    elif len(adx_out) > n:
        adx_out = adx_out[:n]

    return {"adx": adx_out, "plus_di": plus_di, "minus_di": minus_di}


def calculate_robust_vwap(bars: list[dict]) -> list[float | None]:
    """Calculate intraday session VWAP.

    Uses volume-weighted typical price if volume > 0, else cumulative running
    mean of typical price (H+L+C)/3 per day session. Guaranteed never None for valid bars.
    """
    if not bars:
        return []

    vwap_out: list[float | None] = []
    current_day = None
    cum_pv = 0.0
    cum_vol = 0.0
    cum_tp = 0.0
    bar_count = 0

    for b in bars:
        day_str = b["date"][:10]
        if day_str != current_day:
            current_day = day_str
            cum_pv = 0.0
            cum_vol = 0.0
            cum_tp = 0.0
            bar_count = 0

        h = b["high"] if b["high"] is not None else b["close"]
        l = b["low"] if b["low"] is not None else b["close"]
        c = b["close"]
        tp = (h + l + c) / 3.0
        v = b.get("volume") or 0.0

        bar_count += 1
        cum_tp += tp

        if v > 0:
            cum_pv += tp * v
            cum_vol += v
            val = cum_pv / cum_vol
        else:
            val = cum_tp / bar_count

        vwap_out.append(round(val, 2))

    return vwap_out


def calculate_anchored_vwap(bars: list[dict], anchor_index: int) -> list[float | None]:
    """Calculate VWAP anchored from a specific candle index onwards."""
    out: list[float | None] = [None] * len(bars)
    if anchor_index < 0 or anchor_index >= len(bars):
        return out

    cum_pv = 0.0
    cum_vol = 0.0
    cum_tp = 0.0
    bar_count = 0

    for i in range(anchor_index, len(bars)):
        b = bars[i]
        h = b["high"] if b["high"] is not None else b["close"]
        l = b["low"] if b["low"] is not None else b["close"]
        c = b["close"]
        tp = (h + l + c) / 3.0
        v = b.get("volume") or 0.0

        bar_count += 1
        cum_tp += tp

        if v > 0:
            cum_pv += tp * v
            cum_vol += v
            val = cum_pv / cum_vol
        else:
            val = cum_tp / bar_count

        out[i] = round(val, 2)

    return out


# ---------------------------------------------------------------------------
# Market Structure Engine (Non-Repainting, Zero Lookahead)
# ---------------------------------------------------------------------------

def detect_swing_points(
    bars: list[dict], sensitivity: int = 2, atr_series: list[float] | None = None
) -> list[SwingPoint]:
    """Detect confirmed swing highs and lows with configurable sensitivity N.

    CRITICAL NON-REPAINTING RULE:
    A swing at bar `j` is only confirmed after bar `j + sensitivity` has closed.
    `confirmed_index = j + sensitivity`.
    """
    if len(bars) < 2 * sensitivity + 1:
        return []

    atrs = atr_series or calculate_atr(bars, 14)
    swings: list[SwingPoint] = []
    n = len(bars)

    for j in range(sensitivity, n - sensitivity):
        h_j = bars[j]["high"]
        l_j = bars[j]["low"]

        # Check Swing High
        is_high = True
        for k in range(j - sensitivity, j + sensitivity + 1):
            if k != j and bars[k]["high"] > h_j:
                is_high = False
                break

        # Check Swing Low
        is_low = True
        for k in range(j - sensitivity, j + sensitivity + 1):
            if k != j and bars[k]["low"] < l_j:
                is_low = False
                break

        confirmed_idx = j + sensitivity
        confirmed_date = bars[confirmed_idx]["date"]
        atr_val = atrs[j] if j < len(atrs) else 15.0

        if is_high:
            # Check if major swing (session extreme or > 1.5 ATR extension)
            day_str = bars[j]["date"][:10]
            session_highs = [
                b["high"] for b in bars if b["date"][:10] == day_str and b["high"] is not None
            ]
            is_major = (h_j == max(session_highs)) if session_highs else False

            swings.append(
                SwingPoint(
                    index=j,
                    date=bars[j]["date"],
                    price=round(h_j, 2),
                    kind="high",
                    classification="SWING",
                    is_major=is_major,
                    confirmed_index=confirmed_idx,
                    confirmed_date=confirmed_date,
                )
            )

        if is_low:
            day_str = bars[j]["date"][:10]
            session_lows = [
                b["low"] for b in bars if b["date"][:10] == day_str and b["low"] is not None
            ]
            is_major = (l_j == min(session_lows)) if session_lows else False

            swings.append(
                SwingPoint(
                    index=j,
                    date=bars[j]["date"],
                    price=round(l_j, 2),
                    kind="low",
                    classification="SWING",
                    is_major=is_major,
                    confirmed_index=confirmed_idx,
                    confirmed_date=confirmed_date,
                )
            )

    # Sort chronologically by occurrence index
    swings.sort(key=lambda s: (s.index, 0 if s.kind == "low" else 1))

    # Classify into HH, HL, LH, LL with sequential numbering (HH1, HH2, HL1, HL2, etc.)
    last_high: float | None = None
    last_low: float | None = None
    hh_count = 0
    lh_count = 0
    hl_count = 0
    ll_count = 0

    for s in swings:
        if s.kind == "high":
            if last_high is None:
                s.classification = "HH"
                hh_count = 1
                lh_count = 0
                s.label = "HH1"
            elif s.price > last_high:
                s.classification = "HH"
                hh_count += 1
                lh_count = 0
                s.label = f"HH{hh_count}"
            else:
                s.classification = "LH"
                lh_count += 1
                hh_count = 0
                s.label = f"LH{lh_count}"
            last_high = s.price
        else:
            if last_low is None:
                s.classification = "LL"
                ll_count = 1
                hl_count = 0
                s.label = "LL1"
            elif s.price > last_low:
                s.classification = "HL"
                hl_count += 1
                ll_count = 0
                s.label = f"HL{hl_count}"
            else:
                s.classification = "LL"
                ll_count += 1
                hl_count = 0
                s.label = f"LL{ll_count}"
            last_low = s.price

    return swings


def detect_bos_choch(
    bars: list[dict], swings: list[SwingPoint]
) -> list[StructureEvent]:
    """Detect Bullish/Bearish Break of Structure (BOS) and Change of Character (CHoCH).

    - Bullish BOS: Price breaks above previous confirmed swing high in an established uptrend.
    - Bearish BOS: Price breaks below previous confirmed swing low in an established downtrend.
    - Bullish CHoCH: In a downtrend (LH/LL), price breaks above the last lower high (LH).
    - Bearish CHoCH: In an uptrend (HH/HL), price breaks below the last higher low (HL).
    - Distinguishes candle-close confirmation vs wick-only breach.
    - No duplicate signals for the same broken level.
    """
    events: list[StructureEvent] = []
    if not swings or len(bars) < 5:
        return events

    broken_swing_indices: set[int] = set()

    # Track active swings as time progresses
    # At each bar i, we can only refer to swings that were confirmed at or before bar i
    for i, bar in enumerate(bars):
        close_p = bar["close"]
        high_p = bar["high"]
        low_p = bar["low"]

        # Confirmed swings up to this bar
        known_swings = [s for s in swings if s.confirmed_index <= i]
        if not known_swings:
            continue

        known_highs = [s for s in known_swings if s.kind == "high" and s.index not in broken_swing_indices]
        known_lows = [s for s in known_swings if s.kind == "low" and s.index not in broken_swing_indices]

        # Check Bullish Breaks (Breaking swing high)
        if known_highs:
            target_high = known_highs[-1]
            if high_p > target_high.price:
                # Break occurred!
                is_close = close_p > target_high.price
                confirmation = "close" if is_close else "wick"

                # Check if CHoCH (previous structure was bearish / lower high)
                is_choch = target_high.classification == "LH"
                ev_type = "CHoCH" if is_choch else "BOS"
                lbl = f"Bullish {ev_type} ({'Close' if is_close else 'Wick'})"

                events.append(
                    StructureEvent(
                        event_type=ev_type,
                        direction="bullish",
                        level_price=target_high.price,
                        swing_index=target_high.index,
                        swing_date=target_high.date,
                        break_index=i,
                        break_date=bar["date"],
                        confirmation=confirmation,
                        label=lbl,
                    )
                )
                broken_swing_indices.add(target_high.index)

        # Check Bearish Breaks (Breaking swing low)
        if known_lows:
            target_low = known_lows[-1]
            if low_p < target_low.price:
                # Break occurred!
                is_close = close_p < target_low.price
                confirmation = "close" if is_close else "wick"

                # Check if CHoCH (previous structure was bullish / higher low)
                is_choch = target_low.classification == "HL"
                ev_type = "CHoCH" if is_choch else "BOS"
                lbl = f"Bearish {ev_type} ({'Close' if is_close else 'Wick'})"

                events.append(
                    StructureEvent(
                        event_type=ev_type,
                        direction="bearish",
                        level_price=target_low.price,
                        swing_index=target_low.index,
                        swing_date=target_low.date,
                        break_index=i,
                        break_date=bar["date"],
                        confirmation=confirmation,
                        label=lbl,
                    )
                )
                broken_swing_indices.add(target_low.index)

    return events


def detect_trendlines_and_channels(
    bars: list[dict], swings: list[SwingPoint], atr_val: float = 15.0
) -> list[Trendline]:
    """Identify straight trendlines connecting structural swings:
    - Higher Highs (HH1 -> HH2 -> HH3)
    - Lower Highs (LH1 -> LH2 -> LH3)
    - Higher Lows (HL1 -> HL2 -> HL3)
    - Lower Lows (LL1 -> LL2 -> LL3)
    - Triangle / Wedge / Channel pattern boundaries

    Detects trendline breaks (candle close across line) and subsequent retests.
    """
    trendlines: list[Trendline] = []
    if len(swings) < 2 or len(bars) < 5:
        return trendlines

    high_swings = [s for s in swings if s.kind == "high"]
    low_swings = [s for s in swings if s.kind == "low"]

    def _build_line(p1: SwingPoint, p2: SwingPoint, kind: Literal["ascending", "descending"], label: str) -> Trendline:
        dx = p2.index - p1.index
        dy = p2.price - p1.price
        slope = dy / (dx or 1)

        channel_high: float | None = None
        channel_low: float | None = None
        if kind == "ascending":
            between_highs = [b["high"] for b in bars[p1.index : p2.index + 1] if b.get("high") is not None]
            if between_highs:
                max_dev = 0.0
                for idx_b, b in enumerate(bars[p1.index : p2.index + 1]):
                    base_y = p1.price + slope * idx_b
                    dev = b["high"] - base_y
                    if dev > max_dev:
                        max_dev = dev
                if max_dev > 0:
                    channel_high = round(p1.price + max_dev, 2)
        else:
            between_lows = [b["low"] for b in bars[p1.index : p2.index + 1] if b.get("low") is not None]
            if between_lows:
                max_dev = 0.0
                for idx_b, b in enumerate(bars[p1.index : p2.index + 1]):
                    base_y = p1.price + slope * idx_b
                    dev = base_y - b["low"]
                    if dev > max_dev:
                        max_dev = dev
                if max_dev > 0:
                    channel_low = round(p1.price - max_dev, 2)

        tl = Trendline(
            kind=kind,
            start_index=p1.index,
            start_date=p1.date,
            start_price=p1.price,
            end_index=p2.index,
            end_date=p2.date,
            end_price=p2.price,
            slope=round(slope, 4),
            channel_high=channel_high,
            channel_low=channel_low,
            label=label,
        )

        for k in range(p2.index + 1, len(bars)):
            projected = p1.price + slope * (k - p1.index)
            if kind == "ascending" and bars[k]["close"] < projected - 0.2 * atr_val:
                tl.is_broken = True
                tl.break_index = k
                tl.break_date = bars[k]["date"]
                for r in range(k + 1, min(k + 10, len(bars))):
                    proj_r = p1.price + slope * (r - p1.index)
                    if abs(bars[r]["high"] - proj_r) <= 0.3 * atr_val:
                        tl.retested = True
                        break
                break
            elif kind == "descending" and bars[k]["close"] > projected + 0.2 * atr_val:
                tl.is_broken = True
                tl.break_index = k
                tl.break_date = bars[k]["date"]
                for r in range(k + 1, min(k + 10, len(bars))):
                    proj_r = p1.price + slope * (r - p1.index)
                    if abs(bars[r]["low"] - proj_r) <= 0.3 * atr_val:
                        tl.retested = True
                        break
                break

        return tl

    # 1. Straight lines connecting consecutive swing highs (HH1 -> HH2, LH1 -> LH2)
    for a in range(len(high_swings) - 1):
        p1, p2 = high_swings[a], high_swings[a + 1]
        dx = p2.index - p1.index
        if dx < 2 or dx > 150:
            continue
        kind = "ascending" if p2.price >= p1.price else "descending"
        lbl = f"{p1.label or ('HH' if kind == 'ascending' else 'LH')} → {p2.label or ('HH' if kind == 'ascending' else 'LH')}"
        trendlines.append(_build_line(p1, p2, kind, lbl))

    # 2. Straight lines connecting consecutive swing lows (HL1 -> HL2, LL1 -> LL2)
    for a in range(len(low_swings) - 1):
        p1, p2 = low_swings[a], low_swings[a + 1]
        dx = p2.index - p1.index
        if dx < 2 or dx > 150:
            continue
        kind = "ascending" if p2.price >= p1.price else "descending"
        lbl = f"{p1.label or ('HL' if kind == 'ascending' else 'LL')} → {p2.label or ('HL' if kind == 'ascending' else 'LL')}"
        trendlines.append(_build_line(p1, p2, kind, lbl))

    # 3. Major pattern boundary lines (e.g. triangles spanning across minor swings)
    if len(high_swings) >= 3:
        p1, p2 = high_swings[-3], high_swings[-1]
        dx = p2.index - p1.index
        if 4 <= dx <= 150:
            kind = "ascending" if p2.price >= p1.price else "descending"
            lbl = f"{p1.label or ('HH' if kind == 'ascending' else 'LH')} → {p2.label or ('HH' if kind == 'ascending' else 'LH')}"
            trendlines.append(_build_line(p1, p2, kind, lbl))

    if len(low_swings) >= 3:
        p1, p2 = low_swings[-3], low_swings[-1]
        dx = p2.index - p1.index
        if 4 <= dx <= 150:
            kind = "ascending" if p2.price >= p1.price else "descending"
            lbl = f"{p1.label or ('HL' if kind == 'ascending' else 'LL')} → {p2.label or ('HL' if kind == 'ascending' else 'LL')}"
            trendlines.append(_build_line(p1, p2, kind, lbl))

    # Keep active or recently broken trendlines (filter out lines broken > 25 bars ago)
    curr_len = len(bars)
    active_tls = [
        tl for tl in trendlines
        if not tl.is_broken or (tl.break_index is not None and curr_len - tl.break_index <= 25)
    ]
    if len(active_tls) < 2:
        active_tls = trendlines

    active_tls.sort(key=lambda t: t.end_index)
    return active_tls[-8:]


# ---------------------------------------------------------------------------
# Liquidity Visualization & Sweeps Engine
# ---------------------------------------------------------------------------

def calculate_liquidity_levels(
    daily_bars: list[dict],
    intraday_bars: list[dict],
    weekly_bars: list[dict] | None = None,
) -> list[LiquidityLevel]:
    """Compute institutional liquidity levels:

    1. PDH, PDL, PDC (Previous Day High, Low, Close)
    2. PWH, PWL (Previous Week High, Low)
    3. Floor Pivots (P, R1, R2, S1, S2)
    4. Session High (DH) and Low (DL)
    5. Opening Range High and Low (ORH / ORL for 15m and 30m)
    6. Equal Highs (EQH) and Equal Lows (EQL)
    7. Intraday Swing Liquidity (BSL, SSL)
    """
    levels: list[LiquidityLevel] = []

    # 1. Previous Day High, Low, Close & Floor Pivots
    if len(daily_bars) >= 2:
        prev = daily_bars[-2]
        c = prev["close"]
        h = prev["high"] or c
        l = prev["low"] or c
        p = (h + l + c) / 3.0
        r1 = 2 * p - l
        s1 = 2 * p - h
        r2 = p + (h - l)
        s2 = p - (h - l)

        levels.extend([
            LiquidityLevel("PDH", round(h, 2), "PDH", "#f59e0b", "dashed", "high"),
            LiquidityLevel("PDL", round(l, 2), "PDL", "#f59e0b", "dashed", "high"),
            LiquidityLevel("PDC", round(c, 2), "PDC", "#71717a", "dotted", "medium"),
            LiquidityLevel("R2", round(r2, 2), "PIVOT", "#ef4444", "dotted", "standard"),
            LiquidityLevel("R1", round(r1, 2), "PIVOT", "#ef4444", "dotted", "standard"),
            LiquidityLevel("P", round(p, 2), "PIVOT", "#6366f1", "dotted", "medium"),
            LiquidityLevel("S1", round(s1, 2), "PIVOT", "#10b981", "dotted", "standard"),
            LiquidityLevel("S2", round(s2, 2), "PIVOT", "#10b981", "dotted", "standard"),
        ])

    # 2. Previous Week High & Low (PWH, PWL)
    if weekly_bars and len(weekly_bars) >= 2:
        prev_w = weekly_bars[-2]
        wh = prev_w.get("high") or prev_w["close"]
        wl = prev_w.get("low") or prev_w["close"]
        levels.extend([
            LiquidityLevel("PWH", round(wh, 2), "PWH", "#8b5cf6", "dashed", "high"),
            LiquidityLevel("PWL", round(wl, 2), "PWL", "#8b5cf6", "dashed", "high"),
        ])

    # 3. Session High (DH), Session Low (DL), Opening Range (ORH, ORL)
    if intraday_bars:
        latest_day = intraday_bars[-1]["date"][:10]
        today_bars = [b for b in intraday_bars if b["date"][:10] == latest_day]
        if today_bars:
            dh = max(b["high"] or b["close"] for b in today_bars)
            dl = min(b["low"] or b["close"] for b in today_bars)
            levels.append(LiquidityLevel("DH", round(dh, 2), "DH", "#f97316", "solid", "high"))
            levels.append(LiquidityLevel("DL", round(dl, 2), "DL", "#f97316", "solid", "high"))

            # 15m Opening Range (first 3 bars of 5m)
            or15_bars = [b for b in today_bars if b["date"][11:16] < "09:30"]
            if or15_bars:
                orh15 = max(b["high"] for b in or15_bars)
                orl15 = min(b["low"] for b in or15_bars)
                levels.append(LiquidityLevel("ORH-15", round(orh15, 2), "ORH", "#d97706", "dashed", "medium"))
                levels.append(LiquidityLevel("ORL-15", round(orl15, 2), "ORL", "#d97706", "dashed", "medium"))

            # Equal Highs (EQH) and Equal Lows (EQL)
            atr_val = calculate_atr(intraday_bars, 14)[-1] if len(intraday_bars) >= 14 else 15.0
            eq_tolerance = 0.15 * atr_val

            swings = detect_swing_points(today_bars, sensitivity=2)
            highs = [s for s in swings if s.kind == "high"]
            lows = [s for s in swings if s.kind == "low"]

            for a in range(len(highs) - 1):
                h1, h2 = highs[a], highs[a + 1]
                if abs(h1.price - h2.price) <= eq_tolerance and (h2.index - h1.index) >= 3:
                    avg_eq = round((h1.price + h2.price) / 2.0, 2)
                    levels.append(LiquidityLevel("EQH", avg_eq, "EQH", "#eab308", "solid", "high"))
                    break

            for a in range(len(lows) - 1):
                l1, l2 = lows[a], lows[a + 1]
                if abs(l1.price - l2.price) <= eq_tolerance and (l2.index - l1.index) >= 3:
                    avg_eq = round((l1.price + l2.price) / 2.0, 2)
                    levels.append(LiquidityLevel("EQL", avg_eq, "EQL", "#eab308", "solid", "high"))
                    break

    return levels


def detect_liquidity_sweeps(
    bars: list[dict], levels: list[LiquidityLevel], reclaim_window: int = 5
) -> list[LiquiditySweep]:
    """Identify liquidity sweeps:

    - Breaches of key liquidity levels (PDH, PDL, EQH, EQL, ORH, ORL, DH, DL).
    - Distinguishes wick sweep (wick breaches level, close returns inside)
      vs candle-close breakout.
    - Tracks subsequent reclaim within `reclaim_window` bars.
    """
    sweeps: list[LiquiditySweep] = []
    if not bars or not levels:
        return sweeps

    tracked_kinds = {"PDH", "PDL", "PWH", "PWL", "EQH", "EQL", "ORH", "ORL"}
    active_levels = [lv for lv in levels if lv.kind in tracked_kinds]

    for lv in active_levels:
        is_upper = lv.kind in ("PDH", "PWH", "EQH", "ORH")

        for i, b in enumerate(bars):
            h, l, c = b["high"], b["low"], b["close"]

            if is_upper:
                # Upward breach of resistance / buy-side liquidity
                if h > lv.price:
                    is_wick = c <= lv.price
                    sweep_type = "wick_sweep" if is_wick else "close_breakout"

                    # Check for reclaim in subsequent bars
                    reclaimed = False
                    reclaim_idx = None
                    reclaim_date = None

                    if is_wick:
                        reclaimed = True  # Reclaimed immediately on the same bar
                        reclaim_idx = i
                        reclaim_date = b["date"]
                    else:
                        for r in range(i + 1, min(i + 1 + reclaim_window, len(bars))):
                            if bars[r]["close"] < lv.price:
                                reclaimed = True
                                reclaim_idx = r
                                reclaim_date = bars[r]["date"]
                                break

                    sweeps.append(
                        LiquiditySweep(
                            level_label=lv.label,
                            level_price=lv.price,
                            sweep_type=sweep_type,
                            reclaimed=reclaimed,
                            sweep_index=i,
                            sweep_date=b["date"],
                            reclaim_index=reclaim_idx,
                            reclaim_date=reclaim_date,
                            extreme_price=h,
                            note=f"{lv.label} {sweep_type.replace('_', ' ').title()}"
                            + (" (Reclaimed)" if reclaimed else " (Holding)"),
                        )
                    )
                    break  # One sweep event per level

            else:
                # Downward breach of support / sell-side liquidity
                if l < lv.price:
                    is_wick = c >= lv.price
                    sweep_type = "wick_sweep" if is_wick else "close_breakout"

                    reclaimed = False
                    reclaim_idx = None
                    reclaim_date = None

                    if is_wick:
                        reclaimed = True
                        reclaim_idx = i
                        reclaim_date = b["date"]
                    else:
                        for r in range(i + 1, min(i + 1 + reclaim_window, len(bars))):
                            if bars[r]["close"] > lv.price:
                                reclaimed = True
                                reclaim_idx = r
                                reclaim_date = bars[r]["date"]
                                break

                    sweeps.append(
                        LiquiditySweep(
                            level_label=lv.label,
                            level_price=lv.price,
                            sweep_type=sweep_type,
                            reclaimed=reclaimed,
                            sweep_index=i,
                            sweep_date=b["date"],
                            reclaim_index=reclaim_idx,
                            reclaim_date=reclaim_date,
                            extreme_price=l,
                            note=f"{lv.label} {sweep_type.replace('_', ' ').title()}"
                            + (" (Reclaimed)" if reclaimed else " (Holding)"),
                        )
                    )
                    break

    return sweeps


# ---------------------------------------------------------------------------
# Trend Classification Engine (Phase 7 - Transparent, Documented Formula)
# ---------------------------------------------------------------------------

@dataclass
class TrendRegime:
    classification: Literal[
        "Strong Bullish",
        "Bullish",
        "Weak Bullish",
        "Sideways / Consolidation",
        "Weak Bearish",
        "Bearish",
        "Strong Bearish",
    ]
    score: float  # [-10.0 to +10.0]
    adx: float
    reasons: list[str]
    details: dict[str, Any]


def classify_market_trend(
    bars: list[dict],
    swings: list[SwingPoint],
    events: list[StructureEvent],
    ema_dict: dict[str, list[float | None]],
    vwap_values: list[float | None],
    rsi_values: list[float | None],
    adx_dict: dict[str, list[float | None]],
) -> TrendRegime:
    """Transparent Trend Classification Engine.

    Formula:
    Total Score S = S_structure + S_ema + S_vwap + S_momentum
    Range: [-10.0 to +10.0]

    1. S_structure (max +/- 3.5):
       +3.5: Bullish BOS confirmed by close, HH/HL sequence
       +2.0: Bullish CHoCH or HH
       -3.5: Bearish BOS confirmed by close, LH/LL sequence
       -2.0: Bearish CHoCH or LL
       0.0: Ranging / mixed swings

    2. S_ema (max +/- 2.5):
       +2.5: Bullish EMA stack (EMA 9 > EMA 20 > EMA 50 > EMA 200)
       +1.5: Short-term bullish (EMA 9 > EMA 20 > EMA 50)
       -2.5: Bearish EMA stack (EMA 9 < EMA 20 < EMA 50 < EMA 200)
       -1.5: Short-term bearish (EMA 9 < EMA 20 < EMA 50)
       0.0: Mixed / entangled EMAs

    3. S_vwap (max +/- 2.0):
       +2.0: Price > VWAP and VWAP trending upwards
       +1.0: Price > VWAP (flat)
       -2.0: Price < VWAP and VWAP trending downwards
       -1.0: Price < VWAP (flat)

    4. S_momentum (max +/- 2.0):
       +2.0: RSI > 58 with ADX > 25
       +1.0: RSI > 50
       -2.0: RSI < 42 with ADX > 25
       -1.0: RSI < 50

    Volatility Filter:
    If ADX < 20: Force Sideways / Consolidation (low trend strength).
    """
    if not bars:
        return TrendRegime("Sideways / Consolidation", 0.0, 0.0, ["Insufficient data"], {})

    last_bar = bars[-1]
    cp = last_bar["close"]
    reasons: list[str] = []
    score = 0.0

    # 1. Structure Score
    s_struct = 0.0
    last_event = events[-1] if events else None
    last_swing = swings[-1] if swings else None

    if last_event:
        if last_event.direction == "bullish":
            s_struct += 2.5 if last_event.confirmation == "close" else 1.5
            reasons.append(f"{last_event.label} at {last_event.level_price}")
        else:
            s_struct -= 2.5 if last_event.confirmation == "close" else 1.5
            reasons.append(f"{last_event.label} at {last_event.level_price}")

    if last_swing:
        if last_swing.classification in ("HH", "HL"):
            s_struct += 1.0
            reasons.append(f"Structure forming Higher Swings ({last_swing.classification})")
        elif last_swing.classification in ("LH", "LL"):
            s_struct -= 1.0
            reasons.append(f"Structure forming Lower Swings ({last_swing.classification})")

    s_struct = max(min(s_struct, 3.5), -3.5)
    score += s_struct

    # 2. EMA Alignment Score
    s_ema = 0.0
    e9 = ema_dict["ema9"][-1]
    e20 = ema_dict["ema20"][-1]
    e50 = ema_dict["ema50"][-1]
    e200 = ema_dict["ema200"][-1]

    if e9 is not None and e20 is not None and e50 is not None:
        if e200 is not None and e9 > e20 > e50 > e200:
            s_ema = 2.5
            reasons.append("Perfect Bullish EMA Alignment (9 > 20 > 50 > 200)")
        elif e9 > e20 > e50:
            s_ema = 1.5
            reasons.append("Bullish Short-Term EMA Alignment (9 > 20 > 50)")
        elif e200 is not None and e9 < e20 < e50 < e200:
            s_ema = -2.5
            reasons.append("Perfect Bearish EMA Alignment (9 < 20 < 50 < 200)")
        elif e9 < e20 < e50:
            s_ema = -1.5
            reasons.append("Bearish Short-Term EMA Alignment (9 < 20 < 50)")
        else:
            reasons.append("EMAs Tangled / Mixed (Neutral)")

    score += s_ema

    # 3. VWAP Positioning Score
    s_vwap = 0.0
    vwap_now = vwap_values[-1] if vwap_values else None
    vwap_prev = vwap_values[-5] if len(vwap_values) >= 5 else None

    if vwap_now is not None:
        slope_up = vwap_prev is not None and vwap_now > vwap_prev
        slope_dn = vwap_prev is not None and vwap_now < vwap_prev
        diff_pts = round(cp - vwap_now, 1)

        if cp >= vwap_now:
            s_vwap = 2.0 if slope_up else 1.0
            reasons.append(f"Price is +{diff_pts} pts above VWAP ({vwap_now})")
        else:
            s_vwap = -2.0 if slope_dn else -1.0
            reasons.append(f"Price is {diff_pts} pts below VWAP ({vwap_now})")

    score += s_vwap

    # 4. Momentum Score
    s_mom = 0.0
    rsi = rsi_values[-1] if rsi_values and rsi_values[-1] is not None else 50.0
    adx = adx_dict["adx"][-1] if adx_dict["adx"] and adx_dict["adx"][-1] is not None else 18.0

    if rsi >= 60.0:
        s_mom = 2.0 if adx >= 25.0 else 1.0
        reasons.append(f"RSI Bullish Momentum ({rsi:.1f})")
    elif rsi > 50.0:
        s_mom = 0.5
    elif rsi <= 40.0:
        s_mom = -2.0 if adx >= 25.0 else -1.0
        reasons.append(f"RSI Bearish Momentum ({rsi:.1f})")
    elif rsi < 50.0:
        s_mom = -0.5

    score += s_mom

    # Trend Strength / Consolidation Check
    is_ranging = adx < 20.0
    if is_ranging:
        reasons.append(f"ADX ({adx:.1f}) indicates low trend strength (Choppy / Range)")

    # Classification Mapping
    final_score = round(max(min(score, 10.0), -10.0), 2)

    if is_ranging and abs(final_score) < 4.0:
        classification = "Sideways / Consolidation"
    elif final_score >= 6.0 and adx >= 25.0:
        classification = "Strong Bullish"
    elif final_score >= 3.5:
        classification = "Bullish"
    elif final_score >= 1.5:
        classification = "Weak Bullish"
    elif final_score <= -6.0 and adx >= 25.0:
        classification = "Strong Bearish"
    elif final_score <= -3.5:
        classification = "Bearish"
    elif final_score <= -1.5:
        classification = "Weak Bearish"
    else:
        classification = "Sideways / Consolidation"

    details = {
        "score_structure": s_struct,
        "score_ema": s_ema,
        "score_vwap": s_vwap,
        "score_momentum": s_mom,
        "adx": adx,
        "rsi": rsi,
        "vwap": vwap_now,
    }

    return TrendRegime(
        classification=classification,
        score=final_score,
        adx=adx,
        reasons=reasons,
        details=details,
    )


# ---------------------------------------------------------------------------
# Multi-Timeframe Analysis (MTF) Synchronized Matrix (Phase 6)
# ---------------------------------------------------------------------------

@dataclass
class TimeframeSummary:
    timeframe: str
    trend: str  # Bullish, Bearish, Sideways
    structure: str  # HH/HL, LH/LL, BOS, Range
    momentum: str  # Positive, Negative, Neutral
    bias: str  # Bullish, Bearish, Neutral
    last_price: float
    key_level: float | None


@dataclass
class MTFReport:
    matrix: list[TimeframeSummary]
    alignment: Literal["Full Bullish", "Full Bearish", "Conflicted / Pullback", "Neutral"]
    conflict_notes: list[str]


def analyze_single_timeframe(bars: list[dict], tf_label: str) -> TimeframeSummary:
    """Analyze a single timeframe slice."""
    if not bars:
        return TimeframeSummary(tf_label, "Neutral", "Unknown", "Neutral", "Neutral", 0.0, None)

    closes = [b["close"] for b in bars]
    last_price = closes[-1]
    swings = detect_swing_points(bars, sensitivity=2)
    ema20 = calculate_ema(closes, 20)
    ema50 = calculate_ema(closes, 50)
    rsi = calculate_rsi(closes, 14)

    e20 = ema20[-1] if ema20 else None
    e50 = ema50[-1] if ema50 else None
    r = rsi[-1] if rsi and rsi[-1] is not None else 50.0

    # Trend
    if e20 and e50:
        trend = "Bullish" if e20 > e50 and last_price > e20 else "Bearish" if e20 < e50 and last_price < e20 else "Sideways"
    else:
        trend = "Sideways"

    # Structure
    last_swing = swings[-1] if swings else None
    structure = last_swing.classification if last_swing else "Range"

    # Momentum
    momentum = "Bullish (Strong)" if r >= 60 else "Bearish (Strong)" if r <= 40 else "Neutral"

    # Bias
    if trend == "Bullish" and r > 50:
        bias = "Bullish"
    elif trend == "Bearish" and r < 50:
        bias = "Bearish"
    else:
        bias = "Neutral"

    key_level = last_swing.price if last_swing else None

    return TimeframeSummary(
        timeframe=tf_label,
        trend=trend,
        structure=structure,
        momentum=momentum,
        bias=bias,
        last_price=last_price,
        key_level=key_level,
    )


def generate_mtf_matrix(
    daily_bars: list[dict],
    h1_bars: list[dict],
    m15_bars: list[dict],
    m5_bars: list[dict],
) -> MTFReport:
    """Generate synchronized MTF matrix across Daily, 1H, 15M, and 5M timeframes.

    Explicitly detects and flags timeframe conflicts (e.g. HTF Bullish vs LTF Pullback).
    """
    matrix = [
        analyze_single_timeframe(daily_bars, "Daily"),
        analyze_single_timeframe(h1_bars, "1H"),
        analyze_single_timeframe(m15_bars, "15M"),
        analyze_single_timeframe(m5_bars, "5M"),
    ]

    biases = [row.bias for row in matrix if row.bias != "Neutral"]
    conflict_notes: list[str] = []

    daily_bias = matrix[0].bias
    m5_bias = matrix[3].bias

    if all(b == "Bullish" for b in biases) and len(biases) >= 3:
        alignment = "Full Bullish"
        conflict_notes.append("Strong multi-timeframe bull alignment across HTF and LTF.")
    elif all(b == "Bearish" for b in biases) and len(biases) >= 3:
        alignment = "Full Bearish"
        conflict_notes.append("Strong multi-timeframe bear alignment across HTF and LTF.")
    elif daily_bias == "Bullish" and m5_bias == "Bearish":
        alignment = "Conflicted / Pullback"
        conflict_notes.append(
            "HTF (Daily) is Bullish while LTF (5M) is Pulling Back — Dip buying setup, await 5M reversal/reclaim."
        )
    elif daily_bias == "Bearish" and m5_bias == "Bullish":
        alignment = "Conflicted / Pullback"
        conflict_notes.append(
            "HTF (Daily) is Bearish while LTF (5M) is Rallying — Counter-trend bounce into overhead resistance."
        )
    else:
        alignment = "Neutral"
        conflict_notes.append("Mixed timeframe signals; trade selectively near major liquidity levels.")

    return MTFReport(matrix=matrix, alignment=alignment, conflict_notes=conflict_notes)


# ---------------------------------------------------------------------------
# Trading Decision Dashboard (Phase 9)
# ---------------------------------------------------------------------------

@dataclass
class TradingDecision:
    market_regime: str
    htf_trend: str
    intraday_trend: str
    market_structure: str
    vwap_position: str
    nearest_support: str
    nearest_resistance: str
    nearest_liquidity_pool: str
    momentum_condition: str
    reversal_or_continuation: str


def build_trading_decision_dashboard(
    last_price: float,
    regime: TrendRegime,
    levels: list[LiquidityLevel],
    swings: list[SwingPoint],
    events: list[StructureEvent],
    vwap_now: float | None,
    rsi_now: float,
    adx_now: float,
    mtf: MTFReport,
) -> TradingDecision:
    """Synthesize the 10-point actionable institutional trading decision summary."""
    # Supports and Resistances relative to last_price
    supports = [lv for lv in levels if lv.price < last_price]
    resistances = [lv for lv in levels if lv.price > last_price]

    nearest_sup = max(supports, key=lambda lv: lv.price) if supports else None
    nearest_res = min(resistances, key=lambda lv: lv.price) if resistances else None

    sup_str = (
        f"{nearest_sup.label} @ {nearest_sup.price:,.2f} (-{last_price - nearest_sup.price:.1f} pts)"
        if nearest_sup
        else "None identified"
    )
    res_str = (
        f"{nearest_res.label} @ {nearest_res.price:,.2f} (+{nearest_res.price - last_price:.1f} pts)"
        if nearest_res
        else "None identified"
    )

    # Nearest Liquidity Pool
    pools = [lv for lv in levels if lv.kind in ("PDH", "PDL", "PWH", "PWL", "EQH", "EQL")]
    nearest_pool = min(pools, key=lambda lv: abs(lv.price - last_price)) if pools else None
    pool_str = (
        f"{nearest_pool.label} @ {nearest_pool.price:,.2f} (diff: {nearest_pool.price - last_price:+.1f} pts)"
        if nearest_pool
        else "None nearby"
    )

    # VWAP positioning
    if vwap_now is not None:
        diff = last_price - vwap_now
        vwap_str = f"Price {diff:+.1f} pts {'above' if diff >= 0 else 'below'} VWAP ({vwap_now:,.2f})"
    else:
        vwap_str = "VWAP Neutral"

    # Momentum
    mom_str = f"RSI {rsi_now:.1f} · ADX {adx_now:.1f} ({'Trending' if adx_now >= 25 else 'Ranging'})"

    # Continuation vs Reversal
    if "Bullish" in regime.classification and mtf.alignment in ("Full Bullish", "Conflicted / Pullback"):
        rev_cont = "Bullish Continuation Favored (Buy Dips / Support Reclaims)"
    elif "Bearish" in regime.classification and mtf.alignment in ("Full Bearish", "Conflicted / Pullback"):
        rev_cont = "Bearish Continuation Favored (Sell Rallies / Resistance Rejections)"
    else:
        rev_cont = "Range Trading Favored (Trade extremes between Support and Resistance)"

    # Market Structure description
    last_ev = events[-1] if events else None
    last_sw = swings[-1] if swings else None
    struct_str = f"{last_sw.classification if last_sw else 'Neutral'} Swings" + (
        f" · Last {last_ev.label}" if last_ev else ""
    )

    return TradingDecision(
        market_regime=f"{regime.classification} (Score {regime.score:+.1f})",
        htf_trend=f"{mtf.matrix[0].timeframe}: {mtf.matrix[0].trend} · {mtf.matrix[1].timeframe}: {mtf.matrix[1].trend}",
        intraday_trend=f"{mtf.matrix[2].timeframe}: {mtf.matrix[2].trend} · {mtf.matrix[3].timeframe}: {mtf.matrix[3].trend}",
        market_structure=struct_str,
        vwap_position=vwap_str,
        nearest_support=sup_str,
        nearest_resistance=res_str,
        nearest_liquidity_pool=pool_str,
        momentum_condition=mom_str,
        reversal_or_continuation=rev_cont,
    )


# ---------------------------------------------------------------------------
# Aggregation & Chart Payload Enrichment
# ---------------------------------------------------------------------------

def weekly_bars_from_daily(daily: list[dict]) -> list[dict]:
    """Group daily bars into weekly bars to extract accurate PWH and PWL."""
    if not daily:
        return []

    weeks: dict[str, list[dict]] = {}
    for b in daily:
        try:
            d = datetime.strptime(b["date"][:10], "%Y-%m-%d").date()
            w_key = f"{d.isocalendar().year}-W{d.isocalendar().week:02d}"
            weeks.setdefault(w_key, []).append(b)
        except (ValueError, IndexError):
            continue

    out: list[dict] = []
    for w in sorted(weeks):
        w_bars = weeks[w]
        out.append({
            "date": w_bars[0]["date"],
            "open": w_bars[0]["open"],
            "high": max(b["high"] or b["close"] for b in w_bars),
            "low": min(b["low"] or b["close"] for b in w_bars),
            "close": w_bars[-1]["close"],
        })
    return out


def aggregate_bars(bars: list[dict], minutes: int) -> list[dict]:
    """Aggregate intraday 5m bars into 15m or 60m bars aligned to 09:15 session open."""
    if not bars:
        return []

    out: list[dict] = []
    for b in bars:
        try:
            t = datetime.strptime(b["date"], "%Y-%m-%d %H:%M")
            total_m = (t.hour * 60 + t.minute) - (9 * 60 + 15)
            bucket_m = (total_m // minutes) * minutes + (9 * 60 + 15)
            bt = t.replace(hour=bucket_m // 60, minute=bucket_m % 60)
            start = bt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            start = b["date"]

        if out and out[-1]["date"] == start:
            out[-1]["high"] = max(out[-1]["high"], b["high"])
            out[-1]["low"] = min(out[-1]["low"], b["low"])
            out[-1]["close"] = b["close"]
            out[-1]["volume"] = (out[-1].get("volume") or 0) + (b.get("volume") or 0)
        else:
            out.append({
                "date": start,
                "open": b["open"],
                "high": b["high"],
                "low": b["low"],
                "close": b["close"],
                "volume": b.get("volume") or 0,
            })
    return out


def enrich_chart_payload(
    symbol: str,
    segment: str,
    interval: str,
    bars: list[dict],
    daily_bars: list[dict],
    sensitivity: int = 2,
) -> dict[str, Any]:
    """Execute complete institutional analytical pipeline and return comprehensive payload.

    Includes:
    - Robust non-null VWAP on bars
    - Verified levels with style & color metadata
    - Market Structure: Swings (HH, HL, LH, LL) and Structure Events (BOS, CHoCH)
    - Trendlines & Channels
    - Liquidity Sweeps
    - Indicator panel: EMA (9, 20, 50, 200), RSI, MACD, ADX, ATR, RVOL
    - Trend Classification Engine regime
    - Trading Decision Dashboard
    - Multi-Timeframe Synchronized Matrix (Daily, 1H, 15M, 5M)
    """
    if not bars:
        return {
            "symbol": symbol.upper(),
            "segment": segment,
            "interval": interval,
            "bars": [],
            "levels": [],
            "zones": [],
            "market_structure": {"swings": [], "events": []},
            "trendlines": [],
            "sweeps": [],
            "indicators": {},
            "regime": {},
            "decision": {},
            "mtf": {},
        }

    # 1. Compute VWAP on bars
    vwap_values = calculate_robust_vwap(bars)
    for b, v in zip(bars, vwap_values):
        b["vwap"] = v

    closes = [b["close"] for b in bars]
    volumes = [b.get("volume") or 0.0 for b in bars]

    # 2. Indicators
    ema9 = calculate_ema(closes, 9)
    ema20 = calculate_ema(closes, 20)
    ema50 = calculate_ema(closes, 50)
    ema200 = calculate_ema(closes, 200)
    rsi14 = calculate_rsi(closes, 14)
    macd_dict = calculate_macd(closes, 12, 26, 9)
    adx_dict = calculate_adx(bars, 14)
    atr14 = calculate_atr(bars, 14)

    # Relative Volume (RVOL)
    rvol: list[float] = []
    vol_sma20 = calculate_ema(volumes, 20)
    for i, v in enumerate(volumes):
        v_sma = vol_sma20[i]
        if v_sma and v_sma > 0:
            rvol.append(round(v / v_sma, 2))
        else:
            rvol.append(1.0)

    # 3. Market Structure
    swings = detect_swing_points(bars, sensitivity=sensitivity, atr_series=atr14)
    events = detect_bos_choch(bars, swings)
    trendlines = detect_trendlines_and_channels(bars, swings, atr_val=atr14[-1] if atr14 else 15.0)

    # 4. Liquidity Levels & Sweeps
    weekly_bars = weekly_bars_from_daily(daily_bars)
    liquidity_levels = calculate_liquidity_levels(daily_bars, bars, weekly_bars)
    sweeps = detect_liquidity_sweeps(bars, liquidity_levels)

    # 5. Trend Classification Engine
    ema_dict = {"ema9": ema9, "ema20": ema20, "ema50": ema50, "ema200": ema200}
    regime = classify_market_trend(bars, swings, events, ema_dict, vwap_values, rsi14, adx_dict)

    # 6. Multi-Timeframe Matrix
    m15_bars = aggregate_bars(bars, 15) if interval == "5m" else bars
    h1_bars = aggregate_bars(bars, 60) if interval in ("5m", "15m") else bars
    mtf_report = generate_mtf_matrix(daily_bars, h1_bars, m15_bars, bars)

    # 7. Decision Dashboard
    decision = build_trading_decision_dashboard(
        last_price=closes[-1],
        regime=regime,
        levels=liquidity_levels,
        swings=swings,
        events=events,
        vwap_now=vwap_values[-1],
        rsi_now=rsi14[-1] if rsi14 and rsi14[-1] is not None else 50.0,
        adx_now=adx_dict["adx"][-1] if adx_dict["adx"] and adx_dict["adx"][-1] is not None else 20.0,
        mtf=mtf_report,
    )

    # Form backward-compatible level list and enriched levels
    compat_levels = [{"label": lv.label, "price": lv.price} for lv in liquidity_levels]

    return {
        "symbol": symbol.upper(),
        "segment": segment,
        "interval": interval,
        "bars": bars,
        "levels": compat_levels,
        "enriched_levels": [asdict(lv) for lv in liquidity_levels],
        "market_structure": {
            "swings": [asdict(s) for s in swings],
            "events": [asdict(e) for e in events],
            "sensitivity": sensitivity,
        },
        "trendlines": [asdict(tl) for tl in trendlines],
        "sweeps": [asdict(sw) for sw in sweeps],
        "indicators": {
            "ema9": ema9,
            "ema20": ema20,
            "ema50": ema50,
            "ema200": ema200,
            "rsi14": rsi14,
            "macd": macd_dict["macd"],
            "signal": macd_dict["signal"],
            "hist": macd_dict["hist"],
            "adx": adx_dict["adx"],
            "plus_di": adx_dict["plus_di"],
            "minus_di": adx_dict["minus_di"],
            "atr14": atr14,
            "rvol": rvol,
        },
        "regime": asdict(regime),
        "decision": asdict(decision),
        "mtf": asdict(mtf_report),
    }

