"""Hard stop-loss as % of capital: rules, rupee economics frozen at entry, and the review report's sweep."""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.agents.review_crew.hard_stop import hard_stop_proposal, pick_stop, sweep_row, sweep_rules
from app.agents.trade_crew.graph import run_tick
from app.agents.trade_crew.legs import Leg, entry_economics
from app.config import get_settings
from app.engine.costs import resolve_cost_profile
from app.engine.market import Bar
from app.engine.sim import Management, Scenario, dte_window, simulate
from app.live.store import LiveStore
from app.market.historical import HistoricalFeed
from app.strategies.rules import PositionState, StrategyRules, check_exit, risk_reward_ratio
from app.strategies.schema import parse_strategy_markdown
from app.trading.policy import TradePolicy
from tests.conftest import APPROVABLE_MARKDOWN, approve_and_start
from tests.test_simulator import ENTRY as ENTRY_CHAIN
from tests.test_simulator import chain, seed, store  # noqa: F401
from tests.test_trade_crew import ENTRY, Clock, intents, make_ctx  # noqa: F401

PREMIUM_EXITS = "  - {kind: profit_target_pct_of_credit, value: 50}\n  - {kind: stop_loss_pct_of_credit, value: 100}\n"
CAPITAL_MARKDOWN = APPROVABLE_MARKDOWN.replace(
    PREMIUM_EXITS, "  - {kind: stop_loss_pct_of_capital, value: 1}\n  - {kind: profit_target_r_multiple, value: 2}\n"
)
CONDOR = parse_strategy_markdown(APPROVABLE_MARKDOWN).frontmatter["rules"]
CAPITAL_EXITS = [{"kind": "stop_loss_pct_of_capital", "value": 3}, {"kind": "profit_target_r_multiple", "value": 3}]


def rules(instrument: dict | None = None, exit: list | None = None) -> StrategyRules:
    raw = copy.deepcopy(CONDOR)
    if instrument is not None:
        raw["instrument"] = instrument
    raw["exit"] = CAPITAL_EXITS if exit is None else exit
    return StrategyRules.model_validate(raw)


# Rules


def test_capital_exits_parse_and_give_the_ratio():
    r = rules()
    assert r.uses_capital_stop and (r.stop_loss_pct_of_capital, r.profit_target_r_multiple) == (3, 3)
    assert r.profit_target_pct is None and r.stop_loss_pct is None
    assert risk_reward_ratio(r) == 3
    assert risk_reward_ratio(StrategyRules.model_validate(CONDOR)) == 0.5  # 50% target over a 100% stop
    with_dte = rules(exit=[*CAPITAL_EXITS, {"kind": "dte_at_or_below", "value": 2}])
    assert with_dte.exit_value("dte_at_or_below") == 2


@pytest.mark.parametrize(
    ("exit", "message"),
    [
        ([CAPITAL_EXITS[0]], "exit needs profit_target_r_multiple"),
        ([CAPITAL_EXITS[1]], "exit needs stop_loss_pct_of_capital"),
        ([*CAPITAL_EXITS, {"kind": "stop_loss_pct_of_credit", "value": 100}], "cannot be combined"),
        ([{"kind": "stop_loss_pct_of_capital", "value": 0}, CAPITAL_EXITS[1]], "above 0 and at most 20"),
        ([{"kind": "stop_loss_pct_of_capital", "value": 25}, CAPITAL_EXITS[1]], "above 0 and at most 20"),
        ([CAPITAL_EXITS[0], {"kind": "profit_target_r_multiple", "value": 0}], "must be above 0"),
    ],
)
def test_invalid_capital_exits_are_rejected(exit, message):
    with pytest.raises(ValidationError, match=message):
        rules(exit=exit)


# Economics


def test_bought_option_stop_is_capped_at_the_debit():
    long_call = rules({"underlying": "NIFTY", "structure": "long_call"})
    econ = entry_economics(long_call, [Leg("BUY", "CE", 22_000)], [100.0], 65, 50_000)
    assert (econ.target_pnl, econ.stop_loss_pnl, econ.max_loss, econ.risk_reward) == (4_500, -1_500, 1_500, 3)
    cheap = entry_economics(long_call, [Leg("BUY", "CE", 22_000)], [10.0], 65, 50_000)
    assert (cheap.target_pnl, cheap.stop_loss_pnl, cheap.max_loss) == (1_950, -650, 650)  # loses at most the 650


