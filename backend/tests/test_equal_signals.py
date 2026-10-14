from app.equal_signals import today_inputs


def test_today_inputs_use_completed_bars_session_vwap_and_atr_across_days():
    bars = [{"date": "2026-10-13 15:25", "open": 90, "high": 110, "low": 90, "close": 100},
            {"date": "2026-10-14 09:15", "open": 100, "high": 103, "low": 97, "close": 100},
            {"date": "2026-10-14 09:20", "open": 100, "high": 106, "low": 100, "close": 106},
            {"date": "2026-10-14 09:25", "open": 106, "high": 200, "low": 0, "close": 50}]  # fmt: skip
    day, vwap, atrs = today_inputs(
        bars, "2026-10-14", "2026-10-14 09:20"
    )  # 09:25 bar still open
    assert [b["date"][11:] for b in day] == ["09:15", "09:20"]
    assert vwap == [
        100,
        (100 + 104) / 2,
    ]  # running mean of (H + L + C) / 3, from today's open only
    assert atrs == [
        (20 + 6) / 2,
        (20 + 6 + 6) / 3,
    ]  # yesterday's bar counts; first bar of a day ignores the gap
    assert today_inputs(bars, "2026-10-15", "2026-10-15 09:20") == ([], [], [])


def test_staged_exit_breakeven_partial_and_runner():
    from app.liquidity_signals import staged_exit_of

    sig = {
        "side": "Long",
        "entry": 100.0,
        "stop": 90.0,
        "risk": 10.0,
        "target": 130.0,  # 3R target
        "time": "2026-10-14 09:30",
    }

    # Case 1: Reaches +1R (110) then falls back to entry (100) -> Breakeven 0.0R
    day_be = [
        {"date": "2026-10-14 09:30", "open": 100, "high": 105, "low": 98, "close": 104},
        {"date": "2026-10-14 09:35", "open": 104, "high": 112, "low": 103, "close": 111},  # touches +1R (110)
        {"date": "2026-10-14 09:40", "open": 111, "high": 111, "low": 99, "close": 100},   # drops below 100
    ]
    res_be = staged_exit_of(day_be, sig)
    assert res_be["exit_reason"] == "Breakeven"
    assert res_be["exit_price"] == 100.0
    assert res_be["r_multiple"] == 0.0
    assert res_be["be_triggered"] is True
    assert res_be["partial_hit"] is False

    # Case 2: Reaches +2R (120) -> Partial 50% booked, then SL locked at +1R (110) -> +1.5R total
    day_partial = [
        {"date": "2026-10-14 09:30", "open": 100, "high": 105, "low": 98, "close": 104},
        {"date": "2026-10-14 09:35", "open": 104, "high": 122, "low": 104, "close": 121},  # touches +2R (120)
        {"date": "2026-10-14 09:40", "open": 121, "high": 121, "low": 99, "close": 100},   # drops to +1R stop (110)
    ]
    res_part = staged_exit_of(day_partial, sig, partial_r=2.0, partial_pct=0.5)
    assert res_part["exit_reason"] == "Partial + +1R Trail Stop"
    assert res_part["exit_price"] == 110.0
    assert res_part["r_multiple"] == 1.5  # 0.5 * 2R + 0.5 * 1R = 1.5R
    assert res_part["partial_hit"] is True

    # Case 3: Reaches final target +3R (130) -> +2.5R total
    day_target = [
        {"date": "2026-10-14 09:30", "open": 100, "high": 105, "low": 98, "close": 104},
        {"date": "2026-10-14 09:35", "open": 104, "high": 132, "low": 104, "close": 131},  # hits 130
    ]
    res_target = staged_exit_of(day_target, sig, partial_r=2.0, partial_pct=0.5)
    assert res_target["exit_reason"] == "Target"
    assert res_target["exit_price"] == 130.0
    assert res_target["r_multiple"] == 2.5  # 0.5 * 2R + 0.5 * 3R = 2.5R

    # Case 4: Hits initial stop (90) without ever reaching +1R -> -1.0R
    day_stop = [
        {"date": "2026-10-14 09:30", "open": 100, "high": 102, "low": 89, "close": 90},
    ]
    res_stop = staged_exit_of(day_stop, sig)
    assert res_stop["exit_reason"] == "Stop"
    assert res_stop["exit_price"] == 90.0
    assert res_stop["r_multiple"] == -1.0
