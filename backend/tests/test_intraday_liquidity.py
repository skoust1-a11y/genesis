from app.intraday_liquidity import (
    equal_levels,
    group_events,
    hour_levels,
    opening_range_levels,
    scan_levels,
    swing_levels,
)


def day(rows, start=9 * 60 + 15):
    """5m bars from 09:15; rows of (open, high, low, close)."""
    return [{"date": f"2026-10-14 {(start + 5 * k) // 60:02d}:{(start + 5 * k) % 60:02d}", "open": o, "high": h,
             "low": low, "close": c} for k, (o, h, low, c) in enumerate(rows)]  # fmt: skip


# Swing high 115 at 09:25, sweep of the 100 level at 09:40, 5m close above 115 at 09:50.
SETUP = [(110, 111, 108, 109), (109, 110, 106, 107), (107, 115, 106, 112), (112, 113, 104, 105),
         (105, 106, 101, 102), (102, 103, 97, 101), (101, 110, 100.5, 109), (109, 118, 108, 117)]  # fmt: skip


def test_levels_are_usable_only_after_the_bars_that_define_them_close():
    bars = day(SETUP)
    high = next(lv for lv in swing_levels(bars) if lv["tf"] == 5 and lv["side"] == "high" and lv["price"] == 115)
    assert high["known"] == 5  # 09:25 swing needs the 09:30 and 09:35 bars closed: first usable bar 09:40
    full = day([(100, 101, 99, 100)] * 75)
    assert [(lv["kind"], lv["known"]) for lv in opening_range_levels(full)][::2] == [("or15", 3), ("or30", 6)]
    first = hour_levels(full)[0]
    assert (first["known"], first["until"]) == (12, 24)  # 09:15-10:15 candle: used 10:15-11:15 only


def test_sweep_reclaim_structure_shift_and_vwap_make_one_long():
    bars, level = day(SETUP), [{"id": "x", "side": "low", "price": 100.0, "known": 0, "until": None, "tf": 5,
                                 "touches": 1, "external": False}]  # fmt: skip
    [s] = scan_levels(bars, [100.0] * 8, [10.0] * 8, level, "test", min_risk=10)
    assert (s["side"], s["time"], s["sweep"], s["entry"]) == ("Long", "2026-10-14 09:55", "2026-10-14 09:45", 117)
    assert (s["stop"], s["risk"], s["target"]) == (95, 22, 161)  # sweep low 97 - 2, 2R
    assert scan_levels(bars, [200.0] * 8, [10.0] * 8, level, "test", min_risk=10) == []  # below VWAP
    assert scan_levels(bars, [100.0] * 8, [10.0] * 8, level, "test") == []  # 22 pts: under the 30 pt minimum
    assert scan_levels(bars[:7], [100.0] * 7, [10.0] * 7, level, "test", min_risk=10) == []  # no structure shift yet


def test_equal_lows_need_a_pullback_and_an_untouched_level():
    lows = [105, 103, 100, 103, 106, 108, 106, 103, 100.5, 103, 105, 106, 107]
    [lv] = [x for x in equal_levels(day([(x + 2, x + 4, x, x + 2) for x in lows]), [10.0] * 13) if x["side"] == "low"]
    assert (lv["price"], lv["known"], lv["touches"]) == (100, 11, 2)
    lows[5] = 98  # traded through the level between the two touches: not a pool
    assert not [x for x in equal_levels(day([(x + 2, x + 4, x, x + 2) for x in lows]), [10.0] * 13) if x["side"] == "low"]


def test_signals_from_one_move_share_an_event_id():
    sigs = [{"time": "2026-10-14 10:00", "sweep": "2026-10-14 09:45", "side": "Long"},
            {"time": "2026-10-14 10:10", "sweep": "2026-10-14 09:55", "side": "Long"},
            {"time": "2026-10-14 11:30", "sweep": "2026-10-14 11:00", "side": "Long"},
            {"time": "2026-10-14 10:05", "sweep": "2026-10-14 09:50", "side": "Short"}]  # fmt: skip
    a, b, c, d = (s["event_id"] for s in group_events(sigs))
    assert a == b != c and d not in (a, c)
