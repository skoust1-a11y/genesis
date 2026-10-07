"""Option buying: long options and debit spreads, exits against the premium paid, and the previous-day range."""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from app.agents.trade_crew.legs import Leg, build_legs, entry_economics
from app.config import get_settings
from app.engine.costs import resolve_cost_profile
from app.engine.sim import Scenario, build_scenarios, dte_window, simulate
from app.market.feed import MarketSnapshot
from app.market.historical import HistoricalFeed
from app.market.models import IndexBar, OptionEod
from app.strategies.rules import Instrument, PositionState, StrategyRules, check_entry, check_exit
from app.strategies.schema import parse_strategy_markdown
from tests.test_simulator import store  # noqa: F401

LONG_CALL = """---
name: breakout-call
underlying: NIFTY
max_lots: 1
rules:
  instrument: {underlying: NIFTY, structure: long_call}
  entry:
  - {field: pct_from_prev_high, op: '>', value: 0}
  - {field: dte, op: '>=', value: 7}
  - {field: dte, op: '<=', value: 21}
  exit:
  - {kind: profit_target_pct_of_debit, value: 80}
  - {kind: stop_loss_pct_of_debit, value: 40}
  risk: {max_lots: 1, max_daily_loss_pct: 2, min_risk_reward: 1.5}
---
# Thesis
Buy an at-the-money call when NIFTY breaks above the previous day's high.

## Entry
- Spot above the previous day's high, 7 to 21 days to expiry

## Exit
- Take profit at 80% of the premium paid, stop at 40% of it

## Risk
- One lot, stop for the day after a 2% loss

## Gaps
"""
RAW = parse_strategy_markdown(LONG_CALL).frontmatter["rules"]


def rules(instrument: dict | None = None, exit: list | None = None) -> StrategyRules:
    raw = copy.deepcopy(RAW)
    raw["instrument"] |= instrument or {}
    if exit is not None:
        raw["exit"] = exit
    return StrategyRules.model_validate(raw)


def test_debit_legs_buy_toward_the_money_and_spreads_sell_further_out():
    def legs(structure: str, **extra) -> list[Leg]:
        return build_legs(Instrument(underlying="NIFTY", structure=structure, **extra), 22_010, 50)

    assert legs("long_call") == [Leg("BUY", "CE", 22_000)]
    assert legs("long_put", strike_offset=100) == [Leg("BUY", "PE", 21_900)]
    assert legs("bull_call_spread", wing_width=200) == [Leg("BUY", "CE", 22_000), Leg("SELL", "CE", 22_200)]
    assert legs("bear_put_spread", strike_offset=-50, wing_width=200) == [
        Leg("BUY", "PE", 22_050), Leg("SELL", "PE", 21_850),
    ]  # fmt: skip


def test_debit_economics_are_percentages_of_the_premium_paid():
    long_call = entry_economics(rules(), [Leg("BUY", "CE", 22_000)], [100.0], 65)
    assert long_call.credit == -6_500
    assert (long_call.target_pnl, long_call.stop_loss_pnl, long_call.max_loss) == (5_200, -2_600, 2_600)
    assert long_call.risk_reward == 2.0

    spread = rules({"structure": "bull_call_spread", "wing_width": 100})
    econ = entry_economics(spread, [Leg("BUY", "CE", 22_000), Leg("SELL", "CE", 22_100)], [100.0, 40.0], 65)
    assert econ.credit == -3_900
    assert econ.target_pnl == 2_600  # 80% of the debit is 3,120, but the 100-point spread pays at most 2,600


def test_debit_exits_trigger_on_the_premium_paid():
    r = rules()
    triggered = {c.rule: c.passed for c in check_exit(r, PositionState(credit=-6_500, pnl=5_200), None)}
    assert triggered == {"exit.profit_target": True, "exit.stop_loss": False}
    triggered = {c.rule: c.passed for c in check_exit(r, PositionState(credit=-6_500, pnl=-2_600), None)}
    assert triggered == {"exit.profit_target": False, "exit.stop_loss": True}


