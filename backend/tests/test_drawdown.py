from __future__ import annotations

import pytest

from app.agents.trade_crew.graph import run_tick
from app.config import get_settings
from app.engine.market import Bar
from app.live.drawdown import DrawdownPolicy, decide, effective_ladder, parse_policy, scaled_lots
from app.live.store import LiveStore
from tests.conftest import ACTOR, approval_body, approve_and_start
from tests.test_trade_crew import ENTRY, RALLY, Clock, intents, make_ctx, orders  # noqa: F401

SLIDE = Bar("2026-01-08", 22900, 80, 11)  # open condor down about Rs 3.7k, short of its stop loss
LADDER = DrawdownPolicy.model_validate(
    {
        "ladder": [
            {"at_pct": 1, "action": "warn"},
            {"at_pct": 2, "action": "reduce_size", "size_factor": 0.5},
            {"at_pct": 3, "action": "stop_new_entries"},
            {"at_pct": 4, "action": "pause"},
        ],
        "restore_below_pct": 0.5,
    }
).ladder


def run(ctx, clock, ticks: int) -> list[dict]:
    out = []
    for _ in range(ticks):
        out.append(run_tick(ctx))
        clock.advance(days=1)
    return out


def start(client, approvable, account, policy: dict | None = None, **limits) -> str:
    risk_limits = limits | ({"drawdown": policy} if policy else {})
    return approve_and_start(client, approvable, account, risk_limits=risk_limits)


# Policy


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"ladder": [{"at_pct": 3, "action": "warn"}, {"at_pct": 2, "action": "pause"}]}, "must increase"),
        ({"ladder": [{"at_pct": 2, "action": "pause"}, {"at_pct": 3, "action": "warn"}]}, "weaker than pause"),
        ({"ladder": [{"at_pct": 2, "action": "reduce_size"}]}, "size_factor is required"),
        ({"ladder": [{"at_pct": 2, "action": "warn", "size_factor": 0.5}]}, "size_factor is required"),
        ({"ladder": [{"at_pct": 2, "action": "warn", "close_open_positions": True}]}, "only allowed on a pause"),
        ({"ladder": [{"at_pct": 2, "action": "warn"}], "restore_below_pct": 2}, "below the first level"),
        ({"ladder": [{"at_pct": 25, "action": "pause"}]}, "past the MAX_DRAWDOWN_PCT pause"),
        ({"ladder": [{"at_pct": 20, "action": "warn"}]}, "past the MAX_DRAWDOWN_PCT pause"),
        ({"ladder": []}, "at least 1 item"),
        ({"ladder": [{"at_pct": 2, "action": "warn"}], "scope": "portfolio"}, "Extra inputs"),
    ],
)
def test_invalid_policies_are_rejected(raw, message):
    with pytest.raises(ValueError, match=message):
        parse_policy(raw, max_drawdown_pct=20)


def test_effective_ladder_always_ends_with_the_env_pause():
    policy = DrawdownPolicy.model_validate({"ladder": [{"at_pct": 5, "action": "warn"}]})
    assert [(lvl.at_pct, lvl.action) for lvl in effective_ladder(policy, 20)] == [(5, "warn"), (20, "pause")]
    assert [(lvl.at_pct, lvl.action) for lvl in effective_ladder(None, 20)] == [(20, "pause")]
    capped = DrawdownPolicy.model_validate(
        {"ladder": [{"at_pct": 20, "action": "pause", "close_open_positions": True}]}
    )
    assert effective_ladder(capped, 20) == capped.ladder
    assert effective_ladder(None, 0) == []


