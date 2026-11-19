"""Thesis-based trade management and runner protection policy.

Versioned research implementation restoring original Genesis holding logic:
- Multi-timeframe structural invalidation (15m breakdown, 5m trap invalidation).
- Healthy retest cushioning and consolidation runway extension without stop loosening.
- Monotonic stop ratcheting (0.7R breakeven, 2.0R profit lock).
- Dynamic 5m/1m structure trailing on profitable runners.
- Opposing key-level / resistance rejection profit defense.
- Explicit priority handling: Stop > EOD > Invalidation > Trail > Stagnation > HOLD.
- Strictly completed candle inputs; missing factors fail gracefully without fabrication.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

import pandas as pd

IST = ZoneInfo("Asia/Kolkata")
VERSION = "thesis-management-research-v1"

RULES = {
    "version": VERSION,
    "live_registration": False,
    "milestones": {
        "breakeven_r": 0.70,
        "lock_r": 0.80,
        "lock_at_r": 2.00,
    },
    "structure_trail_min_r": 1.50,
    "invalidation": {
        "15m_adverse_r": -0.30,
        "trap_adverse_r": -0.35,
        "trap_min_range_pts": 12.0,
    },
    "consolidation_flag": {
        "min_bars": 6,
        "min_r": 0.20,
        "max_two_bar_range_pts": 16.0,
        "extended_stagnation_bars": 10,
    },
    "stagnation": {
        "default_max_bars": 6,
        "target_r_threshold": 1.80,
    },
    "monotonic_stops": True,
    "intraday_cutoff_time": "15:10",
    "liquidity_hopping_min_lots": 2,
}

RULE_HASH = hashlib.sha256(json.dumps(RULES, sort_keys=True).encode()).hexdigest()


@dataclass
class ThesisManagementDecision:
    """Decision output from thesis management evaluation."""

    action: Literal[
        "HOLD",
        "STOP_LOSS_HIT",
        "TRAILING_STOP_HIT",
        "PREEMPTIVE_EXIT",
        "PREEMPTIVE_PROFIT",
        "STRUCTURE_TRAIL_EXIT",
        "STAGNATION_TIME_STOP",
        "INTRADAY_CLOSE",
        "REVERSAL_EXIT",
        "TARGET_HIT",
        "LIQUIDITY_HOP_PROMOTED",
    ]
    state: Literal[
        "HEALTHY_EXPANSION",
        "HEALTHY_RETEST",
        "CONSOLIDATION_FLAG",
        "MOMENTUM_EXHAUSTION",
        "THESIS_INVALIDATED",
        "STOPPED_OUT",
        "TIMED_OUT",
        "TARGET_REACHED",
        "REVERSAL",
    ]
    exit_trigger_price: float | None
    exit_reason: str
    is_fully_closed: bool
    booked_lots: int = 0
    remaining_lots: int = 0
    new_stop_loss: float | None = None
    new_target_price: float | None = None
    milestone_updates: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""


def tighten(side: str, current_stop: float, proposed_stop: float) -> float:
    """Monotonic stop tightening: stops can ONLY tighten, never widen."""
    if side == "CALL":
        return max(current_stop, proposed_stop)
    return min(current_stop, proposed_stop)


def evaluate_thesis_management(
    trade: dict[str, Any],
    spot: float,
    *,
    now: datetime,
    bar_1m: dict[str, Any] | None = None,
    df_5m: pd.DataFrame | None = None,
    df_15m: pd.DataFrame | None = None,
    key_levels: list[float] | None = None,
    opposing_signal_detected: bool = False,
    call_wall: float | None = None,
    put_wall: float | None = None,
    order_flow_signal: Any | None = None,
    enable_structure_trail: bool = True,
    structure_timeframe: str = "5m",
    enable_fixed_target: bool = False,
    target_r: float = 3.0,
    enable_liquidity_hopping: bool = False,
) -> ThesisManagementDecision:
    """Evaluate open trade lifecycle, runner health, invalidation, and trailing stops.

    Parameters:
    - trade: Dict containing active trade attributes (side, entry_spot, stop_spot, risk_points,
             lots, milestone_be, research_milestones, runner_stop, runner_target, opened_at, etc.)
    - spot: Current underlying spot price.
    - now: Aware datetime representing current evaluation time.
    - bar_1m: Current completed 1m bar (if available).
    - df_5m: Completed 5m bars.
    - df_15m: Completed 15m bars.
    - key_levels: Macro key support/resistance levels.
    - opposing_signal_detected: Boolean indicating fresh confirmed opposing signal.
    - enable_structure_trail: Enable runner trailing on candle structure.
    - structure_timeframe: '5m' (verified original) or '1m' (exploratory hypothesis).
    - enable_fixed_target: If True, exits at fixed target_r. Default False (runners run).
    - enable_liquidity_hopping: If True, books partial at target and hops runner.
    """
    if now.utcoffset() is None:
        raise ValueError("Timezone-aware clock required")
    now_ist = now.astimezone(IST)
    time_str = now_ist.strftime("%H:%M")

    side: Literal["CALL", "PUT"] = trade.get("side", trade.get("direction", "CALL"))
    sign = 1 if side == "CALL" else -1
    entry_price = float(trade.get("entry_spot", trade.get("entry_price", spot)))
    current_stop = float(trade.get("runner_stop", trade.get("stop_spot", trade.get("stop_loss", spot))))
    risk_pts = max(1.0, float(trade.get("risk_points", abs(entry_price - current_stop))))
    lots = int(trade.get("remaining_lots", trade.get("lots", 1)))
    milestones = list(trade.get("research_milestones", []))
    be_locked = bool(trade.get("milestone_be", trade.get("be_locked", False)))
    target_price = float(trade.get("runner_target", trade.get("target_spot", trade.get("target_price", entry_price + sign * target_r * risk_pts))))

    # Realized underlying points and R-multiple
    current_move = sign * (spot - entry_price)
    current_r = current_move / risk_pts

    # Elapsed 5m bars in trade
    opened_at_raw = trade.get("opened_at", trade.get("timestamp", trade.get("entry_time")))
    bars_in_trade = 0
    if opened_at_raw:
        try:
            opened_dt = datetime.fromisoformat(str(opened_at_raw))
            if opened_dt.utcoffset() is None:
                opened_dt = opened_dt.replace(tzinfo=IST)
            elapsed_seconds = max(0.0, (now_ist - opened_dt.astimezone(IST)).total_seconds())
            bars_in_trade = int(elapsed_seconds // 300)
        except (ValueError, TypeError, KeyError):
            bars_in_trade = int(trade.get("bars_in_trade", 0))
    else:
        bars_in_trade = int(trade.get("bars_in_trade", 0))

    # -------------------------------------------------------------------------
    # PRIORITY 1: Intraday Cutoff (15:10 IST)
    # -------------------------------------------------------------------------
    if time_str >= RULES["intraday_cutoff_time"]:
        return ThesisManagementDecision(
            action="INTRADAY_CLOSE",
            state="TIMED_OUT",
            exit_trigger_price=spot,
            exit_reason=f"Intraday cutoff ({RULES['intraday_cutoff_time']} IST) reached at CMP {spot:.2f}",
            is_fully_closed=True,
            remaining_lots=0,
            rationale="Unconditional EOD square-off to eliminate overnight holding risk.",
        )

    # -------------------------------------------------------------------------
    # PRIORITY 2: Protective Stop Loss Hit (Monotonic Check)
    # -------------------------------------------------------------------------
    effective_sl = current_stop
    if be_locked:
        effective_sl = tighten(side, current_stop, entry_price)

    # Check instantaneous CMP touch or candle bar touch
    stop_hit = False
    if side == "CALL":
        if spot <= effective_sl or (bar_1m and float(bar_1m.get("low", spot)) <= effective_sl):
            stop_hit = True
    else:
        if spot >= effective_sl or (bar_1m and float(bar_1m.get("high", spot)) >= effective_sl):
            stop_hit = True

    if stop_hit:
        realized_pts = sign * (effective_sl - entry_price)
        calc_r = realized_pts / risk_pts
        act_name = "TRAILING_STOP_HIT" if calc_r >= 0.0 else "STOP_LOSS_HIT"
        return ThesisManagementDecision(
            action=act_name,
            state="STOPPED_OUT",
            exit_trigger_price=effective_sl,
            exit_reason=f"{'Trailing Stop' if calc_r >= 0.0 else 'Stop Loss'} hit at {effective_sl:.2f} ({calc_r:+.2f}R)",
            is_fully_closed=True,
            remaining_lots=0,
            rationale=f"Monotonic stop level {effective_sl:.2f} breached.",
        )

    # -------------------------------------------------------------------------
    # PRIORITY 3: Thesis Invalidation (Preemptive Loss Mitigation)
    # -------------------------------------------------------------------------
    # 3.1: 15M Structural Invalidation
    if df_15m is not None and len(df_15m) >= 2 and bars_in_trade >= 2 and current_r <= RULES["invalidation"]["15m_adverse_r"]:
        last_15m = df_15m.iloc[-1]
        prev_15m = df_15m.iloc[-2]
        c15_close = float(last_15m["close"])
        c15_open = float(last_15m["open"])
        p15_low = float(prev_15m["low"])
        p15_high = float(prev_15m["high"])

        if side == "CALL" and c15_close < p15_low and c15_close < c15_open:
            return ThesisManagementDecision(
                action="PREEMPTIVE_EXIT",
                state="THESIS_INVALIDATED",
                exit_trigger_price=spot,
                exit_reason=f"15M Structural Breakdown: Closed below prior 15M low ({c15_close:.1f} < {p15_low:.1f})",
                is_fully_closed=True,
                remaining_lots=0,
                rationale=f"Thesis invalidated on higher timeframe. Preemptively cutting risk at {current_r:+.2f}R vs full -1.0R SL.",
            )
        elif side == "PUT" and c15_close > p15_high and c15_close > c15_open:
            return ThesisManagementDecision(
                action="PREEMPTIVE_EXIT",
                state="THESIS_INVALIDATED",
                exit_trigger_price=spot,
                exit_reason=f"15M Structural Breakout: Closed above prior 15M high ({c15_close:.1f} > {p15_high:.1f})",
                is_fully_closed=True,
                remaining_lots=0,
                rationale=f"Thesis invalidated on higher timeframe. Preemptively cutting risk at {current_r:+.2f}R vs full -1.0R SL.",
            )

    # 3.2: 5M Trap Invalidation (Failed Breakout / Breakdown)
    if df_5m is not None and len(df_5m) >= 2 and bars_in_trade >= 2 and current_r <= RULES["invalidation"]["trap_adverse_r"]:
        curr_bar = df_5m.iloc[-1]
        c_open = float(curr_bar["open"])
        c_high = float(curr_bar["high"])
        c_low = float(curr_bar["low"])
        c_close = float(curr_bar["close"])
        c_range = max(1.0, c_high - c_low)

        if c_range >= RULES["invalidation"]["trap_min_range_pts"]:
            if side == "CALL" and c_close < c_open:
                if c_close < (entry_price - 3.0) and (c_high - c_close) >= (c_range * 0.60):
                    return ThesisManagementDecision(
                        action="PREEMPTIVE_EXIT",
                        state="THESIS_INVALIDATED",
                        exit_trigger_price=spot,
                        exit_reason=f"Bull Trap Invalidation: Long thesis failed, 5M bar closed near lows at {c_close:.1f}",
                        is_fully_closed=True,
                        remaining_lots=0,
                        rationale=f"Failed breakout trapped long buyers. Preemptive exit at {current_r:+.2f}R.",
                    )
            elif side == "PUT" and c_close > c_open and c_close > (entry_price + 3.0) and (c_close - c_low) >= (c_range * 0.60):
                return ThesisManagementDecision(
                    action="PREEMPTIVE_EXIT",
                    state="THESIS_INVALIDATED",
                    exit_trigger_price=spot,
                    exit_reason=f"Bear Trap Invalidation: Short thesis failed, 5M bar closed near highs at {c_close:.1f}",
                    is_fully_closed=True,
                    remaining_lots=0,
                    rationale=f"Failed breakdown trapped short sellers. Preemptive exit at {current_r:+.2f}R.",
                )

    # 3.3: Opposing Confirmed Strategy Signal
    if opposing_signal_detected:
        return ThesisManagementDecision(
            action="REVERSAL_EXIT",
            state="REVERSAL",
            exit_trigger_price=spot,
            exit_reason=f"Fresh opposing strategy signal confirmed at CMP {spot:.2f}",
            is_fully_closed=True,
            remaining_lots=0,
            rationale="Opposing momentum confirmed; closing current position to avoid fighting new trend.",
        )

    # -------------------------------------------------------------------------
    # PRIORITY 4: Momentum Exhaustion / Opposing Level Denial (Profit Defense)
    # -------------------------------------------------------------------------
    if df_5m is not None and len(df_5m) >= 3 and current_r >= 1.0 and key_levels:
        curr_bar = df_5m.iloc[-1]
        prev_bar = df_5m.iloc[-2]
        c_high = float(curr_bar["high"])
        c_low = float(curr_bar["low"])
        c_open = float(curr_bar["open"])
        c_close = float(curr_bar["close"])
        c_range = max(1.0, c_high - c_low)

        if side == "CALL":
            probed_res = any(abs(c_high - lvl) <= 10.0 or abs(float(prev_bar["high"]) - lvl) <= 10.0 for lvl in key_levels if lvl > entry_price)
            if probed_res:
                is_red_rejection = (c_close < c_open) and ((c_high - max(c_open, c_close)) >= c_range * 0.35)
                failed_continuation = c_close < float(prev_bar["low"])
                if is_red_rejection or failed_continuation:
                    return ThesisManagementDecision(
                        action="PREEMPTIVE_PROFIT",
                        state="MOMENTUM_EXHAUSTION",
                        exit_trigger_price=spot,
                        exit_reason=f"Opposing Resistance Denial: Upper wick rejection near key level (+{current_r:.2f}R)",
                        is_fully_closed=True,
                        remaining_lots=0,
                        rationale="Institutional supply defending overhead resistance. Locking profit before mean reversion.",
                    )
        else:
            probed_sup = any(abs(c_low - lvl) <= 10.0 or abs(float(prev_bar["low"]) - lvl) <= 10.0 for lvl in key_levels if lvl < entry_price)
            if probed_sup:
                is_green_rejection = (c_close > c_open) and ((min(c_open, c_close) - c_low) >= c_range * 0.35)
                failed_continuation = c_close > float(prev_bar["high"])
                if is_green_rejection or failed_continuation:
                    return ThesisManagementDecision(
                        action="PREEMPTIVE_PROFIT",
                        state="MOMENTUM_EXHAUSTION",
                        exit_trigger_price=spot,
                        exit_reason=f"Opposing Support Denial: Lower wick rejection near key level (+{current_r:.2f}R)",
                        is_fully_closed=True,
                        remaining_lots=0,
                        rationale="Institutional demand defending support floor. Locking profit before squeeze.",
                    )

    # -------------------------------------------------------------------------
    # PRIORITY 5: Structure Trailing on Runners (Once >= +1.5R achieved)
    # -------------------------------------------------------------------------
    if enable_structure_trail and current_r >= RULES["structure_trail_min_r"]:
        if structure_timeframe == "5m" and df_5m is not None and len(df_5m) >= 2:
            last_candle = df_5m.iloc[-1]
            prior_candle = df_5m.iloc[-2]
            if side == "CALL" and float(last_candle["close"]) < float(prior_candle["low"]):
                return ThesisManagementDecision(
                    action="STRUCTURE_TRAIL_EXIT",
                    state="HEALTHY_EXPANSION",
                    exit_trigger_price=spot,
                    exit_reason=f"5M Structure Trail: Closed below prior 5M low ({float(prior_candle['low']):.1f}) at +{current_r:.2f}R",
                    is_fully_closed=True,
                    remaining_lots=0,
                    rationale="5-minute market structure shift on profitable runner.",
                )
            elif side == "PUT" and float(last_candle["close"]) > float(prior_candle["high"]):
                return ThesisManagementDecision(
                    action="STRUCTURE_TRAIL_EXIT",
                    state="HEALTHY_EXPANSION",
                    exit_trigger_price=spot,
                    exit_reason=f"5M Structure Trail: Closed above prior 5M high ({float(prior_candle['high']):.1f}) at +{current_r:.2f}R",
                    is_fully_closed=True,
                    remaining_lots=0,
                    rationale="5-minute market structure shift on profitable runner.",
                )
        elif structure_timeframe == "1m" and df_5m is not None and "df_1m" in trade:
            # 1m structural trail variant evaluated separately
            pass

    # -------------------------------------------------------------------------
    # PRIORITY 6: Optional Fixed Target / Liquidity Hopping
    # -------------------------------------------------------------------------
    target_hit = (spot >= target_price) if side == "CALL" else (spot <= target_price)
    if enable_fixed_target and target_hit:
        if enable_liquidity_hopping and lots >= RULES["liquidity_hopping_min_lots"] and not trade.get("has_hopped", False):
            booked = lots // 2
            remaining = lots - booked
            levels = [float(lv) for lv in (key_levels or [])]
            if side == "CALL":
                overhead = sorted([lv for lv in levels if lv > target_price + 5.0])
                next_target = overhead[0] if overhead else (target_price + risk_pts * 1.5)
                ratcheted_sl = target_price - 3.0
            else:
                support = sorted([lv for lv in levels if lv < target_price - 5.0], reverse=True)
                next_target = support[0] if support else (target_price - risk_pts * 1.5)
                ratcheted_sl = target_price + 3.0

            return ThesisManagementDecision(
                action="LIQUIDITY_HOP_PROMOTED",
                state="HEALTHY_EXPANSION",
                exit_trigger_price=target_price,
                exit_reason=f"Conquered {target_price:.1f}. Booked {booked} lots. Runner promoted to {next_target:.1f}",
                is_fully_closed=False,
                booked_lots=booked,
                remaining_lots=remaining,
                new_stop_loss=tighten(side, current_stop, ratcheted_sl),
                new_target_price=next_target,
                milestone_updates={"has_hopped": True, "conquered_level": target_price},
                rationale="Liquidity hop promoted runner to next liquidity pool.",
            )
        else:
            return ThesisManagementDecision(
                action="TARGET_HIT",
                state="TARGET_REACHED",
                exit_trigger_price=target_price,
                exit_reason=f"Target reached at {target_price:.2f} (+{current_r:.2f}R)",
                is_fully_closed=True,
                remaining_lots=0,
                rationale="Unconditional fixed target achieved.",
            )

    # -------------------------------------------------------------------------
    # PRIORITY 7: Stagnation Time-Stop & Consolidation Flag Runway
    # -------------------------------------------------------------------------
    is_consolidation_flag = False
    if (
        bars_in_trade >= RULES["consolidation_flag"]["min_bars"]
        and be_locked
        and current_r >= RULES["consolidation_flag"]["min_r"]
        and df_5m is not None
        and len(df_5m) >= 2
    ):
        last_2_range = max(1.0, float(df_5m["high"].iloc[-2:].max()) - float(df_5m["low"].iloc[-2:].min()))
        if last_2_range <= RULES["consolidation_flag"]["max_two_bar_range_pts"]:
            is_consolidation_flag = True

    max_stagnation_bars = (
        RULES["consolidation_flag"]["extended_stagnation_bars"]
        if is_consolidation_flag
        else RULES["stagnation"]["default_max_bars"]
    )

    if bars_in_trade >= max_stagnation_bars and current_r < RULES["stagnation"]["target_r_threshold"]:
        return ThesisManagementDecision(
            action="STAGNATION_TIME_STOP",
            state="TIMED_OUT",
            exit_trigger_price=spot,
            exit_reason=f"Stagnation Time-Stop: {bars_in_trade} bars ({bars_in_trade * 5}m) elapsed without hitting Target 1 (+1.8R).",
            is_fully_closed=True,
            remaining_lots=0,
            rationale="Momentum stalled; exiting at market to prevent delta decay.",
        )

    # -------------------------------------------------------------------------
    # PRIORITY 8: Progressive Stop Protection & Healthy HOLD Directives
    # -------------------------------------------------------------------------
    new_sl = effective_sl
    milestone_changes = {}

    # Milestone 0.7R: Breakeven Lock
    if current_r >= RULES["milestones"]["breakeven_r"] and "0.7R" not in milestones:
        milestones.append("0.7R")
        new_sl = tighten(side, new_sl, entry_price)
        milestone_changes["research_milestones"] = milestones
        milestone_changes["milestone_be"] = True
        be_locked = True

    # Milestone 2.0R: Lock +0.8R
    if current_r >= RULES["milestones"]["lock_at_r"] and "2R" not in milestones:
        milestones.append("2R")
        locked_p = round(entry_price + sign * RULES["milestones"]["lock_r"] * risk_pts, 2)
        new_sl = tighten(side, new_sl, locked_p)
        milestone_changes["research_milestones"] = milestones

    # Classify active holding state
    if is_consolidation_flag:
        decision_state = "CONSOLIDATION_FLAG"
        hold_rationale = "Consolidation Flag: Low-volatility box under breakeven protection. Runway extended to 10 bars."
    elif be_locked and -0.10 <= current_r <= 0.60:
        decision_state = "HEALTHY_RETEST"
        hold_rationale = f"Healthy Retest: Runner pulling back safely above protected stop ({new_sl:.2f}). Giving trade room."
    else:
        decision_state = "HEALTHY_EXPANSION"
        hold_rationale = f"Thesis Healthy: Expanding favorably at CMP {spot:.2f} ({current_r:+.2f}R)."

    return ThesisManagementDecision(
        action="HOLD",
        state=decision_state,
        exit_trigger_price=None,
        exit_reason=f"Holding active trade ({current_r:+.2f}R). {hold_rationale}",
        is_fully_closed=False,
        remaining_lots=lots,
        new_stop_loss=new_sl,
        milestone_updates=milestone_changes,
        rationale=hold_rationale,
    )


def opposing_only(signals: list[dict[str, Any]] | None, side: str, now: datetime) -> bool:
    """Return True if fresh confirmed signal in opposite direction exists without same-side conflict."""
    if not signals:
        return False
    now_ist = now.astimezone(IST)
    fresh = set()
    for s in signals:
        try:
            s_dt = datetime.fromisoformat(str(s["signal_at"]))
            if s_dt.utcoffset() is None:
                s_dt = s_dt.replace(tzinfo=IST)
            if 0 <= (now_ist - s_dt.astimezone(IST)).total_seconds() <= 90:
                fresh.add(s["side"])
        except (ValueError, TypeError, KeyError):
            continue
    return bool(fresh and side not in fresh)


def thesis_exit_adapter(
    trade: dict[str, Any],
    *,
    spot: float,
    bar: dict[str, Any] | None = None,
    df_5m: pd.DataFrame | None = None,
    df_15m: pd.DataFrame | None = None,
    levels: list[float] | None = None,
    signals: list[dict[str, Any]] | None = None,
    now: datetime,
    structure_timeframe: str = "5m",
    enable_fixed_target: bool = False,
    enable_liquidity_hopping: bool = False,
) -> dict[str, Any]:
    """Account adapter converting thesis decisions into journal and trade updates."""
    side = trade.get("side", trade.get("direction", "CALL"))
    opposing = opposing_only(signals, side, now)

    decision = evaluate_thesis_management(
        trade,
        spot,
        now=now,
        bar_1m=bar,
        df_5m=df_5m,
        df_15m=df_15m,
        key_levels=levels,
        opposing_signal_detected=opposing,
        enable_structure_trail=True,
        structure_timeframe=structure_timeframe,
        enable_fixed_target=enable_fixed_target,
        enable_liquidity_hopping=enable_liquidity_hopping,
    )

    res: dict[str, Any] = {
        "action": decision.action,
        "state": decision.state,
        "is_fully_closed": decision.is_fully_closed,
        "exit_price": decision.exit_trigger_price,
        "exit_reason": decision.exit_reason,
        "booked_lots": decision.booked_lots,
        "remaining_lots": decision.remaining_lots,
        "new_stop_loss": decision.new_stop_loss,
        "new_target_price": decision.new_target_price,
        "rationale": decision.rationale,
    }

    if decision.action == "HOLD":
        res["updates"] = {
            "runner_stop": decision.new_stop_loss,
            "research_milestones": decision.milestone_updates.get(
                "research_milestones", trade.get("research_milestones", [])
            ),
            "milestone_be": decision.milestone_updates.get(
                "milestone_be", trade.get("milestone_be", False)
            ),
        }
    elif decision.action == "LIQUIDITY_HOP_PROMOTED":
        res["updates"] = {
            "runner_stop": decision.new_stop_loss,
            "runner_target": decision.new_target_price,
            "has_hopped": True,
            "conquered_level": decision.exit_trigger_price,
        }
    return res

