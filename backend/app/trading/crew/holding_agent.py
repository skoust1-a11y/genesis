"""Genesis HoldingAgent: Active Position Lifecycle, Thesis Health & Trap Reversal Engine.

Institutional position supervisor that bridges EntryAgent and ExitAgent:
- Actively monitors open position health, momentum acceleration, and volume absorption.
- Classifies shallow pullbacks as Healthy Retests and grants dynamic cushions (preventing premature flat-cost choking).
- Identifies Thesis Invalidation (Bull/Bear Traps) to execute Preemptive Damage Control (-0.3R to -0.4R vs full -1.0R SL).
- Emits immediate Trap Reversal Flips to exploit cascading trapped liquidity.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
import logging
import pandas as pd

from app.trading.scanner_crew.detector_agent import CandidateSetup

logger = logging.getLogger("genesis.trading.holding_agent")


def compute_session_vwap(df: pd.DataFrame | None) -> float | None:
    """Calculate volume-weighted average price (VWAP) across intraday session bars."""
    if df is None or len(df) == 0 or "volume" not in df.columns:
        return None
    try:
        tp = (df["high"] + df["low"] + df["close"]) / 3.0
        vol = df["volume"]
        cum_vol = vol.cumsum()
        cum_pv = (tp * vol).cumsum()
        if float(cum_vol.iloc[-1]) > 0:
            return float(cum_pv.iloc[-1] / cum_vol.iloc[-1])
        # Yahoo Nifty volume is 0. Typical price is a price fallback, not a volume surge.
        return float(tp.mean())
    except Exception:
        pass
    return None


@dataclass
class HoldingDecision:
    """Diagnostic verdict and action directive for an active trade."""
    state: Literal[
        "HEALTHY_EXPANSION",
        "HEALTHY_RETEST",
        "CONSOLIDATION_FLAG",
        "MOMENTUM_EXHAUSTION",
        "THESIS_INVALIDATED",
    ]
    action: Literal[
        "HOLD",
        "PREEMPTIVE_PROFIT",
        "PREEMPTIVE_EXIT",
        "FLIP_AND_REVERSE",
    ]
    reversal_setup: CandidateSetup | None = None
    cushion_sl: float | None = None
    ratcheted_sl: float | None = None
    rationale: str = ""


class HoldingAgent:
    """Specialized Active Position Supervisor & Invalidation Reversal Engine."""

    def __init__(
        self,
        retest_cushion_pts: float = 8.0,
        preemptive_loss_cutoff_r: float = -0.35,
        trap_range_min_pts: float = 12.0,
    ) -> None:
        self.retest_cushion_pts = retest_cushion_pts
        self.preemptive_loss_cutoff_r = preemptive_loss_cutoff_r
        self.trap_range_min_pts = trap_range_min_pts

    def evaluate_holding(
        self,
        active_trade: dict[str, Any],
        cmp: float,
        df_5m: pd.DataFrame | None = None,
        bars_in_trade: int = 0,
        current_time_str: str | None = None,
        key_levels: list[float] | None = None,
        daily_flips_count: int = 0,
        max_daily_flips: int = 1,
        df_15m: pd.DataFrame | None = None,
        call_wall: float | None = None,
        put_wall: float | None = None,
        india_vix: float | None = None,
        order_flow_signal: Any | None = None,
    ) -> HoldingDecision:
        """Inspect active trade mechanics, assess thesis health, and direct position actions."""
        direction: Literal["CALL", "PUT"] = active_trade.get("direction", "CALL")
        entry_price = float(active_trade.get("entry_price", cmp))
        stop_loss = float(active_trade.get("stop_loss", cmp))
        target_price = float(active_trade.get("target_price", cmp))
        risk_pts = max(1.0, float(active_trade.get("risk_points", abs(entry_price - stop_loss))))
        strategy = str(active_trade.get("strategy", ""))
        be_locked = bool(active_trade.get("be_locked", False))

        # Realized move in index points and R-multiple
        current_move = (cmp - entry_price) if direction == "CALL" else (entry_price - cmp)
        current_r = round(current_move / risk_pts, 2)

        # Dynamic VIX correlation adjustment
        effective_cushion = self.retest_cushion_pts
        if india_vix is not None:
            if india_vix > 16.0:
                effective_cushion = 10.0
            elif india_vix < 12.0:
                effective_cushion = 6.0

        # ---------------------------------------------------------------------
        # 1. Multi-Timeframe (15M) Structural Confluence Correlation
        # ---------------------------------------------------------------------
        # If in adverse territory (current_r <= -0.30) and 15M candle broke opposite structure
        if df_15m is not None and len(df_15m) >= 2 and bars_in_trade >= 2 and current_r <= -0.30:
            last_15m = df_15m.iloc[-1]
            prev_15m = df_15m.iloc[-2]
            c15_close = float(last_15m["close"])
            c15_open = float(last_15m["open"])
            p15_low = float(prev_15m["low"])
            p15_high = float(prev_15m["high"])

            if direction == "CALL" and c15_close < p15_low and c15_close < c15_open:
                return HoldingDecision(
                    state="THESIS_INVALIDATED",
                    action="PREEMPTIVE_EXIT",
                    rationale=f"15M Structural Breakdown: Candle body closed below prior 15M low ({c15_close:.1f} < {p15_low:.1f}). Cutting risk early at {current_r:.2f}R.",
                )
            elif direction == "PUT" and c15_close > p15_high and c15_close > c15_open:
                return HoldingDecision(
                    state="THESIS_INVALIDATED",
                    action="PREEMPTIVE_EXIT",
                    rationale=f"15M Structural Breakout: Candle body closed above prior 15M high ({c15_close:.1f} > {p15_high:.1f}). Cutting risk early at {current_r:.2f}R.",
                )

        # ---------------------------------------------------------------------
        # 2. Gamma Wall Proximity Correlation Check
        # ---------------------------------------------------------------------
        # If in profit (current_r >= 1.0) and testing Gamma Wall (Call Wall / Put Wall)
        if df_5m is not None and len(df_5m) >= 2 and current_r >= 1.0:
            curr_bar = df_5m.iloc[-1]
            c_high = float(curr_bar["high"])
            c_low = float(curr_bar["low"])
            c_open = float(curr_bar["open"])
            c_close = float(curr_bar["close"])
            c_range = max(1.0, c_high - c_low)

            if direction == "CALL" and call_wall is not None and abs(c_high - call_wall) <= 15.0:
                if (c_close < c_open) and ((c_high - max(c_open, c_close)) >= c_range * 0.35):
                    return HoldingDecision(
                        state="MOMENTUM_EXHAUSTION",
                        action="PREEMPTIVE_PROFIT",
                        rationale=f"Gamma Call Wall Defended ({call_wall:.0f}): Dealers absorbing call gamma. Securing +{current_r:.2f}R profit before dealer hedging slap-down.",
                    )
            elif direction == "PUT" and put_wall is not None and abs(c_low - put_wall) <= 15.0:
                if (c_close > c_open) and ((min(c_open, c_close) - c_low) >= c_range * 0.35):
                    return HoldingDecision(
                        state="MOMENTUM_EXHAUSTION",
                        action="PREEMPTIVE_PROFIT",
                        rationale=f"Gamma Put Wall Defended ({put_wall:.0f}): Put delta floor defended. Securing +{current_r:.2f}R profit before mean reversion.",
                    )

        # ---------------------------------------------------------------------
        # 3. Order Flow CVD Absorption Divergence Correlation
        # ---------------------------------------------------------------------
        if order_flow_signal is not None and current_r >= 1.0:
            if direction == "CALL" and getattr(order_flow_signal, "absorption_side", "") == "SELLER_ABSORBING_BUYERS":
                return HoldingDecision(
                    state="MOMENTUM_EXHAUSTION",
                    action="PREEMPTIVE_PROFIT",
                    rationale=f"Order Flow Divergence: Institutional limit sellers absorbing aggressive buyers at highs. Securing +{current_r:.2f}R profit.",
                )
            elif direction == "PUT" and getattr(order_flow_signal, "absorption_side", "") == "BUYER_ABSORBING_SELLERS":
                return HoldingDecision(
                    state="MOMENTUM_EXHAUSTION",
                    action="PREEMPTIVE_PROFIT",
                    rationale=f"Order Flow Divergence: Institutional limit buyers absorbing aggressive sellers at lows. Securing +{current_r:.2f}R profit.",
                )

        vwap = compute_session_vwap(df_5m)

        # ---------------------------------------------------------------------
        # 4. Opposing Key Level / EQH / EQL Denial (Preemptive Profit Locking)
        # ---------------------------------------------------------------------
        # If in healthy profit (>= 1.0R) and price is rejected at an opposing structural ceiling/floor
        if df_5m is not None and len(df_5m) >= 3 and current_r >= 1.0 and key_levels:
            curr_bar = df_5m.iloc[-1]
            prev_bar = df_5m.iloc[-2]
            c_high = float(curr_bar["high"])
            c_low = float(curr_bar["low"])
            c_open = float(curr_bar["open"])
            c_close = float(curr_bar["close"])
            c_range = max(1.0, c_high - c_low)

            if direction == "CALL":
                probed_res = any(abs(c_high - lvl) <= 10.0 or abs(float(prev_bar["high"]) - lvl) <= 10.0 for lvl in key_levels if lvl > entry_price)
                if probed_res:
                    is_red_rejection = (c_close < c_open) and ((c_high - max(c_open, c_close)) >= c_range * 0.35)
                    failed_continuation = c_close < float(prev_bar["low"])
                    if is_red_rejection or failed_continuation:
                        return HoldingDecision(
                            state="MOMENTUM_EXHAUSTION",
                            action="PREEMPTIVE_PROFIT",
                            rationale=f"Opposing Resistance Denial: Upper wick rejection at resistance. Securing +{current_r:.2f}R profit before reversal.",
                        )
            else: # PUT
                probed_sup = any(abs(c_low - lvl) <= 10.0 or abs(float(prev_bar["low"]) - lvl) <= 10.0 for lvl in key_levels if lvl < entry_price)
                if probed_sup:
                    is_green_rejection = (c_close > c_open) and ((min(c_open, c_close) - c_low) >= c_range * 0.35)
                    failed_continuation = c_close > float(prev_bar["high"])
                    if is_green_rejection or failed_continuation:
                        return HoldingDecision(
                            state="MOMENTUM_EXHAUSTION",
                            action="PREEMPTIVE_PROFIT",
                            rationale=f"Opposing Support Denial: Lower wick rejection at support. Securing +{current_r:.2f}R profit before reversal.",
                        )

        # ---------------------------------------------------------------------
        # 2. VWAP Invalidation Check (Preemptive Loss Cutting)
        # ---------------------------------------------------------------------
        if df_5m is not None and len(df_5m) >= 4 and bars_in_trade >= 2 and current_r <= -0.40 and vwap is not None:
            curr_bar = df_5m.iloc[-1]
            c_close = float(curr_bar["close"])
            c_open = float(curr_bar["open"])
            if direction == "CALL" and c_close < (vwap - 5.0) and c_close < c_open:
                return HoldingDecision(
                    state="THESIS_INVALIDATED",
                    action="PREEMPTIVE_EXIT",
                    rationale=f"VWAP Lost: 5M body closed below VWAP ({c_close:.1f} < {vwap:.1f}). Preemptive exit at {current_r:.2f}R.",
                )
            elif direction == "PUT" and c_close > (vwap + 5.0) and c_close > c_open:
                return HoldingDecision(
                    state="THESIS_INVALIDATED",
                    action="PREEMPTIVE_EXIT",
                    rationale=f"VWAP Reclaimed: 5M body closed above VWAP ({c_close:.1f} > {vwap:.1f}). Preemptive exit at {current_r:.2f}R.",
                )

        # ---------------------------------------------------------------------
        # 3. Thesis Invalidation & Failed Breakout Trap Recognition
        # ---------------------------------------------------------------------
        if df_5m is not None and len(df_5m) >= 4 and bars_in_trade >= 2 and current_r <= self.preemptive_loss_cutoff_r:
            curr_bar = df_5m.iloc[-1]
            prev_bar = df_5m.iloc[-2]
            c_open = float(curr_bar["open"])
            c_high = float(curr_bar["high"])
            c_low = float(curr_bar["low"])
            c_close = float(curr_bar["close"])
            c_range = c_high - c_low

            # Case A: Long position (CALL) trapped in a Bull Trap / Failed Breakout
            if direction == "CALL" and c_close < c_open and c_range >= self.trap_range_min_pts:
                # Price broke back below entry and closed near lows of the bar
                if c_close < (entry_price - 3.0) and (c_high - c_close) >= (c_range * 0.6):
                    trap_high = max(c_high, float(prev_bar["high"]))
                    rev_risk = max(15.0, min(35.0, trap_high + 5.0 - c_close))
                    rev_target = c_close - (rev_risk * 3.0)

                    reversal_setup = None
                    action: Literal["PREEMPTIVE_EXIT", "FLIP_AND_REVERSE"] = "PREEMPTIVE_EXIT"
                    is_macro_strat = ("pd" in strategy.lower() or "or-30" in strategy.lower())
                    if daily_flips_count < max_daily_flips and is_macro_strat:
                        action = "PREEMPTIVE_EXIT"
                        strike_round = int(round(c_close / 50.0) * 50) - 100
                        reversal_setup = CandidateSetup(
                            strategy=f"Trap Reversal Flip [5M]",
                            symbol=active_trade.get("symbol", "NIFTY"),
                            direction="PUT",
                            trigger_price=round(c_close, 2),
                            tentative_stop=round(trap_high + 5.0, 2),
                            tentative_target=round(rev_target, 2),
                            timeframe="5m",
                            timestamp=current_time_str or "",
                            rationale=f"Bull Trap Confirmed: Failed long at {entry_price:.1f}, closed red at {c_close:.1f}. Trapped buyers flushing.",
                            news_sentiment_score=0.0,
                            event_risk_cleared=True,
                            is_valid_for_trade_crew=True,
                            suggested_strike=f"NIFTY {strike_round} PE",
                            risk_points=round(rev_risk, 2),
                            target_points=round(abs(c_close - rev_target), 2),
                            rr_ratio=3.0,
                            is_super_trend=True,
                        )

                    return HoldingDecision(
                        state="THESIS_INVALIDATED",
                        action=action,
                        reversal_setup=reversal_setup,
                        rationale=f"Bull Trap Invalidation: Long thesis failed at {entry_price:.1f}. Preemptive exit to cap loss at {current_r:.2f}R.",
                    )

            # Case B: Short position (PUT) trapped in a Bear Trap / Failed Breakdown
            elif direction == "PUT" and c_close > c_open and c_range >= self.trap_range_min_pts:
                # Price broke back above entry and closed near highs of the bar
                if c_close > (entry_price + 3.0) and (c_close - c_low) >= (c_range * 0.6):
                    trap_low = min(c_low, float(prev_bar["low"]))
                    rev_risk = max(15.0, min(35.0, c_close - (trap_low - 5.0)))
                    rev_target = c_close + (rev_risk * 3.0)

                    reversal_setup = None
                    action = "PREEMPTIVE_EXIT"
                    is_macro_strat = ("pd" in strategy.lower() or "or-30" in strategy.lower())
                    if daily_flips_count < max_daily_flips and is_macro_strat:
                        action = "PREEMPTIVE_EXIT"
                        strike_round = int(round(c_close / 50.0) * 50) + 100
                        reversal_setup = CandidateSetup(
                            strategy=f"Trap Reversal Flip [5M]",
                            symbol=active_trade.get("symbol", "NIFTY"),
                            direction="CALL",
                            trigger_price=round(c_close, 2),
                            tentative_stop=round(trap_low - 5.0, 2),
                            tentative_target=round(rev_target, 2),
                            timeframe="5m",
                            timestamp=current_time_str or "",
                            rationale=f"Bear Trap Confirmed: Failed short at {entry_price:.1f}, closed green at {c_close:.1f}. Trapped sellers squeezing.",
                            news_sentiment_score=0.0,
                            event_risk_cleared=True,
                            is_valid_for_trade_crew=True,
                            suggested_strike=f"NIFTY {strike_round} CE",
                            risk_points=round(rev_risk, 2),
                            target_points=round(abs(c_close - rev_target), 2),
                            rr_ratio=3.0,
                            is_super_trend=True,
                        )

                    return HoldingDecision(
                        state="THESIS_INVALIDATED",
                        action=action,
                        reversal_setup=reversal_setup,
                        rationale=f"Bear Trap Invalidation: Short thesis failed at {entry_price:.1f}. Preemptive exit to cap loss at {current_r:.2f}R.",
                    )

        # ---------------------------------------------------------------------
        # 4. Consolidation Flag Runway Extension
        # ---------------------------------------------------------------------
        if bars_in_trade >= 6 and be_locked and current_r >= 0.20:
            if df_5m is not None and len(df_5m) >= 2:
                last_2_range = max(float(df_5m["high"].iloc[-2:].max()) - float(df_5m["low"].iloc[-2:].min()), 1.0)
                if last_2_range <= 16.0:
                    return HoldingDecision(
                        state="CONSOLIDATION_FLAG",
                        action="HOLD",
                        rationale=f"Consolidation Flag ({last_2_range:.1f} pt box): Position risk-free in profit (+{current_r:.2f}R). Extending hold runway.",
                    )

        # ---------------------------------------------------------------------
        # 5. Healthy Retest Cushioning (Preventing Runner Choking)
        # ---------------------------------------------------------------------
        if be_locked and -0.10 <= current_r <= 0.60:
            # Trade hit milestones earlier but is pulling back in normal shallow consolidation
            cushion_sl = (entry_price - self.retest_cushion_pts) if direction == "CALL" else (entry_price + self.retest_cushion_pts)
            return HoldingDecision(
                state="HEALTHY_RETEST",
                action="HOLD",
                cushion_sl=round(cushion_sl, 2),
                rationale=f"Healthy Retest Cushion ({self.retest_cushion_pts} pts beyond cost): Holding runner through shallow retest.",
            )

        # Default Healthy Holding state
        return HoldingDecision(
            state="HEALTHY_EXPANSION",
            action="HOLD",
            rationale=f"Active thesis healthy at CMP {cmp:.2f} (+{current_r:.2f}R).",
        )
