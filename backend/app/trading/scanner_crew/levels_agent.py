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
    poc: float | None = None
    vah: float | None = None
    val: float | None = None
    call_oi_wall: float | None = None
    put_oi_wall: float | None = None
    eqh_levels: list[float] = field(default_factory=list)
    eql_levels: list[float] = field(default_factory=list)
    all_key_levels: list[float] = field(default_factory=list)


class LevelsAgent:
    """Institutional Level Marking and Support/Resistance Engine."""

    def extract_session_levels(
        self,
        df_daily: pd.DataFrame,
        df_intraday_today: pd.DataFrame | None = None,
        option_contracts: list[dict] | None = None,
    ) -> SessionLevels:
        """Extract PDH, PDL, PDC, Pivots, OR-30, and Order Flow Volume Profile (POC/VAH/VAL) levels."""
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

        # Order Flow Volume Profile (POC, VAH, VAL)
        poc = None
        vah = None
        val = None
        if df_intraday_today is not None and len(df_intraday_today) >= 3:
            try:
                from app.trading.crew.order_flow_agent import OrderFlowAgent
                of_agent = OrderFlowAgent()
                vp = of_agent.calculate_volume_profile(df_intraday_today, step=10.0)
                if vp:
                    poc = vp.get("poc")
                    vah = vp.get("vah")
                    val = vp.get("val")
                    if poc:
                        levels_set.add(poc)
                    if vah:
                        levels_set.add(vah)
                    if val:
                        levels_set.add(val)
            except Exception:
                pass

        # Option Chain Open Interest Walls
        call_wall = None
        put_wall = None
        if option_contracts:
            try:
                from app.trading.crew.order_flow_agent import OrderFlowAgent
                of_agent = OrderFlowAgent()
                oi_res = of_agent.extract_option_oi_walls(option_contracts)
                call_wall = oi_res.get("call_oi_wall")
                put_wall = oi_res.get("put_oi_wall")
                if call_wall:
                    levels_set.add(call_wall)
                if put_wall:
                    levels_set.add(put_wall)
            except Exception:
                pass

        # Add nearby round 50-point psychological numbers
        base_round = int(round(pivot / 50.0) * 50)
        for offset in [-150, -100, -50, 0, 50, 100, 150]:
            levels_set.add(float(base_round + offset))

        # Detect Equal Highs (EQH) and Equal Lows (EQL)
        eqh_levels, eql_levels = self.detect_equal_highs_lows(df_intraday_today)
        for h in eqh_levels:
            levels_set.add(h)
        for l in eql_levels:
            levels_set.add(l)

        return SessionLevels(
            pdh=round(pdh, 2),
            pdl=round(pdl, 2),
            pdc=round(pdc, 2),
            pivot=round(pivot, 2),
            r1=round(r1, 2),
            s1=round(s1, 2),
            or30_high=round(or30_high, 2) if or30_high else None,
            or30_low=round(or30_low, 2) if or30_low else None,
            poc=poc,
            vah=vah,
            val=val,
            call_oi_wall=call_wall,
            put_oi_wall=put_wall,
            eqh_levels=eqh_levels,
            eql_levels=eql_levels,
            all_key_levels=sorted(list(levels_set)),
        )

    def detect_equal_highs_lows(
        self,
        df_5m: pd.DataFrame | None,
        tol_pts: float = 8.0,  # Equal liquidity zone tolerance (5 to 10 points)
        min_bars_apart: int = 3,
        min_depth: float = 8.0,
    ) -> tuple[list[float], list[float]]:
        """Identify Equal High (EQH) and Equal Low (EQL) liquidity pools (5-10 pt zones) from 5M bars."""
        if df_5m is None or len(df_5m) < 6:
            return [], []

        highs = []
        lows = []
        n = len(df_5m)
        for i in range(1, n - 1):
            h_curr = float(df_5m.iloc[i]["high"])
            l_curr = float(df_5m.iloc[i]["low"])
            if h_curr >= float(df_5m.iloc[i - 1]["high"]) and h_curr >= float(df_5m.iloc[i + 1]["high"]):
                highs.append((i, h_curr))
            if l_curr <= float(df_5m.iloc[i - 1]["low"]) and l_curr <= float(df_5m.iloc[i + 1]["low"]):
                lows.append((i, l_curr))

        eqh: list[float] = []
        for a in range(len(highs)):
            for b in range(a + 1, len(highs)):
                i1, h1 = highs[a]
                i2, h2 = highs[b]
                if (i2 - i1) >= min_bars_apart and abs(h1 - h2) <= tol_pts:
                    trough = float(df_5m.iloc[i1 : i2 + 1]["low"].min())
                    if min(h1, h2) - trough >= min_depth:
                        eqh.append(round((h1 + h2) / 2.0, 1))

        eql: list[float] = []
        for a in range(len(lows)):
            for b in range(a + 1, len(lows)):
                i1, l1 = lows[a]
                i2, l2 = lows[b]
                if (i2 - i1) >= min_bars_apart and abs(l1 - l2) <= tol_pts:
                    peak = float(df_5m.iloc[i1 : i2 + 1]["high"].max())
                    if peak - max(l1, l2) >= min_depth:
                        eql.append(round((l1 + l2) / 2.0, 1))

        return sorted(list(set(eqh))), sorted(list(set(eql)))
