"""The Trade Crew: one per deployment, deciding only through the strategy's rule checks.

Each tick: verify the frozen snapshot, gate on `started`, idle outside market hours (broker
feed only), observe the market, reconcile the broker's positions with the live database (and
handle a protective stop the broker filled), check open positions against the exit rules and
then the trade policy's booking and time exits (see `app.trading.policy`) and snapshot the ones
still open, apply the drawdown ladder (see `app.live.drawdown`) and the daily profit target,
then evaluate entry, sizing, risk and margin. Orders go through `app.trading.orders` (hedge
first, wait for fills, re-price, unwind a failed entry). An optional LLM writes the reason text
after a decision is made; it is never an input to one.

With LLM_TRADE_OPINION set, an LLM also gives an opinion (take, reduce or skip) on entries the
rules approved. In shadow mode it is only recorded. In veto mode (enabled per deployment, see
`app.live.veto`) a skip records a `vetoed` intent with no orders and a reduction sends fewer lots
after the risk checks run again; the removed lots are tracked as phantom positions until an exit
rule fires. Exits are never sent to the LLM.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import alerts
from app.agents.trade_crew.legs import signed
from app.agents.trade_crew.lessons import trade_facts, write_lesson
from app.agents.trade_crew.opinion import OpinionResult, get_opinion
from app.audit import audit_context, current_actor
from app.brokers.base import TERMINAL_ORDER_STATUSES, BrokerAdapter, OrderRequest
from app.brokers.sandbox import to_tick
from app.config import Settings
from app.db.hashing import money, sha256_hex
from app.db.models import BrokerCharge, DeployedStrategy, Deployment, Order, TradeIntent, TradeOpinion
from app.db.types import utcnow
from app.engine.costs import CostProfile, leg_costs, resolve_cost_profile
from app.engine.sim import outcome_label
from app.live import drawdown, veto
from app.live import money as money_rules
from app.live import odds as live_odds
from app.live.equity import EquityState, capital_in_force, day_pnl, equity_state
from app.live.store import TERMINAL_STATUSES, LiveStore
from app.market.context import ContextProvider
from app.market.feed import IST, MarketFeed, MarketSnapshot, snapshot_age_seconds
from app.market.hours import market_status, trading_days_between
from app.strategies.rules import (
    AccountState,
    Check,
    RiskLimits,
    StrategyRules,
    check_entry,
    losing_streak_cooldown,
    rules_sha256,
    with_common_risk,
)
from app.strategies.schema import parse_strategy_markdown
from app.trading import odds as odds_gate
from app.trading import policy as trade_policy
from app.trading.decisions import (
    MissingMarketConfig,
    build_entry_proposal,
    describe_check,
    entry_risk_checks,
    entry_rule_text,
    exit_checks,
    fired_exit,
    frozen_exits,
    lot_size_for,
    mark_position,
    proposal_lots,
    resize_proposal,
)
from app.trading.orders import EXCHANGE, PRODUCT, OrderManager, RateLimiter, split_quantity

__all__ = ["EXCHANGE", "PRODUCT", "CrewContext", "halt", "run_tick"]


@dataclass
class CrewContext:
    live: Session
    deployment_id: str
    adapter: BrokerAdapter
    feed: MarketFeed
    settings: Settings
    clock: Callable[[], datetime] = utcnow
    explain: bool = False
    feed_kind: str = "replay"  # replay | broker
    enforce_market_hours: bool = False
    sleep: Callable[[float], None] = time.sleep
    limiter: RateLimiter | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.limiter is None:
            self.limiter = RateLimiter(self.settings.max_orders_per_minute, self.sleep)


class TickState(TypedDict, total=False):
    ctx: CrewContext
    stop: bool
    outcome: str
    detail: str
    deployment: Deployment
    snapshot: DeployedStrategy
    rules: StrategyRules
    profile: CostProfile
    market: MarketSnapshot
    exits: list[dict]
    phantoms: list[dict]
    open_positions: int
    equity: EquityState
    ladder: drawdown.LadderDecision
    floor_inr: float  # the floor in force, when the deployment has one
    daily_check: Check
    entry_checks: list[Check]
    max_lots: int
    sizing_check: Check | None
    unscaled_lots: int
    vix_factor: float
    odds: odds_gate.Odds  # the odds gate's view of today's VIX band, when the policy has one
    proposal: dict
    risk_checks: list[Check]
    approval: str
    opinion: OpinionResult
    opinion_mode: str
    rules_lots: int
    reason_text: str
    model: str | None
    intent_id: str


def verify_snapshot(snapshot: DeployedStrategy) -> list[str]:
    problems = []
    if sha256_hex(snapshot.markdown) != snapshot.content_sha256:
        problems.append("markdown does not match content_sha256")
    if rules_sha256(snapshot.rules) != snapshot.rules_sha256:
        problems.append("rules do not match rules_sha256")
    doc = parse_strategy_markdown(snapshot.markdown)
    if rules_sha256(doc.frontmatter.get("rules")) != snapshot.rules_sha256:
        problems.append("rules in the markdown differ from the approved rules")
    problems += doc.rules_errors
    return problems


def halt(ctx: CrewContext, reason: str) -> None:
    """Halt the deployment and cancel its working orders. Protective stops stay at the broker: with the
    crew halted they are what still guards open positions."""
    store = LiveStore(ctx.live)
    if store.deployment_status(ctx.deployment_id) not in TERMINAL_STATUSES:
        store.add_deployment_event(ctx.deployment_id, "halted", reason)
    ctx.live.commit()
    try:
        cancel_working_orders(ctx, include_stops=False)
    except Exception as exc:  # the halt is recorded; a failed cancel must not hide it
        reason = f"{reason} (cancelling working orders failed: {type(exc).__name__}: {exc})"
    alerts.notify(ctx.settings, "halted", ctx.deployment_id, reason)


def order_manager(ctx: CrewContext, deployment: Deployment, profile: CostProfile, lot_size: int | None = None):
    underlying = None
    try:
        snapshot = LiveStore(ctx.live).deployed_strategy(deployment.deployed_strategy_id)
        underlying = StrategyRules.model_validate(snapshot.rules).instrument.underlying
    except Exception:
        pass

    def freeze(leg: dict) -> int | None:
        instrument = getattr(ctx.feed, "instrument", None)
        if instrument and underlying:
            found = instrument(underlying, leg["strike"], leg["option_type"], leg["expiry"])
            if found and found.freeze_quantity:
                return found.freeze_quantity
        return ctx.settings.freeze_quantity(underlying) if underlying else None

    return OrderManager(
        ctx.live, ctx.adapter, ctx.settings, profile,
        broker_account_id=deployment.broker_account_id, sebi_algo_id=deployment.sebi_algo_id,
        clock=ctx.clock, sleep=ctx.sleep, limiter=ctx.limiter, freeze_quantity=freeze, lot_size=lot_size,
    )  # fmt: skip


def _manager(ctx: CrewContext, state: TickState) -> OrderManager:
    lot = state.get("proposal", {}).get("lot_size") if state.get("proposal") else None
    if lot is None and state.get("market") is not None:
        lot = lot_size_for(state["rules"], state["market"], ctx.settings)
    return order_manager(ctx, state["deployment"], state["profile"], lot)


def _is_stop_intent(intent: TradeIntent) -> bool:
    return intent.action == "protect"


def cancel_working_orders(ctx: CrewContext, include_stops: bool) -> list[str]:
    """Cancel every non-terminal order of the deployment (protective stops only with `include_stops`)."""
    store = LiveStore(ctx.live)
    deployment = store.deployment(ctx.deployment_id)
    snapshot = store.deployed_strategy(deployment.deployed_strategy_id)
    profile = resolve_cost_profile(ctx.settings, parse_strategy_markdown(snapshot.markdown))
    manager = order_manager(ctx, deployment, profile)
    cancelled = []
    for order in store.working_orders(ctx.deployment_id):
        intent = ctx.live.get(TradeIntent, order.trade_intent_id)
        if intent is not None and _is_stop_intent(intent) and not include_stops:
            continue
        manager.cancel(order)
        cancelled.append(order.id)
    return cancelled


def _still_started(ctx: CrewContext) -> bool:
    return LiveStore(ctx.live).deployment_status(ctx.deployment_id) == "started"


def _check_dict(c: Check) -> dict:
    return asdict(c)


_describe = describe_check


def _llm_explain(settings: Settings, facts: dict[str, Any]) -> tuple[str, str] | None:
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        from app.llm import chat_model, served_model

        llm = chat_model(settings)
        resp = llm.invoke(
            [
                SystemMessage(
                    content="Explain this already-made trading decision in two sentences for a trade journal. "
                    "Use only the facts given. Do not recommend anything."
                ),
                HumanMessage(content=str(facts)),
            ]
        )
        text = str(resp.content).strip()
        return (text, served_model(resp, settings)) if text else None
    except Exception:
        return None


def _reason(ctx: CrewContext, default: str, facts: dict[str, Any]) -> tuple[str, str | None]:
    if not (ctx.explain and ctx.settings.llm_enabled):
        return default, None
    ctx.live.rollback()  # release the SQLite write lock while the LLM runs
    explained = _llm_explain(ctx.settings, facts)
    return (f"{default}\n\n{explained[0]}", explained[1]) if explained else (default, None)


# Orders


def _send_orders(
    ctx: CrewContext, state: TickState, intent: TradeIntent, legs: list[dict], retries: int = 0
) -> tuple[list[Order], str | None]:
    """Send every leg (hedges first) and wait for the fills. Stops at the first leg that cannot be filled."""
    return _manager(ctx, state).execute(intent, legs, retries=retries)


def _leg_key(leg: dict) -> tuple:
    return (leg["side"], float(leg["strike"]), leg["option_type"], leg["expiry"])


def _filled_legs(store: LiveStore, intent: TradeIntent) -> list[dict]:
    """One row per leg with its total filled quantity and average price, across split and re-priced orders,
    in the order of the intent's legs."""
    grouped: dict[tuple, dict] = {}
    for order in store.orders_for_intent(intent.id):
        fills = store.fills_for_order(order.id)
        qty = sum(f.quantity for f in fills)
        if not qty or order.strike is None:
            continue
        key = (order.side, float(order.strike), order.option_type, order.expiry)
        row = grouped.setdefault(key, {"quantity": 0, "cost": Decimal(0), "order": order})
        row["quantity"] += qty
        row["cost"] += sum(f.price * f.quantity for f in fills)
    quotes = {_leg_key(leg): leg.get("quote") for leg in intent.legs or [] if leg.get("strike") is not None}
    rank = {key: i for i, key in enumerate(quotes)}
    legs = []
    for key in sorted(grouped, key=lambda k: rank.get(k, len(rank))):
        row, order = grouped[key], grouped[key]["order"]
        quote = quotes.get(key)
        legs.append(
            {
                "side": order.side,
                "option_type": order.option_type,
                "strike": float(order.strike),
                "expiry": order.expiry,
                "tradingsymbol": order.tradingsymbol,
                "quantity": row["quantity"],
                "price": float(row["cost"] / row["quantity"]),
                "quote": float(quote) if quote is not None else float(order.limit_price),
            }
        )
    return legs


