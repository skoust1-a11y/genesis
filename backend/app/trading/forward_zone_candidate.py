"""Versioned experimental zone detector. Closed bars only; no fills or network."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

VERSION = "zone10-5m-1m-v1"
RULES = {
    "version": VERSION,
    "zone_half_width_points": 10,
    "zone_pair_max_difference": 20,
    "zone_timeframe": "5m",
    "confirmation_timeframe": "1m",
    "pivot_right_bars": 1,
    "pivot_separation_bars": 4,
    "pullback_points": 15,
    "trigger_buffer_points": 0.05,
    "stop_buffer_points": 2,
    "entry_confirmation": "completed 1m close beyond rejection high or low",
    "pending_minutes": 20,
    "risk_points_min": 10,
    "risk_points_max": 55,
    "target_r": 3,
    "last_entry": "15:00",
    "square_off": "15:10",
    "one_attempt_per_zone": True,
    "max_positions": 1,
    "otm_steps": 2,
}
RULE_HASH = hashlib.sha256(json.dumps(RULES, sort_keys=True).encode()).hexdigest()


def initial_state(day: str) -> dict:
    return {"day": day, "last_bar": "", "zones": [], "pending": [], "known": []}


def _five(rows):
    out = []
    for i in range(0, len(rows) - 4, 5):
        chunk = rows[i : i + 5]
        out.append(
            {
                "date": chunk[0]["date"],
                "open": chunk[0]["open"],
                "high": max(x["high"] for x in chunk),
                "low": min(x["low"] for x in chunk),
                "close": chunk[-1]["close"],
            }
        )
    return out


def _centres(bars):
    for side, field, comparison in (("PUT", "high", max), ("CALL", "low", min)):
        pivots = [
            (i, b[field])
            for i, b in enumerate(bars[1:-1], 1)
            if b[field] == comparison(x[field] for x in bars[i - 1 : i + 2])
        ]
        for a, (i, value) in enumerate(pivots):
            for j, other in pivots[a + 1 :]:
                if j - i < 4 or abs(value - other) > 20:
                    continue
                span = bars[i : j + 1]
                depth = (
                    min(value, other) - min(x["low"] for x in span)
                    if side == "PUT"
                    else max(x["high"] for x in span) - max(value, other)
                )
                if depth >= 15:
                    yield side, round((value + other) / 2, 1)


def advance(rows: list[dict], state: dict) -> tuple[dict, list[dict]]:
    """Deterministic, restartable detector over a validated contiguous current-day 1m frame.

    No historical order is generated here. The runtime rejects old signals and
    prices accepted ones only against a contemporaneous executable option book.
    """
    signals = []
    for i, bar in enumerate(rows):
        stamp = bar["date"]
        if stamp <= state["last_bar"]:
            continue
        opening = datetime.fromisoformat(stamp)
        close = opening + timedelta(minutes=1)
        close_text = close.isoformat()
        state["pending"] = [
            x
            for x in state["pending"]
            if x["expires"] >= close_text and not state["zones"][x["zone"]]["used"]
        ]
        for pending in list(state["pending"]):
            zone = state["zones"][pending["zone"]]
            sign = 1 if zone["side"] == "CALL" else -1
            if (
                opening.isoformat() < pending["ready"]
                or sign * (bar["close"] - pending["trigger"]) < 0
            ):
                continue
            zone["used"] = True
            entry, stop = float(bar["close"]), pending["stop"]
            risk = sign * (entry - stop)
            signal = {
                "version": VERSION,
                "day": state["day"],
                "side": zone["side"],
                "zone_center": zone["center"],
                "signal_at": close_text,
                "entry_spot": entry,
                "stop_spot": stop,
                "target_spot": round(entry + sign * 3 * risk, 2),
                "risk_points": round(risk, 2),
                "zone_known_at": zone["known_at"],
            }
            signal["signal_id"] = hashlib.sha256(
                json.dumps(signal, sort_keys=True).encode()
            ).hexdigest()[:24]
            signals.append(signal)
        state["pending"] = [
            x for x in state["pending"] if not state["zones"][x["zone"]]["used"]
        ]
        for index, zone in enumerate(state["zones"]):
            if zone["used"] or zone["known_at"] > opening.isoformat():
                continue
            lo, hi = zone["center"] - 10, zone["center"] + 10
            swept = bar["high"] > hi if zone["side"] == "PUT" else bar["low"] < lo
            if swept and lo <= bar["close"] <= hi:
                state["pending"] = [x for x in state["pending"] if x["zone"] != index]
                state["pending"].append(
                    {
                        "zone": index,
                        "ready": close_text,
                        "expires": (close + timedelta(minutes=20)).isoformat(),
                        "trigger": bar["high"] + 0.05
                        if zone["side"] == "CALL"
                        else bar["low"] - 0.05,
                        "stop": bar["low"] - 2
                        if zone["side"] == "CALL"
                        else bar["high"] + 2,
                    }
                )
        # The 09:15 aligned frame is validated by the caller before reaching here.
        if (i + 1) % 5 == 0:
            for side, center in sorted(set(_centres(_five(rows[: i + 1])))):
                key = f"{side}:{center}"
                if key in state["known"]:
                    continue
                state["known"].append(key)
                if any(
                    z["side"] == side and abs(z["center"] - center) <= 20
                    for z in state["zones"]
                ):
                    continue
                state["zones"].append(
                    {
                        "side": side,
                        "center": center,
                        "known_at": close_text,
                        "used": False,
                    }
                )
        state["last_bar"] = stamp
    return state, signals
