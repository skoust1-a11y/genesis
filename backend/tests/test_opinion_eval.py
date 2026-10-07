from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.config import get_settings
from app.live.opinion_eval import Outcome, bootstrap_mean_ci, evaluate, rolling_is_negative, summarize
from app.live.store import LiveStore
from tests.conftest import ACTOR
from tests.test_trade_crew import deployment_id  # noqa: F401

T0 = datetime(2026, 1, 5, 4, 0, tzinfo=UTC)


def outcome(i: int, decision: str, rules_net: float, llm_net: float) -> Outcome:
    return Outcome(f"o{i:03d}", f"i{i:03d}", "shadow", decision, 2, 0, T0 + timedelta(days=i), rules_net, llm_net)


def test_no_verdict_until_enough_skips_or_reductions():
    rows = [outcome(i, "skip", -100, 0) for i in range(29)] + [outcome(29, "take", 50, 50)]
    summary = summarize(rows, 30)
    assert summary["verdict"] is None and summary["interventions"] == 29
    assert summary["verdict_reason"] == "needs 1 more closed skip or reduce opinions"

    rows.append(outcome(30, "reduce", -100, -50))
    summary = summarize(rows, 30)
    assert summary["verdict"] == "veto_helps" and summary["difference"]["ci95"][0] > 0


def test_verdict_reflects_the_confidence_interval():
    hurts = [outcome(i, "skip", 200, 0) for i in range(30)]
    assert summarize(hurts, 30)["verdict"] == "veto_hurts"
    mixed = [outcome(i, "skip", 100 if i % 2 else -100, 0) for i in range(30)]
    assert summarize(mixed, 30)["verdict"] == "inconclusive"


def test_bootstrap_is_seeded_and_brackets_the_mean():
    values = [float(v) for v in (-50, 120, 30, -10, 80, 5, -200, 60)]
    low, high = bootstrap_mean_ci(values)
    assert (low, high) == bootstrap_mean_ci(values)
    assert low < sum(values) / len(values) < high
    assert bootstrap_mean_ci([]) == (0.0, 0.0)


def test_counterfactual_curve_and_drawdowns():
    rows = [outcome(0, "take", 500, 500), outcome(1, "skip", -800, 0), outcome(2, "reduce", 300, 150)]
    summary = summarize(rows, 1)
    assert [p["rules"] for p in summary["equity_curve"]] == [500, -300, 0]
    assert [p["with_llm"] for p in summary["equity_curve"]] == [500, 500, 650]
    assert summary["rules"] == {"net": 0, "max_drawdown": 800}
    assert summary["with_llm"] == {"net": 650, "max_drawdown": 0}
    assert summary["difference"]["total"] == 650
    assert summary["by_decision"]["skip"] == {
        "count": 1, "rules_net": -800, "mean_rules_net": -800, "win_rate": 0, "max_drawdown": 800,
    }  # fmt: skip
    assert summary["by_decision"]["error"]["count"] == 0


def _intent(store: LiveStore, deployment_id: str, approval: str = "auto") -> str:
    intent = store.create_trade_intent(
        deployment_id=deployment_id, action="open", legs=[], reason_text="test", decided_by="test", approval=approval
    )
    return intent.id


def _trade(store: LiveStore, deployment_id: str, intent_id: str, net: str, day: int) -> None:
    store.close_live_trade(
        deployment_id=deployment_id, open_intent_id=intent_id, legs=[], opened_at=T0,
        closed_at=T0 + timedelta(days=day), holding_minutes=0, exit_reason="profit_target",
        gross=Decimal(net), est_costs=Decimal("0"), net=Decimal(net), outcome_label="win",
    )  # fmt: skip


def _opinion(store, deployment_id, intent_id, *, mode="shadow", status="ok", decision, lots, final_lots, rules_lots=2):
    return store.add_trade_opinion(
        deployment_id=deployment_id, trade_intent_id=intent_id, mode=mode, status=status, decision=decision,
        lots=lots, rules_lots=rules_lots, final_lots=final_lots, applied=final_lots != rules_lots,
        prompt_sha256="0" * 64,
    )  # fmt: skip


