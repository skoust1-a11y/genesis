import copy
import json
from datetime import datetime, timedelta

import pytest

from app.trading import structure_entry_research as policy
from app.trading.option_quote import IST

START = datetime(2026, 11, 17, 9, 15, tzinfo=IST)


def bars(count=23):
    return [
        {
            "date": (START + timedelta(minutes=i)).isoformat(),
            "open": 25000,
            "high": 25006,
            "low": 24998,
            "close": 25000,
        }
        for i in range(count)
    ]


def clock(rows):
    return datetime.fromisoformat(rows[-1]["date"]) + timedelta(minutes=1)


def sweep_rows():
    rows = bars(8)
    rows[4].update(low=24990)
    rows[5].update(low=24994)
    rows[6].update(open=24996, low=24987, high=25000, close=24992)
    rows[7].update(open=24992, low=24991, high=25008, close=25007)
    return rows


def mirror(rows):
    return [
        {
            **r,
            "open": 50000 - r["open"],
            "close": 50000 - r["close"],
            "high": 50000 - r["low"],
            "low": 50000 - r["high"],
        }
        for r in rows
    ]


@pytest.mark.parametrize("reverse,side", [(False, "CALL"), (True, "PUT")])
def test_sweep_waits_for_next_closed_candle_and_is_symmetric(reverse, side):
    rows = sweep_rows()
    if reverse:
        rows = mirror(rows)
    state, before = policy.advance(
        rows, policy.initial_state("2026-11-17"), clock(rows[:-1])
    )
    assert not [s for s in before if s["source_family"] == "LOCAL_SWEEP_RECLAIM"]
    _, after = policy.advance(rows, state, clock(rows))
    signal = next(s for s in after if s["source_family"] == "LOCAL_SWEEP_RECLAIM")
    assert signal["side"] == side
    assert signal["signal_at"] == clock(rows).isoformat()
    assert signal["reference_known_at"] < rows[-1]["date"]


@pytest.mark.parametrize("reverse,side", [(False, "CALL"), (True, "PUT")])
def test_pullback_uses_separated_confirmed_pivots(reverse, side):
    rows = bars(10)
    rows[1].update(low=24970)
    rows[5].update(high=25010)
    rows[7].update(low=24980)
    rows[8].update(low=24990)
    rows[9].update(high=25015, close=25012)
    if reverse:
        rows = mirror(rows)
    _, signals = policy.advance(rows, policy.initial_state("2026-11-17"), clock(rows))
    found = [s for s in signals if s["source_family"] == "PULLBACK_BREAKOUT"]
    assert len(found) == 1 and found[0]["side"] == side
    assert found[0]["signal_at"] == clock(rows).isoformat()


@pytest.mark.parametrize("reverse,side", [(False, "CALL"), (True, "PUT")])
def test_box_is_frozen_before_break_and_source_cannot_break_itself(reverse, side):
    rows = bars(21)
    for r in rows[:20]:
        r.update(high=25012, low=24988)
    rows[20].update(high=25016, low=24998, close=25015)
    if reverse:
        rows = mirror(rows)
    state, before = policy.advance(
        rows[:-1], policy.initial_state("2026-11-17"), clock(rows[:-1])
    )
    assert state["box"] and not before
    _, signals = policy.advance(rows, state, clock(rows))
    found = [s for s in signals if s["source_family"] == "RANGE_BREAK"]
    assert len(found) == 1 and found[0]["side"] == side
    assert found[0]["reference_known_at"] == rows[-1]["date"]


def test_full_replay_matches_incremental_json_restart_and_no_duplicate_poll():
    rows = sweep_rows()
    state = policy.initial_state("2026-11-17")
    seen = []
    for i in range(len(rows)):
        state, emitted = policy.advance(rows[: i + 1], state, clock(rows[: i + 1]))
        state = json.loads(json.dumps(state))
        seen.extend(emitted)
    full, signals = policy.advance(
        rows, policy.initial_state("2026-11-17"), clock(rows)
    )
    assert seen == signals and state == full
    again, repeated = policy.advance(rows, state, clock(rows))
    assert again == state and repeated == []


def test_future_rows_cannot_change_past_signals():
    rows = sweep_rows()
    future = {**rows[-1], "date": (clock(rows)).isoformat(), "high": 99999}
    expected = policy.advance(rows, policy.initial_state("2026-11-17"), clock(rows))
    assert (
        policy.advance(rows + [future], policy.initial_state("2026-11-17"), clock(rows))
        == expected
    )


@pytest.mark.parametrize("damage", ["gap", "duplicate", "naive", "bad_ohlc", "nan"])
def test_invalid_input_fails_without_mutating_state(damage):
    rows = sweep_rows()
    state = policy.initial_state("2026-11-17")
    saved = copy.deepcopy(state)
    now = clock(rows)
    if damage == "gap":
        del rows[2]
    elif damage == "duplicate":
        rows.append(rows[2])
    elif damage == "naive":
        rows[2]["date"] = rows[2]["date"][:19]
    elif damage == "bad_ohlc":
        rows[2]["close"] = 99999
    else:
        rows[2]["low"] = float("nan")
    with pytest.raises(ValueError):
        policy.advance(rows, state, now)
    assert state == saved


