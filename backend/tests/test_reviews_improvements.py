from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.agents.review_crew.graph import _verdict_node
from app.agents.review_crew.improver import propose_improvements
from app.agents.review_crew.reviewer import review_sim_runs, review_trade
from app.db.models import SimTrade, StrategyImprovement, TradeReview
from app.db.types import utcnow
from app.engine.costs import CostBreakdown
from app.engine.sim import Condition, Scenario, SimResult, TradeResult
from app.extract.extractor import FIXTURE_MARKDOWN
from app.journal.store import JournalStore
from app.reports.store import ReportStore
from app.strategies.store import StrategyStore

SCENARIO = Scenario(
    label="base",
    rules={
        "instrument": {"underlying": "NIFTY", "structure": "iron_condor", "short_distance": 300, "wing_width": 100},
        "entry": [
            {"field": "iv_rank", "op": ">=", "value": 35},
            {"field": "dte", "op": ">=", "value": 7},
            {"field": "dte", "op": "<=", "value": 21},
        ],
        "exit": [{"kind": "profit_target_pct_of_credit", "value": 50}],
        "risk": {"max_lots": 1, "max_daily_loss_pct": 2},
    },
    lots=1,
)


def make_trade(iv: float, dte: int, gross: float, costs: float, date: str = "2024-01-02") -> TradeResult:
    net = gross - costs
    return TradeResult(
        date=date,
        gross=gross,
        costs=costs,
        net=net,
        entry_rule=SCENARIO.entry_rule,
        entry_conditions=[
            Condition("iv_rank", ">=", SCENARIO.iv_rank_min, iv, iv >= SCENARIO.iv_rank_min),
            Condition("dte", ">=", SCENARIO.dte_min, dte, dte >= SCENARIO.dte_min),
            Condition("dte", "<=", SCENARIO.dte_max, dte, dte <= SCENARIO.dte_max),
        ],
        reason_text=f"IV rank {iv} DTE {dte}",
        edge=iv - SCENARIO.iv_rank_min,
        market_snapshot={"date": date, "close": 22000.0, "iv_rank": iv, "dte": dte},
        lots=1,
        credit=1200,
        max_loss=2400,
        notional=1200,
        orders=4,
        cost_breakdown=CostBreakdown(costs, 0, 0, 0, 0, 0, 0),
        target_pnl=1200,
        stop_loss_pnl=-2400,
        risk_reward=0.5,
        pnl_vs_target=net - 1200,
        cost_ratio=(costs / abs(gross)) if gross else None,
        outcome_label="win" if net > 12 else ("loss" if net < -12 else "scratch"),
    )


def sim_result(trades: list[TradeResult], mode: str) -> SimResult:
    return SimResult(
        scenario=SCENARIO,
        mode=mode,
        trades=trades,
        gross_pnl=sum(t.gross for t in trades),
        total_costs=sum(t.costs for t in trades),
        net_pnl=sum(t.net for t in trades),
        win_rate=0.0,
        max_drawdown=0.0,
        window_start="2024-01-02",
        window_end="2024-02-01",
        bars_in_window=20,
    )


def test_review_rules_are_deterministic():
    marginal = make_trade(iv=36, dte=14, gross=1000, costs=100)
    review = review_trade(marginal)
    assert review["mistake_tags"] == ["marginal_entry"]
    assert review["process_verdict"] == "bad_process_good_outcome"
    assert review["suggested_rule_change"] == {
        "section": "Entry", "field": "iv_rank_min", "current": 35, "proposed": 37.0,
    }  # fmt: skip
    assert review == review_trade(marginal)

    eaten = review_trade(make_trade(iv=50, dte=14, gross=50, costs=100))
    assert eaten["mistake_tags"] == ["costs_ate_edge", "high_cost_ratio"]
    assert eaten["process_verdict"] == "bad_process_bad_outcome"
    assert eaten["suggested_rule_change"]["section"] == "Risk"

    clean = review_trade(make_trade(iv=50, dte=14, gross=1000, costs=50))
    assert clean["mistake_tags"] == []
    assert clean["process_verdict"] == "good_process_good_outcome"
    assert clean["followed_plan"] is True