def _phantom(store, deployment_id, intent_id, opinion_id, kind, lots, net, day):
    store.record_phantom_trade(
        deployment_id=deployment_id, trade_intent_id=intent_id, trade_opinion_id=opinion_id, kind=kind, lots=lots,
        legs=[], opened_at=T0, closed_at=T0 + timedelta(days=day), exit_reason="stop_loss",
        gross=Decimal(net), est_costs=Decimal("0"), net=Decimal(net),
    )  # fmt: skip


@pytest.fixture
def evaluated(live_session, deployment_id):
    store = LiveStore(live_session)
    a = _intent(store, deployment_id)
    _opinion(store, deployment_id, a, decision="take", lots=2, final_lots=2)
    _trade(store, deployment_id, a, "1000", 1)
    b = _intent(store, deployment_id)
    _opinion(store, deployment_id, b, decision="reduce", lots=1, final_lots=2)
    _trade(store, deployment_id, b, "-600", 2)
    c = _intent(store, deployment_id)
    _opinion(store, deployment_id, c, decision="skip", lots=0, final_lots=2)
    _trade(store, deployment_id, c, "400", 3)
    d = _intent(store, deployment_id, approval="vetoed")
    op = _opinion(store, deployment_id, d, mode="veto", decision="skip", lots=0, final_lots=0)
    _phantom(store, deployment_id, d, op.id, "vetoed", 2, "-500", 4)
    e = _intent(store, deployment_id)
    op = _opinion(store, deployment_id, e, mode="veto", decision="reduce", lots=1, final_lots=1)
    _trade(store, deployment_id, e, "200", 5)
    _phantom(store, deployment_id, e, op.id, "reduced", 1, "200", 6)
    f = _intent(store, deployment_id)
    _opinion(store, deployment_id, f, decision="take", lots=2, final_lots=2)  # still open
    g = _intent(store, deployment_id)
    _opinion(store, deployment_id, g, status="error", decision=None, lots=None, final_lots=2)
    _trade(store, deployment_id, g, "100", 7)
    live_session.commit()
    return deployment_id


def test_outcomes_join_live_and_phantom_trades(live_session, evaluated):
    result = evaluate(LiveStore(live_session), get_settings(), evaluated)
    rows = [(o["decision"], o["mode"], o["rules_net"], o["llm_net"]) for o in result["outcomes"]]
    assert rows == [
        ("take", "shadow", 1000, 1000),
        ("reduce", "shadow", -600, -300),
        ("skip", "shadow", 400, 0),
        ("skip", "veto", -500, 0),
        ("reduce", "veto", 400, 200),  # 200 traded plus 200 on the removed lot
        ("error", "shadow", 100, 100),
    ]
    assert result["pending"] == 1 and result["interventions"] == 4
    assert result["difference"]["total"] == 300 - 400 + 500 - 200
    assert result["verdict"] is None and result["rolling"]["window"] == get_settings().opinion_eval_window
    live_session.rollback()


def test_evaluation_endpoint(client, evaluated):
    response = client.get("/api/live/opinions/evaluation", params={"deployment_id": evaluated}, headers=ACTOR)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["closed"] == 6 and body["by_decision"]["skip"]["count"] == 2 and "bootstrap" in body["method"]
    assert len(client.get("/api/live/opinions", params={"deployment_id": evaluated}).json()["opinions"]) == 7
    assert len(client.get("/api/live/phantom-trades").json()["phantom_trades"]) == 2


def test_rolling_window_turns_negative():
    good = [outcome(i, "skip", -100, 0) for i in range(5)]
    assert not rolling_is_negative({"rolling": summarize(good, 0)})
    bad = good + [outcome(5, "skip", 900, 0)]
    assert rolling_is_negative({"rolling": summarize(bad, 0)})
    assert not rolling_is_negative({"rolling": summarize([outcome(0, "take", -50, -50)], 0)})
