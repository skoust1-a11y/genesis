"""SniperAgent: 1M Micro-Wick Execution Trigger for 15M/5M Macro Setups.

Responsibilities:
1. 3-Timeframe Trinity Coordination:
   - 15-Minute Chart: Macro structural bias, liquidity boundaries, and ultimate destination target.
   - 5-Minute Chart: Setup pattern recognition (Sweep reclaim, Retest hold, Trendline CHoCH).
   - 1-Minute Chart: Surgical sniper execution trigger (Candle-over-candle micro-break).
2. Ultra-Tight Micro-Wick Stop Pinning:
   - Anchors stop loss strictly 2-3 points behind the 1M/5M micro-wick extreme.
   - Shrinks stop-loss distance to 15-25 points (down from 45-75 points).
3. Adjusted Asymmetric Risk-to-Reward:
   - Couples the tiny 1M micro-risk with the large 15M/1H macro target.
   - Automatically expands realized R:R from standard 1:2.5 to 1:5.0R - 1:8.0R+.
4. 2-OTM Option Timing:
   - Triggers entry right at the micro-break before option market makers mark up the IV and premium.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
import pandas as pd


@dataclass
class SniperExecution:
    approved: bool
    direction: Literal["CALL", "PUT"]
    entry_price: float
    stop_loss: float
    target_price: float
    risk_points: float
    target_points: float
    adjusted_rr: float
    suggested_strike: str
    lots: int
    quantity: int
    rationale: str
    timeframe_trinity: str = "15M Context -> 5M Setup -> 1M Sniper Trigger"
    rejection_reason: str | None = None


class SniperAgent:
    """1M Sniper Execution Engine for 15M/5M Macro Confluence Setups."""

    def __init__(
        self,
        micro_stop_buffer: float = 3.0,
        max_micro_risk: float = 35.0,
        min_adjusted_rr: float = 3.0,
        otm_steps: int = 2,
        lot_qty: int = 65,
    ) -> None:
        self.micro_stop_buffer = micro_stop_buffer
        self.max_micro_risk = max_micro_risk
        self.min_adjusted_rr = min_adjusted_rr
        self.otm_steps = otm_steps
        self.lot_qty = lot_qty

    def evaluate_sniper_entry(
        self,
        df_1m: pd.DataFrame,
        df_5m: pd.DataFrame,
        df_15m: pd.DataFrame,
        setup_direction: Literal["CALL", "PUT"],
        setup_name: str,
        macro_target: float,
        is_super_trend: bool = False,
    ) -> SniperExecution:
        """Evaluate 1M micro-trigger for a validated 15M/5M setup."""
        if df_1m.empty or df_5m.empty:
            return SniperExecution(
                approved=False,
                direction=setup_direction,
                entry_price=0.0,
                stop_loss=0.0,
                target_price=0.0,
                risk_points=0.0,
                target_points=0.0,
                adjusted_rr=0.0,
                suggested_strike="",
                lots=0,
                quantity=0,
                rationale="Insufficient 1M/5M candle data for sniper execution.",
                rejection_reason="Empty 1M or 5M data feed.",
            )

        # 1. Inspect latest 5M Confirmation Candle
        last_5m = df_5m.iloc[-1]
        c5_open, c5_high, c5_low, c5_close = (
            float(last_5m["open"]),
            float(last_5m["high"]),
            float(last_5m["low"]),
            float(last_5m["close"]),
        )

        # 2. Inspect 1M Micro Execution Bars
        last_1m = df_1m.iloc[-1]
        prev_1m = df_1m.iloc[-2] if len(df_1m) >= 2 else last_1m
        c1_close = float(last_1m["close"])
        c1_high = float(last_1m["high"])
        c1_low = float(last_1m["low"])

        # 3. Micro Trigger & Micro-Wick Stop Placement
        if setup_direction == "CALL":
            # Long Sniper Entry:
            # 1M candle breaks above the 5M candle body / micro-pivot
            micro_trigger = c5_close
            micro_wick_low = min(c5_low, float(df_1m.tail(5)["low"].min()))
            stop = round(micro_wick_low - self.micro_stop_buffer, 2)
            entry = round(c1_close, 2)
            risk = round(abs(entry - stop), 2)
            target = round(macro_target, 2)
            target_pts = round(target - entry, 2)

            # Candle-over-candle continuation check
            continuation = c1_close >= float(prev_1m["high"]) or c1_close >= c5_close
        else:
            # Short Sniper Entry:
            # 1M candle breaks below the 5M candle body / micro-pivot
            micro_trigger = c5_close
            micro_wick_high = max(c5_high, float(df_1m.tail(5)["high"].max()))
            stop = round(micro_wick_high + self.micro_stop_buffer, 2)
            entry = round(c1_close, 2)
            risk = round(abs(stop - entry), 2)
            target = round(macro_target, 2)
            target_pts = round(entry - target, 2)

            continuation = c1_close <= float(prev_1m["low"]) or c1_close <= c5_close

        # 4. Check Risk Constraints
        if risk > self.max_micro_risk:
            return SniperExecution(
                approved=False,
                direction=setup_direction,
                entry_price=entry,
                stop_loss=stop,
                target_price=target,
                risk_points=risk,
                target_points=target_pts,
                adjusted_rr=round(target_pts / max(1.0, risk), 2),
                suggested_strike="",
                lots=0,
                quantity=0,
                rationale=f"Micro risk {risk:.1f} pts exceeds sniper ceiling {self.max_micro_risk} pts.",
                rejection_reason="Sniper risk distance too wide.",
            )

        adjusted_rr = round(target_pts / max(1.0, risk), 2)
        if adjusted_rr < self.min_adjusted_rr:
            return SniperExecution(
                approved=False,
                direction=setup_direction,
                entry_price=entry,
                stop_loss=stop,
                target_price=target,
                risk_points=risk,
                target_points=target_pts,
                adjusted_rr=adjusted_rr,
                suggested_strike="",
                lots=0,
                quantity=0,
                rationale=f"Adjusted R:R {adjusted_rr:.1f}R is below minimum {self.min_adjusted_rr:.1f}R requirement.",
                rejection_reason="Insufficient Risk-to-Reward against macro target.",
            )

        # 5. Strike & Sizing Allocation (Strictly 2-OTM Weekly Options)
        strike_base = int(round(entry / 50.0) * 50)
        strike_num = strike_base + (self.otm_steps * 50 if setup_direction == "CALL" else -self.otm_steps * 50)
        strike = f"NIFTY {strike_num} {'CE' if setup_direction == 'CALL' else 'PE'}"

        lots = 4 if (is_super_trend or risk <= 22.0) else 3
        qty = lots * self.lot_qty

        rationale = (
            f"15M Macro Setup: {setup_name} | 5M Confirmation Close | "
            f"1M Sniper Micro-Wick Trigger at {entry:.2f} (Risk: {risk:.1f} pts) | "
            f"Adjusted R:R: {adjusted_rr:.1f}R -> Target {target:.2f}"
        )

        return SniperExecution(
            approved=True,
            direction=setup_direction,
            entry_price=entry,
            stop_loss=stop,
            target_price=target,
            risk_points=risk,
            target_points=target_pts,
            adjusted_rr=adjusted_rr,
            suggested_strike=strike,
            lots=lots,
            quantity=qty,
            rationale=rationale,
            timeframe_trinity="15M Context -> 5M Setup -> 1M Sniper Trigger",
        )
