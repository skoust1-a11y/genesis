"""Causal spot-entry coverage research. Not registered with the paper supervisor.

No orders, option prices, news assumptions or Telegram delivery live here. Signal
generation and account admission are deliberately separate. A rejected signal
does not prohibit a later, distinct structure at the same price level.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from datetime import datetime, timedelta

from app.trading.option_quote import IST

VERSION = "structure-entries-research-v1"
RULES = {
    "version": VERSION,
    "live_registration": False,
    "pivot_left_right": 1,
    "pivot_lookback_minutes": 30,
    "sweep_confirmation_minutes": 5,
    "minimum_pullback_points": 8,
    "pullback_pivot_separation_minutes": 5,
    "range_bars": 20,
    "range_width_points": [10, 40],
    "range_edge_tolerance_points": 5,
    "range_expiry_minutes": 20,
    "trigger_buffer_points": 0.05,
    "stop_buffer_points": 2,
    "risk_points": [10, 55],
    "signal_max_age_seconds": 90,
    "last_entry": "15:00",
    "one_position": True,
}
RULE_HASH = hashlib.sha256(json.dumps(RULES, sort_keys=True).encode()).hexdigest()


def _time(value) -> datetime:
    result = datetime.fromisoformat(str(value))
    if result.utcoffset() is None:
        raise ValueError("Timezone-aware candles and clock required")
    return result.astimezone(IST)


def initial_state(day: str) -> dict:
    return {
        "day": day,
        "rule_hash": RULE_HASH,
        "last_bar": None,
        "pivots": {"low": [], "high": []},
        "pending": {},
        "box": None,
        "box_not_before": None,
        "emitted": [],
        "decisions": {},
    }


def _validated(rows: list[dict], now: datetime) -> list[dict]:
    now = _time(now)
    result = []
    for raw in rows:
        stamp = _time(raw["date"])
        if (
            stamp.date() != now.date()
            or not "09:15" <= stamp.strftime("%H:%M") < "15:30"
        ):
            continue
        if stamp + timedelta(minutes=1) > now:
            continue
        if stamp.second or stamp.microsecond:
            raise ValueError("Candles must align to minute starts")
        row = {key: float(raw[key]) for key in ("open", "high", "low", "close")}
        if not all(math.isfinite(x) and x > 0 for x in row.values()):
            raise ValueError("Invalid OHLC")
        if (
            not row["low"]
            <= min(row["open"], row["close"])
            <= max(row["open"], row["close"])
            <= row["high"]
        ):
            raise ValueError("Invalid OHLC geometry")
        result.append({**row, "date": stamp.isoformat()})
    result.sort(key=lambda row: row["date"])
    if result:
        opening = now.replace(hour=9, minute=15, second=0, microsecond=0)
        due = min(now, now.replace(hour=15, minute=30, second=0, microsecond=0))
        expected = int((due - opening).total_seconds() // 60)
        if len(result) != expected or any(
            _time(row["date"]) != opening + timedelta(minutes=i)
            for i, row in enumerate(result)
        ):
            raise ValueError("Full contiguous current-session closed candles required")
    elif now.strftime("%H:%M") >= "09:16":
        raise ValueError("Current-session candles missing")
    return result


def _signal(state, family, side, bar, stop, identity, reference, known_at, levels):
    sign = 1 if side == "CALL" else -1
    risk = sign * (bar["close"] - stop)
    close = _time(bar["date"]) + timedelta(minutes=1)
    if not RULES["risk_points"][0] <= risk <= RULES["risk_points"][1]:
        return None
    if close.strftime("%H:%M") >= RULES["last_entry"]:
        return None
    # One signal per concrete structure, rather than one lifetime attempt per level.
    key = hashlib.sha256(
        json.dumps([VERSION, family, side, identity], sort_keys=True).encode()
    ).hexdigest()[:24]
    if key in state["emitted"]:
        return None
    state["emitted"].append(key)
    destinations = sorted(
        {
            round(p["value"], 2)
            for p in levels
            if sign * (p["value"] - bar["close"]) > 0
        },
        reverse=side == "PUT",
    )
    return {
        "version": VERSION,
        "signal_id": key,
        "day": state["day"],
        "source_family": family,
        "side": side,
        "signal_at": close.isoformat(),
        "entry_spot": bar["close"],
        "stop_spot": round(stop, 2),
        "risk_points": round(risk, 2),
        "reference": reference,
        "reference_known_at": known_at,
        "structure": identity,
        "destination_candidates": destinations,
    }


def advance(rows: list[dict], state: dict, now: datetime) -> tuple[dict, list[dict]]:
    """Process a complete session prefix; restartable through JSON state.

    Bar starts carry offsets. Pivots become usable only after their right-hand
    candle closes. A frozen range uses only bars before its breakout candle.
    Calendar/news/option eligibility remain the caller's separate responsibility.
    """
    now = _time(now)
    bars = _validated(rows, now)
    if state["day"] != now.date().isoformat() or state["rule_hash"] != RULE_HASH:
        raise ValueError("Session or research rule identity mismatch")
    state = copy.deepcopy(state)
    if state["last_bar"] and (not bars or state["last_bar"] > bars[-1]["date"]):
        raise ValueError("Cannot rewind detector state")
    signals = []
    for i, bar in enumerate(bars):
        if state["last_bar"] and bar["date"] <= state["last_bar"]:
            continue
        stamp = _time(bar["date"])
        close = stamp + timedelta(minutes=1)
        # References available at this candle's OPEN. Current candle cannot
        # confirm a pivot and then retroactively break that same reference.
        pivots = state["pivots"]
        recent = {
            kind: [p for p in values if stamp - _time(p["at"]) <= timedelta(minutes=30)]
            for kind, values in pivots.items()
        }
        for side, field, other in (("CALL", "low", "high"), ("PUT", "high", "low")):
            sign = 1 if side == "CALL" else -1
            pending = state["pending"].get(side)
            if (
                pending
                and close <= _time(pending["expires"])
                and sign * (bar["close"] - pending["trigger"]) >= 0
            ):
                signal = _signal(
                    state,
                    "LOCAL_SWEEP_RECLAIM",
                    side,
                    bar,
                    pending["stop"],
                    [pending["pivot_at"], pending["rejection_at"]],
                    pending["reference"],
                    pending["known_at"],
                    pivots[other],
                )
                if signal:
                    signals.append(signal)
                del state["pending"][side]
            elif pending and close > _time(pending["expires"]):
                del state["pending"][side]
            # Re-entry: two confirmed same-side pivots improve; a confirmed
            # opposite swing after the older pivot provides a breakout trigger.
            same, opposite = recent[field], recent[other]
            latest = same[-1] if same else None
            separated = [
                p
                for p in same[:-1]
                if latest
                and _time(latest["at"]) - _time(p["at"]) >= timedelta(minutes=5)
            ]
            if separated:
                older = separated[-1]
                highs = [p for p in opposite if p["at"] > older["at"]]
                if sign * (latest["value"] - older["value"]) > 0 and highs:
                    trigger = highs[-1]
                    depth = sign * (trigger["value"] - latest["value"])
                    crossed = i > 0 and sign * (
                        bars[i - 1]["close"] - trigger["value"]
                    ) < 0.05 <= sign * (bar["close"] - trigger["value"])
                    if depth >= RULES["minimum_pullback_points"] and crossed:
                        signal = _signal(
                            state,
                            "PULLBACK_BREAKOUT",
                            side,
                            bar,
                            latest["value"] - sign * 2,
                            [older["at"], latest["at"], trigger["at"]],
                            trigger["value"],
                            max(latest["known_at"], trigger["known_at"]),
                            pivots[other],
                        )
                        if signal:
                            signals.append(signal)
            # Sweep/reclaim against the latest confirmed local pivot. A later
            # candle must close beyond this rejection candle's opposite extreme.
            if recent[field]:
                pivot = recent[field][-1]
                if (
                    sign * (bar[field] - pivot["value"]) < 0
                    and sign * (bar["close"] - pivot["value"]) >= 0
                ):
                    state["pending"][side] = {
                        "pivot_at": pivot["at"],
                        "rejection_at": bar["date"],
                        "reference": pivot["value"],
                        "known_at": pivot["known_at"],
                        "trigger": bar[other] + sign * 0.05,
                        "stop": bar[field] - sign * 2,
                        "expires": (close + timedelta(minutes=5)).isoformat(),
                    }
        box = state["box"]
        if box and stamp > _time(box["expires"]):
            box = state["box"] = None
        if box:
            for side, edge, stop in (
                ("CALL", box["high"], box["low"] - 2),
                ("PUT", box["low"], box["high"] + 2),
            ):
                sign = 1 if side == "CALL" else -1
                if sign * (bar["close"] - edge) >= 0.05:
                    signal = _signal(
                        state,
                        "RANGE_BREAK",
                        side,
                        bar,
                        stop,
                        [box["start"], box["known_at"]],
                        edge,
                        box["known_at"],
                        pivots["high" if side == "CALL" else "low"],
                    )
                    if signal:
                        signals.append(signal)
                    state["box"] = None
                    # Require a new, disjoint 20-bar range after a break.
                    state["box_not_before"] = (
                        close + timedelta(minutes=20)
                    ).isoformat()
                    break
        # Freeze a new box at CLOSE; it cannot fire against its own source bars.
        if (
            not state["box"]
            and i >= 19
            and (not state["box_not_before"] or close >= _time(state["box_not_before"]))
        ):
            window = bars[i - 19 : i + 1]
            lo, hi = min(r["low"] for r in window), max(r["high"] for r in window)
            low_hits = [j for j, r in enumerate(window) if r["low"] <= lo + 5]
            high_hits = [j for j, r in enumerate(window) if r["high"] >= hi - 5]
            if (
                10 <= hi - lo <= 40
                and low_hits[-1] - low_hits[0] >= 3
                and high_hits[-1] - high_hits[0] >= 3
            ):
                state["box"] = {
                    "low": lo,
                    "high": hi,
                    "start": window[0]["date"],
                    "known_at": close.isoformat(),
                    "expires": (close + timedelta(minutes=20)).isoformat(),
                }
        # Confirm the middle pivot only now, for use by the NEXT candle.
        if i >= 2:
            left, middle = bars[i - 2], bars[i - 1]
            for field, sign in (("low", 1), ("high", -1)):
                if (
                    sign * (middle[field] - left[field]) < 0
                    and sign * (middle[field] - bar[field]) <= 0
                ):
                    pivots[field].append(
                        {
                            "value": middle[field],
                            "at": middle["date"],
                            "known_at": close.isoformat(),
                        }
                    )
        state["last_bar"] = bar["date"]
    return state, sorted(
        signals, key=lambda s: (s["signal_at"], s["source_family"], s["signal_id"])
    )


def admission_reason(
    signal: dict, state: dict, now: datetime, *, position_open: bool
) -> str | None:
    """Entry coverage is not permission to trade; caller must also apply risk/quotes/news."""
    age = (_time(now) - _time(signal["signal_at"])).total_seconds()
    if signal["signal_id"] in state["decisions"]:
        return "ALREADY_DECIDED"
    if signal["day"] != _time(now).date().isoformat() or not 0 <= age <= 90:
        return "STALE_OR_FUTURE_SIGNAL"
    if _time(now).strftime("%H:%M") >= "15:00":
        return "ENTRY_CUTOFF"
    if position_open:
        return "POSITION_OR_EXIT_PENDING"
    return None


def record_decision(
    state: dict, signal: dict, *, outcome: str, reason: str, at: datetime
) -> dict:
    """Persist an admission outcome without consuming a price zone or other setups."""
    if outcome not in {"OPENED", "REJECTED"}:
        raise ValueError("Unknown decision outcome")
    if signal["signal_id"] in state["decisions"]:
        raise ValueError("Signal already decided")
    result = copy.deepcopy(state)
    result["decisions"][signal["signal_id"]] = {
        "outcome": outcome,
        "reason": reason,
        "at": _time(at).isoformat(),
    }
    return result


def merge_candidates(signals: list[dict]) -> tuple[list[dict], list[dict]]:
    """One candidate per clock/side; preserve existing-zone priority.

    Opposite directions at the same close are ambiguous and both are skipped.
    These are research scheduling decisions, not executions or stale retries.
    """
    rank = {
        "ZONE": 0,
        "LOCAL_SWEEP_RECLAIM": 1,
        "PULLBACK_BREAKOUT": 2,
        "RANGE_BREAK": 3,
    }
    grouped = {}
    for signal in signals:
        grouped.setdefault(signal["signal_at"], []).append(signal)
    accepted, suppressed = [], []
    for stamp, group in sorted(grouped.items()):
        if len({s["side"] for s in group}) > 1:
            suppressed.extend(
                {"signal_id": s["signal_id"], "at": stamp, "reason": "OPPOSING_SIGNALS"}
                for s in group
            )
            continue
        ordered = sorted(
            group,
            key=lambda s: (
                rank.get(s.get("source_family", "ZONE"), 99),
                s["signal_id"],
            ),
        )
        selected = {
            **ordered[0],
            "merged_signal_ids": [s["signal_id"] for s in ordered],
            "merged_families": sorted(
                {s.get("source_family", "ZONE") for s in ordered}
            ),
        }
        accepted.append(selected)
        suppressed.extend(
            {
                "signal_id": s["signal_id"],
                "at": stamp,
                "reason": "OVERLAPPING_SIGNAL",
                "selected": selected["signal_id"],
            }
            for s in ordered[1:]
        )
    return accepted, suppressed


def time_bucket(now: datetime) -> str:
    """Prespecified research buckets, not fitted winning entry times."""
    clock = _time(now).strftime("%H:%M")
    return (
        "OPEN_0915_1100"
        if clock < "11:00"
        else "MID_1100_1330"
        if clock < "13:30"
        else "LATE_1330_1500"
    )


def strike_step(now: datetime, expiry: str, policy: str) -> int:
    """Nearest-50 ATM convention; -1 is shallow ITM, +2 two strikes OTM.

    Time rotation is a hypothesis to test, never a profitability assertion.
    All policies retain the current OTM choice outside expiry day.
    """
    choices = {"OTM": 2, "ATM": 0, "ITM": -1}
    if policy not in {*choices, "TIME_ROTATION"}:
        raise ValueError("Unknown expiry selection policy")
    if _time(now).date().isoformat() != expiry:
        return 2
    if policy == "TIME_ROTATION":
        return {"OPEN_0915_1100": -1, "MID_1100_1330": 0, "LATE_1330_1500": 2}[
            time_bucket(now)
        ]
    return choices[policy]