def _open_legs(store: LiveStore, intent: TradeIntent) -> list[dict]:
    """Filled legs less the quantities that partial bookings have already closed."""
    closed: dict[tuple, int] = {}
    for trade in store.partial_trades(intent.id):
        for leg in trade.legs:
            key = (float(leg["strike"]), leg["option_type"], leg["expiry"])
            closed[key] = closed.get(key, 0) + leg["quantity"]
    legs = []
    for leg in _filled_legs(store, intent):
        quantity = leg["quantity"] - closed.get((leg["strike"], leg["option_type"], leg["expiry"]), 0)
        if quantity > 0:
            legs.append(leg | {"quantity": quantity})
    return legs


def _costs(store: LiveStore, orders: list[Order]) -> tuple[Decimal, Decimal | None]:
    """(estimated costs excluding slippage, which is already in fill prices; broker-reported costs), unrounded."""
    est, actual, reported = Decimal("0"), Decimal("0"), False
    for order in orders:
        for f in store.fills_for_order(order.id):
            est += sum((Decimal(str(v)) for k, v in f.est_cost_breakdown.items() if k != "slippage"), Decimal("0"))
        for c in store.session.scalars(select(BrokerCharge).where(BrokerCharge.order_id == order.id)):
            actual += c.total
            reported = True
    return est, actual if reported else None


# Nodes


def _verify(state: TickState) -> TickState:
    ctx = state["ctx"]
    store = LiveStore(ctx.live)
    deployment = store.deployment(ctx.deployment_id)
    snapshot = store.deployed_strategy(deployment.deployed_strategy_id)
    problems = verify_snapshot(snapshot)
    if problems:
        halt(ctx, "snapshot verification failed: " + "; ".join(problems))
        return {"stop": True, "outcome": "halted", "detail": "; ".join(problems)}
    rules = StrategyRules.model_validate(snapshot.rules)
    profile = resolve_cost_profile(ctx.settings, parse_strategy_markdown(snapshot.markdown))
    return {"deployment": deployment, "snapshot": snapshot, "rules": rules, "profile": profile}


def _gate(state: TickState) -> TickState:
    status = LiveStore(state["ctx"].live).deployment_status(state["ctx"].deployment_id)
    if status != "started":
        return {"stop": True, "outcome": "idle", "detail": f"deployment is {status}"}
    return {}


def _observe(state: TickState) -> TickState:
    ctx = state["ctx"]
    store = LiveStore(ctx.live)
    held = {
        (leg["strike"], leg["option_type"], leg["expiry"])
        for intent in store.open_intents(ctx.deployment_id)
        for leg in _open_legs(store, intent)
    } | {
        (leg["strike"], leg["option_type"], leg["expiry"])
        for _, intent in store.open_phantoms(ctx.deployment_id)
        for leg in intent.legs
    }
    market = ctx.feed.snapshot(state["rules"].instrument.underlying, sorted(held))
    if market is None:
        return {"stop": True, "outcome": "no_market", "detail": "feed returned no snapshot"}
    age = snapshot_age_seconds(market, ctx.clock())
    if age > ctx.settings.max_snapshot_age_seconds:
        return {
            "stop": True,
            "outcome": "stale_market",
            "detail": f"snapshot is {age:.0f}s old (max {ctx.settings.max_snapshot_age_seconds}s)",
        }
    return {"market": market}


def _hours(state: TickState) -> TickState:
    """Outside the NSE session the crew does nothing (broker feed only; replayed bars carry their own dates)."""
    ctx = state["ctx"]
    if not ctx.enforce_market_hours:
        return {}
    is_open, detail = market_status(ctx.clock(), ctx.settings)
    return {} if is_open else {"stop": True, "outcome": "market_closed", "detail": detail}


# Reconciliation and protective stops


def expected_positions(store: LiveStore, broker_account_id: str) -> dict[str, int]:
    """Net quantity per tradingsymbol the live database says the account holds: open legs of every
    deployment on the account, plus fills of protective stops whose position is still open."""
    net: dict[str, int] = {}
    for d in store.deployments_for_account(broker_account_id):
        for intent in store.open_intents(d.id):
            for leg in _open_legs(store, intent):
                net[leg["tradingsymbol"]] = net.get(leg["tradingsymbol"], 0) + leg["quantity"] * -signed(leg["side"])
            for protect in store.protect_intents(intent.id):
                for leg in _filled_legs(store, protect):
                    qty = leg["quantity"] * -signed(leg["side"])
                    net[leg["tradingsymbol"]] = net.get(leg["tradingsymbol"], 0) + qty
    return {k: v for k, v in net.items() if v}


def position_diff(store: LiveStore, adapter: BrokerAdapter, broker_account_id: str) -> dict[str, tuple[int, int]]:
    """tradingsymbol -> (expected, at broker) for every symbol where they differ."""
    expected = expected_positions(store, broker_account_id)
    actual = {p["tradingsymbol"]: int(p["quantity"]) for p in adapter.positions() if int(p["quantity"])}
    return {s: (expected.get(s, 0), actual.get(s, 0)) for s in expected.keys() | actual.keys()
            if expected.get(s, 0) != actual.get(s, 0)}  # fmt: skip


