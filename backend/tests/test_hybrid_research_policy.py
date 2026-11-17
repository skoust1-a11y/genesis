from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from app.trading import hybrid_research_policy as hybrid
from app.trading.option_quote import IST
from app.trading.scanner_crew.detector_agent import CandidateSetup
from app.trading.scanner_crew.news_agent import NewsSentiment

NOW = datetime(2026, 11, 16, 10, 0, 5, tzinfo=IST)
CALENDAR = {"2026-11-10": "TEST"}
NEWS = NewsSentiment(0, "NEUTRAL", "LOW", True, False)


def bars(minutes, now=NOW, forming=False):
    opening = now.replace(hour=9, minute=15, second=0)
    count = int((now - opening).total_seconds() // (60 * minutes))
    result = pd.DataFrame(
        [
            {
                "date": (opening + timedelta(minutes=minutes * i)).isoformat(),
                "open": 25000,
                "high": 25010,
                "low": 24990,
                "close": 25000,
                "volume": 0,
            }
            for i in range(count + int(forming))
        ]
    )
    if forming:
        result.loc[result.index[-1], "high"] = 99999
    return result


def setup(
    tf="1m", strategy="Breakout & Retest (PD) [1M]", timestamp="2026-11-16 10:00"
):
    return CandidateSetup(
        strategy,
        "NIFTY",
        "CALL",
        25000,
        24980,
        25060,
        tf,
        timestamp,
        "test",
        0,
        True,
        True,
    )


@pytest.mark.parametrize("tf", ["1m", "5m", "15m"])
def test_retest_signal_is_bound_to_correct_closed_source_candle(tf):
    item = setup(tf, f"Breakout & Retest (PD) [{tf.upper()}]")
    signal, why = hybrid.normalize_setup(item, bars(int(tf[:-1])), NOW)
    assert why is None and signal["signal_at"] == "2026-11-16T10:00:00+05:30"
    assert datetime.fromisoformat(signal["source_bar_close"]) - datetime.fromisoformat(
        signal["source_bar_start"]
    ) == timedelta(minutes=int(tf[:-1]))


@pytest.mark.parametrize("minutes", [1, 5, 15])
def test_equal_level_start_stamp_is_shifted_by_actual_duration(minutes):
    opening = NOW.replace(second=0) - timedelta(minutes=minutes)
    item = setup(
        f"{minutes}m",
        f"Equal Lows Sweep [{minutes}M]",
        opening.strftime("%Y-%m-%d %H:%M"),
    )
    signal, why = hybrid.normalize_setup(item, bars(minutes), NOW)
    assert why is None and signal["signal_at"] == "2026-11-16T10:00:00+05:30"


@pytest.mark.parametrize(
    "strategy", ["Liquidity Sweep [15M]", "Breakout & Retest (OR-30) [15M]"]
)
def test_five_minute_only_helpers_are_not_promoted_by_relabeling(strategy):
    assert (
        hybrid.normalize_setup(setup("15m", strategy), bars(15), NOW)[1]
        == "FROZEN_HELPER_IS_5M_ONLY"
    )


def test_reject_forming_stale_wrong_source_and_wrong_side_of_stop():
    frame = bars(1, forming=True)
    item = setup(timestamp="2026-11-16 10:01")
    assert hybrid.normalize_setup(item, frame, NOW)[1] == "FORMING_SIGNAL"
    assert (
        hybrid.normalize_setup(setup(timestamp="2026-11-16 09:58"), frame, NOW)[1]
        == "STALE_SIGNAL"
    )
    assert (
        hybrid.normalize_setup(replace(setup(), trigger_price=25001), frame, NOW)[1]
        == "ENTRY_NOT_SOURCE_CLOSE"
    )
    assert (
        hybrid.normalize_setup(replace(setup(), tentative_stop=25020), frame, NOW)[1]
        == "INITIAL_RISK_OUTSIDE_10_55"
    )


def test_scanner_never_sees_forming_bars_and_uses_true_completed_opening_range():
    adapter = hybrid.LegacyEntries()
    captured = {}

    class Detector:
        def scan_multi_timeframe_strategies(self, frames, levels, **kwargs):
            captured.update(frames=frames, levels=levels)
            return [setup()]

    adapter.detector = Detector()
    daily = pd.DataFrame(
        [
            {"date": "2026-11-13", "high": 25040, "low": 24960, "close": 25000},
            {"date": "2026-11-16", "high": 99999, "low": 1, "close": 25000},
        ]
    )
    snapshot = adapter.scan(
        raw_frames={f"{m}m": bars(m, forming=True) for m in (1, 5, 15)},
        daily=daily,
        now=NOW,
        holidays=CALENDAR,
        news=NEWS,
    )
    assert len(snapshot.signals) == 1
    assert captured["levels"].pdh == 25040
    assert captured["levels"].or30_high == 25010
    for tf, frame in captured["frames"].items():
        assert frame["high"].max() == 25010
        assert (
            datetime.fromisoformat(frame.iloc[-1]["date"]).replace(tzinfo=IST)
            + timedelta(minutes=int(tf[:-1]))
            <= NOW
        )


def test_missing_news_or_previous_session_cannot_create_native_entries():
    adapter = hybrid.LegacyEntries()
    raw = {f"{m}m": bars(m) for m in (1, 5, 15)}
    result = adapter.scan(
        raw_frames=raw, daily=pd.DataFrame(), now=NOW, holidays=CALENDAR, news=None
    )
    assert (
        not result.signals
        and result.rejections[0]["reason"] == "NEWS_UNAVAILABLE_OR_UNSAFE"
    )
    result = adapter.scan(
        raw_frames=raw, daily=pd.DataFrame(), now=NOW, holidays=CALENDAR, news=NEWS
    )
    assert (
        not result.signals
        and result.rejections[-1]["reason"] == "PREVIOUS_SESSION_LEVELS_UNVERIFIED"
    )


def trade(side="CALL", **updates):
    sign = 1 if side == "CALL" else -1
    return dict(
        side=side,
        entry_spot=25000,
        stop_spot=25000 - sign * 20,
        target_spot=25000 + sign * 60,
        risk_points=20,
        lots=3,
        opened_at="2026-11-16T09:55:05+05:30",
        **updates,
    )


def evaluate(t, spot, **kwargs):
    return hybrid.legacy_exit(
        t, spot=spot, bar=None, five=[], levels=[], signals=[], now=NOW, **kwargs
    )


@pytest.mark.parametrize(
    "side,spot,stop", [("CALL", 25015, 25000), ("PUT", 24985, 25000)]
)
def test_breakeven_is_underlying_stop_for_next_observation(side, spot, stop):
    result = evaluate(trade(side), spot)
    assert result["action"] == "HOLD" and result["updates"]["runner_stop"] == stop
    assert result["updates"]["milestone_be"]


def test_two_r_lock_never_loosens_an_existing_hop_stop():
    t = trade(
        runner_stop=25057,
        runner_target=25100,
        has_hopped=True,
        milestone_be=True,
        research_milestones=["0.7R"],
    )
    result = evaluate(t, 25070)
    assert result["action"] == "HOLD" and result["updates"]["runner_stop"] == 25057


def test_target_hop_uses_next_known_level_and_whole_lots():
    result = hybrid.legacy_exit(
        trade(),
        spot=25061,
        bar=None,
        five=[],
        levels=[25090, 25110],
        signals=[],
        now=NOW,
    )
    assert result["action"] == "LIQUIDITY_HOP_PROMOTED"
    assert result["booked_lots"] == 1 and result["remaining_lots"] == 2
    assert result["next_target"] == 25090 and result["ratcheted_sl"] == 25057


def test_opposing_exit_requires_fresh_unambiguous_confirmation():
    opposite = {"side": "PUT", "signal_at": NOW.isoformat()}
    assert hybrid.opposing_only([opposite], "CALL", NOW)
    assert not hybrid.opposing_only(
        [opposite, {"side": "CALL", "signal_at": NOW.isoformat()}], "CALL", NOW
    )
    assert not hybrid.opposing_only(
        [{"side": "PUT", "signal_at": (NOW - timedelta(seconds=91)).isoformat()}],
        "CALL",
        NOW,
    )
    result = hybrid.legacy_exit(
        trade(), spot=25001, bar=None, five=[], levels=[], signals=[opposite], now=NOW
    )
    assert result["action"] == "REVERSAL_EXIT" and result["is_fully_closed"]


def test_stagnation_and_stop_priority_use_actual_elapsed_time():
    t = trade()
    t["opened_at"] = "2026-11-16T09:15:05+05:30"
    assert evaluate(t, 25001)["action"] == "STAGNATION_TIME_STOP"
    result = hybrid.legacy_exit(
        t,
        spot=25061,
        bar={"date": "2026-11-16T09:59:00+05:30", "low": 24970, "high": 25070},
        five=[],
        levels=[25100],
        signals=[],
        now=NOW,
    )
    assert result["action"] == "STOP_LOSS_HIT" and result["is_fully_closed"]


def test_identity_is_separate_from_operational_strategy():
    assert len(hybrid.identity()) == 64 and hybrid.RULES["live_registration"] is False


def test_native_confirmation_is_not_reissued_after_losing_same_bar_priority():
    native, _ = hybrid.normalize_setup(setup(), bars(1), NOW)
    state, first = hybrid.merge_signals({}, [], [native])
    state, second = hybrid.merge_signals(state, [], [native])
    assert first == [native] and second == []
    revised = {**native, "signal_id": "revised", "stop_spot": 24979}
    assert hybrid.merge_signals(state, [], [revised])[1] == []


def test_zone_priority_and_signal_ids_are_versioned():
    original = {
        "signal_id": "zone-original",
        "version": "zone10-5m-1m-v1",
        "day": "2026-11-16",
        "side": "CALL",
        "signal_at": "2026-11-16T10:00:00+05:30",
    }
    native, _ = hybrid.normalize_setup(setup(), bars(1), NOW)
    _, signals = hybrid.merge_signals({}, [original], [native])
    assert signals[0]["source_family"] == "ZONE"
    assert signals[0]["signal_id"] != original["signal_id"]
    assert signals[0]["origin_signal_id"] == original["signal_id"]


def test_exit_cutoff_uses_ist_even_when_clock_is_utc():
    utc = NOW.replace(hour=15, minute=10).astimezone(UTC)
    result = hybrid.legacy_exit(
        trade(), spot=25001, bar=None, five=[], levels=[], signals=[], now=utc
    )
    assert result["action"] == "INTRADAY_CLOSE"


def test_unchanged_source_candle_cannot_create_a_second_confirmation():
    adapter = hybrid.LegacyEntries()
    captured = []

    class Detector:
        def scan_multi_timeframe_strategies(self, frames, levels, **kwargs):
            captured.append(set(frames))
            return []

    adapter.detector = Detector()
    daily = pd.DataFrame(
        [{"date": "2026-11-13", "high": 25040, "low": 24960, "close": 25000}]
    )
    raw = {f"{m}m": bars(m) for m in (1, 5, 15)}
    adapter.scan(raw_frames=raw, daily=daily, now=NOW, holidays=CALENDAR, news=NEWS)
    adapter.scan(raw_frames=raw, daily=daily, now=NOW, holidays=CALENDAR, news=NEWS)
    assert captured == [{"1m", "5m", "15m"}, set()]