@pytest.mark.parametrize(
    ("instrument", "exit", "message"),
    [
        (None, [{"kind": "profit_target_pct_of_credit", "value": 50}, {"kind": "stop_loss_pct_of_debit", "value": 40}],
         "exit needs profit_target_pct_of_debit"),
        (None, [*RAW["exit"], {"kind": "stop_loss_pct_of_credit", "value": 100}], "does not apply to long_call"),
        (None, [RAW["exit"][0], {"kind": "stop_loss_pct_of_debit", "value": 150}], "cannot exceed 100"),
        ({"short_distance": 200}, None, "use strike_offset"),
        ({"structure": "bull_call_spread"}, None, "wing_width is required"),
    ],
)
def test_invalid_debit_rules_are_rejected(instrument, exit, message):
    with pytest.raises(ValidationError, match=message):
        rules(instrument, exit)


def test_credit_structures_reject_strike_offset():
    with pytest.raises(ValidationError, match="strike_offset only applies"):
        Instrument(underlying="NIFTY", structure="iron_condor", short_distance=1000, wing_width=200, strike_offset=50)


def test_entry_on_a_break_of_the_previous_day_range():
    def snapshot(spot: float) -> MarketSnapshot:
        return MarketSnapshot(
            underlying="NIFTY", timestamp="2026-01-06T10:00:00+05:30", as_of="2026-01-06", spot=spot,
            expiry="2026-01-15", dte=9, iv_rank=None, prev_high=22_100, prev_low=21_900,
        )  # fmt: skip

    assert snapshot(22_210).pct_from_prev_high == 0.4977
    assert snapshot(21_800).pct_from_prev_low == -0.4566
    assert all(c.passed for c in check_entry(rules(), snapshot(22_210)))
    assert not all(c.passed for c in check_entry(rules(), snapshot(22_050)))
    assert MarketSnapshot(**{**snapshot(22_000).__dict__, "prev_high": None}).pct_from_prev_high is None


def test_sim_buys_the_breakout_and_books_the_target(store):  # noqa: F811
    expiry = "2026-01-15"
    days = [  # (date, high, low, close, price of the 22,200 call)
        ("2026-01-05", 22_100, 21_900, 22_000, 60.0),
        ("2026-01-06", 22_250, 22_050, 22_200, 100.0),  # above the 22,100 high: buy the 22,200 call at 100
        ("2026-01-07", 22_450, 22_200, 22_400, 190.0),  # +90 per unit beats the 80% target
    ]
    for day, high, low, close, price in days:
        store.session.add(
            IndexBar(underlying="NIFTY", date=day, close=close, high=high, low=low, vix=15.0, iv_rank=50)
        )
        store.session.add(
            OptionEod(
                underlying="NIFTY", date=day, expiry=expiry, strike=22_200.0, option_type="CE", close=price,
                settle=price, volume=100, open_interest=1000, lot_size=65, underlying_price=None,
            )  # fmt: skip
        )
    store.session.commit()

    settings = get_settings()
    doc = parse_strategy_markdown(LONG_CALL)
    feed = HistoricalFeed(store, "NIFTY", dte_window=dte_window(doc.rules))
    result = simulate(
        doc.rules, feed, len(feed.dates), resolve_cost_profile(settings, doc), settings,
        scenario=Scenario("base", doc.frontmatter["rules"], lots=1), mode="backtest",
    )  # fmt: skip
    [trade] = result.trades
    assert trade.date == "2026-01-06"
    assert trade.exit_rule == "exit.profit_target: pnl >= 5200 (actual 5850)"  # 80% of the 6,500 paid
    assert trade.gross == pytest.approx(90 * 65)
    assert trade.net < trade.gross  # brokerage and taxes on both legs


def test_debit_scenarios_never_stop_beyond_the_premium_paid():
    labels = [s.label for s in build_scenarios(parse_strategy_markdown(LONG_CALL), get_settings())]
    assert labels == ["base", "stop_loss 20%", "stop_loss 60%", "profit_target 48%", "profit_target 112%"]
    capped = LONG_CALL.replace("stop_loss_pct_of_debit, value: 40", "stop_loss_pct_of_debit, value: 100")
    assert "stop_loss 100%" not in [s.label for s in build_scenarios(parse_strategy_markdown(capped), get_settings())]


def test_historical_feed_reads_the_previous_day_range(store):  # noqa: F811
    store.session.add_all(
        [
            IndexBar(underlying="NIFTY", date="2026-01-05", close=22_000, high=22_100, low=21_900),
            IndexBar(underlying="NIFTY", date="2026-01-06", close=22_200, high=22_250, low=22_050),
        ]
    )
    store.session.commit()
    assert store.previous_range("NIFTY", "2026-01-06") == (22_100, 21_900)
    assert store.previous_range("NIFTY", "2026-01-05") is None
