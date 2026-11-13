"""The trade management policy: sizing, booking and time exits, the daily profit target, compounding,
in the Trade Crew, in the simulator, and the approval backtest."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.agents.trade_crew.graph import run_tick
from app.config import get_settings
from app.engine.costs import resolve_cost_profile
from app.engine.market import Bar
from app.engine.sim import Management, Scenario, dte_window, simulate
from app.live.drawdown import DrawdownPolicy
from app.live.store import LiveStore
from app.market.historical import HistoricalFeed
from app.strategies.schema import parse_strategy_markdown
from app.trading.decisions import PositionMark
from app.trading.policy import (
    TradePolicy,
    booking_exit,
    daily_target_check,
    parse_policy,
    partial_lots,
    size_lots,
    sizing_capital,
)
from tests.conftest import ACTOR, APPROVABLE_MARKDOWN, approval_body, approve_and_start
from tests.test_simulator import ENTRY as ENTRY_CHAIN
from tests.test_simulator import chain, seed, store  # noqa: F401
from tests.test_trade_crew import CALM, ENTRY, Clock, intents, make_ctx  # noqa: F401

RULES = parse_strategy_markdown(APPROVABLE_MARKDOWN).frontmatter["rules"]
UP11 = Bar("2026-01-07", 22000, 80, 12)  # open condor up 11% of its credit
UP23 = Bar("2026-01-07", 22000, 70, 12)  # up 23%
UP35 = Bar("2026-01-07", 22000, 60, 12)  # up 35%
DTE7 = Bar("2026-01-12", 22000, 80, 7)  # up 49%, just short of the 50% profit target


def partial_at(pct: float, close_pct: float) -> dict:
    return {"at_pct_of_credit": pct, "close_pct": close_pct}


def mark(pnl_pct: float, dte: int = 10) -> PositionMark:
    return PositionMark(quotes=[], credit=10_000, pnl=pnl_pct * 100, dte=dte)


def policy(**raw) -> TradePolicy:
    return TradePolicy.model_validate(raw)


def run(ctx, clock, ticks: int, **step) -> list[dict]:
    out = []
    for _ in range(ticks):
        out.append(run_tick(ctx))
        clock.advance(**(step or {"days": 1}))
    return out


def start(client, approvable, account, trade_policy: dict, **limits) -> str:
    return approve_and_start(client, approvable, account, risk_limits=limits | {"policy": trade_policy})


# Validation


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"sizing": {"mode": "risk_pct"}}, "risk_pct is required"),
        ({"sizing": {"mode": "fixed", "risk_inr": 5000}}, "risk_inr is required"),
        ({"sizing": {"mode": "risk_inr", "risk_inr": 5000, "lots": 1}}, "only allowed for fixed"),
        ({"sizing": {"mode": "fixed", "lots": 3}}, "above the approved max of 2 lots"),
        ({"booking": {"partial": [partial_at(30, 50), partial_at(20, 20)]}}, "must increase"),
        ({"booking": {"partial": [partial_at(20, 60), partial_at(30, 40)]}}, "less than 100"),
        ({"booking": {"partial": [partial_at(60, 50)]}}, "past the strategy's 50% profit target"),
        ({"booking": {"trailing": {"after_pct_of_credit": 50, "giveback_pct": 30}}}, "never fire"),
        ({"booking": {"lock_in": {"after_pct_of_credit": 20, "lock_pct_of_credit": 25}}}, "below after_pct"),
        ({"time_exit": {"at": "15:00"}}, "at needs days_before_expiry"),
        ({"time_exit": {"days_before_expiry": 2, "at": "25:00"}}, "should match pattern"),
        ({"time_exit": {"before_events": ["diwali"]}}, "Input should be"),
        ({"compounding": {"profit_share_pct": 50}}, "compounding only applies"),
        ({"daily_target": {"profit_inr": 0}}, "greater than 0"),
        ({"trailing": {}}, "Extra inputs"),
    ],
)
def test_invalid_policies_are_rejected(raw, message):
    with pytest.raises(ValueError, match=message):
        parse_policy(raw, RULES)


# Sizing


def test_risk_sizing_picks_lots_within_the_budget_and_max_lots():
    risk_pct = policy(sizing={"mode": "risk_pct", "risk_pct": 1}).sizing
    assert size_lots(risk_pct, 4, 500_000, 2_000)[0] == 2  # 5,000 budget affords 2 lots of 2,000
    assert size_lots(risk_pct, 1, 500_000, 2_000)[0] == 1  # never above max lots
    lots, check = size_lots(risk_pct, 4, 500_000, 6_000)
    assert lots == 0 and (check.rule, check.actual, check.passed) == ("policy.sizing", 0, False)
    assert size_lots(risk_pct, 4, 500_000, None)[0] == 0  # no quotes: nothing is affordable

    strict = policy(sizing={"mode": "risk_inr", "risk_inr": 5_000, "min_lots": 3}).sizing
    assert size_lots(strict, 4, 0, 2_000)[0] == 0  # affords 2, below min_lots
    assert size_lots(policy(sizing={"mode": "fixed", "lots": 1}).sizing, 2, 0, None) == (1, None)
    assert size_lots(None, 2, 0, None) == (2, None)


def test_compounding_adds_a_share_of_realized_profit_up_to_a_ceiling():
    p = policy(sizing={"mode": "risk_pct", "risk_pct": 1}, compounding={"profit_share_pct": 50, "ceiling_inr": 520_000})
    assert sizing_capital(p, 500_000, 20_000) == 510_000
    assert sizing_capital(p, 500_000, 100_000) == 520_000
    assert sizing_capital(p, 500_000, -30_000) == 500_000  # losses never shrink the base
    assert sizing_capital(policy(), 500_000, 20_000) == 500_000


# Exits


def test_booking_exits_fire_in_order():
    p = policy(
        booking={
            "partial": [{"at_pct_of_credit": 20, "close_pct": 50}],
            "lock_in": {"after_pct_of_credit": 30, "lock_pct_of_credit": 10},
            "trailing": {"after_pct_of_credit": 25, "giveback_pct": 40},
        }
    )
    kw = {"partials_done": 0, "lots_opened": 4, "lots_open": 4}
    assert booking_exit(p, mark(15), peak_pct=15, **kw) is None
    partial = booking_exit(p, mark(22), peak_pct=22, **kw)
    assert (partial.check.rule, partial.close_lots) == ("policy.partial_booking", 2)
    assert booking_exit(p, mark(22), peak_pct=22, **kw | {"partials_done": 1}) is None
    trailing = booking_exit(p, mark(17), peak_pct=30, **kw | {"partials_done": 1})
    assert (trailing.check.rule, trailing.check.threshold, trailing.close_lots) == ("policy.trailing_stop", 18, None)
    lock = booking_exit(p, mark(9), peak_pct=35, **kw)
    assert lock.check.rule == "policy.lock_in"  # lock-in is checked before the trailing stop


def test_partial_bookings_always_leave_a_lot_open():
    cases = ((4, 4, 50), (4, 2, 50), (2, 2, 10), (1, 1, 50))
    assert [partial_lots(*case) for case in cases] == [2, 1, 1, 0]


def test_time_and_event_exits():
    p = policy(time_exit={"days_before_expiry": 2, "at": "15:00", "before_events": ["rbi_policy"]})
    kw = {"peak_pct": None, "partials_done": 0, "lots_opened": 1, "lots_open": 1}
    morning, afternoon = datetime(2026, 1, 5, 4, 0, tzinfo=UTC), datetime(2026, 1, 5, 9, 45, tzinfo=UTC)
    assert booking_exit(p, mark(0, dte=2), now=morning, **kw) is None  # 09:30 IST
    assert booking_exit(p, mark(0, dte=2), now=afternoon, **kw).check.rule == "policy.time_exit"  # 15:15 IST
    assert booking_exit(p, mark(0, dte=1), now=morning, **kw).check.rule == "policy.time_exit"
    assert booking_exit(p, mark(0, dte=2), **kw).check.rule == "policy.time_exit"  # end of day

    rbi = [{"kind": "rbi_policy", "days_from": 1}]
    assert booking_exit(p, mark(0), events=rbi, **kw).check.rule == "policy.event_exit"
    assert booking_exit(p, mark(0), events=[{"kind": "budget", "days_from": 0}], **kw) is None
    assert booking_exit(p, mark(0), events=[{"kind": "rbi_policy", "days_from": 3}], **kw) is None
    assert booking_exit(p, mark(0), events=None, **kw) is None


def test_daily_target_check():
    target = policy(daily_target={"profit_inr": 5_000}).daily_target
    assert daily_target_check(target, 4_999).passed
    assert not daily_target_check(target, 5_000).passed


# The policy in the Trade Crew


def test_partial_booking_then_trailing_stop(client, live_session, make_ctx, approvable, paper_account):
    booking = {
        "partial": [{"at_pct_of_credit": 20, "close_pct": 50}],
        "trailing": {"after_pct_of_credit": 30, "giveback_pct": 50},
    }
    deployment_id = start(client, approvable, paper_account, {"booking": booking})
    clock = Clock()
    results = run(make_ctx(deployment_id, [ENTRY, UP23, UP35, UP11], clock=clock), clock, 4)
    assert [r["outcome"] for r in results] == ["entered", "exited", "holding", "exited"]
    assert results[1]["exits"][0] | {"live_trade_id": None} == {
        "open_intent_id": results[0]["intent_id"], "live_trade_id": None,
        "exit_reason": "policy.partial_booking", "partial": True, "lots": 1,
    }  # fmt: skip

    store = LiveStore(live_session)
    final, partial = store.live_trades(deployment_id)
    assert (partial.partial, partial.exit_reason, {leg["quantity"] for leg in partial.legs}) == (
        True, "policy.partial_booking", {75},
    )  # fmt: skip
    assert (final.partial, final.exit_reason, {leg["quantity"] for leg in final.legs}) == (
        False, "policy.trailing_stop", {75},
    )  # fmt: skip
    assert store.open_intents(deployment_id) == []
    opened = intents(live_session, deployment_id)[0]
    half_target = float(opened.target_pnl) / 2  # half the target went with half the lots
    assert float(partial.pnl_vs_target) == pytest.approx(float(partial.net) - half_target, abs=1e-3)

    snapshots = LiveStore(live_session).position_snapshots([deployment_id])
    peaks = [s.positions[0]["peak_pct"] for s in snapshots]  # at fill prices, which include slippage
    assert peaks == [pytest.approx(23, abs=0.5), pytest.approx(35.4, abs=0.5)]
    assert {leg["quantity"] for leg in snapshots[0].positions[0]["legs"]} == {75}
    live_session.rollback()
    equity = client.get("/api/live/equity", params={"deployment_id": deployment_id}).json()
    assert equity["unrealized_inr"] == 0
    assert equity["realized_inr"] == pytest.approx(float(partial.net + final.net))


def test_time_exit_waits_for_its_time_of_day(client, live_session, make_ctx, approvable, paper_account):
    deployment_id = start(client, approvable, paper_account, {"time_exit": {"days_before_expiry": 7, "at": "15:00"}})
    clock = Clock()  # 09:30 IST
    ctx = make_ctx(deployment_id, [ENTRY, DTE7, DTE7], clock=clock)
    assert run_tick(ctx)["outcome"] == "entered"
    clock.advance(days=1)
    assert run_tick(ctx)["outcome"] == "holding"  # 7 days to expiry, but before 15:00
    clock.advance(hours=6)  # 15:30 IST
    exited = run_tick(ctx)
    assert exited["outcome"] == "exited" and exited["exits"][0]["exit_reason"] == "policy.time_exit"


def test_risk_sizing_trades_fewer_lots_or_blocks(client, live_session, make_ctx, approvable, paper_account):
    sized = start(client, approvable, paper_account, {"sizing": {"mode": "risk_inr", "risk_inr": 6_000}})
    clock = Clock()
    [entered] = run(make_ctx(sized, [ENTRY], clock=clock), clock, 1)
    assert entered["outcome"] == "entered"
    [intent] = LiveStore(live_session).intents(sized)
    assert {leg["quantity"] for leg in intent.legs} == {75}  # max loss is about 4,900 a lot
    assert "Position sizing allows 1 of 2 approved lots." in intent.reason_text
    assert intent.risk_checks["lot_size"] == 75
    live_session.rollback()
    client.post(f"/api/live/deployments/{sized}/stop", headers=ACTOR)

    tight = start(client, approvable, paper_account, {"sizing": {"mode": "risk_inr", "risk_inr": 3_000}})
    clock = Clock()
    [blocked] = run(make_ctx(tight, [ENTRY], clock=clock), clock, 1)
    assert blocked["outcome"] == "blocked" and blocked["detail"] == "policy.sizing"


def test_daily_target_stops_entries_for_the_day(client, live_session, make_ctx, approvable, paper_account):
    deployment_id = start(client, approvable, paper_account, {"daily_target": {"profit_inr": 2_000}})
    clock = Clock()
    ctx = make_ctx(deployment_id, [ENTRY, CALM, ENTRY, ENTRY], clock=clock)
    results = run(ctx, clock, 3, hours=1)
    assert [r["outcome"] for r in results] == ["entered", "exited", "blocked"]
    assert results[2]["detail"] == "policy.daily_profit_target"
    clock.advance(days=1)
    assert run_tick(ctx)["outcome"] == "entered"  # a new day


def test_daily_target_can_close_everything(client, live_session, make_ctx, approvable, paper_account):
    target = {"daily_target": {"profit_inr": 2_000, "on_hit": "close_all"}}
    deployment_id = start(client, approvable, paper_account, target)
    clock = Clock()
    results = run(make_ctx(deployment_id, [ENTRY, UP23], clock=clock), clock, 2)
    assert [r["outcome"] for r in results] == ["entered", "exited"]
    [trade] = LiveStore(live_session).live_trades(deployment_id)
    assert trade.exit_reason == "policy.daily_profit_target" and trade.net > 0


def test_approval_freezes_the_policy(client, approvable, paper_account):
    refused = client.post(
        "/api/live/approvals",
        json=approval_body(approvable, paper_account, risk_limits={"policy": {"sizing": {"mode": "fixed", "lots": 5}}}),
        headers=ACTOR,
    )
    assert refused.status_code == 400 and "above the approved max" in refused.json()["detail"]

    raw = {"sizing": {"mode": "risk_pct", "risk_pct": 1}, "daily_target": {"profit_inr": 5000}}
    deployment_id = start(client, approvable, paper_account, raw)
    frozen = client.get(f"/api/live/deployments/{deployment_id}").json()["risk_limits"]["policy"]
    assert frozen == {
        "sizing": {"mode": "risk_pct", "risk_pct": 1.0, "min_lots": 1},
        "daily_target": {"profit_inr": 5000.0, "on_hit": "stop_new_entries"},
    }


# The policy in the simulator


def simulate_with(store, management: Management | None):  # noqa: F811
    settings = get_settings()
    doc = parse_strategy_markdown(APPROVABLE_MARKDOWN)
    feed = HistoricalFeed(store, "NIFTY", dte_window=dte_window(doc.rules))
    return simulate(
        doc.rules, feed, len(feed.dates), resolve_cost_profile(settings, doc), settings,
        scenario=Scenario("base", RULES, lots=2), mode="backtest", management=management,
    )  # fmt: skip


def managed(**raw) -> Management:
    ladder = raw.pop("drawdown", None)
    return Management(
        policy=TradePolicy.model_validate(raw) if raw else None,
        drawdown=DrawdownPolicy.model_validate(ladder) if ladder else None,
    )


def test_sim_partial_booking_and_trailing_stop(store):  # noqa: F811
    seed(
        store,
        [
            ("2026-01-05", 22000, 60, ENTRY_CHAIN),
            ("2026-01-06", 22000, 10, chain(40, 20, 40, 20)),  # up 33%
            ("2026-01-07", 22000, 10, chain(35, 18, 35, 18)),  # up 43%
            ("2026-01-08", 22000, 10, chain(50, 25, 50, 25)),  # back to 17%
        ],
    )
    plain = simulate_with(store, None)
    assert [t.exit_reason for t in plain.trades] == ["window_end"] and plain.management == {}
    assert simulate_with(store, Management()).net_pnl == pytest.approx(plain.net_pnl)

    booking = {"partial": [partial_at(20, 50)], "trailing": {"after_pct_of_credit": 30, "giveback_pct": 50}}
    result = simulate_with(store, managed(booking=booking))
    partial, final = result.trades
    assert (partial.exit_date, partial.exit_reason, partial.lots, partial.partial) == (
        "2026-01-06", "policy.partial_booking", 1, True,
    )  # fmt: skip
    assert (final.exit_date, final.exit_reason, final.lots, final.partial) == (
        "2026-01-08", "policy.trailing_stop", 1, False,
    )  # fmt: skip
    assert partial.gross == pytest.approx(20 * 75) and final.gross == pytest.approx(10 * 75)
    assert partial.target_pnl == pytest.approx(final.target_pnl) == pytest.approx(2250)
    assert result.management["partial_closes"] == 1
    assert result.management["policy_exits"] == {"policy.partial_booking": 1, "policy.trailing_stop": 1}
    assert result.equity_curve[-1]["equity"] == pytest.approx(result.net_pnl)


def test_sim_sizing_time_exit_and_daily_target(store):  # noqa: F811
    seed(
        store,
        [
            ("2026-01-05", 22000, 60, ENTRY_CHAIN),
            ("2026-01-06", 22000, 10, chain(40, 20, 40, 20)),  # up 3,000
            ("2026-01-07", 22000, 10, ENTRY_CHAIN),  # 8 days to expiry
        ],
    )
    [trade] = simulate_with(store, managed(sizing={"mode": "risk_inr", "risk_inr": 5_000})).trades
    assert trade.lots == 1  # max loss 4,500 a lot
    blocked = simulate_with(store, managed(sizing={"mode": "risk_inr", "risk_inr": 4_000}))
    assert blocked.trades == [] and blocked.skipped["policy.sizing"] == 1

    [timed] = simulate_with(store, managed(time_exit={"days_before_expiry": 8})).trades
    assert (timed.exit_date, timed.exit_reason) == ("2026-01-07", "policy.time_exit")

    [booked] = simulate_with(store, managed(daily_target={"profit_inr": 2_000, "on_hit": "close_all"})).trades
    assert (booked.exit_date, booked.exit_reason) == ("2026-01-06", "policy.daily_profit_target")


def test_sim_drawdown_ladder(store):  # noqa: F811
    gap = chain(220, 90, 5, 2)
    seed(
        store,
        [
            ("2026-01-05", 22000, 60, ENTRY_CHAIN),
            ("2026-01-06", 22900, 10, gap),  # stop loss, about 2.3% of capital
            ("2026-01-07", 22000, 60, ENTRY_CHAIN),
        ],
    )
    assert len(simulate_with(store, None).trades) == 2  # re-enters after the stop

    stopped = simulate_with(store, managed(drawdown={"ladder": [{"at_pct": 1, "action": "stop_new_entries"}]}))
    assert len(stopped.trades) == 1 and stopped.skipped["drawdown.entries_allowed"] == 1
    [event] = stopped.management["drawdown_events"]
    assert (event["date"], event["level"], event["action"]) == ("2026-01-06", 1, "stop_new_entries")

    paused = simulate_with(store, managed(drawdown={"ladder": [{"at_pct": 1, "action": "pause"}]}))
    assert paused.management["paused_on"] == "2026-01-06" and paused.skipped["drawdown.paused"] == 1

    capped = Management(max_drawdown_pct=1)
    assert simulate_with(store, capped).management["paused_on"] == "2026-01-06"


# Approval backtest


def test_backtest_compares_rules_with_and_without_the_policy(client, approvable):
    body = {
        "strategy_id": approvable["strategy_id"],
        "version": 1,
        "risk_limits": {
            "capital_inr": 500_000,
            "policy": {"booking": {"partial": [{"at_pct_of_credit": 25, "close_pct": 50}]}},
            "drawdown": {"ladder": [{"at_pct": 5, "action": "stop_new_entries"}]},
        },
    }
    response = client.post("/api/live/approvals/backtest", json=body)
    assert response.status_code == 200, response.text
    out = response.json()
    assert set(out) >= {"rules_only", "with_policy", "risk_limits", "window", "assumptions", "data_source"}
    keys = {"net_pnl", "return_pct", "trades", "max_drawdown_pct_of_capital", "exit_reasons", "equity_curve"}
    for summary in (out["rules_only"], out["with_policy"]):
        assert keys <= set(summary)
        assert summary["equity_curve"][0]["equity"] == pytest.approx(500_000, rel=0.05)
    assert out["rules_only"]["management"]["policy_exits"] == {}
    assert out["risk_limits"]["policy"]["booking"]["partial"][0]["close_pct"] == 50

    bad = body | {"risk_limits": {"policy": {"booking": {"partial": [{"at_pct_of_credit": 80, "close_pct": 50}]}}}}
    assert client.post("/api/live/approvals/backtest", json=bad).status_code == 400
    missing = body | {"strategy_id": "nope"}
    assert client.post("/api/live/approvals/backtest", json=missing).status_code == 404
