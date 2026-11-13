"""Deterministic, causal market-regime classifier (`regime-v2`).

Pure functions over validated closed bars (`bar_close_time <= decision_time`).
No network calls, no database reads, no hidden state mutation, and no strategy
target/stop overwriting. Missing or insufficient observations return explicit
`INSUFFICIENT_DATA` / `UNKNOWN_VOL` labels rather than fabricating proxies.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, Mapping

import numpy as np
import pandas as pd

from app.trading.option_quote import IST

REGIME_VERSION = "regime-v2"
VIX_MAX_AGE_SECONDS = 60.0
MIN_BARS_FOR_STRUCTURE = 14

StructureRegime = Literal[
    "TREND_UP",
    "TREND_DOWN",
    "COMPRESSION_CHOP",
    "RANGE_ROTATION",
    "INSUFFICIENT_DATA",
]
VolatilityRegime = Literal[
    "LOW_VOL",
    "NORMAL_VOL",
    "HIGH_VOL",
    "EXTREME_VOL",
    "UNKNOWN_VOL",
]
GapRegime = Literal[
    "GAP_UP_LARGE",
    "GAP_UP_MODERATE",
    "FLAT_OPEN",
    "GAP_DOWN_MODERATE",
    "GAP_DOWN_LARGE",
    "INSUFFICIENT_DATA",
]
SessionPhase = Literal[
    "OPENING_DISCOVERY",
    "MORNING_PRIME",
    "MIDDAY_LULL",
    "AFTERNOON_TREND",
    "CLOSE_ONLY_WINDOW",
    "OUTSIDE_SESSION",
]


@dataclass(frozen=True)
class VixObservation:
    value: float | Decimal
    observed_at: datetime | None = None
    source: str = "unknown"


@dataclass(frozen=True)
class MarketRegimeVector:
    version: str
    decision_time: str
    structure_regime: StructureRegime
    vol_regime: VolatilityRegime
    gap_regime: GapRegime
    session_phase: SessionPhase
    legacy_regime: str
    spot_close: float | None
    session_vwap: float | None
    ema_15: float | None
    adx_14: float | None
    choppiness_14: float | None
    span_6_bars: float | None
    vix: float | None
    gap_pct: float | None
    or30_ready: bool
    or30_high: float | None
    or30_low: float | None
    closed_bars_5m: int
    closed_bars_15m: int

    @property
    def label(self) -> str:
        return f"{self.structure_regime}|{self.vol_regime}|{self.gap_regime}|{self.session_phase}"

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "label": self.label}


def legacy_regime_v1(df_5m: pd.DataFrame | None) -> str:
    """regime-v1, fixed at the signal: close vs the session's mean typical price, and the 20-bar slope."""
    if df_5m is None or len(df_5m) < 2:
        return "UNKNOWN"
    day = str(df_5m.iloc[-1].get("date") or df_5m.iloc[-1].get("timestamp") or "")[:10]
    col = "date" if "date" in df_5m else ("timestamp" if "timestamp" in df_5m else None)
    session = df_5m[[str(d)[:10] == day for d in df_5m[col]]] if col else df_5m
    typical = ((session["high"] + session["low"] + session["close"]) / 3).mean()
    closes = list(df_5m["close"].astype(float))
    if math.isnan(typical):
        return "UNKNOWN"
    side = "ABOVE_VWAP" if closes[-1] >= typical else "BELOW_VWAP"
    slope = "RISING" if closes[-1] >= closes[-min(20, len(closes))] else "FALLING"
    return f"{side}/{slope}"


def _causal_closed_frame(
    bars: pd.DataFrame | list[Mapping[str, Any]] | None,
    now_ist: datetime,
    bar_minutes: int,
) -> pd.DataFrame:
    """Return validated, strictly ascending bars whose close time is `<= now_ist`."""
    if bars is None:
        return pd.DataFrame()
    df = bars.copy() if isinstance(bars, pd.DataFrame) else pd.DataFrame(list(bars))
    if df.empty or not {"open", "high", "low", "close"}.issubset(df.columns):
        return pd.DataFrame()
    clock = now_ist.astimezone(IST).replace(tzinfo=None)
    delta = timedelta(minutes=bar_minutes)
    starts: list[datetime] = []
    valid_mask: list[bool] = []
    try:
        for _, row in df.iterrows():
            o, h, l, c = (float(row[k]) for k in ("open", "high", "low", "close"))
            if not all(math.isfinite(v) and v > 0 for v in (o, h, l, c)):
                return pd.DataFrame()
            if l > min(o, c) or h < max(o, c) or l > h:
                return pd.DataFrame()
            raw_ts = str(row.get("date") or row.get("timestamp") or "").strip()
            if not raw_ts:
                return pd.DataFrame()
            dt = datetime.fromisoformat(raw_ts)
            if dt.utcoffset() is not None:
                dt = dt.astimezone(IST).replace(tzinfo=None)
            if dt.minute % bar_minutes != 0 or dt.second != 0 or dt.microsecond != 0:
                return pd.DataFrame()
            starts.append(dt)
            valid_mask.append(dt + delta <= clock)
    except (TypeError, ValueError):
        return pd.DataFrame()
    if any(b <= a for a, b in zip(starts, starts[1:])):
        return pd.DataFrame()
    out = df.loc[valid_mask].copy().reset_index(drop=True)
    if not out.empty:
        out["_bar_start"] = [s for s, keep in zip(starts, valid_mask) if keep]
    return out


