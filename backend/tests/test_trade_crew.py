from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.agents.trade_crew import graph
from app.agents.trade_crew.graph import run_tick
from app.agents.trade_crew.runner import build_context, run
from app.brokers.base import OrderAck, OrderRequest
from app.brokers.paper import PaperBroker
from app.config import get_settings
from app.db.models import DeployedStrategy, TradeIntent
from app.engine.costs import resolve_cost_profile
from app.engine.market import Bar
from app.live.store import LiveStore
from app.market.feed import ReplayFeed
from app.strategies.schema import parse_strategy_markdown
from tests.conftest import ACTOR, APPROVABLE_MARKDOWN, approve_and_start, create_account

ENTRY = Bar("2026-01-05", 22000, 80, 14)
NO_SIGNAL = Bar("2026-01-05", 22000, 20, 14)
CALM = Bar("2026-01-16", 22000, 20, 3)  # time decay and falling IV: profit target
RALLY = Bar("2026-01-12", 23500, 50, 7)  # spot through the short call: stop loss


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 5, 4, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture
def deployment_id(client, approvable, paper_account):
    return approve_and_start(client, approvable, paper_account)


@pytest.fixture
def make_ctx(live_session):
    def make(deployment_id, bars, *, settings=None, adapter=None, explain=False, clock=None, feed_clock=None):
        clock = clock or Clock()
        settings = settings or get_settings()
        feed = ReplayFeed("NIFTY", bars=bars, strike_step=50, clock=feed_clock or clock)
        return build_context(
            live_session, deployment_id, settings, feed=feed, adapter=adapter, clock=clock, explain=explain
        )

    return make


def intents(live_session, deployment_id) -> list[TradeIntent]:
    rows = LiveStore(live_session).intents(deployment_id)
    live_session.rollback()
    return rows


def orders(live_session, intent_id):
    store = LiveStore(live_session)
    rows = [(o, store.order_status(o.id), store.fills_for_order(o.id)) for o in store.orders_for_intent(intent_id)]
    live_session.rollback()
    return rows


def test_entry_fill_then_profit_target_exit(client, live_session, make_ctx, deployment_id):
    clock = Clock()
    ctx = make_ctx(deployment_id, [ENTRY, CALM], clock=clock)

    entered = run_tick(ctx)
    assert entered["outcome"] == "entered", entered
    [intent] = intents(live_session, deployment_id)
    assert intent.action == "open" and intent.approval == "auto"
    assert intent.decided_by == f"trade_crew/{deployment_id}"
    assert [c["passed"] for c in intent.entry_conditions] == [True, True, True]
    assert intent.risk_checks["failed"] == []
    assert {c["rule"] for c in intent.risk_checks["checks"]} >= {"risk.min_risk_reward", "portfolio.max_open_positions"}
    assert intent.risk_reward == pytest.approx(0.5, abs=0.01)
    assert [(leg["side"], leg["option_type"], leg["strike"], leg["quantity"]) for leg in intent.legs] == [
        ("SELL", "CE", 23000, 150), ("SELL", "PE", 21000, 150), ("BUY", "CE", 23200, 150), ("BUY", "PE", 20800, 150),
    ]  # fmt: skip
    placed = orders(live_session, intent.id)
    assert [(o.order_type, status, len(fills)) for o, status, fills in placed] == [("LIMIT", "COMPLETE", 1)] * 4
    assert all(o.sebi_algo_id is None and o.broker_order_id.startswith("paper-") for o, _, _ in placed)

    clock.advance(days=1)
    exited = run_tick(ctx)
    assert exited["outcome"] == "exited", exited
    [trade] = LiveStore(live_session).live_trades(deployment_id)
    assert trade.exit_reason == "profit_target"
    assert trade.open_intent_id == intent.id
    assert trade.outcome_label == "win"
    assert trade.net == trade.gross - trade.actual_costs
    assert trade.actual_costs > 0 and trade.slippage_vs_expected < 0
    assert trade.holding_minutes == 24 * 60
    assert [(leg["strike"], leg["option_type"]) for leg in trade.legs] == [
        (23000, "CE"), (21000, "PE"), (23200, "CE"), (20800, "PE"),
    ]  # fmt: skip
    assert all(leg["exit_price"] < leg["entry_price"] for leg in trade.legs if leg["side"] == "SELL")
    close = LiveStore(live_session).session.get(TradeIntent, trade.close_intent_id)
    assert close.action == "close" and close.risk_checks["triggered"] == "exit.profit_target"
    trade_id = trade.id
    live_session.rollback()

    assert run_tick(ctx)["outcome"] == "no_market"
    journal = client.get("/api/journal", params={"kind": "live"}).json()
    assert [row["trade_id"] for row in journal["trades"]] == [trade_id]
    assert client.get("/api/live/audit/verify").json()["ok"]


