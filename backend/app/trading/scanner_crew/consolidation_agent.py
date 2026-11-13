"""ConsolidationAgent: Compression Box, ATR Deadband & Chop Filter Engine.

Responsibilities:
1. Multi-Bar Compression Detection:
   - Evaluates rolling 6-bar price spans. If range is < 32 points, flags a tight compression box.
2. Midday Theta Trap Guard (11:30 - 13:15 IST):
   - Actively identifies midday volume lulls where option premium decays without directional trend.
3. ATR Deadband Measurement:
   - Measures volatility expansion vs contraction to prevent entering low-momentum false breakouts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
import pandas as pd


@dataclass
class ConsolidationStatus:
    is_consolidating: bool
    span_points: float
    atr: float
    state: Literal["COMPRESSED_BOX", "NORMAL_VOLATILITY", "VOLATILITY_EXPANSION"]
    reason: str


class ConsolidationAgent:
    """Intraday Volatility Compression and Theta Deadband Inspector."""

    def __init__(self, lookback_bars: int = 6, max_box_span: float = 32.0) -> None:
        self.lookback = lookback_bars
        self.max_box_span = max_box_span

    def evaluate_compression(
        self,
        df_5m: pd.DataFrame,
        current_time_str: str | None = None,
    ) -> ConsolidationStatus:
        """Inspect recent candles for tight compression boxes and low-ATR deadbands."""
        if len(df_5m) < self.lookback:
            return ConsolidationStatus(
                is_consolidating=False,
                span_points=50.0,
                atr=15.0,
                state="NORMAL_VOLATILITY",
                reason="Insufficient bars for compression analysis",
            )

        recent = df_5m.tail(self.lookback)
        span = float(recent["high"].max() - recent["low"].min())
        
        # Calculate recent ATR
        tr1 = recent["high"] - recent["low"]
        tr2 = (recent["high"] - recent["close"].shift(1)).abs()
        tr3 = (recent["low"] - recent["close"].shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = float(tr.mean())

        # Check time window (midday lull 11:30 to 13:15)
        t_str = current_time_str or ""
        if not t_str and "timestamp" in df_5m.columns:
            last_ts = str(df_5m["timestamp"].iloc[-1])
            if len(last_ts) >= 16:
                t_str = last_ts[11:16]

        is_midday = "11:30" <= t_str <= "13:15" if t_str else False

        if span < self.max_box_span and is_midday:
            return ConsolidationStatus(
                is_consolidating=True,
                span_points=round(span, 2),
                atr=round(atr, 2),
                state="COMPRESSED_BOX",
                reason=f"Midday Consolidation Box: {span:.1f} pts span < {self.max_box_span} pts ({t_str} IST)",
            )
        elif span < 22.0:
            return ConsolidationStatus(
                is_consolidating=True,
                span_points=round(span, 2),
                atr=round(atr, 2),
                state="COMPRESSED_BOX",
                reason=f"Severe Compression: {span:.1f} pts span (ATR: {atr:.1f})",
            )
        elif atr >= 25.0:
            return ConsolidationStatus(
                is_consolidating=False,
                span_points=round(span, 2),
                atr=round(atr, 2),
                state="VOLATILITY_EXPANSION",
                reason=f"Volatile Expansion (ATR: {atr:.1f} pts)",
            )
        else:
            return ConsolidationStatus(
                is_consolidating=False,
                span_points=round(span, 2),
                atr=round(atr, 2),
                state="NORMAL_VOLATILITY",
                reason="Healthy directional volatility",
            )