def compute_session_vwap(session_df: pd.DataFrame) -> float | None:
    """Causal session VWAP (or mean typical price when volume is absent/zero)."""
    if session_df is None or session_df.empty:
        return None
    tp = (
        session_df["high"].astype(float)
        + session_df["low"].astype(float)
        + session_df["close"].astype(float)
    ) / 3.0
    if "volume" in session_df.columns:
        vol = pd.to_numeric(session_df["volume"], errors="coerce").fillna(0.0)
        if (vol < 0).any():
            return None
        total_vol = float(vol.sum())
        if total_vol > 0:
            val = float((tp * vol).sum() / total_vol)
            return val if math.isfinite(val) else None
    val = float(tp.mean())
    return val if math.isfinite(val) else None


def compute_ema(closes: pd.Series, period: int = 15) -> float | None:
    if closes is None or len(closes) < 1:
        return None
    val = float(closes.astype(float).ewm(span=period, adjust=False).mean().iloc[-1])
    return val if math.isfinite(val) else None


def compute_choppiness_14(df: pd.DataFrame, period: int = 14) -> float | None:
    """14-period Choppiness Index over causal closed bars. Requires `>= period + 1` bars."""
    if df is None or len(df) < period + 1:
        return None
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    tr = pd.concat(
        [high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()],
        axis=1,
    ).max(axis=1)
    atr_sum = float(tr.iloc[-period:].sum())
    highest_high = float(high.iloc[-period:].max())
    lowest_low = float(low.iloc[-period:].min())
    range_diff = highest_high - lowest_low
    if range_diff <= 0 or atr_sum <= 0:
        return 50.0
    ci = 100.0 * math.log10(atr_sum / range_diff) / math.log10(period)
    return float(np.clip(ci, 0.0, 100.0))


def compute_adx_14(df: pd.DataFrame, period: int = 14) -> float | None:
    """Wilder-smoothed 14-period ADX over causal closed bars. Requires `>= period` bars."""
    if df is None or len(df) < period:
        return None
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    tr = pd.concat(
        [high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()],
        axis=1,
    ).max(axis=1)
    up_move = high - high.shift(1)
    down_move = low.shift(1) - low
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    atr = tr.ewm(alpha=1.0 / period, adjust=False).mean()
    plus_di = 100.0 * (
        pd.Series(plus_dm, index=df.index).ewm(alpha=1.0 / period, adjust=False).mean()
        / atr.replace(0.0, np.nan)
    )
    minus_di = 100.0 * (
        pd.Series(minus_dm, index=df.index).ewm(alpha=1.0 / period, adjust=False).mean()
        / atr.replace(0.0, np.nan)
    )
    denom = (plus_di + minus_di).replace(0.0, np.nan)
    dx = (100.0 * (plus_di - minus_di).abs() / denom).fillna(0.0)
    adx = float(dx.ewm(alpha=1.0 / period, adjust=False).mean().iloc[-1])
    return float(np.clip(adx, 0.0, 100.0))


def classify_session_phase(now_ist: datetime) -> SessionPhase:
    hhmm = now_ist.astimezone(IST).strftime("%H:%M")
    if hhmm < "09:15" or hhmm >= "15:30":
        return "OUTSIDE_SESSION"
    if hhmm < "09:30":
        return "OPENING_DISCOVERY"
    if hhmm < "11:30":
        return "MORNING_PRIME"
    if hhmm < "13:15":
        return "MIDDAY_LULL"
    if hhmm < "15:10":
        return "AFTERNOON_TREND"
    return "CLOSE_ONLY_WINDOW"


_DAILY_VIX_MARKERS = frozenset({"1d", "daily", "eod", "nse_eod", "yahoo_1d", "historical_1d"})