def test_rule_identity_and_session_cannot_be_reused():
    rows = sweep_rows()
    for state in (
        policy.initial_state("2026-11-16"),
        {**policy.initial_state("2026-11-17"), "rule_hash": "changed"},
    ):
        with pytest.raises(ValueError):
            policy.advance(rows, state, clock(rows))


def test_rejected_signal_does_not_burn_a_later_structure_at_same_level():
    rows = sweep_rows()
    state, signals = policy.advance(
        rows, policy.initial_state("2026-11-17"), clock(rows)
    )
    first = signals[-1]
    rejected = policy.record_decision(
        state, first, outcome="REJECTED", reason="STALE", at=clock(rows)
    )
    assert (
        policy.admission_reason(first, rejected, clock(rows), position_open=False)
        == "ALREADY_DECIDED"
    )
    fresh = {**first, "signal_id": "distinct-confirmed-structure"}
    assert (
        policy.admission_reason(fresh, rejected, clock(rows), position_open=False)
        is None
    )
    assert (
        policy.admission_reason(fresh, rejected, clock(rows), position_open=True)
        == "POSITION_OR_EXIT_PENDING"
    )
    assert state["decisions"] == {}


@pytest.mark.parametrize("seconds", [-1, 91])
def test_stale_and_future_signals_are_never_admitted(seconds):
    rows = sweep_rows()
    state, signals = policy.advance(
        rows, policy.initial_state("2026-11-17"), clock(rows)
    )
    assert (
        policy.admission_reason(
            signals[-1],
            state,
            clock(rows) + timedelta(seconds=seconds),
            position_open=False,
        )
        == "STALE_OR_FUTURE_SIGNAL"
    )


def test_risk_budget_geometry_rejects_unbounded_sweep():
    rows = sweep_rows()
    rows[-1].update(high=25100, close=25090)
    _, signals = policy.advance(rows, policy.initial_state("2026-11-17"), clock(rows))
    assert not [s for s in signals if s["source_family"] == "LOCAL_SWEEP_RECLAIM"]


def test_merge_preserves_zone_priority_and_is_input_order_independent():
    _, signals = policy.advance(
        sweep_rows(), policy.initial_state("2026-11-17"), clock(sweep_rows())
    )
    native = signals[-1]
    zone = {**native, "signal_id": "existing-zone", "source_family": "ZONE"}
    selected, suppressed = policy.merge_candidates([native, zone])
    assert selected[0]["signal_id"] == "existing-zone"
    assert selected[0]["merged_families"] == ["LOCAL_SWEEP_RECLAIM", "ZONE"]
    assert suppressed[0]["reason"] == "OVERLAPPING_SIGNAL"
    assert policy.merge_candidates([zone, native]) == (selected, suppressed)


def test_merge_rejects_opposite_signals_without_arbitrary_direction_preference():
    _, signals = policy.advance(
        sweep_rows(), policy.initial_state("2026-11-17"), clock(sweep_rows())
    )
    call = signals[-1]
    put = {**call, "side": "PUT", "signal_id": "opposite"}
    accepted, suppressed = policy.merge_candidates([call, put])
    assert accepted == []
    assert len(suppressed) == 2 and all(
        s["reason"] == "OPPOSING_SIGNALS" for s in suppressed
    )


def test_fresh_close_remains_eligible_after_an_earlier_overlapping_close():
    _, signals = policy.advance(
        sweep_rows(), policy.initial_state("2026-11-17"), clock(sweep_rows())
    )
    signal = signals[-1]
    next_signal = {
        **signal,
        "signal_id": "later-structure",
        "signal_at": (clock(sweep_rows()) + timedelta(minutes=1)).isoformat(),
    }
    accepted, suppressed = policy.merge_candidates([signal, next_signal])
    assert len(accepted) == 2 and not suppressed


@pytest.mark.parametrize(
    "hour,minute,bucket,step",
    [
        (10, 59, "OPEN_0915_1100", -1),
        (11, 0, "MID_1100_1330", 0),
        (13, 29, "MID_1100_1330", 0),
        (13, 30, "LATE_1330_1500", 2),
    ],
)
def test_expiry_rotation_has_explicit_time_boundaries(hour, minute, bucket, step):
    now = START.replace(hour=hour, minute=minute)
    assert policy.time_bucket(now) == bucket
    assert policy.strike_step(now, "2026-11-17", "TIME_ROTATION") == step


@pytest.mark.parametrize("selection", ["ITM", "ATM", "OTM", "TIME_ROTATION"])
def test_non_expiry_selection_remains_current_otm(selection):
    assert policy.strike_step(START, "2026-11-24", selection) == 2


def test_unknown_strike_policy_fails_closed():
    with pytest.raises(ValueError):
        policy.strike_step(START, "2026-11-17", "BEST_AFTER_LOOKING_AT_PROFIT")
