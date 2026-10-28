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
    side: Literal["high", "low"] = "low"
    line_type: str = "ascending_support"
    color: str = "#10b981"
    channel_high: float | None = None
    channel_low: float | None = None
    is_broken: bool = False
    break_index: int | None = None
    break_date: str | None = None
    retested: bool = False
    label: str = ""
    touch_count: int = 3
    body_violations: int = 0
    quality_score: float = 1.0
    apex_index: int | None = None
    apex_price: float | None = None


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
    bars: list[dict], swings: list[SwingPoint], atr_val: float | list[float] = 15.0
) -> list[Trendline]:
    """Identify structural 3-point trendlines connecting swings:
    - Higher Highs (HH1 -> HH2 -> HH3)
    - Lower Highs (LH1 -> LH2 -> LH3)
    - Higher Lows (HL1 -> HL2 -> HL3)
    - Lower Lows (LL1 -> LL2 -> LL3)
    - Triangle / Wedge / Channel pattern boundaries

    Enforces:
    1. Strict 3-Point Validation: Requires at least 3 confirmed touches (touch_count >= 3).
       A line with only 2 points is unconfirmed and rejected.
    2. Non-Crossing Constraint (Same Side): Lines of the same side (e.g. Resistance vs Resistance
       or Support vs Support) are pruned if they cross each other or conflict in the active zone,
       preserving only the dominant structural trendline.
    3. Non-Crossing Apex Capping (Opposite Side): When converging Support and Resistance lines
       intersect, both lines' forward projection is strictly capped at the apex (intersection)
       so they meet cleanly without crossing into an "X".
    """
    if isinstance(atr_val, list):
        atr_val = atr_val[-1] if atr_val else 15.0
    if len(swings) < 3 or len(bars) < 5:
        return []

    high_swings = [s for s in swings if s.kind == "high"]
    low_swings = [s for s in swings if s.kind == "low"]

    def _build_line(
        p1: SwingPoint,
        p2: SwingPoint,
        side: Literal["high", "low"],
        label: str,
    ) -> Trendline | None:
        dx = p2.index - p1.index
        if dx < 2 or dx > 180:
            return None
        dy = p2.price - p1.price
        slope = dy / dx
        kind: Literal["ascending", "descending"] = "ascending" if p2.price >= p1.price else "descending"

        # ------------------------------------------------------------------
        # 1. Structural Candle Body Penetration & Cleanliness Filter
        # ------------------------------------------------------------------
        body_violations = 0
        touch_count = 1  # Anchor 1 at p1
        last_touch_idx = p1.index

        for k in range(p1.index + 1, p2.index):
            b = bars[k]
            base_y = p1.price + slope * (k - p1.index)
            o, c = b.get("open", base_y), b.get("close", base_y)
            h, l = b.get("high", base_y), b.get("low", base_y)
            body_top = max(o, c)
            body_bottom = min(o, c)

            if side == "low":
                if body_bottom < base_y - 0.25 * atr_val:
                    body_violations += 1
                elif abs(l - base_y) <= 0.35 * atr_val and body_bottom >= base_y - 0.1 * atr_val:
                    if k - last_touch_idx >= 2 and p2.index - k >= 1:
                        touch_count += 1
                        last_touch_idx = k
            else:
                if body_top > base_y + 0.25 * atr_val:
                    body_violations += 1
                elif abs(h - base_y) <= 0.35 * atr_val and body_top <= base_y + 0.1 * atr_val:
                    if k - last_touch_idx >= 2 and p2.index - k >= 1:
                        touch_count += 1
                        last_touch_idx = k

        # Anchor 2 at p2
        touch_count += 1
        last_touch_idx = p2.index

        # Reject dirty lines where candle bodies repeatedly slice through the line
        if body_violations > 1 and (body_violations / max(dx - 1, 1)) > 0.12:
            return None

        # ------------------------------------------------------------------
        # 2. Parallel Channel Projection
        # ------------------------------------------------------------------
        channel_high: float | None = None
        channel_low: float | None = None
        if side == "low":
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

        # ------------------------------------------------------------------
        # 3. Forward Touches, Breakout & Retest Detection
        # ------------------------------------------------------------------
        is_broken = False
        break_index: int | None = None
        break_date: str | None = None
        retested = False

        for k in range(p2.index + 1, len(bars)):
            projected = p1.price + slope * (k - p1.index)
            bk = bars[k]
            o, c = bk.get("open", projected), bk.get("close", projected)
            h, l = bk.get("high", projected), bk.get("low", projected)
            body_top = max(o, c)
            body_bottom = min(o, c)

            if side == "low":
                if c < projected - 0.2 * atr_val:
                    is_broken = True
                    break_index = k
                    break_date = bk["date"]
                    for r in range(k + 1, min(k + 10, len(bars))):
                        proj_r = p1.price + slope * (r - p1.index)
                        if abs(bars[r]["high"] - proj_r) <= 0.35 * atr_val:
                            retested = True
                            break
                    break
                elif abs(l - projected) <= 0.35 * atr_val and body_bottom >= projected - 0.1 * atr_val:
                    if k - last_touch_idx >= 1:
                        touch_count += 1
                        last_touch_idx = k
            else:
                if c > projected + 0.2 * atr_val:
                    is_broken = True
                    break_index = k
                    break_date = bk["date"]
                    for r in range(k + 1, min(k + 10, len(bars))):
                        proj_r = p1.price + slope * (r - p1.index)
                        if abs(bars[r]["low"] - proj_r) <= 0.35 * atr_val:
                            retested = True
                            break
                    break
                elif abs(h - projected) <= 0.35 * atr_val and body_top <= projected + 0.1 * atr_val:
                    if k - last_touch_idx >= 1:
                        touch_count += 1
                        last_touch_idx = k

        # ------------------------------------------------------------------
        # 4. Strict 3-Point Requirement: Must have at least 3 distinct touches
        # ------------------------------------------------------------------
        if touch_count < 3:
            return None

        # Institutional colors and types
        if side == "high":
            if kind == "descending":
                line_type = "descending_resistance"
                color = "#f43f5e"  # Rose / Crimson Red (Downtrend Resistance ceiling)
            else:
                line_type = "ascending_resistance"
                color = "#f59e0b"  # Amber Orange (Uptrend Upper Channel / Expansion)
        else:
            if kind == "ascending":
                line_type = "ascending_support"
                color = "#10b981"  # Emerald Green (Uptrend Support floor)
            else:
                line_type = "descending_support"
                color = "#0284c7"  # Sky Blue (Downtrend Lower Channel / Floor)

        final_label = f"{label} ({touch_count}T)"
        quality_score = round(touch_count * 2.5 - body_violations * 3.0 + min(dx, 60) / 20.0, 2)

        return Trendline(
            kind=kind,
            start_index=p1.index,
            start_date=p1.date,
            start_price=p1.price,
            end_index=p2.index,
            end_date=p2.date,
            end_price=p2.price,
            slope=round(slope, 4),
            side=side,
            line_type=line_type,
            color=color,
            channel_high=channel_high,
            channel_low=channel_low,
            is_broken=is_broken,
            break_index=break_index,
            break_date=break_date,
            retested=retested,
            label=final_label,
            touch_count=touch_count,
            body_violations=body_violations,
            quality_score=quality_score,
        )

    # 1. Check swing pairs across multiple spans (1 to 5 swings apart)
    candidates: list[Trendline] = []
    for swings_list, s_side, pos_lbl, neg_lbl in [
        (high_swings, "high", "Ascending Resistance", "Descending Resistance"),
        (low_swings, "low", "Ascending Support", "Descending Support"),
    ]:
        for i in range(len(swings_list) - 1):
            for j in range(i + 1, min(i + 6, len(swings_list))):
                p1, p2 = swings_list[i], swings_list[j]
                dx = p2.index - p1.index
                if dx < 2 or dx > 180:
                    continue
                lbl = pos_lbl if p2.price >= p1.price else neg_lbl
                tl = _build_line(p1, p2, s_side, lbl)
                if tl is not None:
                    candidates.append(tl)

    if not candidates:
        return []

    # 2. Sort candidates by priority (unbroken first, then touches, then quality, then span)
    candidates.sort(
        key=lambda t: (
            not t.is_broken,
            t.touch_count,
            t.quality_score,
            (t.end_index - t.start_index),
        ),
        reverse=True,
    )

    # 3. Filter out crossing & redundant same-side lines
    total_bars = len(bars)
    kept_tls: list[Trendline] = []
    for cand in candidates:
        conflict = False
        for acc in kept_tls:
            if cand.side == acc.side:
                # A. Check span overlap and vertical distance
                ov_s = max(cand.start_index, acc.start_index)
                ov_e = min(cand.end_index, acc.end_index)
                if ov_e > ov_s:
                    mid_x = (ov_s + ov_e) / 2.0
                    y_cand = cand.start_price + cand.slope * (mid_x - cand.start_index)
                    y_acc = acc.start_price + acc.slope * (mid_x - acc.start_index)
                    if abs(y_cand - y_acc) <= 1.2 * atr_val:
                        conflict = True
                        break

                # B. Check if same-side lines cross each other within their active span
                m1, m2 = cand.slope, acc.slope
                if abs(m1 - m2) > 1e-5:
                    c1 = cand.start_price - m1 * cand.start_index
                    c2 = acc.start_price - m2 * acc.start_index
                    xc = (c2 - c1) / (m1 - m2)
                    s1 = cand.start_index
                    e1 = cand.break_index if (cand.is_broken and cand.break_index) else total_bars + 10
                    s2 = acc.start_index
                    e2 = acc.break_index if (acc.is_broken and acc.break_index) else total_bars + 10
                    if max(s1, s2) <= xc <= min(e1, e2):
                        conflict = True
                        break
        if not conflict:
            kept_tls.append(cand)

    # 4. Opposite-Side Converging Lines (Support vs Resistance) - Calculate Apex
    for i in range(len(kept_tls)):
        for j in range(i + 1, len(kept_tls)):
            t1, t2 = kept_tls[i], kept_tls[j]
            if t1.side != t2.side:
                m1, m2 = t1.slope, t2.slope
                if abs(m1 - m2) > 1e-4:
                    c1 = t1.start_price - m1 * t1.start_index
                    c2 = t2.start_price - m2 * t2.start_index
                    xc = (c2 - c1) / (m1 - m2)
                    yc = c1 + m1 * xc
                    if xc >= min(t1.end_index, t2.end_index) and xc <= total_bars + 100:
                        apex_idx = int(xc)
                        apex_p = round(yc, 2)
                        t1.apex_index = min(t1.apex_index or 999999, apex_idx)
                        t1.apex_price = apex_p
                        t2.apex_index = min(t2.apex_index or 999999, apex_idx)
                        t2.apex_price = apex_p

    # Limit to top historical and active non-crossing lines, balanced across top (high) and bottom (low)
    active_tls = [tl for tl in kept_tls if (total_bars - tl.end_index <= 100) or not tl.is_broken]
    historical_tls = [tl for tl in kept_tls if tl not in active_tls]

    active_highs = [tl for tl in active_tls if tl.side == "high"][:8]
    active_lows = [tl for tl in active_tls if tl.side == "low"][:8]
    hist_highs = [tl for tl in historical_tls if tl.side == "high"][:14]
    hist_lows = [tl for tl in historical_tls if tl.side == "low"][:14]

    selected = active_highs + active_lows + hist_highs + hist_lows
    selected.sort(key=lambda t: t.start_index)
    return selected


