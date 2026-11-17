"""Offline research: immediate completed-1m reclaim, known local pivots only."""

from copy import deepcopy
from datetime import datetime, timedelta

from app.trading import structure_entry_research as base


def reclaim_signals(rows, day):
    if not rows:
        return []
    now = datetime.fromisoformat(rows[-1]["date"]) + timedelta(minutes=1)
    bars = base._validated(rows, now)
    state = base.initial_state(day)
    pivots = {"low": [], "high": []}
    signals = []
    for i, bar in enumerate(bars):
        stamp = datetime.fromisoformat(bar["date"])
        close = stamp + timedelta(minutes=1)
        for side, field, other in [("CALL", "low", "high"), ("PUT", "high", "low")]:
            sign = 1 if side == "CALL" else -1
            recent = [
                p
                for p in pivots[field]
                if stamp - datetime.fromisoformat(p["at"]) <= timedelta(minutes=30)
            ]
            if recent and "09:30" <= close.strftime("%H:%M") < "11:00":
                pivot = recent[-1]
                if (
                    sign * (bar[field] - pivot["value"]) < 0
                    and sign * (bar["close"] - pivot["value"]) >= 0
                ):
                    signal = base._signal(
                        state,
                        "IMMEDIATE_1M_RECLAIM",
                        side,
                        bar,
                        bar[field] - sign * 2,
                        [pivot["at"], bar["date"]],
                        pivot["value"],
                        pivot["known_at"],
                        pivots[other],
                    )
                    if signal:
                        signals.append(signal)
        if i >= 2:
            left, middle = bars[i - 2], bars[i - 1]
            for field, sign in [("low", 1), ("high", -1)]:
                if (
                    sign * (middle[field] - left[field]) < 0
                    and sign * (middle[field] - bar[field]) <= 0
                ):
                    pivots[field].append(
                        {
                            "at": middle["date"],
                            "value": middle[field],
                            "known_at": close.isoformat(),
                        }
                    )
    return signals


def candidates(original, direct, variant):
    # Apply the timestamp gate before merging, without future trade outcomes.
    selected = []
    if variant == "CONTROL":
        return base.merge_candidates(original)
    if variant == "CONFIRM_0930_1100":
        return base.merge_candidates(
            [s for s in original if "09:30" <= s["signal_at"][11:16] < "11:00"]
        )
    end = "10:30" if variant == "RECLAIM_0930_1030" else "11:00"
    if variant != "REVERSAL_ONLY_0930_1100":
        selected.extend(
            s
            for s in original
            if s.get("source_family") != "LOCAL_SWEEP_RECLAIM"
            and "09:30" <= s["signal_at"][11:16] < end
        )
    selected.extend(s for s in direct if "09:30" <= s["signal_at"][11:16] < end)
    if variant == "RECLAIM_PLUS_AFTERNOON":
        selected.extend(
            s for s in original if "12:00" <= s["signal_at"][11:16] < "15:00"
        )
    return base.merge_candidates(deepcopy(selected))
