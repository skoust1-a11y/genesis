import pytest
from app.loss_analyzer import (
    analyze_losing_trade,
    ask_expert_on_streak,
    format_expert_alert,
    format_loss_alert,
)


def test_analyze_losing_trade_vwap_failure():
    day_bars = [
        {"date": "2026-10-15 09:15", "open": 25000, "high": 25050, "low": 24980, "close": 25020},
        {"date": "2026-10-15 09:20", "open": 25020, "high": 25080, "low": 25010, "close": 25070},
        {"date": "2026-10-15 09:25", "open": 25070, "high": 25075, "low": 24990, "close": 25000},  # loses vwap
        {"date": "2026-10-15 09:30", "open": 25000, "high": 25010, "low": 24920, "close": 24930},  # hits stop
    ]
    trade = {
        "side": "Long",
        "pool": "PDL",
        "entry": 25070,
        "stop": 24950,
        "exit_price": 24950,
        "time": "2026-10-15 09:20",
        "exit_time": "2026-10-15 09:30",
        "risk": 120,
    }

    analysis = analyze_losing_trade(trade, day_bars, pdh=25200, pdl=24900)
    assert analysis["side"] == "Long"
    assert analysis["points_lost"] == 120
    assert "what_went_wrong" in analysis
    assert len(analysis["what_went_wrong"]) > 0
    assert analysis["primary_cause"] in ("MOMENTUM_VWAP_FAILURE", "VOLATILITY_EXPANSION_SPIKE")

    alert = format_loss_alert(analysis)
    assert "POST-MORTEM" in alert
    assert "Stop Loss Hit" in alert
    assert "25,070" in alert


def test_analyze_false_breakout():
    day_bars = [
        {"date": "2026-10-15 09:30", "open": 25100, "high": 25150, "low": 25090, "close": 25140},
        {"date": "2026-10-15 09:35", "open": 25140, "high": 25145, "low": 25040, "close": 25050},  # slams below PDH
    ]
    trade = {
        "side": "Long",
        "pool": "PDH Support Flip",
        "entry": 25140,
        "stop": 25080,
        "exit_price": 25080,
        "time": "2026-10-15 09:30",
        "exit_time": "2026-10-15 09:35",
        "risk": 60,
    }

    analysis = analyze_losing_trade(trade, day_bars, pdh=25120, pdl=24800)
    assert analysis["primary_cause"] == "LIQUIDITY_TRAP_FALSE_BREAKOUT"
    assert "Bull trap" in analysis["what_went_wrong"][0]


def test_ask_expert_on_streak():
    losing_trades = [
        {"side": "Long", "pool": "PDH Support Flip", "time": "2026-10-15 09:30", "exit_time": "2026-10-15 09:40", "entry": 25100, "stop": 25050, "risk": 50},
        {"side": "Short", "pool": "PDL Resistance Flip", "time": "2026-10-15 10:15", "exit_time": "2026-10-15 10:30", "entry": 24900, "stop": 24960, "risk": 60},
        {"side": "Long", "pool": "PDL", "time": "2026-10-15 11:45", "exit_time": "2026-10-15 12:15", "entry": 24850, "stop": 24800, "risk": 50},
    ]
    for t in losing_trades:
        t["analysis"] = analyze_losing_trade(t, [], pdh=25150, pdl=24850)

    report = ask_expert_on_streak(losing_trades, [])
    assert len(report) > 50
    assert "REGIME" in report or "DIAGNOSIS" in report

    expert_alert = format_expert_alert(report, "2026-10-20")
    assert "3 CONSECUTIVE LOSSES" in expert_alert
    assert "2026-10-20" in expert_alert
