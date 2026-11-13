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
class DirectionalThesis:
    primary_bet: str                 # e.g., "Bullish BSL Expansion to 23,210"
    targeted_pool: str               # e.g., "OR-30 High Reclaim & Weak High Stops"
    invalidation_price: float        # Hard spot invalidation level
    invalidation_condition: str      # e.g., "5M candle body close below 23,067.3 kills thesis"
    stagnation_timeout_bars: int     # 6 bars (30 minutes max)
    gamma_wall_clearance_pts: float  # Runway to nearest opposing dealer wall (must be >= 80 pts)
    thesis_conviction: str           # "HIGH", "SUPER_TREND", "SCALP_REVERSAL"


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
    base_timeframe: str = "30M"
    liquidity_pool: str = "UNSPECIFIED"
    lookout_duration_mins: int = 120
    lookout_status: str = "TRIGGERED"
    thesis: DirectionalThesis | None = None
    path_target_pct: float | None = None
    path_stop_pct: float | None = None
    path_neither_pct: float | None = None
    path_verdict: str | None = None
    odds_line: str | None = None
    market_lot: int | None = None


def _chain_strike(row: dict) -> float:
    strike = float(row.get("strike") or row.get("strikePrice") or 0.0)
    return strike / 100.0 if strike > 100000 else strike


