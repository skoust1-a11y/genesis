import copy
from datetime import datetime, timedelta

from app.trading.option_quote import IST
from morning_policy import candidates, reclaim_signals

START = datetime(2026, 11, 17, 9, 15, tzinfo=IST)


def rows():
    result = [
        {
            "date": (START + timedelta(minutes=i)).isoformat(),
            "open": 25000,
            "high": 25006,
            "low": 24998,
            "close": 25000,
        }
        for i in range(23)
    ]
    result[19].update(low=24990)
    result[20].update(low=24994)
    result[21].update(open=24995, low=24987, close=24988)
    result[22].update(open=24988, low=24985, close=24998)
    return result


def signal(time, family="PULLBACK_BREAKOUT", side="CALL"):
    return {
        "signal_at": "2026-11-17T" + time + ":00+05:30",
        "signal_id": time + family + side,
        "source_family": family,
        "side": side,
    }


def test_reclaim_waits_for_close_but_not_an_extra_confirmation_bar():
    data = rows()
    saved = copy.deepcopy(data)
    assert not reclaim_signals(data[:-1], "2026-11-17")
    found = reclaim_signals(data, "2026-11-17")
    assert len(found) == 1
    assert found[0]["signal_at"] == "2026-11-17T09:38:00+05:30"
    assert found[0]["entry_spot"] == 24998 and found[0]["stop_spot"] == 24983
    assert found[0]["reference_known_at"] < data[-1]["date"]
    assert data == saved


def test_prefix_results_do_not_depend_on_later_bars():
    data = rows()
    before = reclaim_signals(data, "2026-11-17")
    extended = data + [
        {
            "date": (START + timedelta(minutes=23)).isoformat(),
            "open": 24998,
            "high": 25040,
            "low": 24997,
            "close": 25030,
        }
    ]
    assert [
        s
        for s in reclaim_signals(extended, "2026-11-17")
        if s["signal_at"] <= "2026-11-17T09:38:00+05:30"
    ] == before


def test_time_boundaries_filter_entries_not_exit_times():
    original = [
        signal(t)
        for t in [
            "09:28",
            "09:30",
            "10:29",
            "10:30",
            "10:59",
            "11:00",
            "12:00",
            "14:59",
        ]
    ]
    short, _ = candidates(original, [], "RECLAIM_0930_1030")
    long, _ = candidates(original, [], "RECLAIM_0930_1100")
    afternoon, _ = candidates(original, [], "RECLAIM_PLUS_AFTERNOON")
    assert [s["signal_at"][11:16] for s in short] == ["09:30", "10:29"]
    assert [s["signal_at"][11:16] for s in long] == ["09:30", "10:29", "10:30", "10:59"]
    assert [s["signal_at"][11:16] for s in afternoon] == [
        "09:30",
        "10:29",
        "10:30",
        "10:59",
        "12:00",
        "14:59",
    ]


def test_reversal_only_does_not_inherit_continuation_or_old_sweep_entries():
    original = [signal("09:34"), signal("09:39", "LOCAL_SWEEP_RECLAIM")]
    direct = [signal("09:38", "IMMEDIATE_1M_RECLAIM")]
    pure, _ = candidates(original, direct, "REVERSAL_ONLY_0930_1100")
    mixed, _ = candidates(original, direct, "RECLAIM_0930_1100")
    assert len(pure) == 1 and pure[0]["source_family"] == "IMMEDIATE_1M_RECLAIM"
    assert len(mixed) == 2 and all(
        s["source_family"] != "LOCAL_SWEEP_RECLAIM" for s in mixed
    )


def test_opposing_setups_are_not_resolved_using_future_direction():
    accepted, suppressed = candidates(
        [signal("09:38", side="PUT")],
        [signal("09:38", "IMMEDIATE_1M_RECLAIM")],
        "RECLAIM_0930_1100",
    )
    assert not accepted and len(suppressed) == 2
