"""The day-by-day simulator on imported end-of-day data, and parity with the Trade Crew."""

from __future__ import annotations

import pytest

from app.agents.trade_crew.graph import run_tick
from app.agents.trade_crew.runner import build_context
from app.config import get_settings
from app.engine.costs import leg_costs, resolve_cost_profile
from app.engine.sim import Scenario, build_scenarios, dte_window, simulate
from app.live.store import LiveStore
from app.market.historical import HistoricalFeed
from app.market.models import IndexBar, OptionEod
from app.market.store import open_market_store
from app.strategies.schema import parse_strategy_markdown
from tests.conftest import APPROVABLE_MARKDOWN, approve_and_start
from tests.test_trade_crew import Clock

E1, E2 = "2026-01-15", "2026-01-22"
CE_SHORT, CE_LONG, PE_SHORT, PE_LONG = (23000.0, "CE"), (23200.0, "CE"), (21000.0, "PE"), (20800.0, "PE")
QTY = 150  # 2 lots of 75


def chain(ce_short, ce_long, pe_short, pe_long) -> dict:
    return {CE_SHORT: ce_short, CE_LONG: ce_long, PE_SHORT: pe_short, PE_LONG: pe_long}


ENTRY = chain(60, 30, 60, 30)  # credit 60 x 150 = 9,000; target 4,500; stop 9,000
NEXT_EXPIRY = chain(90, 50, 90, 50)


@pytest.fixture
def doc():
    return parse_strategy_markdown(APPROVABLE_MARKDOWN)


@pytest.fixture
def store(market_db_url):
    s = open_market_store(get_settings())
    yield s
    s.close()


def seed(store, days: list[tuple[str, float, float, dict]]) -> None:
    """(date, spot, iv_rank, prices for the E1 chain); every day also lists the E2 chain."""
    for day, spot, iv_rank, prices in days:
        store.session.add(IndexBar(underlying="NIFTY", date=day, close=spot, vix=15.0, iv_rank=iv_rank))
        for expiry, quotes in ((E1, prices), (E2, NEXT_EXPIRY)):
            if expiry < day:
                continue
            store.session.add_all(
                OptionEod(
                    underlying="NIFTY", date=day, expiry=expiry, strike=strike, option_type=option_type,
                    close=price, settle=price, volume=100, open_interest=1000, lot_size=75, underlying_price=None,
                )  # fmt: skip
                for (strike, option_type), price in quotes.items()
            )
    store.session.commit()


def run(store, doc):
    settings = get_settings()
    feed = HistoricalFeed(store, "NIFTY", dte_window=dte_window(doc.rules))
    scenario = Scenario("base", doc.frontmatter["rules"], lots=2)
    return simulate(
        doc.rules, feed, len(feed.dates), resolve_cost_profile(settings, doc), settings,
        scenario=scenario, mode="backtest", data_source="nse_eod",
    )  # fmt: skip


def test_stop_fills_at_the_close_even_through_a_gap(store, doc):
    gap = chain(220, 90, 5, 2)  # closing the position costs 133 x 150 = 19,950
    seed(store, [("2026-01-05", 22000, 60, ENTRY), ("2026-01-06", 22900, 10, gap), ("2026-01-07", 22900, 10, gap)])
    result = run(store, doc)
    [trade] = result.trades
    assert (trade.date, trade.exit_date, trade.exit_reason) == ("2026-01-05", "2026-01-06", "stop_loss")
    assert {(leg["strike"], leg["option_type"]): leg["exit_price"] for leg in trade.legs} == gap
    assert trade.gross == pytest.approx((60 - 133) * QTY)
    assert trade.gross < trade.stop_loss_pnl  # daily closes: the loss is not capped at the stop
    assert trade.orders == 8 and trade.holding_bars == 1
    assert result.window_end == "2026-01-07" and len(result.equity_curve) == 3