def test_forward_decay_tags_forward_trades():
    backtest = sim_result([make_trade(50, 14, 1000, 50) for _ in range(3)], "backtest")
    forward = sim_result([make_trade(50, 14, 200, 50) for _ in range(3)], "forward")
    reviews = review_sim_runs(
        [
            {"scenario_index": 0, "mode": "backtest", "result": backtest},
            {"scenario_index": 0, "mode": "forward", "result": forward},
        ]
    )
    tags = {r["key"]: r["mistake_tags"] for r in reviews}
    assert all("forward_decay" not in tags[(0, "backtest", s)] for s in (1, 2, 3))
    assert all("forward_decay" in tags[(0, "forward", s)] for s in (1, 2, 3))


def _losing_low_iv_runs():
    losers = [make_trade(iv, 14, 300, 500, date=f"2024-01-0{i}") for i, iv in enumerate((36, 37, 38), start=2)]
    winners = [make_trade(iv, 14, 1200, 60, date=f"2024-01-1{i}") for i, iv in enumerate((45, 48, 52, 55))]
    return [
        {"scenario_index": 0, "mode": "backtest", "result": sim_result(losers + winners, "backtest")},
        {"scenario_index": 0, "mode": "forward", "result": sim_result([], "forward")},
    ]


def test_improver_proposes_iv_threshold_with_evidence():
    proposals = propose_improvements(_losing_low_iv_runs())
    iv = [p for p in proposals if p["proposed_change"].get("field") == "iv_rank_min"]
    assert len(iv) == 1
    assert iv[0]["proposed_change"] == {"section": "Entry", "field": "iv_rank_min", "current": 35, "proposed": 40}
    assert iv[0]["evidence"]["trade_keys"] == [[0, "backtest", 1], [0, "backtest", 2], [0, "backtest", 3]]
    assert iv[0]["evidence"]["value_in_bucket"] < 0 < iv[0]["evidence"]["value_rest"]


def test_improver_is_silent_without_a_losing_group():
    winners = [make_trade(iv, 14, 1200, 60) for iv in (36, 45, 48, 52)]
    runs = [{"scenario_index": 0, "mode": "backtest", "result": sim_result(winners, "backtest")}]
    assert propose_improvements(runs) == []


def test_every_sim_trade_has_one_agent_review(session, demo):
    trade_count = session.scalar(select(func.count()).select_from(SimTrade))
    per_trade = session.execute(
        select(TradeReview.sim_trade_id, func.count())
        .where(TradeReview.reviewer_type == "agent")
        .group_by(TradeReview.sim_trade_id)
    ).all()
    assert len(per_trade) == trade_count > 0
    assert {c for _, c in per_trade} == {1}


def test_persisted_improvement_evidence_points_at_report_trades(session):
    saved = StrategyStore(session).save(FIXTURE_MARKDOWN)
    sim_runs = _losing_low_iv_runs()
    proposals = propose_improvements(sim_runs)
    reviews = review_sim_runs(sim_runs)
    report = ReportStore(session).create_success(
        strategy_id=saved.strategy_id,
        version=saved.version,
        payload={"verdict": {"label": "Conditional"}, "scenario_results": [{"pass_net": False}]},
        sim_runs=sim_runs,
        reviews=reviews,
        improvements=proposals,
        inputs={},
        started_at=utcnow(),
    )
    session.commit()
    improvements = session.scalars(select(StrategyImprovement)).all()
    assert improvements
    for imp in improvements:
        assert imp.report_id == report.id and imp.from_version_id == saved.version.id
        ids = imp.evidence["trade_ids"]
        assert "trade_keys" not in imp.evidence
        rows = session.scalars(select(SimTrade).where(SimTrade.id.in_(ids))).all()
        assert len(rows) == len(ids) and {r.report_id for r in rows} == {report.id}


