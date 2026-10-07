"""Live trading plumbing, exercised against the SandboxBroker: order lifecycle, unwinds, protective
stops, reconciliation, market hours, kill switch, live gates, broker registry and credentials, API
auth, the supervisor and alerts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app import alerts
from app.agents.trade_crew import graph
from app.agents.trade_crew.graph import run_tick
from app.agents.trade_crew.runner import build_context
from app.audit import audit_context
from app.auth import hash_token
from app.auth.__main__ import main as auth_cli
from app.brokers import credentials, registry
from app.brokers.base import OrderRequest
from app.brokers.registry import BrokerSpec, CredentialField
from app.brokers.sandbox import SandboxBroker, SandboxMarket
from app.config import get_settings
from app.db.models import LiveAuditEvent
from app.db.session import get_live_sessionmaker
from app.engine.costs import resolve_cost_profile
from app.live import kill_switch
from app.live.store import LiveStore
from app.live.supervisor import Supervisor
from app.market.feed import BrokerFeed, quote_price
from app.market.hours import market_status
from app.strategies.schema import parse_strategy_markdown
from app.trading.orders import OrderManager, RateLimiter, hedge_first, split_quantity
from tests.conftest import ACTOR, APPROVABLE_MARKDOWN, TOKEN, approve_and_start, create_account

MONDAY_0930_IST = datetime(2026, 1, 5, 4, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime = MONDAY_0930_IST) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture
def sent(monkeypatch):
    """Alerts captured instead of delivered."""
    out: list[tuple[str, str]] = []
    monkeypatch.setattr(alerts, "send", lambda settings, subject, body: out.append((subject, body)) or ["test"])
    return out


@pytest.fixture
def profile():
    return resolve_cost_profile(get_settings(), parse_strategy_markdown(APPROVABLE_MARKDOWN))


@pytest.fixture
def sandbox_account(client):
    return create_account(client, broker="sandbox")


@pytest.fixture
def sandbox_deployment(client, approvable, sandbox_account):
    return approve_and_start(client, approvable, sandbox_account)


@pytest.fixture
def make(live_session, profile, tmp_path):
    """A crew context on a SandboxBroker and the broker feed. Returns (ctx, broker, clock)."""

    def build(deployment_id, *, broker=None, clock=None, settings=None, **broker_kwargs):
        clock = clock or Clock()
        settings = settings or get_settings()
        broker = broker or SandboxBroker(
            "paper", market=SandboxMarket(iv_rank=80, dte=14, spread_pct=0.5), profile=profile, clock=clock,
            state_path=tmp_path / f"{deployment_id}.json", **broker_kwargs,
        )  # fmt: skip
        feed = BrokerFeed(broker, settings, clock=clock, iv_rank=broker.iv_rank)
        ctx = build_context(live_session, deployment_id, settings, feed=feed, adapter=broker, clock=clock,
                            sleep=lambda s: None)  # fmt: skip
        return ctx, broker, clock

    return build


def stops_always():
    return get_settings().model_copy(update={"protective_stops": "always"})


def intents(live_session, deployment_id):
    rows = LiveStore(live_session).intents(deployment_id)
    live_session.rollback()
    return rows


def order_rows(live_session, intent_id):
    store = LiveStore(live_session)
    rows = [(o, store.order_status(o.id)) for o in store.orders_for_intent(intent_id)]
    live_session.rollback()
    return rows


def status(live_session, deployment_id):
    out = LiveStore(live_session).deployment_status(deployment_id)
    live_session.rollback()
    return out


def broker_positions(broker):
    return {p["tradingsymbol"]: p["quantity"] for p in broker.positions()}


# Order lifecycle


def test_split_quantity_respects_freeze_and_lots():
    assert split_quantity(375, 150, 75) == [150, 150, 75]
    assert split_quantity(1800, 1800, 75) == [1800]
    assert split_quantity(200, None, 75) == [200]
    assert split_quantity(2000, 1800, 75) == [1800, 200]


def test_hedges_go_first():
    legs = [{"side": "SELL", "k": 1}, {"side": "BUY", "k": 2}, {"side": "SELL", "k": 3}, {"side": "BUY", "k": 4}]
    assert [leg["k"] for leg in hedge_first(legs)] == [2, 4, 1, 3]


def test_rate_limiter_waits_when_the_budget_is_spent():
    now, waits = [0.0], []
    limiter = RateLimiter(2, lambda s: (waits.append(s), now.__setitem__(0, now[0] + s)), lambda: now[0])
    limiter.acquire()
    limiter.acquire()
    limiter.acquire()
    assert waits == [60.0]


def _manager(live_session, deployment_id, broker, profile, **kwargs):
    store = LiveStore(live_session)
    deployment = store.deployment(deployment_id)
    with audit_context("test"):
        intent = store.create_trade_intent(deployment_id=deployment_id, action="open", legs=[], decided_by="test",
                                           reason_text="order manager test")  # fmt: skip
    live_session.commit()
    manager = OrderManager(live_session, broker, get_settings(), profile, broker_account_id=deployment.broker_account_id,
                           sebi_algo_id="ALGO-1", clock=broker.clock, sleep=lambda s: None, **kwargs)  # fmt: skip
    return manager, intent


def _leg(broker, side="BUY", strike=22000.0, option_type="CE", quantity=150):
    expiry = broker.instruments("NIFTY")[0].expiry
    symbol = next(i.tradingsymbol for i in broker.instruments("NIFTY")
                  if i.strike == strike and i.option_type == option_type)  # fmt: skip
    quote = broker.quotes([symbol])[symbol]
    price, _ = quote_price(quote, 100)
    return {"side": side, "option_type": option_type, "strike": strike, "expiry": expiry, "tradingsymbol": symbol,
            "quantity": quantity, "quote": price}  # fmt: skip


def test_partial_fill_then_timeout_then_cancel_and_reprice(live_session, sandbox_deployment, profile, tmp_path):
    broker = SandboxBroker("paper", market=SandboxMarket(spread_pct=0), profile=profile, clock=Clock(),
                           partial_fill_ratio=0.5, stall_partials=True)  # fmt: skip
    manager, intent = _manager(live_session, sandbox_deployment, broker, profile, lot_size=75)
    leg = _leg(broker, quantity=150)
    leg["quote"] = float(broker.quotes([leg["tradingsymbol"]])[leg["tradingsymbol"]].ask)  # marketable at once
    with audit_context("test"):
        orders, error = manager.execute(intent, [leg])
    assert error is None
    store = LiveStore(live_session)
    first, second = orders
    assert [e.status for e in store.order_events(first.id)][-1] == "CANCELLED"
    assert sum(f.quantity for f in store.fills_for_order(first.id)) == 75
    assert broker.cancels == [first.broker_order_id]
    assert second.quantity == 75 and second.limit_price > first.limit_price  # re-priced toward the market
    assert store.order_status(second.id) == "COMPLETE"
    live_session.rollback()


def test_unfilled_order_gives_up_after_the_reprice_steps(live_session, sandbox_deployment, profile):
    broker = SandboxBroker("paper", market=SandboxMarket(spread_pct=20), profile=profile, clock=Clock())
    manager, intent = _manager(live_session, sandbox_deployment, broker, profile, lot_size=75)
    with audit_context("test"):
        orders, error = manager.execute(intent, [_leg(broker, quantity=75)])
    steps = get_settings().order_reprice_steps
    assert error == f"order for {orders[0].tradingsymbol} not filled after {steps} re-prices (0 of 75 filled)"
    assert len(orders) == steps + 1 and len(broker.cancels) == steps + 1
    live_session.rollback()


def test_orders_above_the_freeze_quantity_are_split(live_session, sandbox_deployment, profile):
    broker = SandboxBroker("paper", market=SandboxMarket(spread_pct=0), profile=profile, clock=Clock(),
                           freeze_quantity=150)  # fmt: skip
    manager, intent = _manager(live_session, sandbox_deployment, broker, profile, lot_size=75,
                               freeze_quantity=lambda leg: 150)  # fmt: skip
    leg = _leg(broker, quantity=375)
    leg["quote"] = float(broker.quotes([leg["tradingsymbol"]])[leg["tradingsymbol"]].ask)
    with audit_context("test"):
        orders, error = manager.execute(intent, [leg])
    assert error is None
    assert [o.quantity for o in orders] == [150, 150, 75]
    assert broker_positions(broker) == {leg["tradingsymbol"]: 375}
    live_session.rollback()


def test_max_order_value_refuses_without_sending(live_session, sandbox_deployment, profile, monkeypatch):
    monkeypatch.setenv("MAX_ORDER_VALUE_INR", "1")
    get_settings.cache_clear()
    broker = SandboxBroker("paper", profile=profile, clock=Clock())
    manager, intent = _manager(live_session, sandbox_deployment, broker, profile)
    with audit_context("test"):
        orders, error = manager.execute(intent, [_leg(broker, quantity=75)])
    assert orders == [] and "exceeds MAX_ORDER_VALUE_INR" in error
    assert broker.positions() == []
    live_session.rollback()


# The crew on the sandbox


def test_entry_fills_on_the_broker_feed_and_matches_broker_positions(live_session, make, sandbox_deployment, sent):
    ctx, broker, _ = make(sandbox_deployment)
    result = run_tick(ctx)
    assert result["outcome"] == "entered", result
    [intent] = intents(live_session, sandbox_deployment)
    sides = [o.side for o, _ in order_rows(live_session, intent.id)]
    assert sides[:2] == ["BUY", "BUY"] and sides[-1] == "SELL"  # hedges first
    store = LiveStore(live_session)
    account_id = store.deployment(sandbox_deployment).broker_account_id
    assert graph.position_diff(store, broker, account_id) == {}
    assert len(broker_positions(broker)) == 4
    live_session.rollback()


def test_second_leg_rejection_unwinds_the_filled_legs(live_session, make, sandbox_deployment, sent):
    first_short = []

    def reject_first_short(order):
        if order.side == "SELL" and not first_short:
            first_short.append(order.tradingsymbol)
            return "margin shortfall"
        return None

    ctx, broker, _ = make(sandbox_deployment, reject_when=reject_first_short)
    result = run_tick(ctx)
    assert result["outcome"] == "halted" and "margin shortfall" in result["detail"]
    entry, unwind = intents(live_session, sandbox_deployment)
    assert unwind.action == "close" and {leg["side"] for leg in unwind.legs} == {"SELL"}
    assert broker_positions(broker) == {}  # nothing left at the broker
    [trade] = LiveStore(live_session).live_trades(sandbox_deployment, 10, 0)
    assert trade.exit_reason == "unwind"
    assert status(live_session, sandbox_deployment) == "halted"
    assert any("halted" in subject for subject, _ in sent)


def test_margin_shortfall_records_a_blocked_intent_with_no_orders(live_session, make, sandbox_deployment, sent):
    ctx, broker, _ = make(sandbox_deployment, available=Decimal("1000"))
    result = run_tick(ctx)
    assert result["outcome"] == "blocked" and "risk.margin" in result["detail"]
    [intent] = intents(live_session, sandbox_deployment)
    assert intent.approval == "blocked"
    margin = next(c for c in intent.risk_checks["checks"] if c["rule"] == "risk.margin")
    assert margin["actual"] == 1000 and margin["passed"] is False
    assert order_rows(live_session, intent.id) == [] and broker.positions() == []


def test_exit_retries_then_halts_with_the_position_open(live_session, make, sandbox_deployment, sent, monkeypatch):
    ctx, broker, clock = make(sandbox_deployment)
    assert run_tick(ctx)["outcome"] == "entered"
    broker.reject_when = lambda o: "exchange unavailable"
    broker.market.spot = 24500  # through the short call: the crew's stop loss fires
    clock.advance(minutes=1)
    result = run_tick(ctx)
    assert result["outcome"] == "halted"
    close = intents(live_session, sandbox_deployment)[-1]
    rejected = [o for o, s in order_rows(live_session, close.id) if s == "REJECTED"]
    assert len(rejected) == 1 + get_settings().order_exit_retries
    assert status(live_session, sandbox_deployment) == "halted"
    assert broker.positions()  # still open, and the halt says so
    assert any("position still open" in body for _, body in sent)


def test_outside_market_hours_the_crew_idles(live_session, make, sandbox_deployment):
    ctx, broker, _ = make(sandbox_deployment, clock=Clock(datetime(2026, 1, 5, 2, 0, tzinfo=UTC)))  # 07:30 IST
    result = run_tick(ctx)
    assert result["outcome"] == "market_closed" and "outside 09:15-15:30" in result["detail"]
    assert intents(live_session, sandbox_deployment) == []


def test_market_status_weekends_holidays_and_session(tmp_path, monkeypatch):
    (tmp_path / "holidays").mkdir()
    (tmp_path / "holidays" / "nse.yaml").write_text("holidays:\n  - {date: 2026-01-26, name: Republic Day}\n")
    settings = get_settings().model_copy(update={"data_dir": str(tmp_path)})
    assert market_status(datetime(2026, 1, 5, 4, 0, tzinfo=UTC), settings) == (True, "market open")
    assert market_status(datetime(2026, 1, 10, 4, 0, tzinfo=UTC), settings)[1].endswith("is a weekend")
    assert "Republic Day" in market_status(datetime(2026, 1, 26, 4, 0, tzinfo=UTC), settings)[1]
    assert not market_status(datetime(2026, 1, 5, 10, 0, tzinfo=UTC), settings)[0]  # 15:30 IST


def test_reconciliation_mismatch_halts(live_session, make, sandbox_deployment, sent):
    ctx, broker, clock = make(sandbox_deployment)
    assert run_tick(ctx)["outcome"] == "entered"
    leg = _leg(broker, side="SELL", strike=22000.0, quantity=75)
    broker.place_order(OrderRequest("manual-1", "NFO", leg["tradingsymbol"], "SELL", 75, "LIMIT", "NRML",
                                    Decimal("0.05")))  # a manual trade in the account  # fmt: skip
    clock.advance(minutes=1)
    result = run_tick(ctx)
    assert result["outcome"] == "halted" and "reconciliation mismatch" in result["detail"]
    assert leg["tradingsymbol"] in result["detail"]
    assert status(live_session, sandbox_deployment) == "halted"
    assert any("reconcile_mismatch" in subject for subject, _ in sent)


def test_startup_reconciliation_refuses_to_run(live_session, make, sandbox_deployment, profile, tmp_path, sent):
    broker = SandboxBroker("paper", profile=profile, clock=Clock(), state_path=tmp_path / "shared.json")
    leg = _leg(broker, side="BUY", quantity=75)
    broker.place_order(OrderRequest("manual-1", "NFO", leg["tradingsymbol"], "BUY", 75, "LIMIT", "NRML",
                                    Decimal("100000")))  # fmt: skip
    broker.positions()
    with pytest.raises(ValueError, match="reconciliation mismatch at startup"):
        make(sandbox_deployment, broker=broker)
    assert status(live_session, sandbox_deployment) == "halted"


def test_heartbeat_per_tick_and_crew_running(client, live_session, make, sandbox_deployment):
    ctx, _, _ = make(sandbox_deployment, clock=Clock(datetime.now(UTC).replace(microsecond=0)))
    run_tick(ctx)
    beat = LiveStore(live_session).latest_heartbeat(sandbox_deployment)
    feed, outcome = beat.feed, beat.outcome
    live_session.rollback()
    assert feed == "broker" and outcome is not None
    deployment = client.get(f"/api/live/deployments/{sandbox_deployment}").json()
    assert deployment["crew_running"] is True and deployment["last_heartbeat"]["feed"] == "broker"


# Protective stops


def test_protective_stops_placed_after_entry_and_cancelled_before_a_normal_exit(live_session, make,
                                                                               sandbox_deployment, sent):  # fmt: skip
    ctx, broker, clock = make(sandbox_deployment, settings=stops_always())
    assert run_tick(ctx)["outcome"] == "entered"
    entry, protect = intents(live_session, sandbox_deployment)
    assert protect.action == "protect" and protect.risk_checks["protects"] == entry.id
    stops = order_rows(live_session, protect.id)
    assert len(stops) == 2 and all(o.order_type == "SL" and o.side == "BUY" for o, _ in stops)
    assert all(s == "TRIGGER_PENDING" for _, s in stops)
    short_prices = {(leg["strike"], leg["option_type"]): leg for leg in entry.legs if leg["side"] == "SELL"}
    for o, _ in stops:
        assert o.trigger_price > Decimal(str(short_prices[(float(o.strike), o.option_type)]["quote"]))
        assert o.limit_price > o.trigger_price

    broker.market.iv_rank, broker.market.dte = 5, 3  # premiums collapse: profit target
    broker.market.expiry = None
    clock.advance(minutes=1)
    result = run_tick(ctx)
    assert result["outcome"] == "exited", result
    assert {s for _, s in order_rows(live_session, protect.id)} == {"CANCELLED"}
    assert broker_positions(broker) == {}


def test_a_gap_past_the_stop_limit_leaves_it_unfilled_and_the_crew_exits(live_session, make, sandbox_deployment,
                                                                         sent):  # fmt: skip
    ctx, broker, clock = make(sandbox_deployment, settings=stops_always())
    assert run_tick(ctx)["outcome"] == "entered"
    broker.market.spot = 24500  # gap far beyond the stop's limit price: the stop-limit cannot fill
    clock.advance(minutes=1)
    result = run_tick(ctx)
    assert result["outcome"] == "exited" and result["exits"][0]["exit_reason"] == "stop_loss"
    assert broker_positions(broker) == {}
    assert LiveStore(live_session).working_orders(sandbox_deployment) == []  # stops cancelled before the exit
    live_session.rollback()


def test_a_fired_protective_stop_is_booked_as_broker_stop_and_pauses(live_session, make, sandbox_deployment, sent):
    settings = stops_always().model_copy(update={"protective_stop_limit_buffer_pct": 1000})
    ctx, broker, clock = make(sandbox_deployment, settings=settings)
    assert run_tick(ctx)["outcome"] == "entered"
    broker.market.spot = 24500  # through the short call; the stop's limit is wide enough to fill
    clock.advance(minutes=1)
    result = run_tick(ctx)
    assert result["outcome"] == "paused", result
    [trade] = LiveStore(live_session).live_trades(sandbox_deployment, 10, 0)
    assert trade.exit_reason == "broker_stop"
    assert broker_positions(broker) == {}
    assert status(live_session, sandbox_deployment) == "paused"
    assert any("protective_stop" in subject for subject, _ in sent)


def test_failed_stop_placement_unwinds_the_entry_and_halts(live_session, make, sandbox_deployment, sent):
    ctx, broker, _ = make(sandbox_deployment, settings=stops_always(),
                          reject_when=lambda o: "SL not allowed" if o.order_type == "SL" else None)  # fmt: skip
    result = run_tick(ctx)
    assert result["outcome"] == "halted" and "SL not allowed" in result["detail"]
    assert broker_positions(broker) == {}
    [trade] = LiveStore(live_session).live_trades(sandbox_deployment, 10, 0)
    assert trade.exit_reason == "unwind"
    assert status(live_session, sandbox_deployment) == "halted"


def test_paper_on_the_default_setting_places_no_stops(live_session, make, sandbox_deployment):
    ctx, _, _ = make(sandbox_deployment)
    run_tick(ctx)
    assert [i.action for i in intents(live_session, sandbox_deployment)] == ["open"]


# Kill switch


def test_kill_switch_cancels_working_orders(live_session, make, sandbox_deployment, profile):
    ctx, broker, _ = make(sandbox_deployment)
    manager, intent = _manager(live_session, sandbox_deployment, broker, profile)
    leg = _leg(broker, quantity=75) | {"side": "BUY"}
    with audit_context("test"):
        order, ack = manager._send(intent, leg, OrderRequest("resting-1", "NFO", leg["tradingsymbol"], "BUY", 75,
                                                             "LIMIT", "NRML", Decimal("0.05")))  # fmt: skip
        manager.sync(order, leg)
        live_session.commit()
        out = kill_switch.stop(live_session, get_settings(), sandbox_deployment, "kill", ctx=ctx)
    assert out["cancelled"] == [order.id] and out["error"] is None
    assert broker.cancels == [order.broker_order_id]
    assert LiveStore(live_session).order_status(order.id) == "CANCELLED"
    assert status(live_session, sandbox_deployment) == "stopped"


def test_flatten_closes_every_position(live_session, make, sandbox_deployment, sent):
    ctx, broker, clock = make(sandbox_deployment, settings=stops_always())
    assert run_tick(ctx)["outcome"] == "entered"
    clock.advance(minutes=1)
    with audit_context("alice"):
        out = kill_switch.stop(live_session, ctx.settings, sandbox_deployment, "flatten", flatten=True, ctx=ctx)
    assert out["error"] is None and len(out["exits"]) == 1
    assert out["exits"][0]["exit_reason"] == "kill_switch"
    assert broker_positions(broker) == {}
    assert LiveStore(live_session).working_orders(sandbox_deployment) == []
    assert status(live_session, sandbox_deployment) == "stopped"


def test_flatten_needs_a_token(client, sandbox_deployment):
    response = client.post(f"/api/live/deployments/{sandbox_deployment}/stop", json={"flatten": True}, headers=ACTOR)
    assert response.status_code == 403 and "API_TOKENS" in response.json()["detail"]
    response = client.post(f"/api/live/deployments/{sandbox_deployment}/pause", json={"flatten": True}, headers=ACTOR)
    assert response.status_code == 400


# Live gates


@pytest.fixture
def live_deployment(client, approvable, live_env):
    account = create_account(client, mode="live", broker="sandbox", headers=live_env)
    return approve_and_start(client, approvable, account, headers=live_env, sebi_algo_id="ALGO-1")


def test_live_sandbox_deployment_builds_with_the_broker_feed(live_session, live_deployment):
    ctx = build_context(live_session, live_deployment, get_settings(), clock=Clock(), sleep=lambda s: None)
    assert ctx.feed_kind == "broker" and isinstance(ctx.feed, BrokerFeed) and ctx.adapter.mode == "live"
    assert ctx.enforce_market_hours


@pytest.mark.parametrize(
    ("update", "kwargs", "message"),
    [
        ({"live_trading_enabled": False}, {}, "LIVE_TRADING_ENABLED is not true"),
        ({"sandbox_allow_live": False}, {}, "does not support live accounts"),
        ({"live_allowed_structures": "short_strangle"}, {}, "structure 'iron_condor' is not allowed live"),
        ({}, {"feed_kind": "replay"}, "needs --feed broker"),
    ],
)
def test_each_live_gate_refuses(live_session, live_deployment, update, kwargs, message):
    settings = get_settings().model_copy(update=update)
    with pytest.raises(ValueError, match=message):
        build_context(live_session, live_deployment, settings, clock=Clock(), **kwargs)


def test_live_gate_refuses_without_an_algo_id(client, approvable, live_env, live_session):
    account = create_account(client, mode="live", broker="sandbox", headers=live_env)
    deployment_id = approve_and_start(client, approvable, account, headers=live_env)
    with pytest.raises(ValueError, match="no sebi_algo_id"):
        build_context(live_session, deployment_id, get_settings(), clock=Clock())


def test_live_approval_refused_for_structures_not_allowed(client, approvable, live_env, monkeypatch):
    account = create_account(client, mode="live", broker="sandbox", headers=live_env)
    monkeypatch.setenv("LIVE_ALLOWED_STRUCTURES", "short_strangle")
    get_settings.cache_clear()
    params = {"strategy_id": approvable["strategy_id"], "version": 1, "mode": "live", "broker_account_id": account["id"]}
    preview = client.get("/api/live/approvals/preview", params=params, headers=live_env).json()
    assert any("not allowed live" in b for b in preview["blockers"])
    response = client.post("/api/live/approvals", json={
        "strategy_id": approvable["strategy_id"], "version": 1, "mode": "live", "broker_account_id": account["id"],
        "confirm_rules_sha256": approvable["preview"]["rules_sha256"], "acknowledge_verdict": True,
    }, headers=live_env)  # fmt: skip
    assert response.status_code == 400 and "not allowed live" in response.json()["detail"]
    monkeypatch.setenv("LIVE_ALLOWED_STRUCTURES", "iron_condor,short_strangle")
    get_settings.cache_clear()
    assert client.get("/api/live/approvals/preview", params=params, headers=live_env).json()["blockers"] == []


def test_live_min_paper_days_counts_broker_feed_days_without_halts(client, approvable, live_env, live_session,
                                                                   monkeypatch):  # fmt: skip
    monkeypatch.setenv("LIVE_MIN_PAPER_DAYS", "2")
    get_settings.cache_clear()
    live_account = create_account(client, mode="live", broker="sandbox", headers=live_env)
    params = {"strategy_id": approvable["strategy_id"], "version": 1, "mode": "live",
              "broker_account_id": live_account["id"]}  # fmt: skip
    blockers = client.get("/api/live/approvals/preview", params=params, headers=live_env).json()["blockers"]
    assert any("0 trading day(s)" in b for b in blockers)

    paper = create_account(client, broker="sandbox", headers=live_env)
    paper_id = approve_and_start(client, approvable, paper, headers=live_env)
    with audit_context("test"):
        for day in (5, 6, 6):
            LiveStore(live_session).record_heartbeat(deployment_id=paper_id, at=datetime(2026, 1, day, 5, tzinfo=UTC),
                                                     feed="broker", outcome="idle", pid=1)  # fmt: skip
        LiveStore(live_session).record_heartbeat(deployment_id=paper_id, at=datetime(2026, 1, 7, 5, tzinfo=UTC),
                                                 feed="replay", outcome="idle", pid=1)  # fmt: skip
    live_session.commit()
    assert client.get("/api/live/approvals/preview", params=params, headers=live_env).json()["blockers"] == []

    with audit_context("test"):
        LiveStore(live_session).add_deployment_event(paper_id, "halted", "test")
    live_session.commit()
    blockers = client.get("/api/live/approvals/preview", params=params, headers=live_env).json()["blockers"]
    assert any("LIVE_MIN_PAPER_DAYS=2" in b for b in blockers)


# Broker registry and credentials


@pytest.fixture
def testbroker(profile):
    def factory(values, settings, profile, clock, account):
        broker = SandboxBroker(account.mode, profile=profile, clock=clock)
        broker.name, broker.credentials = "testbroker", values
        return broker

    spec = registry.register(BrokerSpec(
        name="testbroker", label="Test broker", has_quotes=True, factory=factory,
        modes=lambda settings: ("paper", "live"),
        credential_fields=(CredentialField("api_key", "API key"), CredentialField("access_token", "Access token"),
                           CredentialField("pin", "PIN", required=False)),
    ))  # fmt: skip
    yield spec
    registry.unregister("testbroker")


@pytest.fixture
def fernet_key(monkeypatch):
    key = credentials.generate_key()
    monkeypatch.setenv("BROKER_CREDENTIALS_KEY", key)
    get_settings.cache_clear()
    return key


def test_broker_list_and_configured_status(client, testbroker, monkeypatch):
    brokers = {b["name"]: b for b in client.get("/api/live/brokers").json()["brokers"]}
    assert set(brokers) >= {"paper", "sandbox", "testbroker"}
    assert brokers["paper"]["modes"] == ["paper"] and brokers["paper"]["configured"] is True
    assert brokers["sandbox"]["modes"] == ["paper"] and brokers["sandbox"]["has_quotes"] is True
    assert brokers["testbroker"]["configured"] is False
    monkeypatch.setenv("BROKER_TESTBROKER_API_KEY", "k-env")
    monkeypatch.setenv("BROKER_TESTBROKER_ACCESS_TOKEN", "t-env")
    listed = client.get("/api/live/brokers").json()
    testb = next(b for b in listed["brokers"] if b["name"] == "testbroker")
    assert testb["configured"] is True and {f["source"] for f in testb["fields"] if f["required"]} == {"env"}
    assert "k-env" not in str(listed)
    monkeypatch.setenv("SANDBOX_ALLOW_LIVE", "true")
    get_settings.cache_clear()
    sandbox = next(b for b in client.get("/api/live/brokers").json()["brokers"] if b["name"] == "sandbox")
    assert sandbox["modes"] == ["paper", "live"]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"broker": "zerodha", "mode": "paper"}, "no adapter is registered for broker 'zerodha'"),
        ({"broker": "paper", "mode": "live"}, "does not support live accounts"),
        ({"broker": "sandbox", "mode": "live"}, "does not support live accounts"),
    ],
)
def test_unregistered_broker_or_unsupported_mode_is_rejected(client, body, message):
    response = client.post("/api/live/broker-accounts", json=body | {"account_ref_masked": "X1"}, headers=ACTOR)
    assert response.status_code == 400 and message in response.json()["detail"]


def test_creating_a_live_account_needs_a_token(client, monkeypatch):
    monkeypatch.setenv("SANDBOX_ALLOW_LIVE", "true")
    get_settings.cache_clear()
    body = {"broker": "sandbox", "mode": "live", "account_ref_masked": "X1"}
    assert client.post("/api/live/broker-accounts", json=body, headers=ACTOR).status_code == 403


def test_credentials_db_first_then_env_and_revoked_falls_back(client, live_env, testbroker, fernet_key,
                                                              live_session, monkeypatch):  # fmt: skip
    monkeypatch.setenv("BROKER_TESTBROKER_API_KEY", "key-from-env")
    account = create_account(client, broker="testbroker", headers=live_env)
    url = f"/api/live/broker-accounts/{account['id']}/credentials"

    def resolved():
        store = LiveStore(live_session)
        try:
            return credentials.resolve(store, store.broker_account(account["id"]), testbroker, get_settings())
        finally:
            live_session.rollback()

    assert resolved() == {"api_key": "key-from-env"}
    status_ = client.get(url, headers=live_env).json()
    assert status_["configured"] is False
    assert {f["name"]: f["source"] for f in status_["fields"]} == {"api_key": "env", "access_token": None, "pin": None}

    saved = client.put(url, json={"api_key": "key-from-db", "access_token": "tok-db"}, headers=live_env)
    assert saved.status_code == 200, saved.text
    assert saved.json()["configured"] is True
    assert resolved() == {"api_key": "key-from-db", "access_token": "tok-db"}

    revoked = client.request("DELETE", url, params={"field": "api_key"}, headers=live_env).json()
    assert {f["name"]: f["source"] for f in revoked["fields"]}["api_key"] == "env"
    assert resolved()["api_key"] == "key-from-env"

    # rotate: the latest row wins
    client.put(url, json={"access_token": "tok-2"}, headers=live_env)
    assert resolved()["access_token"] == "tok-2"

    # secrets never appear in responses or audit payloads
    everything = str(client.get(url, headers=live_env).json()) + str(client.get("/api/live/broker-accounts",
                                                                                headers=live_env).json())  # fmt: skip
    audit = [str(e.details) for e in live_session.scalars(select(LiveAuditEvent))]
    live_session.rollback()
    for secret in ("key-from-db", "tok-db", "tok-2", "key-from-env"):
        assert secret not in everything
        assert not any(secret in a for a in audit)
    assert any("broker_credential.revoked" in a or "fingerprint" in a for a in audit)


def test_saving_credentials_without_the_key_is_400(client, live_env, testbroker):
    account = create_account(client, broker="testbroker", headers=live_env)
    response = client.put(f"/api/live/broker-accounts/{account['id']}/credentials", json={"api_key": "x"},
                          headers=live_env)  # fmt: skip
    assert response.status_code == 400 and "BROKER_CREDENTIALS_KEY" in response.json()["detail"]


def test_credential_endpoints_always_need_a_token(client, testbroker, monkeypatch):
    account = create_account(client, broker="testbroker")
    url = f"/api/live/broker-accounts/{account['id']}/credentials"
    assert client.get(url, headers=ACTOR).status_code == 403  # no API_TOKENS configured
    monkeypatch.setenv("API_TOKENS", f"alice:{hash_token(TOKEN)}")
    get_settings.cache_clear()
    assert client.get(url, headers=ACTOR).status_code == 401
    assert client.put(url, json={"api_key": "x"}).status_code == 401
    assert client.request("DELETE", url).status_code == 401


def test_adapter_needs_its_credentials(live_session, client, testbroker, profile):
    account = create_account(client, broker="testbroker")
    row = LiveStore(live_session).broker_account(account["id"])
    with pytest.raises(ValueError, match="not configured: missing api_key, access_token"):
        registry.get_adapter(LiveStore(live_session), row, get_settings(), profile, Clock())
    live_session.rollback()


# API auth


def test_valid_token_sets_the_actor_and_overrides_x_actor(client, live_env, live_session):
    account = create_account(client, headers=live_env | {"X-Actor": "mallory"})
    event = live_session.scalar(select(LiveAuditEvent).where(LiveAuditEvent.entity_id == account["id"]))
    live_session.rollback()
    assert event.actor == "alice"


def test_bad_token_is_401_and_tokens_protect_mutations_and_live_reads(client, live_env):
    assert client.get("/api/live/deployments", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/live/deployments").status_code == 401
    assert client.post("/api/live/broker-accounts", json={"broker": "paper", "mode": "paper",
                                                          "account_ref_masked": "X"}).status_code == 401  # fmt: skip
    assert client.get("/api/strategies").status_code == 200  # research reads stay open
    assert client.get("/api/live/deployments", headers=live_env).status_code == 200
    preflight = client.options("/api/live/deployments", headers={"Origin": "http://127.0.0.1:8842",
                                                                 "Access-Control-Request-Method": "GET"})  # fmt: skip
    assert preflight.status_code != 401  # CORS preflights never need a token


def test_live_start_needs_a_token(client, approvable, live_env, monkeypatch):
    account = create_account(client, mode="live", broker="sandbox", headers=live_env)
    approved = client.post("/api/live/approvals", json={
        "strategy_id": approvable["strategy_id"], "version": 1, "mode": "live", "broker_account_id": account["id"],
        "confirm_rules_sha256": approvable["preview"]["rules_sha256"], "acknowledge_verdict": True,
    }, headers=live_env).json()  # fmt: skip
    monkeypatch.setenv("API_TOKENS", "")
    get_settings.cache_clear()
    response = client.post(f"/api/live/deployments/{approved['deployment']['id']}/start", json={}, headers=ACTOR)
    assert response.status_code == 403


def test_new_token_cli_prints_a_hash_that_verifies(capsys, monkeypatch):
    assert auth_cli(["new-token", "alice"]) == 0
    out = capsys.readouterr().out
    token = out.split("shown once")[1].split("\n")[1].strip()
    line = out.strip().splitlines()[-1].strip()
    assert line == f"alice:{hash_token(token)}"


# Supervisor and alerts


class FakeProcess:
    def __init__(self) -> None:
        self.code: int | None = None
        self.terminated = False

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated, self.code = True, -15

    def wait(self, timeout=None):
        return self.code

    def kill(self):
        self.code = -9


def test_supervisor_restarts_a_crashed_crew_and_stops_with_the_deployment(client, deployment_id_paper, sent):
    spawned: list[FakeProcess] = []
    clock, mono = Clock(datetime.now(UTC)), [0.0]

    def spawn(cmd, deployment_id):
        assert cmd[-4:] == ["--deployment", deployment_id, "--interval", "60.0"] or "--deployment" in cmd
        spawned.append(FakeProcess())
        return spawned[-1]

    sup = Supervisor(get_settings(), get_live_sessionmaker(), spawn=spawn, clock=clock, monotonic=lambda: mono[0],
                     crash_alert_after=2)  # fmt: skip
    assert sup.step() == {deployment_id_paper: "started"}
    assert sup.step() == {}  # running, nothing to do
    spawned[0].code = 1
    assert sup.step() == {deployment_id_paper: "crashed"}  # backoff: not restarted yet
    mono[0] += 10
    assert sup.step() == {deployment_id_paper: "restarted"}
    spawned[1].code = 1
    sup.step()
    assert any("crashes" in subject for subject, _ in sent)
    mono[0] += 100
    sup.step()
    assert len(spawned) == 3
    client.post(f"/api/live/deployments/{deployment_id_paper}/stop", json={"reason": "done"}, headers=ACTOR)
    assert sup.step() == {deployment_id_paper: "terminated"}
    assert spawned[2].terminated


def test_supervisor_alerts_on_a_missed_heartbeat(deployment_id_paper, sent):
    clock = Clock(datetime.now(UTC))
    sup = Supervisor(get_settings(), get_live_sessionmaker(), spawn=lambda cmd, d: FakeProcess(), clock=clock)
    sup.step()
    clock.advance(seconds=get_settings().heartbeat_max_age_s + 5)
    sup.step()
    sup.step()
    assert [s for s, _ in sent if "heartbeat_missed" in s] == [f"[genesis] heartbeat_missed: deployment {deployment_id_paper}"]


@pytest.fixture
def deployment_id_paper(client, approvable, paper_account):
    return approve_and_start(client, approvable, paper_account)


def test_alerts_are_sent_on_halt_and_pause(live_session, make, sandbox_deployment, sent, monkeypatch):
    ctx, _, _ = make(sandbox_deployment)
    with audit_context("test"):
        graph.halt(ctx, "test halt")
    assert sent[-1] == (f"[genesis] halted: deployment {sandbox_deployment}", "test halt")


def test_alert_channels_deliver_and_never_raise(monkeypatch):
    calls = []

    class Response:
        def raise_for_status(self):
            raise RuntimeError("telegram down")

    monkeypatch.setattr(alerts.httpx, "post", lambda url, json, timeout: calls.append((url, json)) or Response())
    settings = get_settings().model_copy(update={"alert_telegram_bot_token": "T", "alert_telegram_chat_id": "42"})
    assert alerts.notify(settings, "halted", "d1", "boom") == []  # failed delivery: logged, not raised
    assert calls[0][0] == "https://api.telegram.org/botT/sendMessage" and "boom" in calls[0][1]["text"]
    assert alerts.notify(get_settings(), "halted", "d1", "no channels") == []