def test_spread_target_is_capped_at_what_the_spread_pays():
    spread = rules({"underlying": "NIFTY", "structure": "bull_call_spread", "wing_width": 100})
    legs = [Leg("BUY", "CE", 22_000), Leg("SELL", "CE", 22_100)]
    econ = entry_economics(spread, legs, [100.0, 40.0], 65, 50_000)
    assert econ.credit == -3_900
    assert (econ.target_pnl, econ.stop_loss_pnl) == (2_600, -1_500)  # 3R is 4,500; the spread pays 6,500 - 3,900


def test_credit_structures_cap_the_target_at_the_credit_and_the_loss_at_the_wings():
    condor = rules(
        exit=[{"kind": "stop_loss_pct_of_capital", "value": 1}, {"kind": "profit_target_r_multiple", "value": 2}]
    )
    legs = [Leg("SELL", "CE", 23_000), Leg("SELL", "PE", 21_000), Leg("BUY", "CE", 23_200), Leg("BUY", "PE", 20_800)]
    econ = entry_economics(condor, legs, [60.0, 60.0, 30.0, 30.0], 150, 500_000)
    assert (econ.credit, econ.target_pnl, econ.stop_loss_pnl, econ.max_loss) == (9_000, 9_000, -5_000, 5_000)
    wide = rules(
        exit=[{"kind": "stop_loss_pct_of_capital", "value": 5}, {"kind": "profit_target_r_multiple", "value": 2}]
    )
    econ = entry_economics(wide, legs, [60.0, 60.0, 30.0, 30.0], 150, 500_000)
    assert (econ.stop_loss_pnl, econ.max_loss) == (-25_000, 21_000)  # the wings lose at most 30,000 - 9,000

    strangle = rules({"underlying": "NIFTY", "structure": "short_strangle", "short_distance": 500})
    econ = entry_economics(strangle, [Leg("SELL", "CE", 22_500), Leg("SELL", "PE", 21_500)], [50.0, 50.0], 75, 50_000)
    assert (econ.target_pnl, econ.stop_loss_pnl, econ.max_loss) == (4_500, -1_500, 1_500)


def test_capital_stop_needs_the_capital():
    with pytest.raises(ValueError, match="needs the capital"):
        entry_economics(rules(), [Leg("SELL", "CE", 23_000)], [60.0], 75)


def test_capital_exits_fire_on_the_frozen_rupee_values_only():
    r = rules()

    def fired(pnl: float, frozen) -> dict:
        return {c.rule: c.passed for c in check_exit(r, PositionState(credit=9_000, pnl=pnl), None, frozen)}

    assert fired(-1_500, (4_500, -1_500)) == {"exit.profit_target": False, "exit.stop_loss": True}
    assert fired(4_500, (4_500, -1_500)) == {"exit.profit_target": True, "exit.stop_loss": False}
    assert fired(-9_000, None) == {"exit.profit_target": False, "exit.stop_loss": False}


# The simulator


def simulate_capital(store, management: Management | None = None):  # noqa: F811
    settings = get_settings()
    doc = parse_strategy_markdown(CAPITAL_MARKDOWN)
    feed = HistoricalFeed(store, "NIFTY", dte_window=dte_window(doc.rules))
    return simulate(
        doc.rules, feed, len(feed.dates), resolve_cost_profile(settings, doc), settings,
        scenario=Scenario("base", doc.frontmatter["rules"], lots=2), mode="backtest", management=management,
    )  # fmt: skip


def test_sim_stops_at_the_rupee_stop_fixed_at_entry(store):  # noqa: F811
    # Closing costs 95 x 150 = 14,250 against the 9,000 credit: -5,250, past the 1% stop (5,000 on 500,000)
    # but far from the 100%-of-credit stop the premium rules would use.
    seed(store, [("2026-01-05", 22000, 60, ENTRY_CHAIN), ("2026-01-06", 22000, 10, chain(110, 45, 60, 30))])
    [trade] = simulate_capital(store).trades
    assert (trade.target_pnl, trade.stop_loss_pnl, trade.max_loss) == (9_000, -5_000, 5_000)
    assert (trade.exit_date, trade.exit_reason) == ("2026-01-06", "stop_loss")
    assert trade.exit_rule == "exit.stop_loss: pnl <= -5000 (actual -5250)"