def test_held_to_expiry_settles_at_intrinsic_with_exercise_stt_only(store, doc):
    days = [("2026-01-05", 22000, 60, ENTRY)]
    days += [(d, 22000, 10, ENTRY) for d in ("2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09", "2026-01-12")]
    days += [(d, 22000, 10, ENTRY) for d in ("2026-01-13", "2026-01-14")]
    days += [(E1, 23300, 10, chain(310, 105, 1, 1))]
    seed(store, days)
    result = run(store, doc)
    [trade] = result.trades
    assert (trade.exit_date, trade.exit_reason, trade.expiry) == (E1, "expiry", E1)
    exits = {(leg["strike"], leg["option_type"]): leg["exit_price"] for leg in trade.legs}
    assert exits == chain(300, 100, 0, 0)  # intrinsic against the 23,300 close, not the traded prices
    assert trade.gross == pytest.approx((60 - 300) * QTY + 60 * QTY + (100 - 30) * QTY - 30 * QTY)
    profile = resolve_cost_profile(get_settings(), doc)
    entry = leg_costs(
        [("SELL", 60, QTY), ("SELL", 60, QTY), ("BUY", 30, QTY), ("BUY", 30, QTY)], profile, "2026-01-05"
    )
    assert trade.costs - entry.total == pytest.approx(100 * QTY * profile.exercise_stt_pct)
    assert trade.orders == 4  # no exit orders: the exchange settles


def test_mark_to_market_drawdown_sees_what_closed_trades_hide(store, doc):
    seed(
        store,
        [
            ("2026-01-05", 22000, 60, ENTRY),
            ("2026-01-06", 22700, 10, chain(150, 60, 30, 15)),  # -6,750 open, stop not reached
            ("2026-01-07", 22000, 10, chain(20, 8, 15, 6)),  # +5,850: profit target
        ],
    )
    result = run(store, doc)
    [trade] = result.trades
    assert trade.exit_reason == "profit_target" and trade.net > 0
    assert result.max_drawdown == 0  # closed-trade equity only ever went up
    assert result.max_drawdown_mtm >= 6750
    assert result.max_drawdown_days == 1
    assert [p["date"] for p in result.equity_curve] == ["2026-01-05", "2026-01-06", "2026-01-07"]


def test_scenarios_are_derived_from_the_strategy_rules(doc):
    scenarios = build_scenarios(doc, get_settings())
    labels = [s.label for s in scenarios]
    assert labels[0] == "base" and scenarios[0].parsed == doc.rules
    assert {"iv_rank >= 45", "iv_rank >= 35", "short_distance 1050", "short_distance 950"} <= set(labels)
    assert all(s.parsed is not None for s in scenarios)
    assert len(scenarios) <= get_settings().max_scenario_combos


def test_trade_crew_and_simulator_make_the_same_decisions(store, doc, client, approvable, paper_account, live_session):
    days = [
        ("2026-01-05", 22000, 60, ENTRY),
        ("2026-01-06", 22700, 10, chain(150, 60, 30, 15)),
        ("2026-01-07", 22000, 10, chain(20, 8, 15, 6)),
        ("2026-01-08", 22000, 70, ENTRY),
        ("2026-01-09", 22900, 10, chain(220, 90, 5, 2)),
    ]
    seed(store, days)
    sim = run(store, doc)

    deployment_id = approve_and_start(client, approvable, paper_account)
    clock = Clock()
    feed = HistoricalFeed(store, "NIFTY", dte_window=dte_window(doc.rules), clock=clock)
    ctx = build_context(live_session, deployment_id, get_settings(), feed=feed, clock=clock)
    outcomes = []
    for _ in days:
        outcomes.append(run_tick(ctx)["outcome"])
        clock.advance(days=1)
    assert outcomes == ["entered", "holding", "exited", "entered", "exited"]

    live = LiveStore(live_session)
    opens = [i for i in live.intents(deployment_id) if i.action == "open"]
    trades = sorted(live.live_trades(deployment_id), key=lambda t: t.opened_at)
    assert [(t.date, t.exit_date, t.exit_reason) for t in sim.trades] == [
        ("2026-01-05", "2026-01-07", "profit_target"),
        ("2026-01-08", "2026-01-09", "stop_loss"),
    ]
    assert [t.exit_reason for t in trades] == [t.exit_reason for t in sim.trades]
    for intent, trade in zip(opens, sim.trades, strict=True):
        assert intent.market_snapshot["as_of"] == trade.date
        assert [(leg["side"], leg["option_type"], leg["strike"], leg["expiry"], leg["quote"]) for leg in intent.legs] == [
            (leg["side"], leg["option_type"], leg["strike"], leg["expiry"], leg["entry_price"]) for leg in trade.legs
        ]
        assert intent.target_pnl == pytest.approx(trade.target_pnl)
    live_session.rollback()