def test_levels_escalate_at_once_and_step_down_below_restore():
    normal = decide(LADDER, 0.5, 0.2, 0)
    assert (normal.level, normal.action, normal.size_factor, normal.block_entries) == (0, "normal", 1.0, False)
    assert (normal.check.threshold, normal.check.passed) == (3, True)

    reduced = decide(LADDER, 0.5, 2.5, 0)
    assert (reduced.level, reduced.action, reduced.size_factor, reduced.block_entries) == (2, "reduce_size", 0.5, False)
    assert reduced.pause is None

    paused = decide(LADDER, 0.5, 4.1, 2)
    assert (paused.level, paused.block_entries, paused.pause.at_pct) == (4, True, 4)
    assert decide(LADDER, 0.5, 4.5, 4).pause is None  # already at the pause level: no second pause

    held = decide(LADDER, 0.5, 1.0, 4)
    assert (held.level, held.block_entries) == (4, True)
    assert (held.check.threshold, held.check.actual, held.check.passed) == (0.5, 1.0, False)
    assert decide(LADDER, 0.5, 0.4, 4).level == 0
    assert decide(LADDER, None, 1.0, 4).level == 1  # no restore_below_pct: steps straight down


def test_scaled_lots_never_go_below_one():
    assert [scaled_lots(lots, f) for lots, f in ((2, 0.5), (1, 0.5), (4, 0.6), (3, 1.0))] == [1, 1, 2, 3]


# Snapshots, positions and equity


def test_open_positions_are_snapshotted_and_marked(client, live_session, make_ctx, approvable, paper_account):
    deployment_id = start(client, approvable, paper_account)
    clock = Clock()
    ctx = make_ctx(deployment_id, [ENTRY, SLIDE], clock=clock)

    assert run(ctx, clock, 1)[0]["outcome"] == "entered"
    [unmarked] = client.get("/api/live/positions", params={"deployment_id": deployment_id}).json()["positions"]
    assert unmarked["pnl"] is None and unmarked["marked_at"] is None

    assert run(ctx, clock, 1)[0]["outcome"] == "holding"
    snapshot = LiveStore(live_session).latest_position_snapshot(deployment_id)
    assert snapshot.deployment_id == deployment_id and len(snapshot.positions) == 1
    live_session.rollback()
    [position] = client.get("/api/live/positions", params={"deployment_id": deployment_id}).json()["positions"]
    assert position["pnl"] == pytest.approx(-3726.81, abs=0.01)
    assert position["target_pnl"] > 0 > position["stop_loss_pnl"]
    assert len(position["legs"]) == 4 and all(leg["quote"] > 0 for leg in position["legs"])

    mtm = client.get("/api/live/equity", params={"deployment_id": deployment_id}).json()
    assert mtm["unrealized_inr"] == pytest.approx(position["pnl"])
    assert mtm["drawdown_inr"] == pytest.approx(-position["pnl"])
    assert mtm["drawdown_pct"] == pytest.approx(-position["pnl"] / 500_000 * 100, abs=1e-4)
    realized = client.get("/api/live/equity", params={"deployment_id": deployment_id, "basis": "realized"}).json()
    assert realized["drawdown_inr"] == 0 and realized["equity_inr"] == 500_000
    assert client.get("/api/live/positions", params={"mode": "paper"}).json()["positions"][0]["pnl"] == position["pnl"]


def test_capital_events_do_not_move_drawdown(client, live_session, make_ctx, approvable, paper_account):
    deployment_id = start(client, approvable, paper_account)
    clock = Clock()
    assert [r["outcome"] for r in run(make_ctx(deployment_id, [ENTRY, RALLY], clock=clock), clock, 2)] == [
        "entered", "exited",
    ]  # fmt: skip
    path = f"/api/live/deployments/{deployment_id}/capital-events"
    before = client.get("/api/live/equity", params={"deployment_id": deployment_id}).json()
    assert before["drawdown_inr"] > 0

    assert "named actor" in client.post(path, json={"kind": "deposit", "amount_inr": 1}).json()["detail"]
    assert client.post(path, json={"kind": "gift", "amount_inr": 1}, headers=ACTOR).status_code == 400
    assert client.post(path, json={"kind": "deposit", "amount_inr": 0}, headers=ACTOR).status_code == 400
    too_much = client.post(path, json={"kind": "withdrawal", "amount_inr": 500_000}, headers=ACTOR)
    assert too_much.status_code == 400 and "zero or below" in too_much.json()["detail"]

    deposit = client.post(path, json={"kind": "deposit", "amount_inr": 100_000, "note": "top up"}, headers=ACTOR)
    assert deposit.status_code == 200, deposit.text
    assert deposit.json()["actor"] == "alice"
    listed = client.get(path).json()
    assert listed["capital_in_force_inr"] == 600_000 and len(listed["capital_events"]) == 1

    after = client.get("/api/live/equity", params={"deployment_id": deployment_id}).json()
    assert after["drawdown_inr"] == pytest.approx(before["drawdown_inr"])
    assert after["peak_inr"] == pytest.approx(before["peak_inr"] + 100_000)
    assert after["capital_inr"] == 600_000
    assert after["drawdown_pct"] < before["drawdown_pct"]
    assert client.get("/api/live/audit/verify").json()["ok"]


