"""OrderFlowAgent: Institutional Order Flow, Volume Delta & Sizing Cap Engine.

Responsibilities:
1. Volume Delta & Cumulative Volume Delta (CVD) Estimation:
   - Measures aggressive market buyers vs aggressive market sellers.
   - Deconstructs candle bodies and rejection wicks to determine order-flow pressure.
2. Passive Limit Absorption Trap Detection:
   - Detects when institutions place large hidden limit sell orders into retail breakout buyers
     (divergence between price making new highs and negative/exhausted delta).
3. Dynamic Sizing Cap Governor:
   - Grade-A+ (Delta Surge >= 1.8x): Unlocks 100% permitted lots.
   - Grade-B (Neutral / Moderate Delta): Halves lot sizing (50% cap) to protect capital.
   - Absorption Trap: Sets sizing cap to 0 Lots (Hard Veto).
4. Staggered Execution Tranches for Whale Sizing (10 to 30 Lots):
   - Tranche 1 (33% initial fill): Tests order book depth without market impact.
   - Tranche 2 (67% confirmation fill): Deploys on aggressive delta follow-through.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal
import pandas as pd
import numpy as np

logger = logging.getLogger("genesis.order_flow_agent")


def _leg_oi(leg: Any) -> float:
    """Groww open interest is liveData.oi. openInterest on that object is empty."""
    if not isinstance(leg, dict):
        return 0.0
    live = leg.get("liveData") or {}
    raw = live.get("oi")
    if raw is None:
        raw = live.get("openInterest")
    if raw is None:
        raw = leg.get("open_interest")
    try:
        return float(raw or 0.0)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class OrderFlowSignal:
    volume_delta: float  # Estimated buyer volume minus seller volume
    delta_ratio: float   # Ratio of current bar delta to rolling 10-bar average
    cvd_trend: Literal["ACCELERATING_BULLISH", "ACCELERATING_BEARISH", "DIVERGENT", "NEUTRAL"]
    is_absorption_trap: bool
    absorption_side: Literal["SELLER_ABSORBING_BUYERS", "BUYER_ABSORBING_SELLERS", "NONE"]
    sizing_multiplier: float  # 1.0 (Full), 0.5 (Half), 0.0 (Veto)
    approved_lots: int
    execution_mode: Literal["SINGLE_FILL", "STAGGERED_TRANCHES"]
    tranche_1_lots: int
    tranche_2_lots: int
    rationale: str


class OrderFlowAgent:
    """Institutional Order Flow, Delta CVD and Whale Sizing Engine."""

    def __init__(self, delta_surge_threshold: float = 1.8, absorption_wick_ratio: float = 0.45) -> None:
        self.delta_surge_threshold = delta_surge_threshold
        self.absorption_wick_ratio = absorption_wick_ratio

    def estimate_candle_volume_delta(self, row: pd.Series | dict[str, Any]) -> float:
        """Estimate aggressive buyer vs seller volume delta from candle price action."""
        c_open = float(row.get("open", 0.0))
        c_high = float(row.get("high", 0.0))
        c_low = float(row.get("low", 0.0))
        c_close = float(row.get("close", 0.0))
        volume = float(row.get("volume", 1000.0))

        range_span = c_high - c_low
        if range_span <= 0:
            return 0.0

        # Close relative to range position (-1.0 to +1.0)
        # Close at high = +1.0 (100% buyer dominance); close at low = -1.0 (100% seller dominance)
        position_ratio = ((c_close - c_low) - (c_high - c_close)) / range_span
        
        # Estimate net volume delta
        volume_delta = volume * position_ratio
        return round(volume_delta, 2)

    def detect_absorption_trap(
        self,
        df_5m: pd.DataFrame,
        direction: Literal["CALL", "PUT"],
    ) -> tuple[bool, Literal["SELLER_ABSORBING_BUYERS", "BUYER_ABSORBING_SELLERS", "NONE"], str]:
        """Detect passive limit absorption where big money traps breakout retail traders."""
        if len(df_5m) < 3:
            return False, "NONE", "Normal tape"

        curr = df_5m.iloc[-1]
        c_open, c_high, c_low, c_close = float(curr["open"]), float(curr["high"]), float(curr["low"]), float(curr["close"])
        vol = float(curr.get("volume") or 0.0)
        has_volume = "volume" in df_5m.columns and (float(df_5m["volume"].sum()) > 0.0)
        avg_vol = float(df_5m["volume"].tail(10).mean()) if has_volume else 0.0
        # Volume 0 (Yahoo Nifty) is not a surge.
        vol_surge = bool(has_volume and avg_vol > 0 and vol > 0 and vol >= avg_vol * 1.3)

        # 1. Bullish Breakout Trap (Sellers absorbing buyers at highs)
        # Price made a high, but has heavy upper wick (>= 50% of candle range)
        if direction == "CALL":
            upper_wick = c_high - max(c_open, c_close)
            body = abs(c_close - c_open)
            total_range = c_high - c_low
            if total_range > 0 and upper_wick >= total_range * self.absorption_wick_ratio and vol_surge:
                vol_msg = f" on volume ({vol:,.0f})" if has_volume else ""
                return (
                    True,
                    "SELLER_ABSORBING_BUYERS",
                    f"Passive Absorption: Upper wick ({upper_wick:.1f} pts, {upper_wick/total_range*100:.0f}% of range){vol_msg} indicates institutional limit selling.",
                )

        # 2. Bearish Breakdown Trap (Buyers absorbing sellers at lows)
        # Price made a low, but has heavy lower wick (>= 50% of candle range)
        if direction == "PUT":
            lower_wick = min(c_open, c_close) - c_low
            body = abs(c_close - c_open)
            total_range = c_high - c_low
            if total_range > 0 and lower_wick >= total_range * self.absorption_wick_ratio and vol_surge:
                vol_msg = f" on volume ({vol:,.0f})" if has_volume else ""
                return (
                    True,
                    "BUYER_ABSORBING_SELLERS",
                    f"Passive Absorption: Lower wick ({lower_wick:.1f} pts, {lower_wick/total_range*100:.0f}% of range){vol_msg} indicates institutional limit buying.",
                )

        return False, "NONE", "Healthy order-flow structure"

    def evaluate_sizing_cap(
        self,
        df_5m: pd.DataFrame,
        direction: Literal["CALL", "PUT"],
        base_permitted_lots: int,
    ) -> OrderFlowSignal:
        """Govern dynamic sizing cap and tranche execution using Order Flow."""
        if len(df_5m) < 2:
            return OrderFlowSignal(
                volume_delta=0.0,
                delta_ratio=1.0,
                cvd_trend="NEUTRAL",
                is_absorption_trap=False,
                absorption_side="NONE",
                sizing_multiplier=1.0,
                approved_lots=base_permitted_lots,
                execution_mode="SINGLE_FILL",
                tranche_1_lots=base_permitted_lots,
                tranche_2_lots=0,
                rationale="Insufficient bars for order-flow analysis. Using base permitted lots.",
            )

        curr_bar = df_5m.iloc[-1]
        delta = self.estimate_candle_volume_delta(curr_bar)
        
        # Calculate rolling delta baseline
        recent_deltas = [self.estimate_candle_volume_delta(df_5m.iloc[i]) for i in range(max(0, len(df_5m) - 10), len(df_5m))]
        avg_abs_delta = max(1.0, float(np.mean([abs(d) for d in recent_deltas])))
        delta_ratio = abs(delta) / avg_abs_delta

        # Check absorption trap
        is_trap, trap_side, trap_reason = self.detect_absorption_trap(df_5m, direction)

        # Determine Sizing Multiplier and Approved Lots
        if is_trap:
            multiplier = 0.0
            approved_lots = 0
            rationale = f"TRAP VETO: {trap_reason} Sizing capped to 0 lots."
        elif (direction == "CALL" and delta > 0 and delta_ratio >= self.delta_surge_threshold) or \
             (direction == "PUT" and delta < 0 and delta_ratio >= self.delta_surge_threshold):
            multiplier = 1.0
            approved_lots = base_permitted_lots
            rationale = f"GRADE-A+ CONVICTION: Aggressive Delta surge ({delta_ratio:.1f}x average) confirms institutional initiative. Full 100% sizing ({approved_lots} lots) approved."
        else:
            multiplier = 0.5
            approved_lots = max(1, base_permitted_lots // 2)
            rationale = f"GRADE-B DEFENSIVE: Moderate Volume Delta ({delta_ratio:.1f}x average). Sizing capped at 50% ({approved_lots} lots) to limit drawdown."

        # Staggered Tranches for Whale Sizes (>= 10 Lots)
        # Protects against self-induced slippage
        if approved_lots >= 10:
            exec_mode = "STAGGERED_TRANCHES"
            tranche_1 = int(round(approved_lots * 0.35))
            tranche_2 = approved_lots - tranche_1
        else:
            exec_mode = "SINGLE_FILL"
            tranche_1 = approved_lots
            tranche_2 = 0

        cvd_trend = "ACCELERATING_BULLISH" if delta > 0 and delta_ratio >= 1.5 else (
            "ACCELERATING_BEARISH" if delta < 0 and delta_ratio >= 1.5 else "NEUTRAL"
        )

        return OrderFlowSignal(
            volume_delta=delta,
            delta_ratio=round(delta_ratio, 2),
            cvd_trend=cvd_trend,
            is_absorption_trap=is_trap,
            absorption_side=trap_side,
            sizing_multiplier=multiplier,
            approved_lots=approved_lots,
            execution_mode=exec_mode,
            tranche_1_lots=tranche_1,
            tranche_2_lots=tranche_2,
            rationale=rationale,
        )

    def calculate_volume_profile(
        self,
        df_intraday: pd.DataFrame,
        step: float = 10.0,
    ) -> dict[str, float]:
        """Compute Session Volume Profile: Point of Control (POC), Value Area High (VAH) & Value Area Low (VAL)."""
        if len(df_intraday) < 3 or "volume" not in df_intraday.columns:
            return {}

        price_bins: dict[float, float] = {}
        for _, row in df_intraday.iterrows():
            c_low = float(row["low"])
            c_high = float(row["high"])
            vol = float(row.get("volume", 0.0))
            if vol <= 0 or c_high <= c_low:
                continue

            bin_low = round(c_low / step) * step
            bin_high = round(c_high / step) * step
            num_bins = max(1, int(round((bin_high - bin_low) / step)) + 1)
            vol_per_bin = vol / num_bins

            curr_b = bin_low
            while curr_b <= bin_high + 0.1:
                price_bins[curr_b] = price_bins.get(curr_b, 0.0) + vol_per_bin
                curr_b += step

        if not price_bins:
            return {}

        # 1. Point of Control (POC): Price bin with peak volume
        poc = max(price_bins.items(), key=lambda x: x[1])[0]

        # 2. Value Area (VAH / VAL): 70% of total session volume
        total_vol = sum(price_bins.values())
        target_vol = total_vol * 0.70

        sorted_bins = sorted(price_bins.items(), key=lambda x: x[0])
        poc_idx = next(i for i, b in enumerate(sorted_bins) if b[0] == poc)

        accumulated_vol = sorted_bins[poc_idx][1]
        up_idx = poc_idx
        dn_idx = poc_idx

        while accumulated_vol < target_vol and (up_idx + 1 < len(sorted_bins) or dn_idx - 1 >= 0):
            next_up_vol = sorted_bins[up_idx + 1][1] if up_idx + 1 < len(sorted_bins) else 0.0
            next_dn_vol = sorted_bins[dn_idx - 1][1] if dn_idx - 1 >= 0 else 0.0

            if next_up_vol >= next_dn_vol and up_idx + 1 < len(sorted_bins):
                up_idx += 1
                accumulated_vol += next_up_vol
            elif dn_idx - 1 >= 0:
                dn_idx -= 1
                accumulated_vol += next_dn_vol
            else:
                break

        vah = sorted_bins[up_idx][0]
        val = sorted_bins[dn_idx][0]

        return {
            "poc": round(poc, 2),
            "vah": round(vah, 2),
            "val": round(val, 2),
        }

    def extract_option_oi_walls(
        self,
        contracts: list[dict[str, Any]],
    ) -> dict[str, float]:
        """Extract Option Chain Open Interest Walls (Max Call OI & Max Put OI) and live Put-Call Ratio."""
        if not contracts:
            return {}

        max_ce_oi = -1.0
        max_pe_oi = -1.0
        call_wall = None
        put_wall = None
        total_ce_oi = 0.0
        total_pe_oi = 0.0

        for r in contracts:
            strike = float(r.get("strike", r.get("strikePrice", 0.0)))
            if strike > 100000:
                strike = strike / 100.0  # normalize Groww paise

            ce_oi = _leg_oi(r.get("ce"))
            pe_oi = _leg_oi(r.get("pe"))

            total_ce_oi += ce_oi
            total_pe_oi += pe_oi

            if ce_oi > 0 and ce_oi > max_ce_oi:
                max_ce_oi = ce_oi
                call_wall = strike

            if pe_oi > 0 and pe_oi > max_pe_oi:
                max_pe_oi = pe_oi
                put_wall = strike

        pcr = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi > 0 else 1.0

        res: dict[str, float] = {"pcr": pcr}
        if call_wall is not None:
            res["call_oi_wall"] = call_wall
        if put_wall is not None:
            res["put_oi_wall"] = put_wall

        return res