def test_stop_loss_exit(live_session, make_ctx, deployment_id):
    clock = Clock()
    ctx = make_ctx(deployment_id, [ENTRY, RALLY], clock=clock)
    assert run_tick(ctx)["outcome"] == "entered"
    clock.advance(days=1)
    assert run_tick(ctx)["outcome"] == "exited"
    [trade] = LiveStore(live_session).live_trades(deployment_id)
    assert trade.exit_reason == "stop_loss"
    assert trade.outcome_label == "loss"
    assert trade.net < 0


def test_holding_position_does_not_reenter(live_session, make_ctx, deployment_id):
    ctx = make_ctx(deployment_id, [ENTRY, ENTRY])
    assert run_tick(ctx)["outcome"] == "entered"
    assert run_tick(ctx)["outcome"] == "holding"
    assert len(intents(live_session, deployment_id)) == 1


def test_no_entry_signal_writes_nothing(live_session, make_ctx, deployment_id):
    result = run_tick(make_ctx(deployment_id, [NO_SIGNAL]))
    assert result["outcome"] == "no_signal"
    assert intents(live_session, deployment_id) == []


def test_failed_risk_rule_blocks_without_orders(live_session, make_ctx, deployment_id):
    settings = get_settings().model_copy(update={"portfolio_max_open_positions": 0})
    result = run_tick(make_ctx(deployment_id, [ENTRY], settings=settings))
    assert result["outcome"] == "blocked"
    assert result["detail"] == "portfolio.max_open_positions"
    [intent] = intents(live_session, deployment_id)
    assert intent.approval == "blocked"
    assert intent.risk_checks["failed"] == ["portfolio.max_open_positions"]
    assert "blocked by portfolio.max_open_positions" in intent.reason_text
    assert orders(live_session, intent.id) == []


def test_tampered_snapshot_halts_without_orders(live_session, live_engine, make_ctx, deployment_id):
    snapshot_id = LiveStore(live_session).deployment(deployment_id).deployed_strategy_id
    live_session.rollback()
    with live_engine.begin() as conn:
        conn.exec_driver_sql("DROP TRIGGER deployed_strategies_no_update")
        conn.execute(
            text("UPDATE deployed_strategies "
                 "SET markdown = replace(markdown, 'Sell an iron condor', 'Sell a strangle') WHERE id = :id"),
                {"id": snapshot_id},
            )  # fmt: skip
    result = run_tick(make_ctx(deployment_id, [ENTRY]))
    assert result["outcome"] == "halted"
    assert "content_sha256" in result["detail"]
    store = LiveStore(live_session)
    assert store.deployment_status(deployment_id) == "halted"
    assert "snapshot verification failed" in store.deployment_events(deployment_id)[-1].reason
    assert "Sell a strangle" in live_session.get(DeployedStrategy, snapshot_id).markdown
    assert store.intents(deployment_id) == []


def test_stopped_deployment_places_no_orders(client, live_session, make_ctx, deployment_id):
    client.post(f"/api/live/deployments/{deployment_id}/stop", json={"reason": "kill switch"}, headers=ACTOR)
    result = run_tick(make_ctx(deployment_id, [ENTRY]))
    assert result == {"outcome": "idle", "detail": "deployment is stopped", "exits": [], "phantoms": [],
                      "intent_id": None, "approval": None, "opinion": None, "drawdown": None}  # fmt: skip
    assert intents(live_session, deployment_id) == []


def test_paused_deployment_waits(client, live_session, make_ctx, deployment_id):
    client.post(f"/api/live/deployments/{deployment_id}/pause", headers=ACTOR)
    assert run_tick(make_ctx(deployment_id, [ENTRY]))["outcome"] == "idle"
    assert intents(live_session, deployment_id) == []


def test_stale_snapshot_is_a_failed_check(live_session, make_ctx, deployment_id):
    clock = Clock()
    stale = lambda: clock() - timedelta(minutes=10)  # noqa: E731
    result = run_tick(make_ctx(deployment_id, [ENTRY], clock=clock, feed_clock=stale))
    assert result["outcome"] == "stale_market"
    assert intents(live_session, deployment_id) == []


class RejectThirdOrder(PaperBroker):
    def place_order(self, order):
        if self._seq == 2:
            self._seq += 1
            return OrderAck(order.client_order_id, "", False, "margin shortfall")
        return super().place_order(order)


def test_rejected_leg_halts_the_deployment(live_session, make_ctx, deployment_id):
    profile = resolve_cost_profile(get_settings(), parse_strategy_markdown(APPROVABLE_MARKDOWN))
    adapter = RejectThirdOrder(profile, clock=Clock())
    result = run_tick(make_ctx(deployment_id, [ENTRY], adapter=adapter))
    assert result["outcome"] == "halted"
    assert "margin shortfall" in result["detail"]
    [intent] = intents(live_session, deployment_id)
    assert [status for _, status, _ in orders(live_session, intent.id)] == ["COMPLETE", "COMPLETE", "REJECTED"]
    assert LiveStore(live_session).deployment_status(deployment_id) == "halted"


