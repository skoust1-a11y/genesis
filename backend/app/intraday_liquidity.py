"""Intraday NIFTY liquidity levels and one sweep entry routine for all of them. Research only: sends no alerts or
orders. The PDH/PDL strategy stays in app.liquidity_signals and is not changed by this module.

Every level is built from completed 5m bars and carries `known`, the index of the first 5m bar allowed to use it:
  swing      2-left/2-right swing highs/lows on 5m and 15m, known once both right bars have closed. "external" when
             it was the session's extreme at that moment (major liquidity), else internal (minor)
  or15/or30  opening range high/low, known at 09:30 / 09:45
  equal      2+ confirmed 5m swings within EQ_TOL x ATR, EQ_GAP+ bars apart, with an EQ_PULL x ATR pullback between
             them and no trade through the level in between (ordinary noise fails one of these)
  range      consolidation box: CONS_BARS bars spanning at most CONS_ATR x ATR whose neighbours overlap CONS_OVERLAP+
  hour       the previous completed 60m candle (09:15-10:15, ...), live only during the following hour

Entry (long; short mirrors): a 5m low trades TICK below an intact low level (the bar before closed above it) -> a 5m
close back above every level swept, within RECLAIM_BARS -> structure shift: 5m (or 15m) close above the last
confirmed swing high, within MSS_BARS of the sweep -> close above VWAP, entries 09:30-14:30. Stop: the sweep low
- max(2, 0.1 ATR), the level's invalidation, not the PDH/PDL swing stop. One trade per side per day per strategy.
"""

from __future__ import annotations

from bisect import bisect_left
from functools import lru_cache
from itertools import pairwise

from app.liquidity_signals import (
    FIRST_ENTRY,
    LAST_ENTRY,
    MIN_RISK,
    SWING,
    _at,
    pivots,
    to_15m,
)

_at = lru_cache(maxsize=1 << 16)(_at)  # strftime dominates a multi-year scan
TICK, RECLAIM_BARS, MSS_BARS = 1.0, 6, 12
EQ_TOL, EQ_GAP, EQ_PULL = 0.15, 4, 0.5
CONS_BARS, CONS_ATR, CONS_OVERLAP = 6, 1.5, 0.5


def atr(bars: list[dict], n: int = 14) -> list[float]:
    """Mean true range of the last `n` bars up to and including each bar; a session's first bar ignores the gap."""
    out, trs = [], []
    for k, b in enumerate(bars):
        prev = bars[k - 1]["close"] if k and bars[k - 1]["date"][:10] == b["date"][:10] else None
        trs.append(b["high"] - b["low"] if prev is None else max(b["high"], prev) - min(b["low"], prev))
        out.append(sum(trs[-n:]) / len(trs[-n:]))
    return out


def _level(kind: str, side: str, price: float, known: int, formed: str, tf: int = 5, **extra) -> dict:
    return {"id": f"{kind}{tf}-{side}-{formed[11:]}", "kind": kind, "side": side, "price": price, "known": known,
            "formed": formed, "tf": tf, "until": None, "touches": 1, "external": False, **extra}  # fmt: skip


def swing_levels(bars: list[dict]) -> list[dict]:
    dates, out = [b["date"] for b in bars], []
    for tf, src in ((5, bars), (15, to_15m(bars))):
        for side in ("high", "low"):
            for j in pivots(src, side):
                known = bisect_left(dates, _at(src[j + SWING]["date"], tf))
                if known >= len(bars):
                    continue
                seen = [b[side] for b in bars[:known]]
                extreme = max(seen) if side == "high" else min(seen)
                out.append(_level("swing", side, src[j][side], known, src[j]["date"], tf, external=src[j][side] == extreme))
    return out


def opening_range_levels(bars: list[dict]) -> list[dict]:
    dates, out = [b["date"] for b in bars], []
    for mins in (15, 30):
        end = _at(f"{bars[0]['date'][:10]} 09:15", mins)
        first, known = [b for b in bars if b["date"] < end], bisect_left(dates, end)
        if first and known < len(bars):
            out += [_level(f"or{mins}", "high", max(b["high"] for b in first), known, first[0]["date"], mins),
                    _level(f"or{mins}", "low", min(b["low"] for b in first), known, first[0]["date"], mins)]  # fmt: skip
    return out