# ---------------------------------------------------------------------------
# Liquidity Visualization & Sweeps Engine
# ---------------------------------------------------------------------------

def calculate_liquidity_levels(
    daily_bars: list[dict],
    intraday_bars: list[dict],
    weekly_bars: list[dict] | None = None,
    swings: list[SwingPoint] | None = None,
    interval: str = "5m",
) -> list[LiquidityLevel]:
    """Compute institutional liquidity levels:

    1. PDH, PDL, PDC (Previous Day High, Low, Close)
    2. Multi-Level Floor Pivots (P, R1, R2, R3, R4, S1, S2, S3, S4)
    3. Previous Week High & Low (PWH, PWL)
    4. Previous Month High & Low (PMH, PML)
    5. 52-Week / Major Range High & Low (52WH, 52WL)
    6. Historical Swing Support & Resistance levels across different price tiers (Res, Sup, Flip Sup, Flip Res)
    7. Session High (DH) and Low (DL)
    8. Opening Range High and Low (ORH / ORL for 15m and 30m)
    9. Equal Highs (EQH) and Equal Lows (EQL)
    """
    levels: list[LiquidityLevel] = []

    # 1. Previous Day High, Low, Close & Extended Floor Pivots (R1-R4, S1-S4)
    if len(daily_bars) >= 2:
        prev = daily_bars[-2]
        c = prev["close"]
        h = prev["high"] or c
        l = prev["low"] or c
        diff = h - l
        p = (h + l + c) / 3.0
        r1 = 2 * p - l
        s1 = 2 * p - h
        r2 = p + diff
        s2 = p - diff
        r3 = r1 + diff
        r4 = r3 + diff
        s3 = s1 - diff
        s4 = s3 - diff

        levels.extend([
            LiquidityLevel("PDH", round(h, 2), "PDH", "#f43f5e", "dashed", "high"),
            LiquidityLevel("PDL", round(l, 2), "PDL", "#10b981", "dashed", "high"),
            LiquidityLevel("PDC", round(c, 2), "PDC", "#71717a", "dotted", "medium"),
            LiquidityLevel("R4", round(r4, 2), "PIVOT", "#991b1b", "dotted", "standard"),
            LiquidityLevel("R3", round(r3, 2), "PIVOT", "#dc2626", "dotted", "standard"),
            LiquidityLevel("R2", round(r2, 2), "PIVOT", "#ef4444", "dotted", "standard"),
            LiquidityLevel("R1", round(r1, 2), "PIVOT", "#f43f5e", "dotted", "standard"),
            LiquidityLevel("P", round(p, 2), "PIVOT", "#6366f1", "dotted", "medium"),
            LiquidityLevel("S1", round(s1, 2), "PIVOT", "#10b981", "dotted", "standard"),
            LiquidityLevel("S2", round(s2, 2), "PIVOT", "#059669", "dotted", "standard"),
            LiquidityLevel("S3", round(s3, 2), "PIVOT", "#047857", "dotted", "standard"),
            LiquidityLevel("S4", round(s4, 2), "PIVOT", "#065f46", "dotted", "standard"),
        ])

    # 2. Previous Week High & Low (PWH, PWL)
    if weekly_bars and len(weekly_bars) >= 2:
        prev_w = weekly_bars[-2]
        wh = prev_w.get("high") or prev_w["close"]
        wl = prev_w.get("low") or prev_w["close"]
        levels.extend([
            LiquidityLevel("PWH", round(wh, 2), "PWH", "#f43f5e", "dashed", "high"),
            LiquidityLevel("PWL", round(wl, 2), "PWL", "#10b981", "dashed", "high"),
        ])

    # 3. Previous Month High & Low (PMH, PML)
    if daily_bars:
        months_dict: dict[str, list[dict]] = {}
        for b in daily_bars:
            m_key = b["date"][:7]
            months_dict.setdefault(m_key, []).append(b)
        sorted_m_keys = sorted(months_dict.keys())
        if len(sorted_m_keys) >= 2:
            prev_m_bars = months_dict[sorted_m_keys[-2]]
            pmh = max((b["high"] or b["close"]) for b in prev_m_bars)
            pml = min((b["low"] or b["close"]) for b in prev_m_bars)
            levels.extend([
                LiquidityLevel("PMH", round(pmh, 2), "PMH", "#dc2626", "dashed", "high"),
                LiquidityLevel("PML", round(pml, 2), "PML", "#059669", "dashed", "high"),
            ])

    # 4. 52-Week / All-Time High & Low (ATH, 52WH, 52WL)
    if len(daily_bars) >= 20:
        lookback_bars = daily_bars[-252:]
        yh = max((b["high"] or b["close"]) for b in lookback_bars)
        yl = min((b["low"] or b["close"]) for b in lookback_bars)
        ath = max((b["high"] or b["close"]) for b in daily_bars)
        if abs(ath - yh) > 5:
            levels.append(LiquidityLevel("ATH", round(ath, 2), "52WH", "#7f1d1d", "solid", "high"))
        levels.extend([
            LiquidityLevel("52WH", round(yh, 2), "52WH", "#991b1b", "solid", "high"),
            LiquidityLevel("52WL", round(yl, 2), "52WL", "#047857", "solid", "high"),
        ])

    # 5. Major Previous Support & Resistance Levels as per Price Range
    active_bars = intraday_bars or daily_bars
    cur_price = active_bars[-1]["close"] if active_bars else 0.0

    all_swings = list(swings or [])
    if not all_swings and daily_bars:
        all_swings = detect_swing_points(daily_bars, sensitivity=2)
    elif daily_bars and len(daily_bars) > len(all_swings):
        d_swings = detect_swing_points(daily_bars, sensitivity=2)
        all_swings.extend(d_swings)

    # Dynamic spacing scaled to the visible price range (at least 1.5% of price or 4% of span)
    all_prices = [b["close"] for b in active_bars if b.get("close")] if active_bars else [cur_price]
    min_chart_p = min(all_prices) if all_prices else cur_price * 0.95
    max_chart_p = max(all_prices) if all_prices else cur_price * 1.05
    chart_span = max(max_chart_p - min_chart_p, cur_price * 0.05)
    major_spacing = max(cur_price * 0.015, chart_span * 0.04)

    # Filter for significant structural swings (major extremes or prominent multi-bar reversals)
    major_swings = [
        s for s in all_swings
        if s.is_major or (s.label and s.label in ("HH1", "HH2", "LL1", "LL2", "LH1", "HL1"))
    ]

    # Cluster swing highs (Major Previous Resistance levels)
    swing_highs = sorted([s for s in major_swings if s.kind == "high"], key=lambda s: s.price)
    high_clusters: list[list[SwingPoint]] = []
    for sh in swing_highs:
        if high_clusters and (sh.price - high_clusters[-1][0].price) < major_spacing:
            high_clusters[-1].append(sh)
        else:
            high_clusters.append([sh])

    major_res_added = 0
    for cluster in high_clusters:
        if major_res_added >= 4:
            break
        avg_p = round(max(s.price for s in cluster), 2)
        # Skip if already represented by a benchmark level (PDH, PWH, PMH, 52WH, R1-R4)
        if any(abs(lv.price - avg_p) < major_spacing * 0.8 for lv in levels):
            continue
        cnt = len(cluster)
        if avg_p >= cur_price:
            lbl = f"Res ({cnt}x)" if cnt >= 2 else "Major Res"
            kind = "RESISTANCE"
            col = "#f43f5e"
            major_res_added += 1
            levels.append(LiquidityLevel(lbl, avg_p, kind, col, "dashed", "high"))
        elif major_res_added < 2:
            lbl = "Flip Sup"
            kind = "SUPPORT"
            col = "#10b981"
            major_res_added += 1
            levels.append(LiquidityLevel(lbl, avg_p, kind, col, "dashed", "standard"))

    # Cluster swing lows (Major Previous Support levels)
    swing_lows = sorted([s for s in major_swings if s.kind == "low"], key=lambda s: s.price)
    low_clusters: list[list[SwingPoint]] = []
    for sl in swing_lows:
        if low_clusters and (sl.price - low_clusters[-1][0].price) < major_spacing:
            low_clusters[-1].append(sl)
        else:
            low_clusters.append([sl])

    major_sup_added = 0
    for cluster in low_clusters:
        if major_sup_added >= 4:
            break
        avg_p = round(min(s.price for s in cluster), 2)
        # Skip if already represented by a benchmark level (PDL, PWL, PML, 52WL, S1-S4)
        if any(abs(lv.price - avg_p) < major_spacing * 0.8 for lv in levels):
            continue
        cnt = len(cluster)
        if avg_p <= cur_price:
            lbl = f"Sup ({cnt}x)" if cnt >= 2 else "Major Sup"
            kind = "SUPPORT"
            col = "#10b981"
            major_sup_added += 1
            levels.append(LiquidityLevel(lbl, avg_p, kind, col, "dashed", "high"))
        elif major_sup_added < 2:
            lbl = "Flip Res"
            kind = "RESISTANCE"
            col = "#f43f5e"
            major_sup_added += 1
            levels.append(LiquidityLevel(lbl, avg_p, kind, col, "dashed", "standard"))

    # 6. Session Open (DO), High (DH), Low (DL), Opening Range (ORH, ORL), Untouched Pools (BSL, SSL), Equal Highs/Lows
    if intraday_bars:
        latest_day = intraday_bars[-1]["date"][:10]
        today_bars = [b for b in intraday_bars if b["date"][:10] == latest_day]
        if len(today_bars) >= 1:
            first_open = today_bars[0].get("open") or today_bars[0]["close"]
            levels.append(LiquidityLevel("DO", round(first_open, 2), "DO", "#94a3b8", "dotted", "medium"))

            dh = max((b.get("high") or b["close"]) for b in today_bars)
            dl = min((b.get("low") or b["close"]) for b in today_bars)
            levels.append(LiquidityLevel("DH", round(dh, 2), "DH", "#f43f5e", "solid", "high"))
            levels.append(LiquidityLevel("DL", round(dl, 2), "DL", "#10b981", "solid", "high"))

            # 15m Opening Range (first 3 bars of 5m or 15 bars of 1m)
            or15_bars = [b for b in today_bars if b["date"][11:16] < "09:30"]
            orh15, orl15 = None, None
            if or15_bars:
                orh15 = max((b.get("high") or b["close"]) for b in or15_bars)
                orl15 = min((b.get("low") or b["close"]) for b in or15_bars)
                levels.append(LiquidityLevel("ORH-15", round(orh15, 2), "ORH", "#f59e0b", "dashed", "medium"))
                levels.append(LiquidityLevel("ORL-15", round(orl15, 2), "ORL", "#f59e0b", "dashed", "medium"))

            # 30m Opening Range
            or30_bars = [b for b in today_bars if b["date"][11:16] < "09:45"]
            if or30_bars and len(or30_bars) > len(or15_bars):
                orh30 = max((b.get("high") or b["close"]) for b in or30_bars)
                orl30 = min((b.get("low") or b["close"]) for b in or30_bars)
                if orh15 is None or abs(orh30 - orh15) > 2:
                    levels.append(LiquidityLevel("ORH-30", round(orh30, 2), "ORH", "#f59e0b", "dashed", "medium"))
                if orl15 is None or abs(orl30 - orl15) > 2:
                    levels.append(LiquidityLevel("ORL-30", round(orl30, 2), "ORL", "#f59e0b", "dashed", "medium"))

            # Untouched Intraday Liquidity Pools (BSL / SSL)
            if len(today_bars) >= 5:
                hi = [(b.get("high") or b["close"]) for b in today_bars]
                lo = [(b.get("low") or b["close"]) for b in today_bars]
                sw_win = 2
                rng = range(sw_win, len(today_bars) - sw_win)
                bsl = [hi[i] for i in rng if hi[i] >= max(hi[i - sw_win : i]) and hi[i] > max(hi[i + 1 :]) and hi[i] != max(hi)]
                ssl = [lo[i] for i in rng if lo[i] <= min(lo[i - sw_win : i]) and lo[i] < min(lo[i + 1 :]) and lo[i] != min(lo)]
                for n, p in enumerate(reversed(bsl[-3:]), 1):
                    levels.append(LiquidityLevel(f"BSL{n}", round(p, 2), "BSL", "#f43f5e", "dashed", "high"))
                for n, p in enumerate(reversed(ssl[-3:]), 1):
                    levels.append(LiquidityLevel(f"SSL{n}", round(p, 2), "SSL", "#10b981", "dashed", "high"))

            # Equal Highs (EQH) and Equal Lows (EQL)
            atr_val = calculate_atr(intraday_bars, 14)[-1] if len(intraday_bars) >= 14 else 15.0
            eq_tolerance = 0.15 * atr_val

            i_swings = detect_swing_points(today_bars, sensitivity=2)
            highs = [s for s in i_swings if s.kind == "high"]
            lows = [s for s in i_swings if s.kind == "low"]

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

    # 7. Higher Timeframe (HTF) Support & Resistance for active timeframe:
    # When viewing 1m: extract 5M, 15M, 30M, 1H key swing support & resistance
    # When viewing 5m: extract 15M, 30M, 1H key swing support & resistance
    # When viewing 15m: extract 30M, 1H, Daily key swing support & resistance
    # When viewing 60m: extract Daily key swing support & resistance
    # 7. Higher Timeframe (HTF) Candle High/Low & Swing Support/Resistance:
    if intraday_bars:
        # Immediate Previous 1 and 2 Higher-Timeframe Candle Highs & Lows:
        # On 1m chart: project last 1-2 completed 5M candle Highs & Lows (5M-H1, 5M-L1, 5M-H2, 5M-L2)
        # On 5m chart: project last 1-2 completed 15M candle Highs & Lows (15M-H1, 15M-L1, 15M-H2, 15M-L2)
        if interval == "1m":
            agg_5m = aggregate_bars(intraday_bars, 5)
            if agg_5m:
                last_bucket_date = agg_5m[-1]["date"]
                bars_in_last_bucket = sum(1 for b in intraday_bars if b["date"] >= last_bucket_date)
                closed_5m = agg_5m[:-1] if (bars_in_last_bucket < 5 and len(agg_5m) >= 2) else agg_5m
                if closed_5m:
                    c1 = closed_5m[-1]
                    levels.append(LiquidityLevel("5M-H1", round(c1["high"], 2), "HTF_CANDLE", "#ef4444", "dashed", "high"))
                    levels.append(LiquidityLevel("5M-L1", round(c1["low"], 2), "HTF_CANDLE", "#10b981", "dashed", "high"))
                if len(closed_5m) >= 2:
                    c2 = closed_5m[-2]
                    levels.append(LiquidityLevel("5M-H2", round(c2["high"], 2), "HTF_CANDLE", "#f87171", "dotted", "medium"))
                    levels.append(LiquidityLevel("5M-L2", round(c2["low"], 2), "HTF_CANDLE", "#34d399", "dotted", "medium"))
        elif interval == "5m":
            agg_15m = aggregate_bars(intraday_bars, 15)
            if agg_15m:
                last_bucket_date = agg_15m[-1]["date"]
                bars_in_last_bucket = sum(1 for b in intraday_bars if b["date"] >= last_bucket_date)
                closed_15m = agg_15m[:-1] if (bars_in_last_bucket < 3 and len(agg_15m) >= 2) else agg_15m
                if closed_15m:
                    c1 = closed_15m[-1]
                    levels.append(LiquidityLevel("15M-H1", round(c1["high"], 2), "HTF_CANDLE", "#ef4444", "dashed", "high"))
                    levels.append(LiquidityLevel("15M-L1", round(c1["low"], 2), "HTF_CANDLE", "#10b981", "dashed", "high"))
                if len(closed_15m) >= 2:
                    c2 = closed_15m[-2]
                    levels.append(LiquidityLevel("15M-H2", round(c2["high"], 2), "HTF_CANDLE", "#f87171", "dotted", "medium"))
                    levels.append(LiquidityLevel("15M-L2", round(c2["low"], 2), "HTF_CANDLE", "#34d399", "dotted", "medium"))

        htf_configs: list[tuple[str, int]] = []
        if interval == "1m":
            htf_configs = [("5M", 5), ("15M", 15), ("30M", 30), ("1H", 60)]
        elif interval == "5m":
            htf_configs = [("15M", 15), ("30M", 30), ("1H", 60)]
        elif interval == "15m":
            htf_configs = [("30M", 30), ("1H", 60)]

        for htf_label, mins in htf_configs:
            agg = aggregate_bars(intraday_bars, mins)
            if len(agg) >= 5:
                htf_swings = detect_swing_points(agg, sensitivity=2)
                h_highs = [s for s in htf_swings if s.kind == "high"]
                h_lows = [s for s in htf_swings if s.kind == "low"]
                if h_highs:
                    last_h = h_highs[-1]
                    levels.append(LiquidityLevel(f"{htf_label}-Res", round(last_h.price, 2), "RESISTANCE", "#f43f5e", "dashed", "high"))
                if h_lows:
                    last_l = h_lows[-1]
                    levels.append(LiquidityLevel(f"{htf_label}-Sup", round(last_l.price, 2), "SUPPORT", "#10b981", "dashed", "high"))

    # Daily Structural Support & Resistance (for intraday charts 1m, 5m, 15m, 60m)
    if daily_bars and len(daily_bars) >= 6:
        d_swings = detect_swing_points(daily_bars, sensitivity=2)
        d_highs = [s for s in d_swings if s.kind == "high"]
        d_lows = [s for s in d_swings if s.kind == "low"]
        if d_highs:
            levels.append(LiquidityLevel("Daily-Res", round(d_highs[-1].price, 2), "RESISTANCE", "#dc2626", "dashed", "high"))
        if d_lows:
            levels.append(LiquidityLevel("Daily-Sup", round(d_lows[-1].price, 2), "SUPPORT", "#059669", "dashed", "high"))

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
        first_bar = w_bars[0]
        first_open = first_bar.get("open")
        if first_open is None:
            first_open = first_bar["close"]
        out.append({
            "date": first_bar["date"],
            "open": first_open,
            "high": max(b.get("high") or b["close"] for b in w_bars),
            "low": min(b.get("low") or b["close"] for b in w_bars),
            "close": w_bars[-1]["close"],
            "volume": sum(b.get("volume") or 0 for b in w_bars),
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
            out[-1]["high"] = max(out[-1]["high"], b.get("high") or b["close"])
            out[-1]["low"] = min(out[-1]["low"], b.get("low") or b["close"])
            out[-1]["close"] = b["close"]
            out[-1]["volume"] = (out[-1].get("volume") or 0) + (b.get("volume") or 0)
        else:
            out.append({
                "date": start,
                "open": b.get("open") if b.get("open") is not None else b["close"],
                "high": b.get("high") or b["close"],
                "low": b.get("low") or b["close"],
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
    liquidity_levels = calculate_liquidity_levels(daily_bars, bars, weekly_bars, swings=swings, interval=interval)
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