def test_apply_improvement_creates_linked_version(session, client):
    saved = StrategyStore(session).save(FIXTURE_MARKDOWN)
    journal = JournalStore(session)
    improvement = journal.propose(
        strategy_id=saved.strategy_id,
        title="raise iv_rank_min",
        rationale="low IV trades lose",
        proposed_change={"section": "Entry", "field": "iv_rank_min", "current": 35, "proposed": 40},
    )
    session.commit()

    r = client.post(f"/api/improvements/{improvement.id}/decisions", json={"decision": "applied"})
    assert r.status_code == 400 and "markdown" in r.json()["detail"]

    new_md = FIXTURE_MARKDOWN.replace("IV rank >= 40", "IV rank >= 45")
    r = client.post(f"/api/improvements/{improvement.id}/decisions", json={"decision": "applied", "markdown": new_md})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "applied"

    versions = client.get(f"/api/strategies/{saved.strategy_id}/versions").json()["versions"]
    assert [v["version"] for v in versions] == [1, 2]
    assert body["resulting_version_id"] == versions[1]["id"]

    r = client.post(f"/api/improvements/{improvement.id}/decisions", json={"decision": "rejected"})
    assert r.status_code == 400 and "already applied" in r.json()["detail"]

    listed = client.get(f"/api/strategies/{saved.strategy_id}/improvements").json()["improvements"]
    assert listed[0]["status"] == "applied" and len(listed[0]["decisions"]) == 1


def test_apply_with_unchanged_markdown_is_rejected(session):
    saved = StrategyStore(session).save(FIXTURE_MARKDOWN)
    journal = JournalStore(session)
    improvement = journal.propose(strategy_id=saved.strategy_id, title="t", rationale="r", proposed_change={})
    with pytest.raises(ValueError, match="unchanged"):
        journal.decide(improvement.id, "applied", markdown=FIXTURE_MARKDOWN)


def test_human_review_supersedes_agent_review(client, demo):
    trade = client.get(f"/api/reports/{demo['report_id']}/trades", params={"limit": 1}).json()["trades"][0]
    reviews = client.get(f"/api/trades/sim/{trade['id']}/reviews").json()["reviews"]
    assert [r["reviewer_type"] for r in reviews] == ["agent"]
    r = client.post(
        f"/api/trades/sim/{trade['id']}/reviews",
        json={"lesson": "agree", "mistake_tags": ["late_entry"], "supersedes_review_id": reviews[0]["id"]},
        headers={"X-Actor": "carol"},
    )
    assert r.status_code == 200
    assert r.json()["reviewer"] == "carol" and r.json()["reviewer_type"] == "human"
    r = client.post(f"/api/trades/sim/{trade['id']}/reviews", json={"supersedes_review_id": 999999})
    assert r.status_code == 400
    assert client.get("/api/trades/sim/999999/reviews").status_code == 404
    assert client.get("/api/trades/bogus/1/reviews").status_code == 400


def _verdict(source: str, bt_trades: int, fw_trades: int) -> str:
    def window(net, trades):
        return {"net_pnl": net, "gross_pnl": net, "trades": trades}

    results = [{"backtest": window(1000, bt_trades), "forward": window(500, fw_trades)}]
    state = {"scenario_results": results, "audit": {"simulatable": True, "issues": []}, "data_source": source}
    return _verdict_node(state)["verdict"]["label"]


def test_synthetic_data_is_never_called_profitable():
    assert _verdict("nse_eod", 40, 12) == "Profitable"
    assert _verdict("sample_synthetic", 40, 12) == "Synthetic data only"


def test_a_handful_of_winning_trades_is_too_few_to_judge():
    assert _verdict("nse_eod", 3, 2) == "Too few trades"