def equal_levels(bars: list[dict], atrs: list[float]) -> list[dict]:
    out = []
    for side in ("high", "low"):
        hi = side == "high"
        piv = pivots(bars, side)
        for a, j in enumerate(piv):
            if j + SWING + 1 >= len(bars):
                continue
            pool = []
            for i in piv[:a]:
                top = max(bars[i][side], bars[j][side]) if hi else min(bars[i][side], bars[j][side])
                between = bars[i + 1 : j]
                if j - i < EQ_GAP or abs(bars[i][side] - bars[j][side]) > EQ_TOL * atrs[j]:
                    continue
                pulled = top - min(b["low"] for b in between) if hi else max(b["high"] for b in between) - top
                intact = all(b["high"] <= top + TICK if hi else b["low"] >= top - TICK for b in between)
                if pulled >= EQ_PULL * atrs[j] and intact:
                    pool.append(i)
            if pool:
                prices = [bars[k][side] for k in (*pool, j)]
                out.append(_level("equal", side, max(prices) if hi else min(prices), j + SWING + 1,
                                  bars[pool[0]]["date"], touches=len(prices)))  # fmt: skip
    return out


def range_levels(bars: list[dict], atrs: list[float]) -> list[dict]:
    out, box = [], None
    for i in range(CONS_BARS - 1, len(bars) - 1):
        w = bars[i - CONS_BARS + 1 : i + 1]
        hi, lo = max(b["high"] for b in w), min(b["low"] for b in w)
        overlap = sum(max(0.0, min(a["high"], b["high"]) - max(a["low"], b["low"]))
                      / max(min(a["high"] - a["low"], b["high"] - b["low"]), 0.05) for a, b in pairwise(w)) / (CONS_BARS - 1)  # fmt: skip
        if hi - lo > CONS_ATR * atrs[i] or overlap < CONS_OVERLAP or (box and box[1] <= lo and hi <= box[0]):
            continue
        box, near = (hi, lo), 0.1 * atrs[i]
        out += [_level("range", "high", hi, i + 1, w[0]["date"], touches=sum(b["high"] >= hi - near for b in w)),
                _level("range", "low", lo, i + 1, w[0]["date"], touches=sum(b["low"] <= lo + near for b in w))]  # fmt: skip
    return out


def hour_levels(bars: list[dict]) -> list[dict]:
    dates, day, out = [b["date"] for b in bars], bars[0]["date"][:10], []
    for k in range(6):
        start = _at(f"{day} 09:15", 60 * k)
        end = _at(start, 60)
        hour, known = [b for b in bars if start <= b["date"] < end], bisect_left(dates, end)
        if hour and known < len(bars):
            until = bisect_left(dates, _at(end, 60))
            out += [_level("hour", "high", max(b["high"] for b in hour), known, start, 60, until=until),
                    _level("hour", "low", min(b["low"] for b in hour), known, start, 60, until=until)]  # fmt: skip
    return out


def day_levels(bars: list[dict], atrs: list[float]) -> dict[str, list[dict]]:
    """Each strategy's levels for one session."""
    orl = opening_range_levels(bars)
    return {"swing": swing_levels(bars), "or15": [lv for lv in orl if lv["tf"] == 15],
            "or30": [lv for lv in orl if lv["tf"] == 30], "equal": equal_levels(bars, atrs),
            "range": range_levels(bars, atrs), "hour": hour_levels(bars)}  # fmt: skip


def _last_pivots(bars: list[dict], key: str) -> list[float | None]:
    """Per bar: the last swing high/low confirmed by the bar before it closing (pivot index <= i - 3)."""
    piv, last, out = set(pivots(bars, key)), None, []
    for i in range(len(bars)):
        if i - SWING - 1 in piv:
            last = bars[i - SWING - 1][key]
        out.append(last)
    return out


def _mss15(bars: list[dict]) -> dict[int, tuple[float, float | None, float | None]]:
    """5m index completing a 15m bar -> (15m close, last confirmed 15m swing high, swing low) before that bar."""
    b15, out = to_15m(bars), {}
    hi, lo = _last_pivots(b15, "high"), _last_pivots(b15, "low")
    at = {_at(x["date"], 10): k for k, x in enumerate(b15)}
    for i, b in enumerate(bars):
        if (k := at.get(b["date"])) is not None:
            out[i] = (b15[k]["close"], hi[k], lo[k])
    return out


