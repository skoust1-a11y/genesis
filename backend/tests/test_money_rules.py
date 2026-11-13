"""The money rules: a rupee floor (raised once), the break after a floor pause, and the withdrawal rule."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app import alerts
from app.agents.trade_crew.graph import run_tick
from app.config import get_settings
from app.db.session import get_live_sessionmaker
from app.engine.market import Bar
from app.engine.sim import Management
from app.live import money
from app.live.drawdown import DrawdownPolicy, Floor, floor_breached, floor_checks, floor_in_force, floor_raise_due
from app.live.store import LiveStore
from app.live.supervisor import Supervisor
from app.trading.policy import TradePolicy, Withdrawal, withdrawal_due
from tests.conftest import ACTOR, APPROVABLE_MARKDOWN, approval_body, approve_and_start
from tests.test_live_plumbing import FakeProcess
from tests.test_risk_rules import simulate_md
from tests.test_simulator import ENTRY as ENTRY_CHAIN
from tests.test_simulator import chain, seed, store  # noqa: F401
from tests.test_trade_crew import CALM, ENTRY, Clock, intents, make_ctx  # noqa: F401

SLIDE = Bar("2026-01-08", 22900, 80, 11)  # open condor down about Rs 3.7k, short of its stop loss
FLOOR = {"floor_inr": 300_000, "raise_once": {"at_equity_inr": 600_000, "to_inr": 400_000}, "resume_after_days": 1}


@pytest.fixture
def sent(monkeypatch):
    out: list[tuple[str, str]] = []
    monkeypatch.setattr(alerts, "send", lambda settings, subject, body: out.append((subject, body)) or ["test"])
    return out


def run(ctx, clock, ticks: int, **advance) -> list[dict]:
    out = []
    for _ in range(ticks):
        out.append(run_tick(ctx))
        clock.advance(**(advance or {"days": 1}))
    return out


def events(live_session, deployment_id: str) -> list[str]:
    live_session.rollback()
    out = [e.event for e in LiveStore(live_session).deployment_events(deployment_id)]
    live_session.rollback()  # live transactions are IMMEDIATE: release the lock for the API
    return out


# The floor


@pytest.mark.parametrize(
    "raw, message",
    [
        ({"floor": {"floor_inr": 100, "raise_once": {"at_equity_inr": 200, "to_inr": 90}}}, "above floor_inr"),
        ({"floor": {"floor_inr": 100, "raise_once": {"at_equity_inr": 200, "to_inr": 200}}}, "below at_equity_inr"),
        ({"floor": {"floor_inr": 100, "resume_after_days": 0}}, "greater than or equal to 1"),
        ({"floor": {"floor_inr": 100}, "restore_below_pct": 1}, "below the first level"),
    ],
)
def test_invalid_floors_are_rejected(raw, message):
    with pytest.raises(ValueError, match=message):
        DrawdownPolicy.model_validate(raw)


def test_floor_without_a_ladder_is_valid():
    policy = DrawdownPolicy.model_validate({"floor": {"floor_inr": 50_000}})
    assert policy.ladder == [] and policy.floor.close_open_positions is True and policy.floor.resume_after_days is None


def test_floor_raise_breach_and_entry_checks():
    floor = Floor.model_validate({"floor_inr": 50_000, "raise_once": {"at_equity_inr": 60_000, "to_inr": 55_000}})
    assert floor_in_force(floor, raised=False) == 50_000 and floor_in_force(floor, raised=True) == 55_000
    assert not floor_raise_due(floor, False, 59_999) and floor_raise_due(floor, False, 60_000)
    assert not floor_raise_due(floor, True, 70_000)  # only once
    assert not floor_breached(55_000, 55_000) and floor_breached(55_000, 54_999.99)

    above, buffer = floor_checks(50_000, 55_000, 5_000)
    assert (above.rule, above.passed, buffer.rule, buffer.passed) == ("drawdown.floor", True, "drawdown.floor_buffer", True)
    assert buffer.threshold == 5_000 and buffer.actual == 5_000
    assert [c.passed for c in floor_checks(50_000, 55_000, 5_001)] == [True, False]
    assert [c.passed for c in floor_checks(50_000, 50_000, 1)] == [False, False]
    assert [c.passed for c in floor_checks(50_000, 55_000, None)] == [True, False]  # unknown max loss


def test_approval_validates_the_floor_against_capital(client, approvable, paper_account):
    def refused(risk_limits: dict) -> str:
        body = approval_body(approvable, paper_account, risk_limits=risk_limits)
        response = client.post("/api/live/approvals", json=body, headers=ACTOR)
        assert response.status_code == 400
        return response.json()["detail"]

    assert "floor_inr must be below capital_inr" in refused({"capital_inr": 50_000, "drawdown": {"floor": {"floor_inr": 50_000}}})
    raise_once = {"floor_inr": 40_000, "raise_once": {"at_equity_inr": 50_000, "to_inr": 45_000}}
    assert "at_equity_inr must be above capital_inr" in refused({"capital_inr": 50_000, "drawdown": {"floor": raise_once}})
    withdrawal = {"withdrawal": {"first_target_inr": 50_000, "first_amount_inr": 5_000}}
    assert "first_target_inr must be above capital_inr" in refused({"capital_inr": 50_000, "policy": withdrawal})


def test_crew_blocks_an_entry_whose_max_loss_does_not_fit_the_buffer(client, live_session, make_ctx, approvable, paper_account):
    risk_limits = {"drawdown": {"floor": {"floor_inr": 495_000}}}
    deployment_id = approve_and_start(client, approvable, paper_account, risk_limits=risk_limits)
    [result] = run(make_ctx(deployment_id, [ENTRY]), Clock(), 1)
    assert result["outcome"] == "blocked" and result["detail"] == "drawdown.floor_buffer"
    [intent] = intents(live_session, deployment_id)
    assert intent.risk_checks["failed"] == ["drawdown.floor_buffer"]


def test_floor_pause_break_top_up_and_restart(client, live_session, make_ctx, approvable, paper_account, sent):
    deployment_id = approve_and_start(client, approvable, paper_account, risk_limits={"drawdown": {"floor": FLOOR}})
    clock = Clock()
    ctx = make_ctx(deployment_id, [ENTRY, SLIDE], clock=clock)
    assert run(ctx, clock, 1)[0]["outcome"] == "entered"
    path = f"/api/live/deployments/{deployment_id}/capital-events"
    assert client.post(path, json={"kind": "withdrawal", "amount_inr": 199_000}, headers=ACTOR).status_code == 200

    [paused] = run(ctx, clock, 1)  # equity about 301,000 - 3,700 is now below the 300,000 floor
    assert paused["outcome"] == "paused" and "below the ₹300,000 floor" in paused["detail"]
    assert "break of 1 trading day(s)" in paused["detail"]
    store = LiveStore(live_session)
    [trade] = store.live_trades(deployment_id)
    assert trade.exit_reason == "drawdown.floor" and store.open_intents(deployment_id) == []
    assert events(live_session, deployment_id)[-2:] == ["paused", "floor_paused"]
    assert store.deployment_status(deployment_id) == "paused"  # floor_paused is a notice, not a status

    deployment = store.deployment(deployment_id)
    pause = store.deployment_events(deployment_id)[-1]
    settings = get_settings()
    assert money.resume_if_due(store, settings, deployment, pause.created_at) is None  # the break has not ended
    live_session.rollback()
    view = client.get(f"/api/live/deployments/{deployment_id}/money").json()["floor"]
    assert view["on_break"] is True and view["break_days_left"] >= 1 and view["floor_in_force_inr"] == 300_000

    later = pause.created_at + timedelta(days=14)
    assert money.resume_if_due(store, settings, deployment, later) == "top_up_needed"
    assert money.resume_if_due(store, settings, deployment, later) is None  # alerted once
    assert "deposit more than" in sent[-1][1]
    live_session.rollback()
    assert client.post(path, json={"kind": "deposit", "amount_inr": 10_000}, headers=ACTOR).status_code == 200

    live_session.rollback()
    sup = Supervisor(settings, get_live_sessionmaker(), spawn=lambda cmd, d: FakeProcess(), clock=lambda: later)
    assert sup.step() == {deployment_id: "started"}  # resumed, then its crew was started
    live_session.rollback()
    restarted = store.deployment_events(deployment_id)[-1]
    assert (restarted.event, restarted.actor) == ("started", money.RESUME_ACTOR)
    assert store.deployment_status(deployment_id) == "started"
    assert any(subject.startswith("[genesis] resumed") for subject, _ in sent)
    live_session.rollback()
    assert client.get(f"/api/live/deployments/{deployment_id}/money").json()["floor"]["on_break"] is False


def test_a_person_restarting_ends_the_break(client, live_session, make_ctx, approvable, paper_account):
    deployment_id = approve_and_start(client, approvable, paper_account, risk_limits={"drawdown": {"floor": FLOOR}})
    clock = Clock()
    ctx = make_ctx(deployment_id, [ENTRY, SLIDE], clock=clock)
    run(ctx, clock, 1)
    client.post(f"/api/live/deployments/{deployment_id}/capital-events", json={"kind": "withdrawal", "amount_inr": 199_000},
                headers=ACTOR)  # fmt: skip
    assert run(ctx, clock, 1)[0]["outcome"] == "paused"
    base = f"/api/live/deployments/{deployment_id}"
    assert client.post(f"{base}/start", json={"reason": "reviewed"}, headers=ACTOR).status_code == 200
    assert client.post(f"{base}/pause", json={"reason": "manual"}, headers=ACTOR).status_code == 200
    store = LiveStore(live_session)
    later = datetime.now(UTC) + timedelta(days=14)
    assert money.resume_if_due(store, get_settings(), store.deployment(deployment_id), later) is None


def test_floor_is_raised_once(client, live_session, make_ctx, approvable, paper_account, sent):
    floor = {"floor_inr": 300_000, "raise_once": {"at_equity_inr": 500_001, "to_inr": 400_000}}
    deployment_id = approve_and_start(client, approvable, paper_account, risk_limits={"drawdown": {"floor": floor}})
    clock = Clock()
    results = run(make_ctx(deployment_id, [ENTRY, CALM, ENTRY], clock=clock), clock, 3, hours=1)
    assert [r["outcome"] for r in results] == ["entered", "exited", "entered"]
    assert events(live_session, deployment_id).count("floor_raised") == 1
    assert any("floor_raised" in subject for subject, _ in sent)
    view = client.get(f"/api/live/deployments/{deployment_id}/money").json()["floor"]
    assert view["raised"] is True and view["floor_in_force_inr"] == 400_000
    assert view["buffer_inr"] == pytest.approx(view["equity_inr"] - 400_000)


# The withdrawal rule


def test_withdrawal_due_first_amount_then_a_share_of_each_rise():
    rule = Withdrawal.model_validate({"first_target_inr": 60_000, "first_amount_inr": 5_000, "then_share_pct": 70})
    assert withdrawal_due(rule, 59_999, 0, None) == 0
    assert withdrawal_due(rule, 60_000, 0, None) == 5_000
    assert withdrawal_due(rule, 61_000, 2_000, 58_000) == 3_000  # the rest of the first amount
    assert withdrawal_due(rule, 55_000, 5_000, 55_000) == 0
    assert withdrawal_due(rule, 57_000, 5_000, 55_000) == 1_400  # 70% of the rise above what was left
    assert withdrawal_due(rule, 60_000, 0, None, keep_above=56_000) == 4_000  # never below the floor
    assert withdrawal_due(rule, 55_000.5, 5_000, 55_000) == 0  # under a rupee
    once = Withdrawal.model_validate({"first_target_inr": 60_000, "first_amount_inr": 5_000})
    assert withdrawal_due(once, 90_000, 5_000, 55_000) == 0
    with pytest.raises(ValueError, match="below first_target_inr"):
        Withdrawal.model_validate({"first_target_inr": 5_000, "first_amount_inr": 5_000})


def test_crew_alerts_once_when_a_withdrawal_is_due(client, live_session, make_ctx, approvable, paper_account, sent):
    rule = {"first_target_inr": 500_001, "first_amount_inr": 1_000, "then_share_pct": 50}
    deployment_id = approve_and_start(client, approvable, paper_account, risk_limits={"policy": {"withdrawal": rule}})
    base = f"/api/live/deployments/{deployment_id}"
    assert client.get(f"{base}/money").json()["withdrawal"]["due_inr"] == 0
    clock = Clock()
    ctx = make_ctx(deployment_id, [ENTRY, CALM, CALM, CALM], clock=clock)
    run(ctx, clock, 3, hours=1)
    assert events(live_session, deployment_id).count("withdrawal_due") == 1
    assert [s for s, _ in sent if "withdrawal_due" in s] == [f"[genesis] withdrawal_due: deployment {deployment_id}"]
    status = client.get(f"{base}/money").json()["withdrawal"]
    assert status["due_inr"] == 1_000 and status["withdrawn_inr"] == 0

    assert client.post(f"{base}/capital-events", json={"kind": "withdrawal", "amount_inr": 1_000}, headers=ACTOR).status_code == 200
    after = client.get(f"{base}/money").json()["withdrawal"]
    assert after["withdrawn_inr"] == 1_000 and after["due_inr"] == 0
    assert after["base_inr"] == pytest.approx(status["realized_equity_inr"] - 1_000)


# Backtests


def test_sim_floor_pause_break_and_restart(store):  # noqa: F811
    seed(
        store,
        [
            ("2026-01-05", 22000, 60, ENTRY_CHAIN),
            ("2026-01-06", 22000, 10, chain(50, 30, 50, 30)),  # up 3,000: equity reaches the raise level
            ("2026-01-07", 22000, 10, chain(110, 30, 60, 30)),  # down 7,500, short of the stop: below the raised floor
            ("2026-01-08", 22000, 60, ENTRY_CHAIN),  # on the break
            ("2026-01-09", 22000, 60, ENTRY_CHAIN),  # break over: topped up and trading again
        ],
    )
    floor = {"floor_inr": 25_000, "raise_once": {"at_equity_inr": 52_000, "to_inr": 45_000}, "resume_after_days": 1}
    managed = Management(drawdown=DrawdownPolicy.model_validate({"floor": floor}), capital_inr=50_000, max_lots=2)
    result = simulate_md(store, APPROVABLE_MARKDOWN, managed)
    assert [(t.date, t.exit_reason) for t in result.trades][:1] == [("2026-01-05", "drawdown.floor")]
    assert [t.date for t in result.trades] == ["2026-01-05", "2026-01-09"]
    m = result.management
    assert [e["event"] for e in m["floor_events"]] == ["floor_raised", "floor_paused", "resumed"]
    assert m["floor_pauses"] == 1 and m["restarts"] == 1 and m["floor_paused_on"] is None
    assert m["topped_up_inr"] == pytest.approx(-result.trades[0].net, abs=1)
    assert result.skipped["drawdown.floor_paused"] == 1

    final = Management(drawdown=DrawdownPolicy.model_validate({"floor": floor | {"resume_after_days": None}}),
                       capital_inr=50_000, max_lots=2)  # fmt: skip
    once = simulate_md(store, APPROVABLE_MARKDOWN, final)
    assert [t.date for t in once.trades] == ["2026-01-05"] and once.management["floor_paused_on"] == "2026-01-07"


def test_sim_floor_buffer_blocks_entries(store):  # noqa: F811
    seed(store, [("2026-01-05", 22000, 60, ENTRY_CHAIN)])
    managed = Management(drawdown=DrawdownPolicy.model_validate({"floor": {"floor_inr": 45_000}}), capital_inr=50_000)
    result = simulate_md(store, APPROVABLE_MARKDOWN, managed)
    assert result.trades == [] and result.skipped["drawdown.floor_buffer"] == 1  # max loss 9,000 > buffer 5,000


def test_sim_applies_the_withdrawal_rule_when_flat(store):  # noqa: F811
    seed(
        store,
        [
            ("2026-01-05", 22000, 60, ENTRY_CHAIN),
            ("2026-01-06", 22000, 10, chain(30, 30, 30, 30)),  # profit target
            ("2026-01-07", 22000, 60, ENTRY_CHAIN),
            ("2026-01-08", 22000, 10, chain(30, 30, 30, 30)),  # profit target again
        ],
    )
    rule = {"first_target_inr": 52_000, "first_amount_inr": 1_000, "then_share_pct": 50}
    managed = Management(policy=TradePolicy.model_validate({"withdrawal": rule}), capital_inr=50_000, max_lots=2)
    result = simulate_md(store, APPROVABLE_MARKDOWN, managed)
    first, second = result.trades
    m = result.management
    assert [w["date"] for w in m["withdrawals"]] == ["2026-01-06", "2026-01-08"]
    assert m["withdrawals"][0]["amount_inr"] == 1_000
    assert m["withdrawals"][1]["amount_inr"] == pytest.approx(int(second.net * 0.5), abs=1)
    assert m["withdrawn_inr"] == pytest.approx(1_000 + m["withdrawals"][1]["amount_inr"])
    assert m["withdrawn_less_topped_up_inr"] == m["withdrawn_inr"]
    assert result.net_pnl == pytest.approx(first.net + second.net)  # withdrawals are not P&L
