"""Versioned, offline research additions to the zone strategy. No orders or delivery.

This module is deliberately not registered with the operational paper supervisor.
The old scanner and exit sources remain frozen; their valid outputs are adapted
to a strict completed-bar contract under a new research identity.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

import pandas as pd

from app.trading import market_validation as mv
from app.trading.crew.exit_agent import ExitAgent
from app.trading.option_quote import IST
from app.trading.scanner_crew.detector_agent import CandidateSetup, DetectorAgent
from app.trading.scanner_crew.levels_agent import LevelsAgent
from app.trading.scanner_crew.news_agent import NewsSentiment

VERSION = "zone-plus-legacy-research-v1"
RULES = {
    "version": VERSION,
    "native_timeframes": ["1m", "5m", "15m"],
    "five_minute_only": ["LIQ_SWEEP_PDHL", "RETEST_OR30"],
    "signal_max_age_seconds": 90,
    "initial_risk_points": [10, 55],
    "priority": "existing zone; native 5m, 15m, 1m; family order; stable signal id",
    "target": "3R underlying reference; optional single liquidity hop",
    "milestones": {"breakeven_r": 0.7, "lock_at_r": 2, "lock_r": 0.8},
    "monotonic_stop": True,
    "structure_min_current_r": 1.5,
    "stagnation": "6 bars; 10 when breakeven locked and current R>=0.2; current R<1.8",
    "opposing_exit": "fresh confirmed opposite direction only, without same-direction conflict",
    "live_registration": False,
}
FAMILIES = {
    "LIQ_SWEEP_PDHL": r"Liquidity Sweep \[\w+\]",
    "RETEST_PD": r"Breakout & Retest \(PD\) \[\w+\]",
    "RETEST_OR30": r"Breakout & Retest \(OR-30\) \[\w+\]",
    "TRENDLINE_CHOCH": r"Trendline Reversal \(\w+\)",
    "EQHL_SWEEP_INLINE": r"Equal (?:Highs|Lows) Sweep \[\w+\]",
}


def identity() -> str:
    backend = Path(__file__).resolve().parents[2]
    sources = [
        Path(__file__),
        *[
            backend / p
            for p in (
                "app/liquidity_signals.py",
                "app/trading/scanner_crew/detector_agent.py",
                "app/trading/scanner_crew/levels_agent.py",
                "app/trading/scanner_crew/consolidation_agent.py",
                "app/trading/crew/exit_agent.py",
                "app/trading/market_validation.py",
                "app/trading/forward_zone_candidate.py",
            )
        ],
    ]
    payload = {
        "rules": RULES,
        "sources": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sources
        },
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _aware(value) -> datetime:
    stamp = datetime.fromisoformat(str(value))
    return (
        stamp.replace(tzinfo=IST)
        if stamp.utcoffset() is None
        else stamp.astimezone(IST)
    )


@lru_cache(maxsize=20000)
def _scanner_stamp(value: str) -> str:
    return _aware(value).strftime("%Y-%m-%d %H:%M")


def family_of(name: str) -> str | None:
    return next(
        (family for family, pattern in FAMILIES.items() if re.fullmatch(pattern, name)),
        None,
    )


def normalize_setup(
    setup: CandidateSetup, frame: pd.DataFrame, now: datetime
) -> tuple[dict | None, str | None]:
    """Bind the signal to its actual source candle, not an assumed stamp offset."""
    if now.utcoffset() is None:
        raise ValueError("Timezone-aware research clock required")
    family, tf = family_of(setup.strategy), setup.timeframe
    if family is None or tf not in RULES["native_timeframes"]:
        return None, "UNSUPPORTED_FAMILY_OR_TIMEFRAME"
    if family in RULES["five_minute_only"] and tf != "5m":
        return None, "FROZEN_HELPER_IS_5M_ONLY"
    if not setup.is_valid_for_trade_crew or not setup.event_risk_cleared:
        return None, "SETUP_VETO_OR_NEWS"
    try:
        span = timedelta(minutes=int(tf[:-1]))
        stamp = _aware(setup.timestamp)
        # Inline equal-level sweeps stamp the start. Retest/trendline helpers
        # stamp the true close with bar_minutes; the sweep helper is 5m only.
        opening = stamp if family == "EQHL_SWEEP_INLINE" else stamp - span
        closing = opening + span
        if opening.date() != now.astimezone(IST).date():
            return None, "OFF_SESSION_SIGNAL"
        if closing > now:
            return None, "FORMING_SIGNAL"
        if (now - closing).total_seconds() > RULES["signal_max_age_seconds"]:
            return None, "STALE_SIGNAL"
        rows = [
            row
            for row in frame.to_dict(orient="records")
            if _aware(row["date"]) == opening
        ]
        if len(rows) != 1 or _aware(rows[0]["date"]) + span > now:
            return None, "SOURCE_CANDLE_UNVERIFIED"
        entry, stop = float(setup.trigger_price), float(setup.tentative_stop)
        sign = 1 if setup.direction == "CALL" else -1 if setup.direction == "PUT" else 0
        if not sign or not all(math.isfinite(v) and v > 0 for v in (entry, stop)):
            return None, "INVALID_PRICE_OR_SIDE"
        if abs(entry - float(rows[0]["close"])) > 0.011:
            return None, "ENTRY_NOT_SOURCE_CLOSE"
        risk = sign * (entry - stop)
        if not 10 <= risk <= 55:
            return None, "INITIAL_RISK_OUTSIDE_10_55"
    except (TypeError, ValueError, KeyError):
        return None, "MALFORMED_SOURCE_SIGNAL"
    signal = {
        "version": VERSION,
        "day": opening.date().isoformat(),
        "side": setup.direction,
        "signal_at": closing.isoformat(),
        "entry_spot": entry,
        "stop_spot": stop,
        "risk_points": round(risk, 2),
        "target_spot": round(entry + sign * 3 * risk, 2),
        "zone_center": entry,
        "zone_known_at": closing.isoformat(),
        "source_family": family,
        "source_strategy": setup.strategy,
        "source_timeframe": tf,
        "source_bar_start": opening.isoformat(),
        "source_bar_close": closing.isoformat(),
        "source_native_timestamp": setup.timestamp,
    }
    signal["signal_id"] = hashlib.sha256(
        json.dumps(signal, sort_keys=True).encode()
    ).hexdigest()[:24]
    return signal, None


@dataclass
class Snapshot:
    signals: list[dict] = field(default_factory=list)
    levels: list[float] = field(default_factory=list)
    frames: dict[str, pd.DataFrame] = field(default_factory=dict)
    rejections: list[dict] = field(default_factory=list)


class LegacyEntries:
    """Read-only scanner adapter; callers supply candles, calendar and news."""

    def __init__(self):
        self.detector = DetectorAgent()
        self.levels = LevelsAgent()
        self._scanned_closes: dict[str, datetime] = {}

    def scan(
        self,
        *,
        raw_frames: dict,
        daily: pd.DataFrame,
        now: datetime,
        holidays: dict,
        news: NewsSentiment | None,
    ) -> Snapshot:
        if now.utcoffset() is None:
            raise ValueError("Timezone-aware research clock required")
        result = Snapshot()
        if news is None or not news.is_trading_safe or news.high_impact_event_active:
            result.rejections.append({"reason": "NEWS_UNAVAILABLE_OR_UNSAFE"})
            return result
        for tf in RULES["native_timeframes"]:
            frame, why = mv.session_frame(
                raw_frames.get(tf), now, int(tf[:-1]), holidays
            )
            result.frames[tf] = frame
            if why:
                result.rejections.append({"timeframe": tf, "reason": why})
        problem = mv.previous_session_problem(daily, now, holidays)
        if problem:
            result.rejections.append(
                {"reason": "PREVIOUS_SESSION_LEVELS_UNVERIFIED", "detail": problem}
            )
            return result
        # Native helpers expect Yahoo-style minute text. Validate before this
        # conversion so offsets and the real candle duration cannot be hidden.
        for tf, frame in result.frames.items():
            frame = frame.copy()
            if not frame.empty:
                frame["date"] = frame["date"].map(lambda x: _scanner_stamp(str(x)))
            result.frames[tf] = frame
        levels = self.levels.extract_session_levels(
            daily,
            result.frames["5m"],
            option_contracts=None,
            session_date=now.astimezone(IST).date().isoformat(),
        )
        result.levels = list(levels.all_key_levels)
        changed = {}
        closes = {}
        for tf, frame in result.frames.items():
            if frame.empty:
                continue
            closing = _aware(frame.iloc[-1]["date"]) + timedelta(minutes=int(tf[:-1]))
            if self._scanned_closes.get(tf) != closing:
                changed[tf], closes[tf] = frame, closing
        # Re-scanning an unchanged source candle cannot create a new candle
        # confirmation. Persisted merge_signals deduplication still protects a
        # process restart, when this performance cache starts empty.
        setups = self.detector.scan_multi_timeframe_strategies(
            changed,
            levels,
            news_sentiment=news,
            current_time_str=now.astimezone(IST).strftime("%H:%M"),
            strategy_filter="auto",
            allow_reentry=True,
        )
        self._scanned_closes.update(closes)
        unique = {}
        for setup in setups:
            signal, why = normalize_setup(
                setup, result.frames.get(setup.timeframe, pd.DataFrame()), now
            )
            if signal is None:
                result.rejections.append(
                    {
                        "strategy": setup.strategy,
                        "timeframe": setup.timeframe,
                        "reason": why,
                    }
                )
            else:
                unique[signal["signal_id"]] = signal
        result.signals = sorted(unique.values(), key=priority)
        return result


def priority(signal: dict) -> tuple:
    family = signal.get("source_family", "ZONE")
    return (
        signal["signal_at"],
        0 if family == "ZONE" else 1,
        {"5m": 0, "15m": 1, "1m": 2}.get(signal.get("source_timeframe"), 0),
        list(FAMILIES).index(family) if family in FAMILIES else -1,
        signal["signal_id"],
    )


def opposing_only(signals: list[dict], side: str, now: datetime) -> bool:
    fresh = {
        s["side"]
        for s in signals
        if 0 <= (now - _aware(s["signal_at"])).total_seconds() <= 90
    }
    return bool(fresh and side not in fresh)


def merge_signals(
    state: dict, zone_signals: list[dict], native_signals: list[dict]
) -> tuple[dict, list[dict]]:
    """Namespace zone entries and consume each native candle confirmation once.

    A setup still inside the 90-second freshness window must not become a new
    opposing confirmation a minute after losing a same-bar priority decision.
    """
    result = []
    seen = set(state.get("legacy_seen", []))
    for original in zone_signals:
        item = {
            **original,
            "version": VERSION,
            "origin_signal_id": original["signal_id"],
            "source_family": "ZONE",
            "source_strategy": "Zone sweep + 1m confirmation",
            "source_timeframe": "1m",
            "source_bar_close": original["signal_at"],
            "source_bar_start": (
                _aware(original["signal_at"]) - timedelta(minutes=1)
            ).isoformat(),
        }
        item.pop("signal_id")
        item["signal_id"] = hashlib.sha256(
            json.dumps(item, sort_keys=True).encode()
        ).hexdigest()[:24]
        result.append(item)
    for signal in native_signals:
        key = ":".join(
            signal[k]
            for k in ("source_family", "source_timeframe", "side", "source_bar_close")
        )
        if key not in seen:
            seen.add(key)
            result.append(dict(signal))
    return {**state, "legacy_seen": sorted(seen)}, sorted(result, key=priority)


def tighten(side: str, old: float, proposed: float) -> float:
    return max(old, proposed) if side == "CALL" else min(old, proposed)


def legacy_exit(
    trade: dict,
    *,
    spot: float,
    bar: dict | None,
    five: list[dict],
    levels: list[float],
    signals: list[dict],
    now: datetime,
) -> dict:
    """Old exit decisions with causal bar touches and monotonic stop updates.

    Returned index thresholds are triggers only. The account must execute from
    a fresh held-contract book, persist every partial and charge every order.
    """
    if now.utcoffset() is None:
        raise ValueError("Timezone-aware research clock required")
    now = now.astimezone(IST)
    side = trade["side"]
    sign = 1 if side == "CALL" else -1
    stop = trade.get("runner_stop", trade["stop_spot"])
    target = trade.get("runner_target", trade["target_spot"])
    lots = trade.get("remaining_lots", trade["lots"])
    eligible = bool(bar and _aware(bar["date"]) >= _aware(trade["opened_at"]))
    stop_touch = sign * (spot - stop) <= 0 or bool(
        eligible and (bar["low"] <= stop if sign == 1 else bar["high"] >= stop)
    )
    target_touch = sign * (spot - target) >= 0 or bool(
        eligible and (bar["high"] >= target if sign == 1 else bar["low"] <= target)
    )
    if now.strftime("%H:%M") >= "15:10":
        return {"action": "INTRADAY_CLOSE", "is_fully_closed": True}
    effective = stop if stop_touch else target if target_touch else spot
    state = {
        "direction": side,
        "entry_price": trade["entry_spot"],
        "stop_loss": stop,
        "target_price": target,
        "risk_points": trade["risk_points"],
        "lots": lots,
        "be_locked": trade.get("milestone_be", False),
        "has_hopped": trade.get("has_hopped", False),
    }
    bars = max(0, int((now - _aware(trade["opened_at"])).total_seconds() // 300))
    decision = ExitAgent(enable_liquidity_hopping=True).evaluate_exit(
        state,
        effective,
        df_5m=pd.DataFrame(five),
        opposing_signal_detected=opposing_only(signals, side, now),
        current_time_str=now.strftime("%H:%M"),
        key_levels=levels,
        bars_in_trade=bars,
    )
    result = asdict(decision)
    if decision.action == "HOLD":
        current_r = sign * (spot - trade["entry_spot"]) / trade["risk_points"]
        milestones = list(trade.get("research_milestones", []))
        new_stop = stop
        if current_r >= 0.7 and "0.7R" not in milestones:
            milestones.append("0.7R")
            new_stop = tighten(side, stop, trade["entry_spot"])
        elif current_r >= 2 and "2R" not in milestones:
            milestones.append("2R")
            new_stop = tighten(
                side,
                stop,
                round(trade["entry_spot"] + sign * 0.8 * trade["risk_points"], 2),
            )
        result["updates"] = {
            "runner_stop": new_stop,
            "research_milestones": milestones,
            "milestone_be": "0.7R" in milestones,
        }
    elif decision.action == "LIQUIDITY_HOP_PROMOTED":
        result["ratcheted_sl"] = tighten(side, stop, decision.ratcheted_sl)
    return result
