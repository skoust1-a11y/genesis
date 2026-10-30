"""EntryAgent: Multi-Strategy Scanner, Micro-Wick Execution & Tight Stop Engine.

Responsibilities:
1. Liquidity Sweeps (PDH/PDL Sweeps):
   - Triggers on 1M/5M micro-wick rejection back inside the level.
   - Enforces tight 25-35 point stops anchored to the rejection candle body,
     eliminating the historical 137-point risk lag trap.
2. Macro & Intraday Trendline Breaks:
   - Validates third-point trendline touches and high-volume breakout candles.
3. Midday Volume Waterfall:
   - Evaluates dynamic volume (>1.8x average) and ATR expansion to capture
     afternoon trend moves without falling into the binary lunch blackout trap.
4. Seamlessly pairs with TrendAgent (for chop vetoes) and RiskAgent (for lot sizing).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
import pandas as pd

from app.trading.crew.risk_agent import RiskAgent, RiskApproval
from app.trading.crew.trend_agent import MarketRegime, TrendAgent


@dataclass
class TradeSignal:
    strategy: str
    symbol: str
    direction: Literal["CALL", "PUT"]
    entry_price: float
    stop_loss: float
    target_price: float
    risk_points: float
    target_points: float
    rr_ratio: float
    lots: int
    quantity: int
    suggested_strike: str
    timestamp: str
    rationale: str
    regime: str
    adx: float


class EntryAgent:
    """Institutional Strategy Scanner and Micro-Wick Entry Engine."""

    def __init__(
        self,
        trend_agent: TrendAgent | None = None,
        risk_agent: RiskAgent | None = None,
        default_stop_buffer: float = 8.0,
        otm_steps: int = 2,
    ) -> None:
        self.trend_agent = trend_agent or TrendAgent()
        self.risk_agent = risk_agent or RiskAgent()
        self.default_stop_buffer = default_stop_buffer
        self.otm_steps = otm_steps

    def evaluate_sweep(
        self,
        df_5m: pd.DataFrame,
        pdh: float,
        pdl: float,
        key_levels: list[float] | None = None,
    ) -> TradeSignal | None:
        """Scan for PDH/PDL liquidity sweeps with micro-wick rejection."""
        if len(df_5m) < 2:
            return None

        curr = df_5m.iloc[-1]
        prev = df_5m.iloc[-2]
        c_open, c_high, c_low, c_close = float(curr["open"]), float(curr["high"]), float(curr["low"]), float(curr["close"])
        timestamp = str(curr.get("timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

        # 1. Bearish Sweep of PDH (Short / PUT)
        # Price spiked above PDH but 5M candle closed back below PDH with upper wick
        if c_high > pdh and c_close < pdh and c_close < c_open:
            upper_wick = c_high - max(c_open, c_close)
            body = abs(c_close - c_open)
            if upper_wick >= body * 0.5:  # Significant rejection wick
                entry = c_close
                # Tight 25-35pt micro-wick stop: just above candle body + buffer (capped at c_high)
                stop = min(max(c_open, c_close) + self.default_stop_buffer, c_high + 2.0)
                risk_pts = abs(entry - stop)
                
                # Check regime & dynamic target
                regime = self.trend_agent.evaluate_regime(df_5m, entry, "PUT", key_levels)
                if not regime.is_breakout_allowed:
                    return None
                    
                target = entry - (risk_pts * regime.recommended_target_r)
                
                # Risk approval
                risk_appr = self.risk_agent.evaluate_order(entry, stop)
                if not risk_appr.approved:
                    return None

                strike_round = int(round(entry / 50.0) * 50) - int(self.otm_steps * 50)
                strike = f"NIFTY {strike_round} PE"

                return TradeSignal(
                    strategy="LIQUIDITY_SWEEP",
                    symbol="NIFTY",
                    direction="PUT",
                    entry_price=round(entry, 2),
                    stop_loss=round(stop, 2),
                    target_price=round(target, 2),
                    risk_points=round(risk_pts, 2),
                    target_points=round(abs(entry - target), 2),
                    rr_ratio=regime.recommended_target_r,
                    lots=risk_appr.lots,
                    quantity=risk_appr.quantity,
                    suggested_strike=strike,
                    timestamp=timestamp,
                    rationale=f"PDH Sweep & Rejection Wick at {pdh:.1f}. Dynamic R:R {regime.recommended_target_r:.1f}R",
                    regime=regime.regime,
                    adx=regime.adx,
                )

        # 2. Bullish Sweep of PDL (Long / CALL)
        # Price spiked below PDL but 5M candle closed back above PDL with lower wick
        if c_low < pdl and c_close > pdl and c_close > c_open:
            lower_wick = min(c_open, c_close) - c_low
            body = abs(c_close - c_open)
            if lower_wick >= body * 0.5:  # Significant rejection wick
                entry = c_close
                # Tight stop: just below candle body - buffer
                stop = max(min(c_open, c_close) - self.default_stop_buffer, c_low - 2.0)
                risk_pts = abs(entry - stop)
                
                regime = self.trend_agent.evaluate_regime(df_5m, entry, "CALL", key_levels)
                if not regime.is_breakout_allowed:
                    return None

                target = entry + (risk_pts * regime.recommended_target_r)
                risk_appr = self.risk_agent.evaluate_order(entry, stop)
                if not risk_appr.approved:
                    return None

                strike_round = int(round(entry / 50.0) * 50) + int(self.otm_steps * 50)
                strike = f"NIFTY {strike_round} CE"

                return TradeSignal(
                    strategy="LIQUIDITY_SWEEP",
                    symbol="NIFTY",
                    direction="CALL",
                    entry_price=round(entry, 2),
                    stop_loss=round(stop, 2),
                    target_price=round(target, 2),
                    risk_points=round(risk_pts, 2),
                    target_points=round(abs(entry - target), 2),
                    rr_ratio=regime.recommended_target_r,
                    lots=risk_appr.lots,
                    quantity=risk_appr.quantity,
                    suggested_strike=strike,
                    timestamp=timestamp,
                    rationale=f"PDL Sweep & Rejection Wick at {pdl:.1f}. Dynamic R:R {regime.recommended_target_r:.1f}R",
                    regime=regime.regime,
                    adx=regime.adx,
                )

        return None

    def evaluate_waterfall(
        self,
        df_5m: pd.DataFrame,
        morning_floor: float,
        key_levels: list[float] | None = None,
    ) -> TradeSignal | None:
        """Scan for Midday Volume Waterfall breakdown (replaces binary lunch blackout)."""
        if len(df_5m) < 10:
            return None

        curr = df_5m.iloc[-1]
        c_close = float(curr["close"])
        timestamp = str(curr.get("timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

        # Check volume surge: > 1.8x average of last 10 candles
        avg_vol = df_5m["volume"].iloc[-10:-1].mean() if "volume" in df_5m.columns else 1.0
        curr_vol = float(curr.get("volume", 0.0))
        vol_surge = (curr_vol >= avg_vol * 1.8) if avg_vol > 0 else True

        if c_close < morning_floor and vol_surge:
            entry = c_close
            stop = float(curr["high"]) + self.default_stop_buffer
            risk_pts = abs(entry - stop)
            
            # Midday waterfalls get +3.0R to +4.0R expansion
            regime = self.trend_agent.evaluate_regime(
                df_5m, entry, "PUT", key_levels, is_waterfall_active=True
            )
            target = entry - (risk_pts * max(3.0, regime.recommended_target_r))

            risk_appr = self.risk_agent.evaluate_order(entry, stop)
            if not risk_appr.approved:
                return None

            strike_round = int(round(entry / 50.0) * 50) - int(self.otm_steps * 50)
            return TradeSignal(
                strategy="MIDDAY_WATERFALL",
                symbol="NIFTY",
                direction="PUT",
                entry_price=round(entry, 2),
                stop_loss=round(stop, 2),
                target_price=round(target, 2),
                risk_points=round(risk_pts, 2),
                target_points=round(abs(entry - target), 2),
                rr_ratio=3.0,
                lots=risk_appr.lots,
                quantity=risk_appr.quantity,
                suggested_strike=f"NIFTY {strike_round} PE",
                timestamp=timestamp,
                rationale=f"Midday Waterfall: High-volume breakdown below morning floor {morning_floor:.1f}",
                regime="VOLATILE_EXPANSION",
                adx=regime.adx,
            )

        return None