def nearest_opposing(levels: list[dict], bars: list[dict], i: int, long: bool, entry: float, min_gap: float) -> float | None:
    """Closest intact level (known by bar i, not traded through since) at least `min_gap` past `entry` on the
    target side, or None."""
    side, out = "high" if long else "low", None
    for lv in levels:
        p = lv["price"]
        if lv["side"] != side or lv["known"] > i or (p - entry if long else entry - p) < min_gap:
            continue
        since = bars[lv["known"] : i + 1]
        if since and (max(b["high"] for b in since) > p if long else min(b["low"] for b in since) < p):
            continue
        out = p if out is None or (p < out if long else p > out) else out
    return out


def scan_levels(bars: list[dict], vwap: list[float], atrs: list[float], levels: list[dict], strategy: str,
                mss_tf: int = 5, min_risk: float = MIN_RISK, target_r: float = 2.0,
                opposing: list[dict] | None = None, min_sweep: float = 2.0,
                with_open_only: bool = False, min_displacement: float = 0.0,
                require_trendline: bool = False, skip_lunch: bool = False) -> list[dict]:  # fmt: skip
    """Signals in one session's completed 5m `bars` (vwap/atrs aligned to them) from sweeps of `levels`.
    `mss_tf` 5 or 15: the structure shift's timeframe; 0: no structure shift. `opposing` (default `levels`): pool for
    the opposing-liquidity target `opp` (nearest intact level 1R+ away).
    `min_sweep`: minimum sweep depth past the level (default 2.0 pts to filter noise).
    `with_open_only`: only take Longs above day open, Shorts below day open.
    `min_displacement`: minimum real body size (in ATR) for the entry candle.
    `require_trendline`: enforce breakdown below ascending trendline (Short) or breakout above descending trendline (Long).
    `skip_lunch`: avoid entries between 11:30 and 13:00 IST to prevent lunch lull chop."""
    if not bars:
        return []
    sh5, sl5, m15, out = _last_pivots(bars, "high"), _last_pivots(bars, "low"), _mss15(bars) if mss_tf == 15 else {}, []
    day_open = bars[0]["open"]
    for side in ("Long", "Short"):
        long, used, setup = side == "Long", set(), None
        key = "low" if long else "high"
        pool = [lv for lv in levels if lv["side"] == key]
        for i, b in enumerate(bars):
            closes_at = _at(b["date"], 5)
            if closes_at[11:] > LAST_ENTRY:
                break
            if skip_lunch and "11:30" <= closes_at[11:] <= "13:00":
                continue
            for lv in pool:
                if lv["id"] in used or lv["known"] > i or (lv["until"] is not None and i >= lv["until"]):
                    continue
                p = lv["price"]
                if (b["low"] <= p - min_sweep) if long else (b["high"] >= p + min_sweep):
                    used.add(lv["id"])
                    if i and (bars[i - 1]["close"] > p if long else bars[i - 1]["close"] < p):
                        setup = setup or {"i": i, "ext": b[key], "levels": []}
                        setup["levels"].append(lv)
            if setup is None:
                continue
            setup["ext"] = min(setup["ext"], b["low"]) if long else max(setup["ext"], b["high"])
            lvl = max(lv["price"] for lv in setup["levels"]) if long else min(lv["price"] for lv in setup["levels"])
            c, sign = b["close"], 1 if long else -1
            beyond = (c - lvl) * sign > 0
            if beyond and "reclaim" not in setup:
                setup["reclaim"] = i
            if "reclaim" not in setup and i - setup["i"] >= RECLAIM_BARS:
                setup = None
                continue
            if "mss" not in setup:
                ref, close = (sh5[i] if long else sl5[i]), c
                if mss_tf == 15:
                    close, hi15, lo15 = m15.get(i, (None, None, None))
                    ref = hi15 if long else lo15
                if mss_tf == 0 or (ref is not None and close is not None and (close - ref) * sign > 0):
                    setup["mss"] = i
            if ("reclaim" in setup and "mss" in setup and beyond and closes_at[11:] >= FIRST_ENTRY
                    and (c > vwap[i]) == long):  # fmt: skip
                if with_open_only and ((c > day_open) != long):
                    continue
                disp = (c - b["open"]) * sign
                if min_displacement > 0.0 and disp < min_displacement * atrs[i]:
                    continue

                # Trendline Confluence Check
                tl_key = "high" if long else "low"
                piv = pivots(bars[: i + 1], tl_key)
                tl_ok, tl_desc = True, ""
                if len(piv) >= 2:
                    x1, x2 = piv[-2], piv[-1]
                    y1, y2 = bars[x1][tl_key], bars[x2][tl_key]
                    if x2 != x1:
                        slope = (y2 - y1) / (x2 - x1)
                        tl_val = y1 + slope * (i - x1)
                        if not long and slope >= 0:
                            tl_ok = c < tl_val
                            tl_desc = f"{closes_at[11:]} trendline breakdown: 5m close {c:,.2f} below ascending trendline {tl_val:,.2f}"
                        elif long and slope <= 0:
                            tl_ok = c > tl_val
                            tl_desc = f"{closes_at[11:]} trendline breakout: 5m close {c:,.2f} above descending trendline {tl_val:,.2f}"
                if require_trendline and not tl_ok:
                    continue

                a = atrs[i]
                stop = setup["ext"] - sign * max(2.0, 0.1 * a)
                risk = abs(c - stop)
                if risk >= min_risk:
                    lvs = setup["levels"]
                    opp = nearest_opposing(opposing if opposing is not None else levels, bars, i, long, c, risk)
                    near = nearest_opposing(opposing if opposing is not None else levels, bars, i, long, c, 0.0)
                    why_items = [
                        f"{setup['levels'][0]['id']} {lvl:,.2f} swept to {setup['ext']:,.2f} at {_at(bars[setup['i']]['date'], 5)[11:]}",
                        (f"reclaim {_at(bars[setup['reclaim']]['date'], 5)[11:]}, {mss_tf or 'no'}m structure shift "
                         f"{_at(bars[setup['mss']]['date'], 5)[11:]}, entry {closes_at[11:]} {'above' if long else 'below'} VWAP"),
                    ]
                    if tl_desc:
                        why_items.append(tl_desc)
                    out.append({
                        "strategy": strategy, "side": side, "time": closes_at, "entry": c, "stop": round(stop, 2),
                        "target": round(c + sign * target_r * risk, 2), "risk": round(risk, 2), "level": lvl,
                        "opp": opp, "levels": [lv["id"] for lv in lvs], "sweep": _at(bars[setup["i"]]["date"], 5),
                        "event_id": None,
                        "features": {
                            "tf": max(lv["tf"] for lv in lvs), "touches": max(lv["touches"] for lv in lvs),
                            "external": int(any(lv["external"] for lv in lvs)), "n_levels": len(lvs),
                            "age_min": (setup["i"] - min(lv["known"] for lv in lvs)) * 5,
                            "depth_atr": round(abs(setup["ext"] - lvl) / a, 3),
                            "reclaim_atr": round(abs(c - lvl) / a, 3),
                            "displacement_atr": round((c - b["open"]) * sign / a, 3),
                            "vwap_atr": round((c - vwap[i]) * sign / a, 3),
                            "room_r": round(abs(near - c) / risk, 2) if near is not None else None,
                            "with_open": int((c - day_open) * sign > 0), "bars_to_entry": i - setup["i"],
                        },
                        "why": why_items,
                    })  # fmt: skip
                    break
                setup = None
                continue
            if setup and i - setup["i"] >= MSS_BARS:
                setup = None
    return out


def group_events(signals: list[dict]) -> list[dict]:
    """Tags each signal with `event_id`: signals on the same day and side whose sweep-to-entry windows overlap came
    from one price move and share an id (day, first sweep time, side)."""
    cur: dict | None = None
    for s in sorted(signals, key=lambda s: (s["time"][:10], s["side"], s["sweep"], s["time"])):
        if cur and cur["day"] == s["time"][:10] and cur["side"] == s["side"] and s["sweep"] <= cur["end"]:
            cur["end"] = max(cur["end"], s["time"])
        else:
            cur = {"day": s["time"][:10], "side": s["side"], "end": s["time"], "id": f"{s['sweep']} {s['side']}"}
        s["event_id"] = cur["id"]
    return signals
