"""Defects and invariants found by the liquidity strategy audit (DocumentsAndDiagrams/liquidity_strategy_audit)."""

from app.config import get_settings
from app.liquidity_signals import message, pivots, scan
from scripts.audit_liquidity import scan_v
from scripts.backtest_liquidity import kaggle_5m

# 15m bars (high, low, close) from 09:15: a PDL 100 sweep at 09:30, swing high 120, swing low 104, then a 15m close
# above 120 at 11:00 (the same shape as test_liquidity_signals.DAY).
DAY = [(112, 105, 106), (108, 98, 100), (115, 101, 114), (120, 110, 112),
       (116, 106, 108), (114, 104, 107), (119, 108, 118), (125, 117, 124)]  # fmt: skip


def day_bars():
    out = []
    for k, (h, low, c) in enumerate(DAY):
        for m in range(3):
            minutes = 9 * 60 + 15 + 15 * k + 5 * m
            out.append({"date": f"2026-10-14 {minutes // 60:02d}:{minutes % 60:02d}", "open": c, "high": h, "low": low, "close": c})
    out[0]["open"] = 110.0
    return out


def test_a_swing_is_usable_only_after_its_second_right_bar_closes():
    bars = [{"high": h, "low": 0} for h in (1, 2, 5, 3, 2, 1)]
    assert pivots(bars[:4], "high") == []  # one bar right of the 5: not confirmed
    assert pivots(bars[:5], "high") == [2]  # the second right bar has closed


def test_equal_highs_both_count_as_swings():
    # The spec says "greater than" both neighbours; pivots() uses >=, so a flat double top yields two swing highs.
    bars = [{"high": h, "low": 0} for h in (1, 2, 5, 5, 2, 1)]
    assert pivots(bars, "high") == [2, 3]


def test_scan_never_uses_bars_after_the_entry_bar():
    b = day_bars()
    above = {x["date"]: True for x in b}
    [s] = scan(b, above, pdh=200, pdl=100, min_risk=10)
    cut = [x for x in b if x["date"] < s["time"]]
    assert scan(cut, above, pdh=200, pdl=100, min_risk=10) == [s]
    assert scan(cut[:-1], above, pdh=200, pdl=100, min_risk=10) == []  # not available one bar earlier


def test_research_scan_matches_the_production_scan():
    b = day_bars()
    for above in ({x["date"]: True for x in b}, {x["date"]: False for x in b}, {}):
        prod = scan(b, above, pdh=200, pdl=100, min_risk=10)
        mine = scan_v(b, above, 200, 100, 100.0, {"min_risk": 10})
        assert [(s["side"], s["time"], s["entry"], s["stop"]) for s in prod] == [
            (s["side"], s["time"], s["entry"], s["stop"]) for s in mine
        ]


def test_kaggle_holiday_sessions_that_repeat_one_minute_all_day_are_dropped(tmp_path):
    rows = ["datetime,open,high,low,close,volume"]
    for m in range(15, 25):
        rows.append(f"2020-02-20 09:{m}:00+05:30,{100 + m},{101 + m},{99 + m},{100 + m},0")
        rows.append(f"2020-02-21 09:{m}:00+05:30,12076.0,12083.7,12074.1,12080.85,0")  # Mahashivratri: closed
    path = tmp_path / "nifty.csv"
    path.write_text("\n".join(rows))
    assert {b["date"][:10] for b in kaggle_5m(path)} == {"2020-02-20"}


def test_alert_states_the_daily_limit_the_scanner_enforces():
    s = {"side": "Long", "time": "2026-10-14 11:15", "entry": 124.0, "stop": 99.0, "target": 174.0, "risk": 25.0, "level": 100.0}
    text = message(s, get_settings())
    assert "after 2 losses" not in text and "at most 2 a day" in text