def _describe_diff(diff: dict[str, tuple[int, int]]) -> str:
    return ", ".join(f"{s}: expected {e}, broker {a}" for s, (e, a) in sorted(diff.items()))


def sync_protective_stops(ctx: CrewContext, deployment: Deployment, profile: CostProfile) -> list[TradeIntent]:
    """Record the latest status and fills of every protective stop; returns open intents whose stop fired."""
    store = LiveStore(ctx.live)
    manager = order_manager(ctx, deployment, profile)
    fired = []
    for intent in store.open_intents(deployment.id):
        hit = False
        for protect in store.protect_intents(intent.id):
            for order in store.orders_for_intent(protect.id):
                if store.order_status(order.id) not in TERMINAL_ORDER_STATUSES:
                    manager.sync(order)
                hit = hit or bool(store.fills_for_order(order.id))
        if hit:
            fired.append(intent)
    ctx.live.commit()
    return fired


def reconcile_positions(ctx: CrewContext, deployment: Deployment, profile: CostProfile) -> str | None:
    """Sync protective stops, then compare the broker's positions with the live database. Returns the
    mismatch, if any. Adapters whose positions do not persist (paper) are not reconciled."""
    if not getattr(ctx.adapter, "reconciles", False):
        return None
    sync_protective_stops(ctx, deployment, profile)
    diff = position_diff(LiveStore(ctx.live), ctx.adapter, deployment.broker_account_id)
    return _describe_diff(diff) if diff else None


def _reconcile(state: TickState) -> TickState:
    ctx = state["ctx"]
    if not getattr(ctx.adapter, "reconciles", False):
        return {}
    fired = sync_protective_stops(ctx, state["deployment"], state["profile"])
    diff = position_diff(LiveStore(ctx.live), ctx.adapter, state["deployment"].broker_account_id)
    if diff:
        detail = f"reconciliation mismatch: {_describe_diff(diff)}"
        halt(ctx, detail)
        alerts.notify(ctx.settings, "reconcile_mismatch", ctx.deployment_id, _describe_diff(diff))
        return {"stop": True, "outcome": "halted", "detail": detail}
    if fired:
        return _broker_stop(state, fired)
    return {}


def _stops_enabled(ctx: CrewContext) -> bool:
    mode = ctx.settings.protective_stops
    return mode == "always" or (mode == "live" and ctx.adapter.mode == "live")


def _place_protective_stops(ctx: CrewContext, state: TickState, intent: TradeIntent, legs: list[dict]) -> str | None:
    """A stop-limit BUY at the broker on every short leg: a backstop set `PROTECTIVE_STOP_MULTIPLIER` times
    the strategy's stop distance above the entry premium, so the crew's own exit normally fires first."""
    settings, rules = ctx.settings, state["rules"]
    if not _stops_enabled(ctx) or rules.instrument.is_debit:  # a debit spread's short leg is covered by its long
        return None
    shorts = [leg for leg in legs if leg["side"] == "SELL"]
    if not shorts:
        return None
    stop_pct = rules.stop_loss_pct or 100
    credit = sum(signed(leg["side"]) * float(leg["price"]) * leg["quantity"] for leg in legs)
    if rules.uses_capital_stop and intent.stop_loss_pnl is not None and credit > 0:
        stop_pct = round(-float(intent.stop_loss_pnl) / credit * 100, 2)  # the rupee stop fixed at entry
    distance = Decimal(str(settings.protective_stop_multiplier * stop_pct / 100))
    buffer = Decimal(str(settings.protective_stop_limit_buffer_pct / 100))
    stops = []
    for leg in shorts:
        trigger = to_tick(Decimal(str(leg["price"])) * (1 + distance), up=True)
        stops.append(leg | {"side": "BUY", "trigger": trigger, "limit": to_tick(trigger * (1 + buffer), up=True)})
    store = LiveStore(ctx.live)
    protect = store.create_trade_intent(
        deployment_id=ctx.deployment_id,
        action="protect",
        legs=[{k: (float(v) if isinstance(v, Decimal) else v) for k, v in s.items() if k != "price"} for s in stops],
        reason_text=f"Broker-side protective stops at {settings.protective_stop_multiplier:g}x the strategy stop "
        f"({stop_pct:g}% of credit) on {len(stops)} short leg(s).",
        market_snapshot=state["market"].summary(),
        thesis_ref=f"deployed_strategy:{state['snapshot'].id}",
        risk_checks={"protects": intent.id, "multiplier": settings.protective_stop_multiplier,
                     "stop_loss_pct_of_credit": stop_pct},  # fmt: skip
        decided_by=current_actor(),
        approval="auto",
        decided_at=ctx.clock(),
    )
    ctx.live.commit()
    manager = _manager(ctx, state)
    for s in stops:
        for qty in split_quantity(s["quantity"], manager.freeze_quantity(s), manager.lot_size):
            _, error = manager.place_stop(protect, s, qty, s["trigger"], s["limit"])
            if error:
                return error
    return None


def _cancel_stops(ctx: CrewContext, state: TickState, intent: TradeIntent) -> str | None:
    """Cancel the position's protective stops before the crew closes it. An error if one already filled."""
    store = LiveStore(ctx.live)
    manager = _manager(ctx, state)
    for protect in store.protect_intents(intent.id):
        for order in store.orders_for_intent(protect.id):
            manager.cancel(order)
            if store.fills_for_order(order.id):
                return f"protective stop {order.tradingsymbol} filled while the position was being closed"
    return None


def _broker_stop(state: TickState, fired: list[TradeIntent]) -> TickState:
    """A protective stop filled at the broker: close what is left of each position, book the trade as
    `broker_stop` and pause the deployment, since the crew's own stop did not act first."""
    ctx = state["ctx"]
    store = LiveStore(ctx.live)
    manager = _manager(ctx, state)
    exits = []
    trigger = Check("exit.broker_stop", "protective_stop_fills", ">", 0, 1, True)
    for intent in fired:
        legs = _open_legs(store, intent)
        stop_orders, stopped = [], {}
        for protect in store.protect_intents(intent.id):
            for order in store.orders_for_intent(protect.id):
                manager.cancel(order)
                if store.fills_for_order(order.id):
                    stop_orders.append(order)
            for leg in _filled_legs(store, protect):
                key = (leg["strike"], leg["option_type"], leg["expiry"])
                stopped[key] = stopped.get(key, 0) + leg["quantity"]
        stop_legs = [leg for p in store.protect_intents(intent.id) for leg in _filled_legs(store, p)]
        remaining = []
        for leg in legs:
            qty = leg["quantity"] - stopped.get((leg["strike"], leg["option_type"], leg["expiry"]), 0)
            if qty > 0:
                remaining.append(leg | {"quantity": qty})
        close_intent, close_orders = None, []
        if remaining:
            close_intent, close_orders, error = _send_close(ctx, state, remaining, trigger, [trigger],
                                                            require_started=False)  # fmt: skip
            if error:
                return {"stop": True, "outcome": "halted", "detail": error, "exits": exits}
        exit_legs = stop_legs + (_filled_legs(store, close_intent) if close_intent else [])
        close_intent_id = close_intent.id if close_intent else store.protect_intents(intent.id)[0].id
        trade = _book_trade(ctx, state, intent, legs, close_intent_id, exit_legs, stop_orders + close_orders,
                            trigger, partial=False)  # fmt: skip
        exits.append({"open_intent_id": intent.id, "live_trade_id": trade.id, "exit_reason": trade.exit_reason})
    reason = f"protective stop filled at the broker on {len(fired)} position(s)"
    if store.deployment_status(ctx.deployment_id) == "started":
        store.add_deployment_event(ctx.deployment_id, "paused", reason)
        ctx.live.commit()
    alerts.notify(ctx.settings, "protective_stop", ctx.deployment_id, reason)
    return {"stop": True, "outcome": "paused", "detail": reason, "exits": exits}