def test_sim_scales_the_stop_after_a_partial_close(store):  # noqa: F811
    seed(
        store,
        [
            ("2026-01-05", 22000, 60, ENTRY_CHAIN),
            ("2026-01-06", 22000, 10, chain(40, 20, 40, 20)),  # up 33%: book one of the two lots
            ("2026-01-07", 22000, 10, chain(110, 45, 70, 35)),  # the last lot is down 40 x 75 = 3,000
        ],
    )
    booking = {"partial": [{"at_pct_of_credit": 20, "close_pct": 50}]}
    partial, final = simulate_capital(store, Management(policy=TradePolicy.model_validate({"booking": booking}))).trades
    assert (partial.exit_reason, partial.partial, partial.lots) == ("policy.partial_booking", True, 1)
    assert (final.exit_reason, final.lots, final.stop_loss_pnl) == ("stop_loss", 1, -2_500)
    assert final.exit_rule == "exit.stop_loss: pnl <= -2500 (actual -3000)"


# The Trade Crew


@pytest.fixture
def capital_strategy(client):
    ingest = client.post("/api/ingest/extract", json={"source_type": "markdown", "text": CAPITAL_MARKDOWN})
    strategy_id = ingest.json()["strategy_id"]
    assert client.post("/api/review/run", json={"strategy_id": strategy_id}).status_code == 200
    preview = client.get("/api/live/approvals/preview", params={"strategy_id": strategy_id, "version": 1}).json()
    assert preview["blockers"] == [], preview
    return {"strategy_id": strategy_id, "preview": preview}


def test_crew_stops_at_the_stored_rupee_stop(client, live_session, make_ctx, capital_strategy, paper_account):  # noqa: F811
    deployment_id = approve_and_start(client, capital_strategy, paper_account)
    clock = Clock()
    ctx = make_ctx(
        deployment_id, [ENTRY, Bar("2026-01-06", 22900, 80, 13), Bar("2026-01-07", 23100, 80, 12)], clock=clock
    )
    assert run_tick(ctx)["outcome"] == "entered"
    [intent] = intents(live_session, deployment_id)
    assert (float(intent.stop_loss_pnl), float(intent.max_loss)) == (-5_000, 5_000)  # 1% of 500,000
    clock.advance(days=1)
    assert run_tick(ctx)["outcome"] == "holding"  # about -4,200
    clock.advance(days=1)
    assert run_tick(ctx)["outcome"] == "exited"
    [trade] = LiveStore(live_session).live_trades(deployment_id)
    assert trade.exit_reason == "stop_loss" and trade.exit_rule.startswith("exit.stop_loss: pnl <= -5000 ")
    assert float(trade.gross) > -float(intent.target_pnl)  # well short of a 100%-of-credit stop


def test_crew_scales_the_stop_after_a_partial_close(
    client,
    live_session,
    make_ctx,  # noqa: F811
    capital_strategy,
    paper_account,
):
    booking = {"booking": {"partial": [{"at_pct_of_credit": 20, "close_pct": 50}]}}
    deployment_id = approve_and_start(client, capital_strategy, paper_account, risk_limits={"policy": booking})
    clock = Clock()
    bars = [ENTRY, Bar("2026-01-07", 22000, 70, 12), Bar("2026-01-08", 23100, 80, 11)]
    ctx = make_ctx(deployment_id, bars, clock=clock)
    outcomes = []
    for _ in bars:
        outcomes.append(run_tick(ctx)["outcome"])
        clock.advance(days=1)
    assert outcomes == ["entered", "exited", "exited"]
    final, partial = LiveStore(live_session).live_trades(deployment_id)
    assert (partial.partial, partial.exit_reason) == (True, "policy.partial_booking")
    assert final.exit_reason == "stop_loss" and final.exit_rule.startswith("exit.stop_loss: pnl <= -2500 ")
    assert float(final.gross) > -5_000  # the full stop would not have fired on the one lot left


# The sweep


def test_sweep_rules_swap_the_target_and_stop_but_keep_the_dte_exit():
    raw = copy.deepcopy(CONDOR)
    raw["exit"].append({"kind": "dte_at_or_below", "value": 2})
    swapped = sweep_rules(StrategyRules.model_validate(raw), 3, 0.5)
    assert swapped["exit"] == [
        {"kind": "dte_at_or_below", "value": 2},
        {"kind": "stop_loss_pct_of_capital", "value": 3},
        {"kind": "profit_target_r_multiple", "value": 0.5},
    ]
    assert StrategyRules.model_validate(swapped).uses_capital_stop