def decisions(live_session, deployment_id):
    out = []
    for i in intents(live_session, deployment_id):
        placed = orders(live_session, i.id)
        out.append(
            (
                i.action, i.approval, i.legs, i.entry_conditions, i.risk_checks, i.target_pnl, i.risk_reward,
                [(o.side, o.tradingsymbol, o.quantity, o.limit_price, status) for o, status, _ in placed],
            )
        )  # fmt: skip
    return out


def test_llm_explanation_never_changes_the_decision(client, approvable, paper_account, live_session, make_ctx,
                                                    monkeypatch):  # fmt: skip
    calls = []
    monkeypatch.setattr(graph, "_llm_explain", lambda settings, facts: calls.append(facts) or "Rich IV, so we sold.")

    plain = approve_and_start(client, approvable, paper_account)
    run_tick(make_ctx(plain, [ENTRY]))
    client.post(f"/api/live/deployments/{plain}/stop", headers=ACTOR)

    explained = approve_and_start(client, approvable, paper_account)
    with_llm = get_settings().model_copy(update={"openai_api_key": "test-key"})
    run_tick(make_ctx(explained, [ENTRY], settings=with_llm, explain=True))

    assert len(calls) == 1
    assert decisions(live_session, plain) == decisions(live_session, explained)
    [a], [b] = intents(live_session, plain), intents(live_session, explained)
    assert a.model is None and b.model == with_llm.openai_model
    assert b.reason_text == f"{a.reason_text}\n\nRich IV, so we sold."


def test_paper_broker_fills_limit_orders_with_slippage():
    profile = resolve_cost_profile(get_settings(), parse_strategy_markdown(APPROVABLE_MARKDOWN))
    broker = PaperBroker(profile, clock=Clock())
    request = OrderRequest("c-1", "NFO", "NIFTY26JAN1923000CE", "SELL", 150, "LIMIT", "NRML", Decimal("100"))
    ack = broker.place_order(request)
    assert ack.accepted
    [fill] = broker.fills()
    assert fill.price == Decimal("100") * (1 - Decimal(str(profile.slippage_bps)) / 10_000)
    assert broker.positions() == [{"tradingsymbol": "NIFTY26JAN1923000CE", "quantity": -150}]
    [charge] = broker.charges(Clock()().date())
    assert "slippage" not in charge.breakdown and charge.total > 0
    assert broker.order_updates()[0].status == "COMPLETE"

    market = broker.place_order(OrderRequest("c-2", "NFO", "X", "BUY", 75, "MARKET", "NRML"))
    assert not market.accepted
    assert len(broker.fills()) == 1


def test_replay_feed_advances_one_bar_and_quotes_requested_contracts():
    feed = ReplayFeed("NIFTY", bars=[ENTRY, RALLY], strike_step=50, strikes_each_side=2, clock=Clock())
    assert feed.snapshot("BANKNIFTY") is None
    first = feed.snapshot("NIFTY")
    assert (first.spot, first.dte, first.expiry) == (22000, 14, "2026-01-19")
    assert first.quote(22000, "CE") == first.quote(22000, "PE") > 0
    assert first.quote(21000, "PE") is None
    second = feed.snapshot("NIFTY", [(21000.0, "PE", "2026-01-19"), (23000.0, "CE", "2026-01-12")])
    assert second.spot == 23500 and second.quote(21000, "PE", "2026-01-19") > 0
    assert second.days_to("2026-01-19") == 7
    assert second.quote(23000, "CE", "2026-01-12") == 500  # expires today: intrinsic value
    assert feed.exhausted and feed.snapshot("NIFTY") is None


def test_runner_refuses_live_deployments(client, approvable, live_session):
    live_account = create_account(client, mode="live", broker="zerodha")
    deployment_id = approve_and_start(client, approvable, live_account)
    with pytest.raises(ValueError, match="live broker adapter"):
        build_context(live_session, deployment_id, get_settings())


def test_runner_refuses_non_paper_broker(client, approvable, live_session):
    account = create_account(client, mode="paper", broker="zerodha")
    deployment_id = approve_and_start(client, approvable, account)
    with pytest.raises(ValueError, match="only paper accounts"):
        build_context(live_session, deployment_id, get_settings())


def test_runner_replays_sample_data(deployment_id):
    sleeps = []
    results = run(deployment_id, interval=5, max_ticks=3, sleep=sleeps.append, emit=lambda _: None)
    assert len(results) == 3
    assert sleeps == [5, 5]
    assert all(r["outcome"] for r in results)
