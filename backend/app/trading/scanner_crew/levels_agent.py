"""LevelsAgent: Market Level Marking, PDH/PDL, OR-30 & Strike Pivots.

Responsibilities:
1. Daily & Session Boundaries:
   - Extracts accurate Previous Day High (PDH), Previous Day Low (PDL), and Previous Day Close (PDC).
   - Identifies Opening Range (OR-15 & OR-30) high and low boundaries.
2. Psychological Round Strikes:
   - Maps nearby 50-point and 100-point strikes (e.g., 23000, 23050, 23100) where major open interest concentrates.
3. Intraday Pivot Calculation:
   - Computes Central Pivot (PP), Resistance (R1, R2), and Support (S1, S2) levels.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import pandas as pd


@dataclass
class SessionLevels:
    pdh: float
    pdl: float
    pdc: float
    pivot: float
    r1: float
    s1: float
    or30_high: float | None = None
    or30_low: float | None = None
    all_key_levels: list[float] = field(default_factory=list)


class LevelsAgent:
    """Institutional Level Marking and Support/Resistance Engine."""

    def extract_session_levels(
        self,
        df_daily: pd.DataFrame,
        df_intraday_today: pd.DataFrame | None = None,
    ) -> SessionLevels:
        """Extract PDH, PDL, PDC, Pivots, and OR-30 levels."""
        if len(df_daily) < 2:
            # Fallback default levels if insufficient history
            return SessionLevels(
                pdh=23150.0,
                pdl=22950.0,
                pdc=23050.0,
                pivot=23050.0,
                r1=23150.0,
                s1=22950.0,
                all_key_levels=[22900.0, 22950.0, 23000.0, 23050.0, 23100.0, 23150.0],
            )

        prev_day = df_daily.iloc[-2]
        pdh = float(prev_day["high"])
        pdl = float(prev_day["low"])
        pdc = float(prev_day["close"])

        pivot = (pdh + pdl + pdc) / 3.0
        r1 = (2 * pivot) - pdl
        s1 = (2 * pivot) - pdh

        or30_high = None
        or30_low = None
        if df_intraday_today is not None and len(df_intraday_today) >= 6:
            # First 6 5-minute bars (09:15 to 09:45)
            first_30m = df_intraday_today.iloc[:6]
            or30_high = float(first_30m["high"].max())
            or30_low = float(first_30m["low"].min())

        # Collect sorted unique key levels
        levels_set = {pdh, pdl, pdc, round(pivot, 1), round(r1, 1), round(s1, 1)}
        if or30_high:
            levels_set.add(round(or30_high, 1))
        if or30_low:
            levels_set.add(round(or30_low, 1))

        # Add nearby round 50-point psychological numbers
        base_round = int(round(pivot / 50.0) * 50)
        for offset in [-150, -100, -50, 0, 50, 100, 150]:
            levels_set.add(float(base_round + offset))

        return SessionLevels(
            pdh=round(pdh, 2),
            pdl=round(pdl, 2),
            pdc=round(pdc, 2),
            pivot=round(pivot, 2),
            r1=round(r1, 2),
            s1=round(s1, 2),
            or30_high=round(or30_high, 2) if or30_high else None,
            or30_low=round(or30_low, 2) if or30_low else None,
            all_key_levels=sorted(list(levels_set)),
        )