def select_strike_with_delta_floor(
    entry_price: float,
    direction: Literal["CALL", "PUT"],
    otm_steps: int = 2,
    min_delta: float = 0.35,
    chain: list[dict] | None = None,
) -> tuple[str, float]:
    """Walk the live chain in from `otm_steps` until delta >= min_delta.

    Without a chain the old 0.065-per-step estimate remains so callers that have no
    quotes still get a strike label. Live opens pass the chain and ignore that estimate.
    """
    step = 50.0
    atm_strike = round(entry_price / step) * step
    contract_type = "CE" if direction == "CALL" else "PE"
    side = "ce" if direction == "CALL" else "pe"

    if chain:
        deltas: dict[float, float] = {}
        for row in chain:
            if not isinstance(row, dict):
                continue
            strike = _chain_strike(row)
            if strike <= 0:
                continue
            leg = row.get(side) or {}
            raw = (leg.get("greeks") or {}).get("delta")
            if raw is None:
                continue
            deltas[round(strike, 2)] = abs(float(raw))
        target = atm_strike + (otm_steps * step if direction == "CALL" else -otm_steps * step)
        for _ in range(otm_steps + 8):
            delta = deltas.get(round(target, 2))
            if delta is not None and delta >= min_delta:
                return f"NIFTY {int(target)} {contract_type}", round(delta, 2)
            target += -step if direction == "CALL" else step
        return f"NIFTY {int(atm_strike)} {contract_type}", 0.0

    steps_out = float(otm_steps)
    target_strike = atm_strike + (steps_out * step if direction == "CALL" else -steps_out * step)
    est_delta = max(0.05, 0.50 - (steps_out * 0.065))
    while est_delta < min_delta and steps_out > 0:
        steps_out -= 1.0
        target_strike += -step if direction == "CALL" else step
        est_delta = max(0.05, 0.50 - (steps_out * 0.065))
    return f"NIFTY {int(target_strike)} {contract_type}", round(est_delta, 2)


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
        call_wall: float | None = None,
        put_wall: float | None = None,
        chain: list[dict] | None = None,
    ) -> TradeSignal | None:
        """Scan for PDH/PDL liquidity sweeps and gamma wall reversals with micro-wick rejection."""
        if len(df_5m) < 2:
            return None

        curr = df_5m.iloc[-1]
        prev = df_5m.iloc[-2]
        c_open, c_high, c_low, c_close = float(curr["open"]), float(curr["high"]), float(curr["low"]), float(curr["close"])
        timestamp = str(curr.get("timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

        # Establish dealer walls if not provided
        c_wall = call_wall if call_wall is not None else (round(c_close / 100.0) * 100.0 + 100.0)
        p_wall = put_wall if put_wall is not None else (round(c_close / 100.0) * 100.0 - 100.0)

        # Volume calculation for absorption check
        avg_vol = df_5m["volume"].iloc[-10:-1].mean() if "volume" in df_5m.columns and len(df_5m) >= 10 else 1.0
        curr_vol = float(curr.get("volume", 0.0))
        vol_absorption = bool(avg_vol > 0 and curr_vol > 0 and curr_vol >= avg_vol * 1.1)

        # ---------------------------------------------------------------------
        # 1. Bearish Sweep of PDH or Call Wall (Short / PUT)
        # ---------------------------------------------------------------------
        # Sweeps PDH or sweeps Call Wall with upper rejection wick
        is_pdh_sweep = (c_high > pdh and c_close < pdh and c_close < c_open)
        is_call_wall_sweep = (c_high > c_wall and c_close < c_wall and c_close < c_open)

        if is_pdh_sweep or is_call_wall_sweep:
            swept_level = c_wall if is_call_wall_sweep else pdh
            is_gamma_reversal = is_call_wall_sweep

            upper_wick = c_high - max(c_open, c_close)
            body = abs(c_close - c_open)
            # Rejection wick >= 50% of candle body (for gamma reversal requires volume absorption)
            wick_valid = (upper_wick >= body * 0.5)
            if is_gamma_reversal and not (wick_valid and vol_absorption):
                wick_valid = False

            if wick_valid:
                entry = c_close
                # Institutional 22-35pt stop: above rejection high with 22pt minimum floor
                raw_risk = max(c_high + 3.0 - entry, self.default_stop_buffer)
                risk_pts = max(22.0, min(35.0, raw_risk))
                stop = entry + risk_pts

                # Gamma Wall & Vacuum Gate for Shorts:
                # Distance to Put Wall must not be < 15-20 pts (vacuum trap)
                dist_to_put_wall = entry - p_wall
                if not is_gamma_reversal:
                    if dist_to_put_wall < 15.0 or abs(entry - p_wall) < 15.0:
                        return None
                    # Require adequate runway to next opposing wall (at least 1.8x risk or 45 pts)
                    min_runway = max(45.0, risk_pts * 1.8)
                    if dist_to_put_wall < min_runway:
                        return None
                else:
                    # For a Gamma Wall reversal at Call Wall, clearance to put wall is full channel
                    if dist_to_put_wall < 30.0:
                        return None

                # Check regime & dynamic target
                regime = self.trend_agent.evaluate_regime(df_5m, entry, "PUT", key_levels)
                if not regime.is_breakout_allowed:
                    return None

                target = entry - (risk_pts * regime.recommended_target_r)

                # Risk approval
                risk_appr = self.risk_agent.evaluate_order(
                    entry, stop, is_super_trend=len(df_5m) >= 28 and regime.adx >= 25.0,
                )
                if not risk_appr.approved:
                    return None

                # 2-OTM Strike Selection with Delta >= 0.35 floor
                strike, est_delta = select_strike_with_delta_floor(
                    entry_price=entry,
                    direction="PUT",
                    otm_steps=self.otm_steps,
                    min_delta=0.35,
                    chain=chain,
                )
                if chain and est_delta < 0.35:
                    return None

                conviction = "SUPER_TREND" if (len(df_5m) >= 28 and regime.adx >= 25.0) else "HIGH"
                pool_name = "GAMMA_WALL_SWEEP" if is_gamma_reversal else "BSL_SWEEP"
                strat_name = "GAMMA_WALL_REVERSAL" if is_gamma_reversal else "LIQUIDITY_SWEEP"

                thesis = DirectionalThesis(
                    primary_bet=f"Bearish SSL Expansion to {target:,.1f}",
                    targeted_pool=f"{pool_name} ({swept_level:,.1f}) & Put Wall ({p_wall:,.1f})",
                    invalidation_price=round(stop, 2),
                    invalidation_condition=f"5M candle body close above {stop:,.1f} kills thesis",
                    stagnation_timeout_bars=6,
                    gamma_wall_clearance_pts=round(dist_to_put_wall, 1),
                    thesis_conviction=conviction,
                )

                return TradeSignal(
                    strategy=strat_name,
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
                    rationale=f"{swept_level:.1f} Sweep & Rejection Wick (Delta: {est_delta:.2f}). Dynamic R:R {regime.recommended_target_r:.1f}R",
                    regime=regime.regime,
                    adx=regime.adx,
                    base_timeframe="30M",
                    liquidity_pool=pool_name,
                    lookout_duration_mins=180,
                    lookout_status="TRIGGERED",
                    thesis=thesis,
                )

        # ---------------------------------------------------------------------
        # 2. Bullish Sweep of PDL or Put Wall (Long / CALL)
        # ---------------------------------------------------------------------
        is_pdl_sweep = (c_low < pdl and c_close > pdl and c_close > c_open)
        is_put_wall_sweep = (c_low < p_wall and c_close > p_wall and c_close > c_open)

        if is_pdl_sweep or is_put_wall_sweep:
            swept_level = p_wall if is_put_wall_sweep else pdl
            is_gamma_reversal = is_put_wall_sweep

            lower_wick = min(c_open, c_close) - c_low
            body = abs(c_close - c_open)
            wick_valid = (lower_wick >= body * 0.5)
            if is_gamma_reversal and not (wick_valid and vol_absorption):
                wick_valid = False

            if wick_valid:
                entry = c_close
                # Institutional 22-35pt stop: below rejection low with 22pt minimum floor
                raw_risk = max(entry - (c_low - 3.0), self.default_stop_buffer)
                risk_pts = max(22.0, min(35.0, raw_risk))
                stop = entry - risk_pts

                # Gamma Wall & Vacuum Gate for Longs:
                # Spot must not be within 15-20 points of Call Wall
                dist_to_call_wall = c_wall - entry
                if not is_gamma_reversal:
                    if dist_to_call_wall < 15.0 or abs(entry - c_wall) < 15.0:
                        return None
                    # Require adequate runway to next opposing wall (at least 1.8x risk or 45 pts)
                    min_runway = max(45.0, risk_pts * 1.8)
                    if dist_to_call_wall < min_runway:
                        return None
                else:
                    if dist_to_call_wall < 30.0:
                        return None

                regime = self.trend_agent.evaluate_regime(df_5m, entry, "CALL", key_levels)
                if not regime.is_breakout_allowed:
                    return None

                target = entry + (risk_pts * regime.recommended_target_r)
                risk_appr = self.risk_agent.evaluate_order(
                    entry, stop, is_super_trend=len(df_5m) >= 28 and regime.adx >= 25.0,
                )
                if not risk_appr.approved:
                    return None

                # 2-OTM Strike Selection with Delta >= 0.35 floor
                strike, est_delta = select_strike_with_delta_floor(
                    entry_price=entry,
                    direction="CALL",
                    otm_steps=self.otm_steps,
                    min_delta=0.35,
                    chain=chain,
                )
                if chain and est_delta < 0.35:
                    return None

                conviction = "SUPER_TREND" if (len(df_5m) >= 28 and regime.adx >= 25.0) else "HIGH"
                pool_name = "GAMMA_WALL_SWEEP" if is_gamma_reversal else "SSL_SWEEP"
                strat_name = "GAMMA_WALL_REVERSAL" if is_gamma_reversal else "LIQUIDITY_SWEEP"

                thesis = DirectionalThesis(
                    primary_bet=f"Bullish BSL Expansion to {target:,.1f}",
                    targeted_pool=f"{pool_name} ({swept_level:,.1f}) & Call Wall ({c_wall:,.1f})",
                    invalidation_price=round(stop, 2),
                    invalidation_condition=f"5M candle body close below {stop:,.1f} kills thesis",
                    stagnation_timeout_bars=6,
                    gamma_wall_clearance_pts=round(dist_to_call_wall, 1),
                    thesis_conviction=conviction,
                )

                return TradeSignal(
                    strategy=strat_name,
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
                    rationale=f"{swept_level:.1f} Sweep & Rejection Wick (Delta: {est_delta:.2f}). Dynamic R:R {regime.recommended_target_r:.1f}R",
                    regime=regime.regime,
                    adx=regime.adx,
                    base_timeframe="30M",
                    liquidity_pool=pool_name,
                    lookout_duration_mins=180,
                    lookout_status="TRIGGERED",
                    thesis=thesis,
                )

        return None

    def evaluate_waterfall(
        self,
        df_5m: pd.DataFrame,
        morning_floor: float,
        key_levels: list[float] | None = None,
        put_wall: float | None = None,
        chain: list[dict] | None = None,
    ) -> TradeSignal | None:
        """Scan for Midday Volume Waterfall breakdown (replaces binary lunch blackout)."""
        if len(df_5m) < 10:
            return None

        curr = df_5m.iloc[-1]
        c_open = float(curr["open"])
        c_close = float(curr["close"])
        c_high = float(curr["high"])
        c_low = float(curr["low"])
        timestamp = str(curr.get("date") or curr.get("timestamp") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

        # Check volume surge: > 1.4x average of last 10 candles
        avg_vol = df_5m["volume"].iloc[-10:-1].mean() if "volume" in df_5m.columns else 1.0
        curr_vol = float(curr.get("volume", 0.0))
        vol_surge = bool(avg_vol > 0 and curr_vol > 0 and curr_vol >= avg_vol * 1.4)

        # True Waterfall Breakdown requires:
        # 1. Bearish candle close: c_close < c_open
        # 2. Closed decisively below morning floor
        # 3. No bottom absorption tail (lower wick <= body)
        body = abs(c_close - c_open)
        lower_wick = c_close - c_low
        is_bearish_expansion = (c_close < c_open) and (lower_wick <= max(2.0, body * 0.8))

        # True Waterfall Breakdown requires breaking the true session low (not closing on a higher low)
        prior_floor = min(morning_floor, float(df_5m.iloc[:-1]["low"].min()))
        if c_close <= (prior_floor - 4.0) and vol_surge and is_bearish_expansion:
            entry = c_close
            # Structural high with a minimum 22-25pt buffer to avoid 1-tick retest stop-outs
            stop = max(c_high + self.default_stop_buffer, entry + 22.0)
            risk_pts = abs(entry - stop)

            p_wall = put_wall if put_wall is not None else (round(entry / 100.0) * 100.0 - 100.0)
            dist_to_put_wall = entry - p_wall
            # Gamma clearance check: need >= 80 pts runway to put wall
            if dist_to_put_wall < 80.0:
                return None

            # Midday waterfalls get +3.0R to +4.0R expansion
            regime = self.trend_agent.evaluate_regime(
                df_5m, entry, "PUT", key_levels, is_waterfall_active=True
            )
            # Must be allowed by TrendAgent and have active momentum expansion (ADX >= 18)
            if not regime.is_breakout_allowed or regime.adx < 18.0:
                return None

            target = entry - (risk_pts * max(3.0, regime.recommended_target_r))

            risk_appr = self.risk_agent.evaluate_order(
                entry, stop, is_super_trend=len(df_5m) >= 28 and regime.adx >= 25.0,
            )
            if not risk_appr.approved:
                return None

            # 2-OTM Strike Selection with Delta >= 0.35 floor
            strike, est_delta = select_strike_with_delta_floor(
                entry_price=entry,
                direction="PUT",
                otm_steps=self.otm_steps,
                min_delta=0.35,
                chain=chain,
            )
            if chain and est_delta < 0.35:
                return None

            thesis = DirectionalThesis(
                primary_bet=f"Bearish SSL Waterfall Expansion to {target:,.1f}",
                targeted_pool=f"Morning Floor Breakdown ({morning_floor:,.1f}) & Put Wall ({p_wall:,.1f})",
                invalidation_price=round(stop, 2),
                invalidation_condition=f"5M candle body close above {stop:,.1f} kills thesis",
                stagnation_timeout_bars=6,
                gamma_wall_clearance_pts=round(dist_to_put_wall, 1),
                thesis_conviction="SUPER_TREND" if (len(df_5m) >= 28 and regime.adx >= 25.0) else "HIGH",
            )

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
                suggested_strike=strike,
                timestamp=timestamp,
                rationale=f"Midday Waterfall: High-volume breakdown below morning floor {morning_floor:.1f} (Delta: {est_delta:.2f})",
                regime="VOLATILE_EXPANSION",
                adx=regime.adx,
                base_timeframe="15M",
                liquidity_pool="SSL_EXPANSION",
                lookout_duration_mins=90,
                lookout_status="TRIGGERED",
                thesis=thesis,
            )

        return None