def _send_close(
    ctx: CrewContext, state: TickState, legs: list[dict], fired: Check, checks, require_started: bool = True
) -> tuple[TradeIntent | None, list[Order], str | None]:
    """Write a close intent for `legs` and send it (buy-backs first, retried). Halts on failure."""
    store, market = LiveStore(ctx.live), state["market"]
    close_legs = [
        leg | {
            "side": "BUY" if leg["side"] == "SELL" else "SELL",
            "quote": market.quote(leg["strike"], leg["option_type"], leg["expiry"]),
        }
        for leg in legs
    ]  # fmt: skip
    default = f"Exit: {_describe(fired)}."
    reason, model = _reason(ctx, default, {"action": "close", "trigger": _describe(fired)})
    if require_started and not _still_started(ctx):
        return None, [], "deployment stopped before the exit was sent"
    now = ctx.clock()
    close_intent = store.create_trade_intent(
        deployment_id=ctx.deployment_id,
        action="close",
        legs=[{k: v for k, v in leg.items() if k != "price"} for leg in close_legs],
        reason_text=reason,
        market_snapshot=market.summary(),
        thesis_ref=f"deployed_strategy:{state['snapshot'].id}",
        risk_checks={"exit_checks": [_check_dict(c) for c in checks], "triggered": fired.rule},
        decided_by=current_actor(),
        model=model,
        approval="auto",
        decided_at=now,
    )
    ctx.live.commit()
    close_orders, error = _send_orders(ctx, state, close_intent, close_legs, retries=ctx.settings.order_exit_retries)
    if error:
        halt(ctx, f"exit failed, position still open: {error}")
        return close_intent, close_orders, error
    return close_intent, close_orders, None


def _close_position(
    ctx: CrewContext,
    state: TickState,
    intent: TradeIntent,
    legs: list[dict],
    fired: Check,
    checks,
    partial=False,
    require_started: bool = True,
):
    """Close `legs` (all or, with `partial`, some of the position's open quantity). The trade carries
    the closed share of the opening costs and target. Protective stops are cancelled first and, after
    a partial close, placed again for what is left."""
    store = LiveStore(ctx.live)
    if store.protect_intents(intent.id):
        error = _cancel_stops(ctx, state, intent)
        if error:
            halt(ctx, f"{error}; reconcile the position by hand")
            return None, error
    close_intent, close_orders, error = _send_close(ctx, state, legs, fired, checks, require_started)
    if error:
        return None, error
    trade = _book_trade(ctx, state, intent, legs, close_intent.id, _filled_legs(store, close_intent), close_orders,
                        fired, partial)  # fmt: skip
    if partial and _stops_enabled(ctx):
        error = _place_protective_stops(ctx, state, intent, _open_legs(store, intent))
        if error:
            halt(ctx, f"protective stops could not be placed again after a partial close: {error}")
    _write_lesson(ctx, state, intent, trade)
    return trade, None