def result(nets: list[float], drawdown: float = 0.0, blocked: int = 0) -> SimpleNamespace:
    wins = sum(n > 0 for n in nets)
    return SimpleNamespace(
        trades=[SimpleNamespace(net=n) for n in nets], net_pnl=sum(nets), win_rate=wins / len(nets) if nets else 0.0,
        max_drawdown=drawdown, max_drawdown_mtm=drawdown, skipped={"risk.max_loss_pct_of_capital": blocked},
    )  # fmt: skip


def test_sweep_row_measures_losses_drawdown_and_eligibility():
    settings = get_settings()  # at least 3 backtest and 2 forward trades
    row = sweep_row(3, 50_000, 3, result([2_000, -1_500, -1_700], 3_200, blocked=4), result([900, 600], 500), settings)
    assert (row["stop_inr"], row["target_inr"]) == (1_500, 4_500)
    assert row["backtest"] == {"trades": 3, "win_rate": 0.3333, "net_pnl": -1_200}
    assert (row["avg_loss"], row["worst_loss"], row["max_drawdown"], row["sizing_blocked"]) == (
        -1_600,
        -1_700,
        3_200,
        4,
    )
    assert not row["eligible"]  # the backtest lost money
    few = sweep_row(3, 50_000, 3, result([500, 500, 500]), result([500]), settings)
    assert not few["eligible"] and few["avg_loss"] is None  # one forward trade is too few


def row(pct: float, net: float, drawdown: float, eligible: bool = True) -> dict:
    return {
        "stop_pct": pct, "stop_inr": pct * 500, "backtest": {"net_pnl": net / 2}, "forward": {"net_pnl": net / 2},
        "max_drawdown": drawdown, "eligible": eligible,
    }  # fmt: skip


def test_pick_the_best_net_per_drawdown_among_eligible_stops():
    settings = get_settings()
    rows = [row(2, 4_000, 2_000), row(3, 9_000, 3_000), row(4, 50_000, 1_000, eligible=False), row(5, 9_000, 4_000)]
    pct, reason = pick_stop(rows, settings)
    assert pct == 3 and reason.startswith("3% (Rs 1,500)") and "one stretch of history" in reason
    tie = [row(4, 6_000, 2_000), row(2, 3_000, 1_000)]
    assert pick_stop(tie, settings)[0] == 2  # same ratio: the smaller stop
    assert pick_stop([row(3, 1_000, 0), row(2, 9_000, 1_000)], settings)[0] == 3  # no drawdown at all wins


def test_no_stop_is_recommended_when_none_qualifies():
    pct, reason = pick_stop([row(2, -100, 500, eligible=False), row(3, 900, 200, eligible=False)], get_settings())
    assert pct is None and reason.startswith("No stop made money in both")


def test_improvement_proposal_for_the_recommended_stop():
    rows = [row(2, 4_000, 2_000), row(3, 9_000, 3_000)]
    sweep = {"capital_inr": 50_000, "risk_reward": 3, "rows": rows, "recommended_pct": 3, "reason": "because"}
    proposal = hard_stop_proposal(sweep, StrategyRules.model_validate(CONDOR))
    assert proposal["proposed_change"] == {
        "section": "Exit", "field": "stop_loss_pct_of_capital", "current": None, "proposed": 3,
    }  # fmt: skip
    assert proposal["evidence"]["rows"] == rows and proposal["rationale"] == "because"
    assert hard_stop_proposal(sweep, rules()) is None  # already at 3%
    assert hard_stop_proposal(sweep | {"recommended_pct": None}, rules()) is None


def test_sweep_setting_parses_and_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("HARD_STOP_SWEEP_PCTS", "5, 2,3")
    get_settings.cache_clear()
    assert get_settings().hard_stop_sweep == [2, 3, 5]
    monkeypatch.setenv("HARD_STOP_SWEEP_PCTS", "")
    get_settings.cache_clear()
    assert get_settings().hard_stop_sweep == []


def test_report_payload_has_the_sweep(client, approvable):
    report = client.get(f"/api/reports/{approvable['report_id']}").json()
    sweep = report["payload"]["hard_stop_sweep"]
    assert sweep["capital_inr"] == 500_000 and sweep["risk_reward"] == 0.5
    assert [r["stop_pct"] for r in sweep["rows"]] == [2, 3, 4, 5]
    assert [r["target_inr"] for r in sweep["rows"]] == [5_000, 7_500, 10_000, 12_500]
    assert sweep["reason"]
    assert report["verdict_label"] == report["payload"]["verdict"]["label"]  # the verdict ignores the sweep
