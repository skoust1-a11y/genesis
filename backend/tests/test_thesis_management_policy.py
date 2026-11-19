"""Tests for Thesis Management Policy (Prompt 02)."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from app.trading.thesis_management_policy import (
    evaluate_thesis_management,
    tighten,
)

IST = ZoneInfo("Asia/Kolkata")


def test_tighten_monotonic():
    """Verify stop can only tighten, never loosen."""
    # CALL: stop must only move UP
    assert tighten("CALL", 25000.0, 25010.0) == 25010.0
    assert tighten("CALL", 25010.0, 25000.0) == 25010.0  # Cannot loosen!

    # PUT: stop must only move DOWN
    assert tighten("PUT", 25100.0, 25090.0) == 25090.0
    assert tighten("PUT", 25090.0, 25100.0) == 25090.0  # Cannot loosen!


def test_protective_stop_priority():
    """Stop loss touch immediately triggers exit with top priority."""
    trade = {
        "side": "CALL",
        "entry_spot": 25300.0,
        "stop_spot": 25280.0,
        "risk_points": 20.0,
        "lots": 2,
    }
    now = datetime(2026, 11, 17, 10, 0, tzinfo=IST)
    # CMP touches stop
    decision = evaluate_thesis_management(trade, spot=25280.0, now=now)
    assert decision.action == "STOP_LOSS_HIT"
    assert decision.is_fully_closed is True
    assert decision.state == "STOPPED_OUT"


def test_eod_squareoff_priority():
    """At or after 15:10 IST, position must exit unconditionally."""
    trade = {
        "side": "CALL",
        "entry_spot": 25300.0,
        "stop_spot": 25280.0,
        "risk_points": 20.0,
        "lots": 2,
    }
    now = datetime(2026, 11, 17, 15, 10, tzinfo=IST)
    decision = evaluate_thesis_management(trade, spot=25400.0, now=now)
    assert decision.action == "INTRADAY_CLOSE"
    assert decision.is_fully_closed is True
    assert decision.state == "TIMED_OUT"


def test_milestone_ratchets_and_healthy_retest():
    """Test 0.7R moves stop to breakeven, then shallow retest HOLDs without loosening."""
    trade = {
        "side": "CALL",
        "entry_spot": 25300.0,
        "stop_spot": 25280.0,
        "risk_points": 20.0,
        "lots": 2,
        "research_milestones": [],
        "milestone_be": False,
    }
    now = datetime(2026, 11, 17, 10, 5, tzinfo=IST)

    # 1. Spot hits +0.8R (25316.0) -> triggers 0.7R milestone
    dec1 = evaluate_thesis_management(trade, spot=25316.0, now=now)
    assert dec1.action == "HOLD"
    assert dec1.new_stop_loss == 25300.0  # Tightened to entry price
    assert "0.7R" in dec1.milestone_updates["research_milestones"]
    assert dec1.milestone_updates["milestone_be"] is True

    # 2. Update trade state to reflect locked breakeven
    trade["runner_stop"] = 25300.0
    trade["milestone_be"] = True
    trade["research_milestones"] = ["0.7R"]

    # 3. Pullback to 25305.0 (+0.25R): shallow healthy retest
    dec2 = evaluate_thesis_management(trade, spot=25305.0, now=now)
    assert dec2.action == "HOLD"
    assert dec2.state == "HEALTHY_RETEST"
    assert dec2.new_stop_loss == 25300.0  # Stop stays strictly at breakeven, NEVER loosened


def test_15m_structural_invalidation():
    """Adverse position with 15M body closing below prior 15M low exits preemptively."""
    trade = {
        "side": "CALL",
        "entry_spot": 25300.0,
        "stop_spot": 25280.0,
        "risk_points": 20.0,
        "lots": 2,
        "bars_in_trade": 3,
    }
    now = datetime(2026, 11, 17, 10, 15, tzinfo=IST)
    # R is -0.35R (spot 25293.0)
    # 15m candle closes below prior 15m low
    df_15m = pd.DataFrame([
        {"open": 25310.0, "high": 25320.0, "low": 25295.0, "close": 25305.0},
        {"open": 25305.0, "high": 25308.0, "low": 25290.0, "close": 25292.0},  # close < prior low (25295) and close < open
    ])

    decision = evaluate_thesis_management(trade, spot=25293.0, df_15m=df_15m, now=now)
    assert decision.action == "PREEMPTIVE_EXIT"
    assert decision.state == "THESIS_INVALIDATED"
    assert decision.is_fully_closed is True


def test_5m_structure_trailing():
    """At >= 1.5R, 5M close below prior 5M low triggers structure exit."""
    trade = {
        "side": "CALL",
        "entry_spot": 25300.0,
        "stop_spot": 25280.0,
        "risk_points": 20.0,
        "lots": 2,
        "research_milestones": ["0.7R", "2R"],
        "milestone_be": True,
        "runner_stop": 25316.0,  # locked +0.8R
    }
    now = datetime(2026, 11, 17, 10, 30, tzinfo=IST)
    # Current spot at 25340.0 (+2.0R)
    # 5M structure: last bar closes below prior bar low
    df_5m = pd.DataFrame([
        {"open": 25330.0, "high": 25350.0, "low": 25335.0, "close": 25348.0},
        {"open": 25348.0, "high": 25350.0, "low": 25330.0, "close": 25332.0},  # close (25332) < prior low (25335)
    ])

    decision = evaluate_thesis_management(
        trade, spot=25340.0, df_5m=df_5m, enable_structure_trail=True, now=now
    )
    assert decision.action == "STRUCTURE_TRAIL_EXIT"
    assert decision.is_fully_closed is True


def test_consolidation_flag_runway_extension():
    """Consolidation box extends stagnation threshold from 6 to 10 bars."""
    trade = {
        "side": "CALL",
        "entry_spot": 25300.0,
        "stop_spot": 25280.0,
        "risk_points": 20.0,
        "lots": 2,
        "milestone_be": True,
        "bars_in_trade": 7,  # > 6 bars!
        "runner_stop": 25300.0,
    }
    now = datetime(2026, 11, 17, 10, 45, tzinfo=IST)
    # Spot at 25310.0 (+0.5R). 2-bar 5m range = 10 pts (<= 16 pts)
    df_5m = pd.DataFrame([
        {"open": 25305.0, "high": 25314.0, "low": 25304.0, "close": 25310.0},
        {"open": 25310.0, "high": 25315.0, "low": 25306.0, "close": 25312.0},
    ])

    decision = evaluate_thesis_management(trade, spot=25310.0, df_5m=df_5m, now=now)
    # Should NOT exit for stagnation! Extends to 10 bars
    assert decision.action == "HOLD"
    assert decision.state == "CONSOLIDATION_FLAG"


def test_regression_0938_winner_thesis_vs_profit_trail():
    """Simulate 09:38 17 Nov CALL winner:
    Profit trail exited at 09:43 because of 20% giveback.
    Thesis management HOLDs through the pullback because 5m structure remains intact!
    """
    trade = {
        "side": "CALL",
        "entry_spot": 25304.0,
        "stop_spot": 25290.85,
        "risk_points": 13.15,
        "lots": 2,
        "opened_at": "2026-11-17T09:38:00+05:30",
        "milestone_be": True,
        "research_milestones": ["0.7R"],
        "runner_stop": 25304.0,
    }

    # At 09:43: spot pulled back from peak 25325 (+1.6R) to 25318 (+1.06R)
    # 5m candles: 09:35-09:40 bar low was 25292.85, high 25320.0, close 25315.0
    # 09:40-09:45 bar: still forming / just completed, low 25310.0, close 25318.0 (> prior close, > entry)
    df_5m = pd.DataFrame([
        {"date": "2026-11-17 09:35", "open": 25295.0, "high": 25320.0, "low": 25292.85, "close": 25315.0},
        {"date": "2026-11-17 09:40", "open": 25315.0, "high": 25325.0, "low": 25310.0, "close": 25318.0},
    ])
    now = datetime(2026, 11, 17, 9, 43, tzinfo=IST)

    decision = evaluate_thesis_management(trade, spot=25318.0, df_5m=df_5m, now=now)
    # Under thesis management, this is a healthy expansion/hold, NOT an exit!
    assert decision.action == "HOLD"
    assert decision.is_fully_closed is False


def test_thesis_exit_adapter_hold_and_opposing():
    from app.trading.thesis_management_policy import opposing_only, thesis_exit_adapter

    now = datetime(2026, 11, 17, 10, 0, tzinfo=IST)
    signals = [
        {"side": "PUT", "signal_at": "2026-11-17T09:59:00+05:30"},  # 60s ago
    ]
    # Opposite signal exists!
    assert opposing_only(signals, "CALL", now) is True
    # If same side also present, should NOT be opposing only
    signals.append({"side": "CALL", "signal_at": "2026-11-17T09:59:30+05:30"})
    assert opposing_only(signals, "CALL", now) is False

    trade = {
        "side": "CALL",
        "entry_spot": 25300.0,
        "stop_spot": 25280.0,
        "risk_points": 20.0,
        "lots": 2,
    }
    adapter_res = thesis_exit_adapter(trade, spot=25310.0, now=now)
    assert adapter_res["action"] == "HOLD"
    assert "updates" in adapter_res
    assert "runner_stop" in adapter_res["updates"]