def _book_trade(
    ctx: CrewContext,
    state: TickState,
    intent: TradeIntent,
    legs: list[dict],
    close_intent_id: str,
    exit_legs: list[dict],
    close_orders: list[Order],
    fired: Check,
    partial: bool,
):
    store = LiveStore(ctx.live)
    gross = sum(signed(leg["side"]) * leg["price"] * leg["quantity"] for leg in legs + exit_legs)
    expected = sum(signed(leg["side"]) * leg["quote"] * leg["quantity"] for leg in legs + exit_legs)
    opened = sum(leg["quantity"] for leg in _filled_legs(store, intent))
    share = Decimal(sum(leg["quantity"] for leg in legs)) / Decimal(opened)
    open_est, open_actual = _costs(store, store.orders_for_intent(intent.id))
    close_est, close_actual = _costs(store, close_orders)
    est_costs = money(open_est * share + close_est)
    actual_costs = None
    if open_actual is not None or close_actual is not None:
        actual_costs = money((open_actual or Decimal("0")) * share + (close_actual or Decimal("0")))
    net = money(gross) - (actual_costs if actual_costs is not None else est_costs)
    closed_at = ctx.clock()
    target = money((intent.target_pnl or Decimal("0")) * share)
    totals: dict[tuple, list[float]] = {}
    for leg in exit_legs:
        t = totals.setdefault((leg["strike"], leg["option_type"]), [0.0, 0])
        t[0] += leg["price"] * leg["quantity"]
        t[1] += leg["quantity"]
    exit_prices = {k: cost / qty for k, (cost, qty) in totals.items()}
    trade = store.close_live_trade(
        deployment_id=ctx.deployment_id,
        open_intent_id=intent.id,
        close_intent_id=close_intent_id,
        legs=[
            {k: v for k, v in leg.items() if k not in ("price", "quote")}
            | {"entry_price": leg["price"], "exit_price": exit_prices[(leg["strike"], leg["option_type"])]}
            for leg in legs
        ],
        opened_at=intent.decided_at,
        closed_at=closed_at,
        holding_minutes=int((closed_at - intent.decided_at).total_seconds() // 60),
        exit_rule=_describe(fired),
        exit_reason=fired.rule.removeprefix("exit."),
        gross=money(gross),
        est_costs=est_costs,
        actual_costs=actual_costs,
        net=net,
        slippage_vs_expected=money(gross - expected),
        pnl_vs_target=net - target,
        outcome_label=outcome_label(float(net), float(target)),
        partial=partial,
    )
    ctx.live.commit()
    return trade


def _write_lesson(ctx: CrewContext, state: TickState, intent: TradeIntent, trade) -> None:
    """With opinions enabled, an LLM reviews the closed trade; its lesson feeds later opinions."""
    if opinion_mode(ctx, state["deployment"]) == "off":
        return
    store = LiveStore(ctx.live)
    facts = trade_facts(trade, intent, store.opinion_for_intent(intent.id))
    trade_id = trade.id
    ctx.live.rollback()  # release the SQLite write lock while the LLM runs
    review = write_lesson(ctx.settings, facts)
    if review is None:
        return
    store.add_agent_review(trade_id, review, f"llm:{ctx.settings.opinion_model}")
    ctx.live.commit()


def _exits(state: TickState) -> TickState:
    """The strategy's exits first; only when none fires, the trade policy's time and booking exits."""
    ctx, rules, market = state["ctx"], state["rules"], state["market"]
    store = LiveStore(ctx.live)
    policy = trade_policy.policy_from_limits(state["deployment"].risk_limits)
    latest = store.latest_position_snapshot(ctx.deployment_id)
    peaks = {p["open_intent_id"]: p.get("peak_pct") for p in latest.positions} if latest else {}
    exits: list[dict] = []
    still_open: list[dict] = []
    for intent in store.open_intents(ctx.deployment_id):
        legs = _open_legs(store, intent)
        if not legs:
            continue
        mark = mark_position(legs, market)
        peak = trade_policy.update_peak(peaks.get(intent.id), mark)
        if mark is None:
            exits.append({"open_intent_id": intent.id, "error": "missing quotes for an open leg; exit not checked"})
            still_open.append(_position_row(intent, legs, None, peak))
            continue
        checks = exit_checks(rules, mark, _frozen(intent, _open_share(state, store, intent, legs)))
        fired, close_lots = fired_exit(checks), None
        if fired is None and policy is not None:
            booking = _booking_exit(state, policy, intent, legs, mark, peak)
            if booking is not None:
                fired, close_lots, checks = booking.check, booking.close_lots, [*checks, booking.check]
        if fired is None:
            still_open.append(_position_row(intent, legs, mark, peak))
            continue
        to_close = legs
        if close_lots is not None:
            open_lots = _lots(state, intent, legs)
            to_close = [leg | {"quantity": leg["quantity"] * close_lots // open_lots} for leg in legs]
        trade, error = _close_position(ctx, state, intent, to_close, fired, checks, partial=close_lots is not None)
        if error:
            return {"stop": True, "outcome": "halted", "detail": error, "exits": exits}
        exits.append(
            {"open_intent_id": intent.id, "live_trade_id": trade.id, "exit_reason": trade.exit_reason}
            | ({"partial": True, "lots": close_lots} if close_lots is not None else {})
        )
        if close_lots is not None:
            remaining = _open_legs(store, intent)
            still_open.append(_position_row(intent, remaining, mark_position(remaining, market), peak))
    _snapshot_positions(ctx, state, still_open)
    phantoms = [p for op, intent in store.open_phantoms(ctx.deployment_id) if (p := _track_phantom(state, op, intent))]
    return {"exits": exits, "phantoms": phantoms, "open_positions": len(store.open_intents(ctx.deployment_id))}


def _frozen(intent: TradeIntent, share: float) -> tuple[float, float] | None:
    """The intent's rupee target and stop from entry, scaled to the share of the position still open."""
    frozen = frozen_exits(intent.target_pnl, intent.stop_loss_pnl)
    return (frozen[0] * share, frozen[1] * share) if frozen else None


def _open_share(state: TickState, store: LiveStore, intent: TradeIntent, legs: list[dict]) -> float:
    opened = _lots(state, intent, _filled_legs(store, intent))
    return _lots(state, intent, legs) / opened if opened else 1.0


def _lot_size(state: TickState, intent: TradeIntent) -> int | None:
    recorded = (intent.risk_checks or {}).get("lot_size")
    return recorded or lot_size_for(state["rules"], state["market"], state["ctx"].settings)


def _lots(state: TickState, intent: TradeIntent, legs: list[dict]) -> int:
    lot_size = _lot_size(state, intent)
    return legs[0]["quantity"] // lot_size if lot_size else 1


def _booking_exit(state: TickState, policy, intent: TradeIntent, legs: list[dict], mark, peak):
    ctx, store = state["ctx"], LiveStore(state["ctx"].live)
    return trade_policy.booking_exit(
        policy,
        mark,
        peak_pct=peak,
        partials_done=len(store.partial_trades(intent.id)),
        lots_opened=_lots(state, intent, _filled_legs(store, intent)),
        lots_open=_lots(state, intent, legs),
        now=ctx.clock(),
        events=state["market"].events,
    )


def _position_row(intent: TradeIntent, legs: list[dict], mark, peak_pct: float | None = None) -> dict:
    """One open position as marked this tick. `pnl` is None when a leg had no quote."""
    quotes = mark.quotes if mark else [None] * len(legs)
    return {
        "open_intent_id": intent.id,
        "opened_at": intent.decided_at,
        "pnl": round(mark.pnl, 4) if mark else None,
        "credit": round(mark.credit, 4) if mark else None,
        "peak_pct": peak_pct,
        "dte": mark.dte if mark else None,
        "target_pnl": float(intent.target_pnl) if intent.target_pnl is not None else None,
        "stop_loss_pnl": float(intent.stop_loss_pnl) if intent.stop_loss_pnl is not None else None,
        "legs": [
            {k: leg[k] for k in ("side", "option_type", "strike", "expiry", "tradingsymbol", "quantity")}
            | {"entry_price": leg["price"], "quote": q}
            for leg, q in zip(legs, quotes, strict=True)
        ],
    }


def _snapshot_positions(ctx: CrewContext, state: TickState, positions: list[dict]) -> None:
    if not positions:
        return
    mtm = sum(p["pnl"] for p in positions if p["pnl"] is not None)
    LiveStore(ctx.live).snapshot_positions(
        broker_account_id=state["deployment"].broker_account_id,
        deployment_id=ctx.deployment_id,
        taken_at=ctx.clock(),
        positions=positions,
        mtm=money(mtm),
    )
    ctx.live.commit()


def phantom_legs(opinion: TradeOpinion, intent: TradeIntent) -> list[dict]:
    """The lots the veto removed, entered at the quotes the rules would have traded at."""
    removed = opinion.rules_lots - opinion.final_lots
    sent = opinion.rules_lots if opinion.final_lots == 0 else opinion.final_lots  # lots in intent.legs
    return [leg | {"quantity": leg["quantity"] * removed // sent, "price": leg["quote"]} for leg in intent.legs]


def _track_phantom(state: TickState, opinion: TradeOpinion, intent: TradeIntent) -> dict | None:
    """Check a phantom position against the exit rules like a real one; record it when one fires. No orders."""
    ctx, market = state["ctx"], state["market"]
    legs = phantom_legs(opinion, intent)
    mark = mark_position(legs, market)
    if mark is None:
        return None
    removed = opinion.rules_lots - opinion.final_lots
    sent = opinion.rules_lots if opinion.final_lots == 0 else opinion.final_lots
    fired = fired_exit(exit_checks(state["rules"], mark, _frozen(intent, removed / sent if sent else 1.0)))
    if fired is None:
        return None
    now, profile = ctx.clock(), state["profile"]
    entry_costs = leg_costs(
        ((leg["side"], leg["price"], leg["quantity"]) for leg in legs), profile, intent.decided_at.date().isoformat()
    )
    exits = [("BUY" if leg["side"] == "SELL" else "SELL", q, leg["quantity"]) for leg, q in zip(legs, mark.quotes)]
    exit_costs = leg_costs(exits, profile, now.date().isoformat())
    gross, est_costs = money(mark.pnl), money(entry_costs.total + exit_costs.total)
    phantom = LiveStore(ctx.live).record_phantom_trade(
        deployment_id=ctx.deployment_id,
        trade_intent_id=intent.id,
        trade_opinion_id=opinion.id,
        kind="vetoed" if opinion.final_lots == 0 else "reduced",
        lots=opinion.rules_lots - opinion.final_lots,
        legs=[
            {k: v for k, v in leg.items() if k not in ("price", "quote")}
            | {"entry_price": leg["price"], "exit_price": q}
            for leg, q in zip(legs, mark.quotes, strict=True)
        ],
        opened_at=intent.decided_at,
        closed_at=now,
        exit_reason=fired.rule.removeprefix("exit."),
        gross=gross,
        est_costs=est_costs,
        net=gross - est_costs,
    )
    ctx.live.commit()
    return {"trade_intent_id": intent.id, "phantom_trade_id": phantom.id, "exit_reason": phantom.exit_reason}


def _drawdown(state: TickState) -> TickState:
    """Measure drawdown from peak equity and apply the ladder, then the floor. Runs after exits, so it
    never blocks one."""
    ctx, deployment = state["ctx"], state["deployment"]
    policy = drawdown.policy_for(deployment)
    ladder = drawdown.effective_ladder(policy, ctx.settings.max_drawdown_pct)
    store = LiveStore(ctx.live)
    floor = money_rules.floor_state(store, deployment)
    if not ladder and floor is None:
        return {}
    eq = equity_state(store, ctx.settings, [deployment], include_unrealized=policy is None or policy.basis == "mtm")
    if not ladder:
        return _floor(state, floor, eq, {"equity": eq})
    decision = drawdown.decide(
        ladder, policy.restore_below_pct if policy else None, eq.drawdown_pct, store.drawdown_level(deployment.id)
    )
    if decision.level != decision.previous:
        store.record_drawdown_event(
            deployment_id=deployment.id,
            level=decision.level,
            action=decision.action,
            at_pct=decision.at_pct,
            drawdown_pct=decision.drawdown_pct,
            drawdown_inr=money(eq.drawdown_inr),
            equity=money(eq.equity_inr),
            peak=money(eq.peak_inr),
            detail=f"level {decision.previous} -> {decision.level} at {decision.drawdown_pct:.2f}% drawdown",
        )
        ctx.live.commit()
    if decision.pause is not None:
        return _drawdown_pause(state, decision)
    out = {"equity": eq, "ladder": decision}
    return _floor(state, floor, eq, out) if floor is not None else out


def _floor(state: TickState, fs: money_rules.FloorState, eq: EquityState, out: TickState) -> TickState:
    """Raise the floor once if equity reached the raise level; pause if equity fell below it."""
    ctx = state["ctx"]
    store, equity = LiveStore(ctx.live), eq.equity_inr
    if drawdown.floor_raise_due(fs.floor, fs.raised, equity):
        reason = (
            f"equity ₹{equity:,.0f} reached ₹{fs.floor.raise_once.at_equity_inr:,.0f}: floor raised from "
            f"₹{fs.floor_inr:,.0f} to ₹{fs.floor.raise_once.to_inr:,.0f}"
        )
        store.add_deployment_event(ctx.deployment_id, money_rules.FLOOR_RAISED, reason)
        ctx.live.commit()
        alerts.notify(ctx.settings, money_rules.FLOOR_RAISED, ctx.deployment_id, reason)
        fs = replace(fs, raised=True, floor_inr=fs.floor.raise_once.to_inr)
    if not drawdown.floor_breached(fs.floor_inr, equity):
        return out | {"floor_inr": fs.floor_inr}
    reason = f"equity ₹{equity:,.0f} fell below the ₹{fs.floor_inr:,.0f} floor"
    if fs.floor.resume_after_days:
        reason += (
            f"; break of {fs.floor.resume_after_days} trading day(s), then it starts again by itself if equity "
            f"is above ₹{fs.floor.floor_inr:,.0f}"
        )
    exits = list(state.get("exits", []))
    if fs.floor.close_open_positions:
        trigger = Check(drawdown.FLOOR_CHECK, "equity_inr", "<", fs.floor_inr, round(equity, 2), True)
        error = _close_all(state, trigger, exits)
        if error:
            return {"stop": True, "outcome": "halted", "detail": error, "exits": exits}
    if store.deployment_status(ctx.deployment_id) == "started":
        store.add_deployment_event(ctx.deployment_id, "paused", reason)
        store.add_deployment_event(ctx.deployment_id, money_rules.FLOOR_PAUSED, reason)
        ctx.live.commit()
        alerts.notify(ctx.settings, "paused", ctx.deployment_id, reason)
    return {"stop": True, "outcome": "paused", "detail": reason, "exits": exits}


def _drawdown_pause(state: TickState, decision: drawdown.LadderDecision) -> TickState:
    ctx, level = state["ctx"], decision.pause
    store = LiveStore(ctx.live)
    reason = f"drawdown {decision.drawdown_pct:.2f}% reached the {level.at_pct:g}% pause level"
    exits = list(state.get("exits", []))
    if level.close_open_positions:
        trigger = Check("drawdown.pause", "drawdown_pct", ">=", level.at_pct, decision.drawdown_pct, True)
        error = _close_all(state, trigger, exits)
        if error:
            return {"stop": True, "outcome": "halted", "detail": error, "exits": exits}
    if store.deployment_status(ctx.deployment_id) == "started":
        store.add_deployment_event(ctx.deployment_id, "paused", reason)
        ctx.live.commit()
        alerts.notify(ctx.settings, "paused", ctx.deployment_id, reason)
    return {"stop": True, "outcome": "paused", "detail": reason, "exits": exits, "ladder": decision}


def _close_all(state: TickState, trigger: Check, exits: list[dict]) -> str | None:
    """Close every open position on `trigger`, appending to `exits`. Returns an error if an exit failed."""
    ctx = state["ctx"]
    store = LiveStore(ctx.live)
    for intent in store.open_intents(ctx.deployment_id):
        legs = _open_legs(store, intent)
        if not legs:
            continue
        if mark_position(legs, state["market"]) is None:
            exits.append({"open_intent_id": intent.id, "error": f"missing quotes; not closed on {trigger.rule}"})
            continue
        trade, error = _close_position(ctx, state, intent, legs, trigger, [trigger])
        if error:
            return error
        exits.append({"open_intent_id": intent.id, "live_trade_id": trade.id, "exit_reason": trade.exit_reason})
    return None


def _daily(state: TickState) -> TickState:
    """The policy's daily profit target: once today's P&L reaches it, block new entries (and with
    on_hit=close_all, close every open position). Runs after exits, so it never blocks one. Also
    alerts when the withdrawal rule says money is due."""
    ctx, deployment = state["ctx"], state["deployment"]
    money_rules.notify_withdrawal_due(LiveStore(ctx.live), ctx.settings, deployment)
    policy = trade_policy.policy_from_limits(deployment.risk_limits)
    target = policy.daily_target if policy else None
    if target is None:
        return {}
    check = trade_policy.daily_target_check(target, day_pnl(LiveStore(ctx.live), ctx.settings, deployment, ctx.clock()))
    if check.passed or target.on_hit == "stop_new_entries":
        return {"daily_check": check}
    exits = list(state.get("exits", []))
    error = _close_all(state, replace(check, op=">=", passed=True), exits)
    if error:
        return {"stop": True, "outcome": "halted", "detail": error, "exits": exits}
    return {"daily_check": check, "exits": exits}


def _entry(state: TickState) -> TickState:
    ctx, rules, market, deployment = state["ctx"], state["rules"], state["market"], state["deployment"]
    closed = [e for e in state.get("exits", []) if "live_trade_id" in e]
    if closed:
        return {"stop": True, "outcome": "exited", "detail": f"closed {len(closed)} position(s)"}
    if state["open_positions"] >= rules.risk.max_open_positions:
        return {"stop": True, "outcome": "holding", "detail": f"{state['open_positions']} open position(s)"}
    checks = check_entry(rules, market)
    if not all(c.passed for c in checks):
        return {"stop": True, "outcome": "no_signal", "entry_checks": checks}

    max_lots = proposal_lots(rules, deployment.risk_limits.get("max_lots"))
    in_force = capital_in_force(LiveStore(ctx.live), ctx.settings, deployment)
    try:
        proposal = build_entry_proposal(rules, market, ctx.settings, max_lots, in_force)
    except MissingMarketConfig as exc:
        halt(ctx, str(exc))
        return {"stop": True, "outcome": "halted", "detail": "missing lot size or strike step"}
    lots, sizing_check = max_lots, None
    policy = trade_policy.policy_from_limits(deployment.risk_limits)
    if policy is not None and policy.sizing is not None:
        store = LiveStore(ctx.live)
        realized = sum(float(t.net) for t in store.closed_trades([deployment.id]))
        capital = trade_policy.sizing_capital(policy, capital_in_force(store, ctx.settings, deployment), realized)
        economics = proposal["economics"]
        per_lot = economics.max_loss / max_lots if economics else None
        lots, sizing_check = trade_policy.size_lots(policy.sizing, max_lots, capital, per_lot)
    unscaled = lots
    ladder = state.get("ladder")
    vix_factor = trade_policy.vix_size_factor(policy.vix_sizing, market.vix) if policy is not None else 1.0
    if lots:
        lots = drawdown.scaled_lots(lots, (ladder.size_factor if ladder else 1.0) * vix_factor)
    lots = max(lots, 1)  # a sizing check that failed still records the entry, as blocked
    if lots != proposal["lots"]:
        proposal = resize_proposal(rules, proposal, lots)
    out: TickState = {
        "entry_checks": checks,
        "proposal": proposal,
        "max_lots": max_lots,
        "sizing_check": sizing_check,
        "unscaled_lots": unscaled,
        "vix_factor": vix_factor,
    }
    gate = policy.odds_gate if policy is not None else None
    if gate is not None:
        snapshot = state["snapshot"]
        trades = live_odds.history(
            deployment.id, snapshot.rules_sha256, rules, state["profile"], ctx.settings, market.as_of
        )
        out["odds"] = odds_gate.odds_for(trades, market.as_of, market.vix, gate)
    return out


def _risk(state: TickState) -> TickState:
    checks = _risk_checks(state, state["proposal"])
    approval = "auto" if all(c.passed for c in checks) else "blocked"
    return {"risk_checks": checks, "approval": approval}


def _risk_checks(state: TickState, proposal: dict) -> list[Check]:
    ctx, deployment = state["ctx"], state["deployment"]
    rules = with_common_risk(state["rules"], ctx.settings.common_risk)
    store = LiveStore(ctx.live)
    now = ctx.clock()
    today = now.date()
    portfolio_open = sum(len(store.open_intents(d.id)) for d in store.active_deployments())
    closed_today = store.trades_closed_on(today, ctx.deployment_id)
    entries = [i.decided_at for i in store.intents(ctx.deployment_id) if i.action == "open" and i.approval == "auto"]
    last_trade = max(entries + [t.closed_at for t in store.closed_trades([ctx.deployment_id])], default=None)
    account = AccountState(
        open_positions=state["open_positions"],
        pnl_today=float(sum(t.net for t in closed_today)),
        portfolio_open_positions=portfolio_open,
        portfolio_pnl_today=float(sum(t.net for t in store.trades_closed_on(today))),
        entries_today=sum(1 for at in entries if at.date() == today),
        cooldown_days_left=_cooldown_days_left(ctx, rules, store, now),
        losses_today=sum(1 for t in closed_today if not t.partial and t.net < 0),
        minutes_since_last_trade=(now - last_trade).total_seconds() / 60 if last_trade else None,
    )
    raw_limits = deployment.risk_limits
    limits = RiskLimits(
        capital_inr=capital_in_force(store, ctx.settings, deployment),
        max_lots=raw_limits.get("max_lots"),
        max_daily_loss_pct=raw_limits.get("max_daily_loss_pct"),
        portfolio_max_open_positions=ctx.settings.portfolio_max_open_positions,
        portfolio_max_daily_loss_inr=ctx.settings.portfolio_max_daily_loss_inr,
    )
    checks = entry_risk_checks(rules, limits, account, proposal)
    ladder = state.get("ladder")
    if ladder is not None and ladder.check is not None:
        checks.append(ladder.check)
    if state.get("floor_inr") is not None:
        economics = proposal["economics"]
        max_loss = economics.max_loss if economics else None
        checks += drawdown.floor_checks(state["floor_inr"], state["equity"].equity_inr, max_loss)
    checks += [c for c in (state.get("sizing_check"), state.get("daily_check")) if c is not None]
    if state.get("odds") is not None:
        checks += odds_gate.odds_checks(state["odds"], live_odds.gate_for(deployment))
    margin = _margin_check(ctx, proposal)
    if margin is not None:
        checks.append(margin)
    return checks


def _cooldown_days_left(ctx: CrewContext, rules: StrategyRules, store: LiveStore, now: datetime) -> int:
    if rules.risk.max_consecutive_losses is None:
        return 0
    today = now.astimezone(IST).date().isoformat()
    closed = [
        (t.closed_at.astimezone(IST).date().isoformat(), float(t.net))
        for t in store.closed_trades([ctx.deployment_id]) if not t.partial
    ]  # fmt: skip
    return losing_streak_cooldown(rules.risk, closed, lambda day: trading_days_between(day, today, ctx.settings))


def _margin_check(ctx: CrewContext, proposal: dict) -> Check | None:
    """Available margin must cover the basket's margin times MARGIN_BUFFER. None when the adapter does not
    check margin (paper); a broker that cannot answer fails the check."""
    if not hasattr(ctx.adapter, "available_margin"):
        return None
    try:
        available = ctx.adapter.available_margin()
        if available is None:
            return None
        requests = [
            OrderRequest(f"margin-{i}", EXCHANGE, leg["tradingsymbol"], leg["side"], leg["quantity"], "LIMIT", PRODUCT,
                         money(leg["quote"]) if leg["quote"] is not None else None)  # fmt: skip
            for i, leg in enumerate(proposal["legs"])
        ]
        required = ctx.adapter.margin_required(requests)
    except Exception:
        return Check("risk.margin", "available_margin", ">=", None, None, False)
    if required is None:
        return Check("risk.margin", "available_margin", ">=", None, float(available), False)
    threshold = float(required) * ctx.settings.margin_buffer
    return Check("risk.margin", "available_margin", ">=", threshold, float(available), float(available) >= threshold)


def opinion_mode(ctx: CrewContext, deployment: Deployment) -> str:
    """off | shadow | veto for this deployment on this tick."""
    if ctx.settings.llm_trade_opinion == "off" or not ctx.settings.llm_enabled:
        return "off"
    if veto.blocker(ctx.settings, deployment) is None and LiveStore(ctx.live).veto_enabled(deployment.id):
        return "veto"
    return "shadow"


def _opinion(state: TickState) -> TickState:
    """Ask the LLM about an entry the rules approved. Runs after risk, so it can never unblock anything."""
    if state["approval"] != "auto":
        return {}
    ctx = state["ctx"]
    if ctx.settings.llm_trade_opinion == "veto":
        veto.auto_revert(ctx.live, ctx.settings, ctx.deployment_id)
    mode = opinion_mode(ctx, state["deployment"])
    if mode == "off":
        return {}
    store = LiveStore(ctx.live)
    lessons = [(r.id, r.lesson) for r in store.recent_lessons(ctx.deployment_id, ctx.settings.llm_opinion_lessons)]
    context = ContextProvider(ctx.settings).context(state["rules"].instrument.underlying, ctx.clock(), state["market"])
    ctx.live.rollback()  # release the SQLite write lock while the LLM runs
    result = get_opinion(
        ctx.settings, context, state["proposal"], state["entry_checks"], state["risk_checks"], lessons,
        parse_strategy_markdown(state["snapshot"].markdown),
    )  # fmt: skip
    rules_lots = state["proposal"]["lots"]
    update: TickState = {"opinion": result, "opinion_mode": mode, "rules_lots": rules_lots}
    if mode == "veto" and result.status == "ok" and result.lots is not None:
        update |= _clamp(state, min(result.lots, rules_lots))
    return update


def _clamp(state: TickState, lots: int) -> TickState:
    """Apply a veto-mode opinion. Lots only go down, and a reduced proposal passes the risk checks again."""
    if lots >= state["proposal"]["lots"]:
        return {}
    if lots <= 0:
        return {"approval": "vetoed"}
    proposal = resize_proposal(state["rules"], state["proposal"], lots)
    checks = _risk_checks(state, proposal)
    if not all(c.passed for c in checks):
        return {"approval": "vetoed"}  # a reduction the risk checks reject is not traded at all
    return {"proposal": proposal, "risk_checks": checks}


def _final_lots(state: TickState) -> int:
    return 0 if state["approval"] == "vetoed" else state["proposal"]["lots"]


def _record_opinion(store: LiveStore, state: TickState, intent_id: str | None, final_lots: int) -> None:
    result = state.get("opinion")
    if result is None:
        return
    rules_lots = state["rules_lots"]
    out = result.output
    store.add_trade_opinion(
        deployment_id=state["ctx"].deployment_id,
        trade_intent_id=intent_id,
        mode=state["opinion_mode"],
        status=result.status,
        decision=result.decision,
        lots=result.lots,
        rules_lots=rules_lots,
        final_lots=final_lots,
        applied=final_lots != rules_lots,
        reasons=out.reasons if out else [],
        risk_flags=out.risk_flags if out else [],
        confidence=out.confidence if out else None,
        model=result.model,
        prompt_sha256=result.prompt_sha256,
        context=result.context,
        lesson_review_ids=result.lesson_review_ids,
        error=result.error,
        latency_ms=result.latency_ms,
    )


def _explain(state: TickState) -> TickState:
    entry = "; ".join(_describe(c) for c in state["entry_checks"])
    failed = [c for c in state["risk_checks"] if not c.passed]
    opinion = state.get("opinion")
    reasons = opinion.output.reasons if opinion and opinion.output else []
    why = "; ".join(reasons) or "no reason given"
    if failed:
        default = f"Entry signal ({entry}) blocked by {', '.join(_describe(c) for c in failed)}."
    elif state["approval"] == "vetoed":
        default = f"Entry signal ({entry}) passed all risk checks but was vetoed by the LLM opinion: {why}."
    else:
        default = f"Entry signal: {entry}. All risk checks passed."
        if state.get("sizing_check") is not None and state["unscaled_lots"] < state["max_lots"]:
            default += f" Position sizing allows {state['unscaled_lots']} of {state['max_lots']} approved lots."
        ladder, rules_lots = state.get("ladder"), state.get("rules_lots", state["proposal"]["lots"])
        if rules_lots < state.get("unscaled_lots", rules_lots):
            cuts = []
            if ladder is not None and ladder.size_factor < 1:
                cuts.append(f"Drawdown level {ladder.level} ({ladder.drawdown_pct:.2f}% from peak)")
            if state.get("vix_factor", 1.0) < 1:
                vix = state["market"].vix
                cuts.append(f"VIX sizing ({'VIX ' + format(vix, '.2f') if vix is not None else 'no VIX reading'})")
            default += f" {' and '.join(cuts)} cut {state['unscaled_lots']} to {rules_lots} lots."
        if state.get("rules_lots", state["proposal"]["lots"]) != state["proposal"]["lots"]:
            default += f" The LLM opinion reduced {state['rules_lots']} to {state['proposal']['lots']} lots: {why}."
    if state.get("odds") is not None:
        default += f" Odds: {odds_gate.odds_text(state['odds'])}."
    facts = {
        "action": "blocked" if failed else "vetoed" if state["approval"] == "vetoed" else "open",
        "entry_checks": [_describe(c) for c in state["entry_checks"]],
        "failed_risk_checks": [_describe(c) for c in failed],
        "legs": [{k: leg[k] for k in ("side", "option_type", "strike")} for leg in state["proposal"]["legs"]],
    }
    text, model = _reason(state["ctx"], default, facts)
    return {"reason_text": text, "model": model}


def _execute(state: TickState) -> TickState:
    ctx, proposal, economics = state["ctx"], state["proposal"], state["proposal"]["economics"]
    store = LiveStore(ctx.live)
    if not _still_started(ctx):
        _record_opinion(store, state, None, _final_lots(state))
        ctx.live.commit()
        return {"stop": True, "outcome": "idle", "detail": "deployment stopped before the entry was sent"}
    entry_checks, risk_checks = state["entry_checks"], state["risk_checks"]
    failed = [c.rule for c in risk_checks if not c.passed]
    intent = store.create_trade_intent(
        deployment_id=ctx.deployment_id,
        action="open",
        legs=proposal["legs"],
        entry_rule=entry_rule_text(entry_checks),
        entry_conditions=[_check_dict(c) for c in entry_checks],
        reason_text=state["reason_text"],
        market_snapshot=state["market"].summary(),
        thesis_ref=f"deployed_strategy:{state['snapshot'].id}",
        target_pnl=money(economics.target_pnl) if economics else None,
        stop_loss_pnl=money(economics.stop_loss_pnl) if economics else None,
        max_loss=money(economics.max_loss) if economics else None,
        risk_reward=round(economics.risk_reward, 4) if economics else None,
        risk_checks={"checks": [_check_dict(c) for c in risk_checks], "failed": failed}
        | {"lot_size": proposal["lot_size"]}
        | ({"drawdown": state["ladder"].summary()} if state.get("ladder") else {}),
        decided_by=current_actor(),
        model=state.get("model"),
        approval=state["approval"],
        decided_at=ctx.clock(),
    )
    _record_opinion(store, state, intent.id, _final_lots(state))
    ctx.live.commit()
    if state["approval"] == "vetoed":
        return {"outcome": "vetoed", "detail": f"LLM opinion: {state['opinion'].decision}", "intent_id": intent.id}
    if failed:
        return {"outcome": "blocked", "detail": ", ".join(failed), "intent_id": intent.id}
    _, error = _send_orders(ctx, state, intent, proposal["legs"])
    if error is None:
        stop_error = _place_protective_stops(ctx, state, intent, _filled_legs(store, intent))
        if stop_error is None:
            return {"outcome": "entered", "intent_id": intent.id}
        error = stop_error
    unwound = _unwind(state, intent)
    halt(ctx, f"entry failed: {error}" + (f"; {unwound}" if unwound else ""))
    return {"outcome": "halted", "detail": error, "intent_id": intent.id}


def _unwind(state: TickState, intent: TradeIntent) -> str | None:
    """Close whatever an entry that failed part-way did fill, so no leg is left without its partner."""
    ctx = state["ctx"]
    store = LiveStore(ctx.live)
    legs = _filled_legs(store, intent)
    if not legs:
        return None
    trigger = Check("exit.unwind", "failed_entry", "==", 1, 1, True)
    trade, error = _close_position(ctx, state, intent, legs, trigger, [trigger], require_started=False)
    if error:
        return f"unwind failed, {len(legs)} filled leg(s) still open: {error}"
    return f"unwound {len(legs)} filled leg(s) as live trade {trade.id}"


def flatten(ctx: CrewContext) -> list[dict]:
    """Kill switch: close every open position now (market-protected limits, re-priced within MAX_SLIPPAGE_BPS).
    Raises ValueError if the market cannot be observed or an exit fails."""
    state: TickState = {"ctx": ctx}
    state |= _verify(state)
    if state.get("stop"):
        raise ValueError(f"cannot flatten: {state.get('detail')}")
    state |= _observe(state)
    if state.get("stop"):
        raise ValueError(f"cannot flatten: {state.get('detail')}")
    store = LiveStore(ctx.live)
    trigger = Check("exit.kill_switch", "flatten", "==", 1, 1, True)
    exits = []
    for intent in store.open_intents(ctx.deployment_id):
        legs = _open_legs(store, intent)
        if not legs:
            continue
        if mark_position(legs, state["market"]) is None:
            raise ValueError(f"cannot flatten: missing quotes for a leg of intent {intent.id}")
        trade, error = _close_position(ctx, state, intent, legs, trigger, [trigger], require_started=False)
        if error:
            raise ValueError(f"flatten failed: {error}")
        exits.append({"open_intent_id": intent.id, "live_trade_id": trade.id, "exit_reason": trade.exit_reason})
    return exits


def _next(node: str):
    return lambda state: END if state.get("stop") else node


def build_trade_graph():
    graph = StateGraph(TickState)
    for name, fn in (
        ("verify", _verify),
        ("gate", _gate),
        ("hours", _hours),
        ("observe", _observe),
        ("reconcile", _reconcile),
        ("exits", _exits),
        ("drawdown", _drawdown),
        ("daily", _daily),
        ("entry", _entry),
        ("risk", _risk),
        ("opinion", _opinion),
        ("explain", _explain),
        ("execute", _execute),
    ):
        graph.add_node(name, fn)
    graph.add_edge(START, "verify")
    graph.add_conditional_edges("verify", _next("gate"))
    graph.add_conditional_edges("gate", _next("hours"))
    graph.add_conditional_edges("hours", _next("observe"))
    graph.add_conditional_edges("observe", _next("reconcile"))
    graph.add_conditional_edges("reconcile", _next("exits"))
    graph.add_conditional_edges("exits", _next("drawdown"))
    graph.add_conditional_edges("drawdown", _next("daily"))
    graph.add_conditional_edges("daily", _next("entry"))
    graph.add_conditional_edges("entry", _next("risk"))
    graph.add_edge("risk", "opinion")
    graph.add_edge("opinion", "explain")
    graph.add_edge("explain", "execute")
    graph.add_edge("execute", END)
    return graph.compile()


_GRAPH = None


def run_tick(ctx: CrewContext) -> dict[str, Any]:
    """One pass of the crew. Returns a summary; every write is already committed."""
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_trade_graph()
    with audit_context(f"trade_crew/{ctx.deployment_id}"):
        try:
            out = _GRAPH.invoke({"ctx": ctx})
        finally:
            ctx.live.rollback()
        try:
            LiveStore(ctx.live).record_heartbeat(
                deployment_id=ctx.deployment_id, at=ctx.clock(), feed=ctx.feed_kind,
                outcome=out.get("outcome"), pid=os.getpid(),
            )  # fmt: skip
            ctx.live.commit()
        except Exception:
            ctx.live.rollback()
    return {
        "outcome": out.get("outcome"),
        "detail": out.get("detail"),
        "exits": out.get("exits", []),
        "phantoms": out.get("phantoms", []),
        "intent_id": out.get("intent_id"),
        "approval": out.get("approval"),
        "opinion": out["opinion"].decision if out.get("opinion") else None,
        "drawdown": out["ladder"].summary() if out.get("ladder") else None,
    }