def classify_volatility(
    vix_quote: VixObservation | Mapping[str, Any] | float | Decimal | None,
    now: datetime,
) -> tuple[VolatilityRegime, float | None]:
    """Classify intraday India VIX only when timestamped, fresh (<=60s), and not sourced from daily/EOD data."""
    if vix_quote is None:
        return "UNKNOWN_VOL", None
    observed_at: datetime | None = None
    source_tag = ""
    cadence_tag = ""
    if isinstance(vix_quote, VixObservation):
        raw_val = vix_quote.value
        observed_at = vix_quote.observed_at
        source_tag = str(vix_quote.source or "").strip().lower()
    elif isinstance(vix_quote, Mapping):
        raw_val = vix_quote.get("value", vix_quote.get("vix"))
        source_tag = str(vix_quote.get("source") or "").strip().lower()
        cadence_tag = str(vix_quote.get("timeframe") or vix_quote.get("cadence") or "").strip().lower()
        raw_obs = vix_quote.get("observed_at")
        if isinstance(raw_obs, str):
            try:
                observed_at = datetime.fromisoformat(raw_obs)
            except ValueError:
                return "UNKNOWN_VOL", None
        elif isinstance(raw_obs, datetime):
            observed_at = raw_obs
        else:
            return "UNKNOWN_VOL", None
    else:
        # Bare scalar float/Decimal or unsupported contract cannot prove intraday freshness; fail closed.
        return "UNKNOWN_VOL", None

    if source_tag in _DAILY_VIX_MARKERS or cadence_tag in _DAILY_VIX_MARKERS:
        return "UNKNOWN_VOL", None
    if any(marker in source_tag for marker in ("daily", "eod", "1d")):
        return "UNKNOWN_VOL", None

    try:
        val = float(raw_val)
    except (TypeError, ValueError):
        return "UNKNOWN_VOL", None
    if not math.isfinite(val) or val <= 0:
        return "UNKNOWN_VOL", None
    if observed_at is None or observed_at.utcoffset() is None or now.utcoffset() is None:
        return "UNKNOWN_VOL", None
    age = (now - observed_at).total_seconds()
    if age < 0 or age > VIX_MAX_AGE_SECONDS:
        return "UNKNOWN_VOL", None
    if val < 12.0:
        return "LOW_VOL", round(val, 4)
    if val < 18.0:
        return "NORMAL_VOL", round(val, 4)
    if val <= 22.0:
        return "HIGH_VOL", round(val, 4)
    return "EXTREME_VOL", round(val, 4)


def classify_gap(
    session_5m: pd.DataFrame,
    levels: Any | None,
) -> tuple[GapRegime, float | None]:
    """Fixed at the first closed session bar (09:15 bar close at 09:20 IST) vs PDC. Never invents GIFT NIFTY."""
    if levels is None or session_5m is None or session_5m.empty:
        return "INSUFFICIENT_DATA", None
    pdc_raw = levels.get("pdc") if isinstance(levels, Mapping) else getattr(levels, "pdc", None)
    try:
        pdc = float(pdc_raw)
    except (TypeError, ValueError):
        return "INSUFFICIENT_DATA", None
    if not math.isfinite(pdc) or pdc <= 0:
        return "INSUFFICIENT_DATA", None
    first_close = float(session_5m.iloc[0]["close"])
    gap_pct = ((first_close - pdc) / pdc) * 100.0
    if gap_pct > 0.35:
        regime: GapRegime = "GAP_UP_LARGE"
    elif gap_pct >= 0.15:
        regime = "GAP_UP_MODERATE"
    elif gap_pct < -0.35:
        regime = "GAP_DOWN_LARGE"
    elif gap_pct <= -0.15:
        regime = "GAP_DOWN_MODERATE"
    else:
        regime = "FLAT_OPEN"
    return regime, round(gap_pct, 4)


def _or30_bounds(
    session_5m: pd.DataFrame,
    levels: Any | None,
    now_ist: datetime,
) -> tuple[bool, float | None, float | None]:
    if now_ist.astimezone(IST).strftime("%H:%M") < "09:45":
        return False, None, None
    if levels is not None:
        raw_h = levels.get("or30_high") if isinstance(levels, Mapping) else getattr(levels, "or30_high", None)
        raw_l = levels.get("or30_low") if isinstance(levels, Mapping) else getattr(levels, "or30_low", None)
        if raw_h is not None and raw_l is not None:
            try:
                h, l = float(raw_h), float(raw_l)
                if math.isfinite(h) and math.isfinite(l) and h >= l > 0:
                    return True, round(h, 2), round(l, 2)
            except (TypeError, ValueError):
                pass
    if session_5m is None or session_5m.empty:
        return False, None, None
    or_bars = session_5m[
        [
            "09:15" <= ts.strftime("%H:%M") < "09:45"
            for ts in session_5m["_bar_start"]
        ]
    ]
    if len(or_bars) < 6:
        return False, None, None
    return (
        True,
        round(float(or_bars["high"].astype(float).max()), 2),
        round(float(or_bars["low"].astype(float).min()), 2),
    )


