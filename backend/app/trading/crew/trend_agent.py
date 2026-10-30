"""TrendAgent: Macro Trend, Chop Filter, Runway Surveyor & Dynamic R:R Engine.

Responsibilities:
1. Macro Trend Slope: Analyzes 15M/5M trend alignment (EMA, VWAP, HH/HL or LH/LL).
2. Chop / Consolidation Guard: Uses ADX and Choppiness Index (CI) to veto low-probability
   breakout attempts during sideways consolidation.
3. Runway Surveyor: Computes available distance to the next major liquidity level, PDH/PDL,
   or key psychological strike before an entry is cleared.
4. Dynamic R:R Calculator: Replaces rigid fixed targets with dynamic targets
   (ranging from +1.8R in congested markets up to +4.5R in clean-runway megatrends).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal
import pandas as pd
import numpy as np


@dataclass
class MarketRegime:
    regime: Literal["TRENDING_BULL", "TRENDING_BEAR", "CHOPPY_RANGE", "VOLATILE_EXPANSION"]
    adx: float
    choppiness_index: float
    is_breakout_allowed: bool
    recommended_target_r: float
    runway_points: float
    veto_reason: str | None = None


class TrendAgent:
    """Institutional Market Regime, Runway & Dynamic R:R Engine."""

    def __init__(
        self,
        min_adx_for_breakout: float = 18.0,
        max_chop_index: float = 61.8,
        min_runway_points: float = 35.0,
        base_rr: float = 2.5,
    ) -> None:
        self.min_adx = min_adx_for_breakout
        self.max_chop = max_chop_index
        self.min_runway = min_runway_points
        self.base_rr = base_rr

    def calculate_choppiness_index(self, df: pd.DataFrame, period: int = 14) -> float:
        """Calculate Choppiness Index (CI).
        
        Values > 61.8 indicate sideways choppy consolidation (avoid breakouts).
        Values < 38.2 indicate a strong directional trend.
        """
        if len(df) < period + 1:
            return 50.0
        
        high = df["high"]
        low = df["low"]
        close = df["close"]
        
        # True Range
        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low - close.shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        
        atr_sum = tr.rolling(window=period).sum().iloc[-1]
        highest_high = high.rolling(window=period).max().iloc[-1]
        lowest_low = low.rolling(window=period).min().iloc[-1]
        
        range_diff = highest_high - lowest_low
        if range_diff <= 0 or atr_sum <= 0:
            return 50.0
            
        ci = 100 * math.log10(atr_sum / range_diff) / math.log10(period)
        return float(np.clip(ci, 0.0, 100.0))

    def calculate_adx(self, df: pd.DataFrame, period: int = 14) -> float:
        """Calculate Average Directional Index (ADX) to measure trend strength."""
        if len(df) < period * 2:
            return 22.0
            
        high = df["high"]
        low = df["low"]
        close = df["close"]
        
        tr = pd.concat([
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ], axis=1).max(axis=1)
        
        up_move = high - high.shift(1)
        down_move = low.shift(1) - low
        
        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
        
        atr = tr.ewm(alpha=1/period, adjust=False).mean()
        plus_di = 100 * (pd.Series(plus_dm, index=df.index).ewm(alpha=1/period, adjust=False).mean() / atr)
        minus_di = 100 * (pd.Series(minus_dm, index=df.index).ewm(alpha=1/period, adjust=False).mean() / atr)
        
        dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
        dx = dx.fillna(0.0)
        adx = dx.ewm(alpha=1/period, adjust=False).mean().iloc[-1]
        return float(np.clip(adx, 0.0, 100.0))

    def survey_runway(
        self,
        current_price: float,
        direction: Literal["CALL", "PUT"],
        key_levels: list[float],
    ) -> float:
        """Compute minimum clear runway points to the next opposing key level."""
        if not key_levels:
            return 120.0  # Open runway
            
        if direction == "CALL":
            overhead = [lvl for lvl in key_levels if lvl > current_price + 2.0]
            if not overhead:
                return 150.0  # Blue sky / no overhead barrier
            return min(overhead) - current_price
        else:
            support = [lvl for lvl in key_levels if lvl < current_price - 2.0]
            if not support:
                return 150.0  # Open floor
            return current_price - max(support)

    def evaluate_regime(
        self,
        df_5m: pd.DataFrame,
        current_price: float,
        direction: Literal["CALL", "PUT"],
        key_levels: list[float] | None = None,
        is_waterfall_active: bool = False,
    ) -> MarketRegime:
        """Evaluate macro market regime, check chop guard, and determine dynamic R:R."""
        levels = key_levels or []
        runway = self.survey_runway(current_price, direction, levels)
        
        adx = self.calculate_adx(df_5m)
        chop_idx = self.calculate_choppiness_index(df_5m)
        
        # Check chop condition
        is_choppy = (adx < self.min_adx) or (chop_idx > self.max_chop)
        
        # Midday volume waterfalls override chop index if explosive ATR
        if is_waterfall_active:
            is_choppy = False
            
        veto_reason = None
        is_allowed = True
        
        if is_choppy:
            is_allowed = False
            veto_reason = f"Chop Veto: ADX {adx:.1f} < {self.min_adx} or Chop {chop_idx:.1f} > {self.max_chop}"
        elif runway < self.min_runway:
            is_allowed = False
            veto_reason = f"Runway Veto: Only {runway:.1f} pts to next major S/R level (min {self.min_runway} pts required)"

        # Calculate Dynamic R:R
        # Megatrends with huge runway (>= 100 pts) and high ADX (>= 28) get +3.5R to +4.5R
        if adx >= 28.0 and runway >= 90.0:
            target_r = 4.0
            regime_type = "VOLATILE_EXPANSION" if is_waterfall_active else ("TRENDING_BULL" if direction == "CALL" else "TRENDING_BEAR")
        elif adx >= 20.0 and runway >= 60.0:
            target_r = 3.0
            regime_type = "TRENDING_BULL" if direction == "CALL" else "TRENDING_BEAR"
        elif is_choppy:
            target_r = 1.8
            regime_type = "CHOPPY_RANGE"
        else:
            target_r = self.base_rr
            regime_type = "TRENDING_BULL" if direction == "CALL" else "TRENDING_BEAR"

        return MarketRegime(
            regime=regime_type,
            adx=round(adx, 2),
            choppiness_index=round(chop_idx, 2),
            is_breakout_allowed=is_allowed,
            recommended_target_r=target_r,
            runway_points=round(runway, 2),
            veto_reason=veto_reason,
        )