# The ladder in the Trade Crew


def test_reduce_size_level_trades_fewer_lots(client, live_session, make_ctx, approvable, paper_account):
    policy = {"ladder": [{"at_pct": 1, "action": "reduce_size", "size_factor": 0.5}]}
    deployment_id = start(client, approvable, paper_account, policy)
    clock = Clock()
    results = run(make_ctx(deployment_id, [ENTRY, RALLY, ENTRY], clock=clock), clock, 3)
    assert [r["outcome"] for r in results] == ["entered", "exited", "entered"]
    assert results[2]["drawdown"]["level"] == 1 and results[2]["drawdown"]["size_factor"] == 0.5

    first, _, reentry = intents(live_session, deployment_id)
    assert {leg["quantity"] for leg in first.legs} == {150}
    assert {leg["quantity"] for leg in reentry.legs} == {75}
    assert reentry.risk_checks["drawdown"]["action"] == "reduce_size"
    assert "cut 2 to 1 lots" in reentry.reason_text
    store = LiveStore(live_session)
    [event] = store.drawdown_events(deployment_id)
    [trade] = store.live_trades(deployment_id)
    assert (event.level, event.action, event.at_pct) == (1, "reduce_size", 1)
    assert event.drawdown_pct == pytest.approx(float(-trade.net) / 500_000 * 100, abs=1e-4)
    assert event.equity == 500_000 + trade.net and event.peak == 500_000


def test_stop_new_entries_blocks_but_records_the_signal(client, live_session, make_ctx, approvable, paper_account):
    deployment_id = start(client, approvable, paper_account, {"ladder": [{"at_pct": 1, "action": "stop_new_entries"}]})
    clock = Clock()
    results = run(make_ctx(deployment_id, [ENTRY, RALLY, ENTRY], clock=clock), clock, 3)
    assert [r["outcome"] for r in results] == ["entered", "exited", "blocked"]
    assert results[2]["detail"] == "drawdown.entries_allowed"
    blocked = intents(live_session, deployment_id)[-1]
    assert blocked.approval == "blocked" and blocked.risk_checks["failed"] == ["drawdown.entries_allowed"]
    assert orders(live_session, blocked.id) == []


def test_pause_level_pauses_once_and_keeps_entries_blocked(client, live_session, make_ctx, approvable, paper_account):
    deployment_id = start(client, approvable, paper_account, {"ladder": [{"at_pct": 2, "action": "pause"}]})
    clock = Clock()
    ctx = make_ctx(deployment_id, [ENTRY, RALLY, ENTRY], clock=clock)
    entered, paused = run(ctx, clock, 2)
    assert entered["outcome"] == "entered"
    assert paused["outcome"] == "paused" and "2% pause level" in paused["detail"]
    assert [e["exit_reason"] for e in paused["exits"]] == ["stop_loss"]  # the exit still ran first
    store = LiveStore(live_session)
    assert store.deployment_status(deployment_id) == "paused"
    assert [(e.level, e.action) for e in store.drawdown_events(deployment_id)] == [(1, "pause")]
    live_session.rollback()

    restarted = client.post(f"/api/live/deployments/{deployment_id}/start", json={"reason": "reviewed"}, headers=ACTOR)
    assert restarted.status_code == 200
    [after_restart] = run(ctx, clock, 1)
    assert after_restart["outcome"] == "blocked" and after_restart["detail"] == "drawdown.entries_allowed"
    assert LiveStore(live_session).deployment_status(deployment_id) == "started"


