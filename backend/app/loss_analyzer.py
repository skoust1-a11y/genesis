"""Post-mortem autopsy for every losing trade and LLM Expert Analysis on 3 consecutive losses."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


def _time_str(dt: Any) -> str:
    if isinstance(dt, str):
        return dt[-5:] if len(dt) >= 5 else dt
    if hasattr(dt, "strftime"):
        return dt.strftime("%H:%M")
    return str(dt)


def analyze_losing_trade(
    trade: dict,
    day_bars: list[dict],
    pdh: float,
    pdl: float,
    daily_bars: list[dict] | None = None,
) -> dict:
    """Performs a technical autopsy on a losing trade to diagnose what went wrong:

    - Identifies regime mismatch, VWAP loss, false breakout trap, noon chop, or volatility spike.
    - Produces a plain-English diagnosis and rule suggestion.
    """
    long = trade.get("side") == "Long"
    entry_px = float(trade.get("entry", 0.0))
    stop_px = float(trade.get("stop", 0.0))
    exit_px = float(trade.get("exit_price", stop_px))
    entry_time = str(trade.get("time", ""))
    exit_time = str(trade.get("exit_time", ""))
    risk_pts = float(trade.get("risk", abs(entry_px - stop_px)))
    pool = str(trade.get("pool", trade.get("strategy", "Liquidity")))

    # Slice bars during the active life of the trade
    trade_bars = [b for b in day_bars if b["date"] >= entry_time and (not exit_time or b["date"] <= exit_time)]
    bars_held = len(trade_bars)
    duration_min = bars_held * 5

    # 1. VWAP Analysis
    # Check if price lost VWAP after entry
    vwap_lost_bar = None
    if day_bars:
        tot_tp = 0.0
        for idx, b in enumerate(day_bars, 1):
            tp = (b["high"] + b["low"] + b["close"]) / 3.0
            tot_tp += tp
            vwap = tot_tp / idx
            if b["date"] >= entry_time:
                lost = (b["close"] < vwap) if long else (b["close"] > vwap)
                if lost and vwap_lost_bar is None:
                    vwap_lost_bar = b["date"]

    # 2. Key Level Interaction (PDH / PDL)
    false_breakout = False
    if pool in ("PDH", "PDL", "PDH Support Flip", "PDL Resistance Flip"):
        ref_level = pdh if "PDH" in pool else pdl
        # If long retest of PDH, did it slam back under PDH?
        if long and any(b["close"] < ref_level - 10 for b in trade_bars):
            false_breakout = True
        elif not long and any(b["close"] > ref_level + 10 for b in trade_bars):
            false_breakout = True

    # 3. Time-of-Day Chop
    entry_clock = _time_str(entry_time)
    is_lunch_hours = "11:30" <= entry_clock <= "13:30"

    # 4. Stop Speed (Fast flush vs Slow Bleed)
    is_fast_flush = bars_held <= 2
    is_slow_bleed = bars_held >= 12

    # 5. Determine Primary Failure Cause
    what_went_wrong: list[str] = []
    primary_cause: str

    if false_breakout:
        primary_cause = "LIQUIDITY_TRAP_FALSE_BREAKOUT"
        ref_name = "PDH" if "PDH" in pool else "PDL"
        what_went_wrong.append(
            f"Bull trap / Bear trap: Price tested {ref_name} ({ref_level:,.1f}) but institutions failed to defend the level, resulting in an aggressive failure back inside the prior range."
        )
    elif is_fast_flush:
        primary_cause = "VOLATILITY_EXPANSION_SPIKE"
        what_went_wrong.append(
            f"Violent adverse flush: Stop was hit within {duration_min} minutes ({bars_held} bars). Sudden order flow imbalance completely overpowered the setup."
        )
    elif vwap_lost_bar:
        primary_cause = "MOMENTUM_VWAP_FAILURE"
        what_went_wrong.append(
            f"Lost institutional VWAP anchor: Price failed to maintain momentum and crossed back below VWAP at {_time_str(vwap_lost_bar)}, signaling loss of buyer control."
        )
    elif is_lunch_hours:
        primary_cause = "NOON_CHOP_RANGE_BOUND"
        what_went_wrong.append(
            f"Midday volume doldrums: Entry at {entry_clock} fell inside the 11:30-13:30 range contraction window, where low volume triggers erratic false moves."
        )
    elif is_slow_bleed:
        primary_cause = "STAGNATION_DECAY"
        what_went_wrong.append(
            f"Stagnation decay: Trade hovered without reaching +1R for over {duration_min}m before finally tagging the stop, indicating a lack of directional conviction."
        )
    else:
        primary_cause = "STRUCTURE_INVALIDATION"
        what_went_wrong.append(
            f"Market structure break: Opposing pressure exceeded invalidation threshold of {risk_pts:.1f} pts."
        )

    # Supporting observations
    if is_lunch_hours and primary_cause != "NOON_CHOP_RANGE_BOUND":
        what_went_wrong.append(f"Entry occurred during midday low-volume chop ({entry_clock} IST).")
    if vwap_lost_bar and primary_cause != "MOMENTUM_VWAP_FAILURE":
        what_went_wrong.append(f"Price crossed against VWAP at {_time_str(vwap_lost_bar)} IST before hitting stop.")

    # Practical rule suggestion
    suggested_fix: str
    if primary_cause == "LIQUIDITY_TRAP_FALSE_BREAKOUT":
        suggested_fix = "Wait for a full 15-minute candle close confirmation rather than 5-minute when trading around extreme daily levels."
    elif primary_cause == "VOLATILITY_EXPANSION_SPIKE":
        suggested_fix = "Check India VIX trend; when volatility is surging, expand stop buffer or cut lot sizing."
    elif primary_cause == "NOON_CHOP_RANGE_BOUND":
        suggested_fix = "Apply lunch restriction: avoid opening fresh setups between 11:45 and 13:15 IST."
    elif primary_cause == "MOMENTUM_VWAP_FAILURE":
        suggested_fix = "Implement early exit rule: close immediately if a 5m candle closes 10+ points beyond VWAP."
    else:
        suggested_fix = "Respect the invalidation level; the loss was well within standard risk parameters."

    points_lost = round(abs(exit_px - entry_px), 2)
    # Estimate option loss for 1 lot (assuming delta ~0.4 - 0.5)
    lot_size = int(trade.get("lot_size", 25))
    opt_loss_est = round(points_lost * 0.45 * lot_size)

    return {
        "side": trade.get("side"),
        "pool": pool,
        "entry_price": entry_px,
        "stop_price": stop_px,
        "exit_price": exit_px,
        "entry_time": entry_time,
        "exit_time": exit_time,
        "duration_min": duration_min,
        "bars_held": bars_held,
        "points_lost": points_lost,
        "option_loss_est": opt_loss_est,
        "primary_cause": primary_cause,
        "what_went_wrong": what_went_wrong,
        "suggested_fix": suggested_fix,
    }


def format_loss_alert(analysis: dict) -> str:
    """Formats a clean, informative Telegram post-mortem alert for a single losing trade."""
    side_icon = "🟢" if analysis["side"] == "Long" else "🔴"
    bullets = "\n".join(f"• {w}" for w in analysis["what_went_wrong"])
    cause_name = analysis["primary_cause"].replace("_", " ").title()

    return (
        f"🔴 NIFTY TRADE POST-MORTEM (Stop Loss Hit)\n\n"
        f"📊 Execution Summary:\n"
        f"• Setup: {side_icon} {analysis['side']} ({analysis['pool']})\n"
        f"• Entry: {analysis['entry_price']:,.2f} at {_time_str(analysis['entry_time'])}\n"
        f"• Stop Hit: {analysis['exit_price']:,.2f} at {_time_str(analysis['exit_time'])} ({analysis['duration_min']}m held)\n"
        f"• Realized Loss: -{analysis['points_lost']:.1f} pts (~ -₹{analysis['option_loss_est']:,}/lot)\n\n"
        f"🔍 Technical Autopsy: What Went Wrong:\n"
        f"• Diagnosis: {cause_name}\n"
        f"{bullets}\n\n"
        f"💡 Actionable Takeaway:\n"
        f"👉 {analysis['suggested_fix']}"
    )


def ask_expert_on_streak(
    losing_trades: list[dict],
    day_bars: list[dict],
    settings: Settings | None = None,
) -> str:
    """Calls an AI Expert Quant / Risk Manager to perform a multi-trade synthesis

    when 3 consecutive losses trigger the circuit breaker.
    """
    settings = settings or get_settings()

    # Build concise autopsy summaries for the LLM
    trade_summaries = []
    for idx, t in enumerate(losing_trades[-3:], 1):
        a = t.get("analysis") or {}
        trade_summaries.append(
            f"Trade #{idx}:\n"
            f"- Setup: {t.get('side')} on {t.get('pool', 'Liquidity')}\n"
            f"- Time: {_time_str(t.get('time'))} -> {_time_str(t.get('exit_time'))} ({a.get('duration_min', '?')} min)\n"
            f"- Entry: {t.get('entry')} | Stop: {t.get('stop')} | Loss: -{a.get('points_lost', t.get('risk'))} pts\n"
            f"- Primary Failure: {a.get('primary_cause', 'Unknown')}\n"
            f"- Observed Issue: {'; '.join(a.get('what_went_wrong', []))}"
        )
    trades_block = "\n\n".join(trade_summaries)

    prompt = (
        "You are the Chief Quantitative Risk Manager at an institutional proprietary trading firm specializing in Indian Index (NIFTY 50) options.\n"
        "The automated Liquidity & Retest Strategy has just incurred 3 CONSECUTIVE LOSSES, triggering a mandatory risk circuit breaker.\n\n"
        "Here is the autopsy of the 3 consecutive failed trades:\n\n"
        f"{trades_block}\n\n"
        "Please provide a concise, razor-sharp Quantitative Expert Review in under 200 words covering:\n"
        "1. 🧠 REGIME DIAGNOSIS: What underlying market regime shift caused these failures (e.g. low-volatility compression, liquidity trap, two-sided institution chop, or breakout exhaustion)?\n"
        "2. ⚠️ COMMON FAILURE PATTERN: What specific tactical flaw or signal trap connects these 3 trades?\n"
        "3. 🛡️ ACTION PLAN: Explicit advice for the trader. Should trading stay paused for the rest of today? What single parameter adjustment (e.g., waiting for 15m candle close, skipping lunch window, increasing min ATR) would neutralize this risk?"
    )

    # Attempt to query LLM through configured providers
    for provider in (settings.llm_providers or [settings.llm_provider, "groq", "google", "openai"]):
        key = settings.provider_key(provider)
        if not key:
            continue
        try:
            from app.llm import _provider_model

            m = _provider_model(settings, provider, settings.provider_model(provider), {"timeout": 8.0, "max_retries": 1})
            res = m.invoke(prompt)
            text = res.content if hasattr(res, "content") else str(res)
            if text and len(text.strip()) > 30:
                return text.strip()
        except Exception as exc:
            logger.warning("LLM provider %s failed for expert analysis: %s", provider, exc)

    # Deterministic fallback expert report
    causes = [t.get("analysis", {}).get("primary_cause", "") for t in losing_trades[-3:]]
    if any("NOON" in c for c in causes):
        regime = "Midday Liquidity Vacuum & Range Contraction"
        pattern = "Entries triggered inside the lunch lull (11:30 - 13:30) where low participation leads to erratic stop hunting."
        action = "Halt trading for the remainder of today. Enforce a strict no-trade rule between 11:45 and 13:15 IST."
    elif any("LIQUIDITY_TRAP" in c for c in causes):
        regime = "Choppy Range Expansion / False Breakout Trap"
        pattern = "Price probed beyond PDH/PDL but immediately met institutional counter-orders, trapping trend followers."
        action = "Pause for 3 trading sessions per strategy rules. Require 15m candle close confirmation before entering breakout retests."
    else:
        regime = "Adverse Trend Momentum / Volatility Expansion"
        pattern = "Consecutive momentum rejections against prevailing order flow without volume confirmation."
        action = "Trading is benched until the next scheduled trading day. Re-verify market alignment against India VIX and VWAP before next entry."

    return (
        f"🧠 REGIME DIAGNOSIS: {regime}\n\n"
        f"⚠️ COMMON FAILURE PATTERN: {pattern}\n\n"
        f"🛡️ ACTION PLAN: {action}"
    )


def format_expert_alert(expert_report: str, break_until: str) -> str:
    """Formats the urgent 3-loss circuit breaker expert report for Telegram."""
    return (
        f"🚨 QUANT EXPERT REVIEW: 3 CONSECUTIVE LOSSES\n"
        f"🛑 Circuit Breaker Activated | Trading PAUSED until {break_until}\n\n"
        f"{expert_report}\n\n"
        f"📌 System Status: All new entries temporarily locked to safeguard capital."
    )