def classify_regime(
    bars_5m: pd.DataFrame | list[Mapping[str, Any]] | None,
    *,
    now: datetime,
    bars_15m: pd.DataFrame | list[Mapping[str, Any]] | None = None,
    bars_1d: pd.DataFrame | list[Mapping[str, Any]] | None = None,
    levels: Any | None = None,
    vix_quote: VixObservation | Mapping[str, Any] | float | Decimal | None = None,
) -> MarketRegimeVector:
    """Deterministically classify the causal market regime at `now` (`regime-v2`)."""
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    now_ist = now.astimezone(IST)
    day_str = now_ist.date().isoformat()
    df_5m = _causal_closed_frame(bars_5m, now_ist, 5)
    df_15m = _causal_closed_frame(bars_15m, now_ist, 15)
    _ = bars_1d  # accepted for unified caller signature;PDC/PDH/PDL come from `levels` or daily bar history

    session_phase = classify_session_phase(now_ist)
    vol_regime, vix_val = classify_volatility(vix_quote, now_ist)

    if df_5m.empty:
        return MarketRegimeVector(
            version=REGIME_VERSION,
            decision_time=now_ist.isoformat(),
            structure_regime="INSUFFICIENT_DATA",
            vol_regime=vol_regime,
            gap_regime="INSUFFICIENT_DATA",
            session_phase=session_phase,
            legacy_regime="UNKNOWN",
            spot_close=None,
            session_vwap=None,
            ema_15=None,
            adx_14=None,
            choppiness_14=None,
            span_6_bars=None,
            vix=vix_val,
            gap_pct=None,
            or30_ready=False,
            or30_high=None,
            or30_low=None,
            closed_bars_5m=0,
            closed_bars_15m=len(df_15m),
        )

    session_5m = df_5m[[ts.date().isoformat() == day_str for ts in df_5m["_bar_start"]]].reset_index(drop=True)
    gap_regime, gap_pct = classify_gap(session_5m, levels)
    or30_ready, or30_high, or30_low = _or30_bounds(session_5m, levels, now_ist)
    legacy = legacy_regime_v1(df_5m)

    spot_close = round(float(df_5m.iloc[-1]["close"]), 2)
    vwap = compute_session_vwap(session_5m if not session_5m.empty else df_5m)
    ema15 = compute_ema(df_5m["close"], period=15)
    adx14 = compute_adx_14(df_5m, period=14)
    chop14 = compute_choppiness_14(df_5m, period=14)
    recent_6 = df_5m.tail(6)
    span6 = (
        round(float(recent_6["high"].astype(float).max() - recent_6["low"].astype(float).min()), 2)
        if len(recent_6) == 6
        else None
    )

    if len(df_5m) < MIN_BARS_FOR_STRUCTURE or vwap is None or vwap <= 0 or ema15 is None or adx14 is None:
        structure: StructureRegime = "INSUFFICIENT_DATA"
    else:
        rel_dist = (spot_close - vwap) / vwap
        is_chop = (chop14 is not None and chop14 > 61.8) or (
            adx14 < 18.0 and span6 is not None and span6 < 0.0025 * spot_close
        )
        if spot_close > vwap and ema15 > vwap and rel_dist >= 0.003 and adx14 >= 20.0 and not is_chop:
            structure = "TREND_UP"
        elif spot_close < vwap and ema15 < vwap and -rel_dist >= 0.003 and adx14 >= 20.0 and not is_chop:
            structure = "TREND_DOWN"
        elif is_chop:
            structure = "COMPRESSION_CHOP"
        else:
            structure = "RANGE_ROTATION"

    return MarketRegimeVector(
        version=REGIME_VERSION,
        decision_time=now_ist.isoformat(),
        structure_regime=structure,
        vol_regime=vol_regime,
        gap_regime=gap_regime,
        session_phase=session_phase,
        legacy_regime=legacy,
        spot_close=spot_close,
        session_vwap=round(vwap, 2) if vwap is not None else None,
        ema_15=round(ema15, 2) if ema15 is not None else None,
        adx_14=round(adx14, 2) if adx14 is not None else None,
        choppiness_14=round(chop14, 2) if chop14 is not None else None,
        span_6_bars=span6,
        vix=vix_val,
        gap_pct=gap_pct,
        or30_ready=or30_ready,
        or30_high=or30_high,
        or30_low=or30_low,
        closed_bars_5m=len(df_5m),
        closed_bars_15m=len(df_15m),
    )


classify_regime_v2 = classify_regime
classify_market_regime = classify_regime