def test_pause_can_close_open_positions(client, live_session, make_ctx, approvable, paper_account):
    policy = {"ladder": [{"at_pct": 0.5, "action": "pause", "close_open_positions": True}]}
    deployment_id = start(client, approvable, paper_account, policy)
    clock = Clock()
    entered, paused = run(make_ctx(deployment_id, [ENTRY, SLIDE], clock=clock), clock, 2)
    assert entered["outcome"] == "entered" and paused["outcome"] == "paused"
    store = LiveStore(live_session)
    [trade] = store.live_trades(deployment_id)
    assert trade.exit_reason == "drawdown.pause" and trade.net < 0
    assert store.open_intents(deployment_id) == []
    assert store.deployment_status(deployment_id) == "paused"


def test_realized_basis_ignores_open_losses(client, live_session, make_ctx, approvable, paper_account):
    policy = {"basis": "realized", "ladder": [{"at_pct": 0.5, "action": "pause"}]}
    deployment_id = start(client, approvable, paper_account, policy)
    clock = Clock()
    results = run(make_ctx(deployment_id, [ENTRY, SLIDE], clock=clock), clock, 2)
    assert [r["outcome"] for r in results] == ["entered", "holding"]
    assert LiveStore(live_session).drawdown_events(deployment_id) == []


def test_env_cap_pauses_deployments_without_a_policy(client, live_session, make_ctx, approvable, paper_account):
    capped = get_settings().model_copy(update={"max_drawdown_pct": 2})
    deployment_id = start(client, approvable, paper_account)
    clock = Clock()
    results = run(make_ctx(deployment_id, [ENTRY, RALLY], clock=clock, settings=capped), clock, 2)
    assert [r["outcome"] for r in results] == ["entered", "paused"]
    client.post(f"/api/live/deployments/{deployment_id}/stop", headers=ACTOR)

    disabled = get_settings().model_copy(update={"max_drawdown_pct": 0})
    other = start(client, approvable, paper_account)
    clock = Clock()
    results = run(make_ctx(other, [ENTRY, RALLY], clock=clock, settings=disabled), clock, 2)
    assert [r["outcome"] for r in results] == ["entered", "exited"]
    assert results[1]["drawdown"] is None


# Approval and API


def test_approval_validates_and_freezes_the_policy(client, approvable, paper_account):
    bad = {"ladder": [{"at_pct": 3, "action": "pause"}, {"at_pct": 4, "action": "warn"}]}
    refused = client.post(
        "/api/live/approvals",
        json=approval_body(approvable, paper_account, risk_limits={"drawdown": bad}),
        headers=ACTOR,
    )
    assert refused.status_code == 400 and "weaker than pause" in refused.json()["detail"]

    policy = {"ladder": [{"at_pct": 3, "action": "reduce_size", "size_factor": 0.5}], "restore_below_pct": 1}
    deployment_id = start(client, approvable, paper_account, policy)
    deployment = client.get(f"/api/live/deployments/{deployment_id}").json()
    assert deployment["risk_limits"]["drawdown"] == {
        "basis": "mtm",
        "ladder": [{"at_pct": 3.0, "action": "reduce_size", "size_factor": 0.5, "close_open_positions": False}],
        "restore_below_pct": 1.0,
    }
    view = client.get(f"/api/live/deployments/{deployment_id}/drawdown").json()
    assert [(lvl["at_pct"], lvl["action"]) for lvl in view["ladder"]] == [(3, "reduce_size"), (20, "pause")]
    assert view["level"] == 0 and view["events"] == []
    assert client.get("/api/live/deployments/nope/drawdown").status_code == 404
