"""The Trade Crew: one per deployment, deciding only through the strategy's rule checks.

Each tick: verify the frozen snapshot, gate on `started`, observe the market, check open
positions against the exit rules and then the trade policy's booking and time exits (see
`app.trading.policy`) and snapshot the ones still open, apply the drawdown ladder (see
`app.live.drawdown`) and the daily profit target, then evaluate entry, sizing and risk. An
optional LLM writes the reason text after a decision is made; it is never an input to one.

With LLM_TRADE_OPINION set, an LLM also gives an opinion (take, reduce or skip) on entries the
rules approved. In shadow mode it is only recorded. In veto mode (enabled per deployment, see
`app.live.veto`) a skip records a `vetoed` intent with no orders and a reduction sends fewer lots
after the risk checks run again; the removed lots are tracked as phantom positions until an exit
rule fires. Exits are never sent to the LLM.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.trade_crew.legs import signed
from app.agents.trade_crew.lessons import trade_facts, write_lesson
from app.agents.trade_crew.opinion import OpinionResult, get_opinion
from app.audit import audit_context, current_actor
from app.brokers.base import BrokerAdapter, OrderRequest
from app.config import Settings
from app.db.hashing import money, sha256_hex
from app.db.models import BrokerCharge, DeployedStrategy, Deployment, Order, TradeIntent, TradeOpinion
from app.db.types import utcnow
from app.engine.costs import CostProfile, cost_breakdown, leg_costs, resolve_cost_profile
from app.engine.sim import outcome_label
from app.live import drawdown, veto
from app.live.equity import EquityState, capital_in_force, day_pnl, equity_state
from app.live.store import TERMINAL_STATUSES, LiveStore
from app.market.context import ContextProvider
from app.market.feed import MarketFeed, MarketSnapshot, snapshot_age_seconds
from app.strategies.rules import (
    AccountState,
    Check,
    RiskLimits,
    StrategyRules,
    check_entry,
    rules_sha256,
)
from app.strategies.schema import parse_strategy_markdown
from app.trading import policy as trade_policy
from app.trading.decisions import (
    MissingMarketConfig,
    build_entry_proposal,
    describe_check,
    entry_risk_checks,
    entry_rule_text,
    exit_checks,
    fired_exit,
    lot_size_for,
    mark_position,
    proposal_lots,
    resize_proposal,
)

EXCHANGE = "NFO"
PRODUCT = "NRML"


@dataclass
class CrewContext:
    live: Session
    deployment_id: str
    adapter: BrokerAdapter
    feed: MarketFeed
    settings: Settings
    clock: Callable[[], datetime] = utcnow
    explain: bool = False


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
    daily_check: Check
    entry_checks: list[Check]
    max_lots: int
    sizing_check: Check | None
    unscaled_lots: int
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
    store = LiveStore(ctx.live)
    if store.deployment_status(ctx.deployment_id) not in TERMINAL_STATUSES:
        store.add_deployment_event(ctx.deployment_id, "halted", reason)
    ctx.live.commit()


def _still_started(ctx: CrewContext) -> bool:
    return LiveStore(ctx.live).deployment_status(ctx.deployment_id) == "started"


def _check_dict(c: Check) -> dict:
    return asdict(c)


_describe = describe_check


def _llm_explain(settings: Settings, facts: dict[str, Any]) -> str | None:
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from langchain_openai import ChatOpenAI

        llm = ChatOpenAI(model=settings.openai_model, api_key=settings.openai_api_key, temperature=0)
        resp = llm.invoke(
            [
                SystemMessage(
                    content="Explain this already-made trading decision in two sentences for a trade journal. "
                    "Use only the facts given. Do not recommend anything."
                ),
                HumanMessage(content=str(facts)),
            ]
        )
        return str(resp.content).strip() or None
    except Exception:
        return None


def _reason(ctx: CrewContext, default: str, facts: dict[str, Any]) -> tuple[str, str | None]:
    if not (ctx.explain and ctx.settings.llm_enabled):
        return default, None
    ctx.live.rollback()  # release the SQLite write lock while the LLM runs
    text = _llm_explain(ctx.settings, facts)
    return (f"{default}\n\n{text}", ctx.settings.openai_model) if text else (default, None)


# Orders


def _send_orders(
    ctx: CrewContext, state: TickState, intent: TradeIntent, legs: list[dict]
) -> tuple[list[Order], str | None]:
    """Send one LIMIT order per leg at its quote. Stops at the first rejection."""
    store, deployment, profile = LiveStore(ctx.live), state["deployment"], state["profile"]
    orders = []
    for i, leg in enumerate(legs):
        now = ctx.clock()
        request = OrderRequest(
            client_order_id=f"tc-{intent.id}-{i:02d}",
            exchange=EXCHANGE,
            tradingsymbol=leg["tradingsymbol"],
            side=leg["side"],
            quantity=leg["quantity"],
            order_type="LIMIT",
            product=PRODUCT,
            limit_price=money(leg["quote"]),
            sebi_algo_id=deployment.sebi_algo_id,
        )
        ack = ctx.adapter.place_order(request)
        order = store.place_order(
            trade_intent_id=intent.id,
            client_order_id=request.client_order_id,
            broker_order_id=ack.broker_order_id or None,
            sebi_algo_id=request.sebi_algo_id,
            exchange=EXCHANGE,
            tradingsymbol=request.tradingsymbol,
            expiry=leg["expiry"],
            strike=money(leg["strike"]),
            option_type=leg["option_type"],
            side=request.side,
            quantity=request.quantity,
            order_type=request.order_type,
            product=PRODUCT,
            limit_price=request.limit_price,
            placed_at=now,
        )
        if not ack.accepted:
            store.add_order_event(order_id=order.id, status="REJECTED", message=ack.message, raw=ack.raw, event_at=now)
            ctx.live.commit()
            return orders, f"order for {request.tradingsymbol} rejected: {ack.message}"
        for u in ctx.adapter.order_updates():
            if u.broker_order_id == ack.broker_order_id:
                store.add_order_event(
                    order_id=order.id, status=u.status, filled_qty=u.filled_qty, pending_qty=u.pending_qty,
                    avg_price=u.avg_price, message=u.message, raw=u.raw, event_at=u.event_at,
                )  # fmt: skip
        for f in ctx.adapter.fills():
            if f.broker_order_id == ack.broker_order_id:
                est = asdict(
                    cost_breakdown(
                        float(leg["quote"]) * f.quantity, orders=1, profile=profile,
                        side=request.side, trade_date=now.date().isoformat(),
                    )
                )  # fmt: skip
                store.record_fill(
                    order_id=order.id, broker_trade_id=f.broker_trade_id, quantity=f.quantity, price=f.price,
                    est_cost_breakdown={k: money(v) for k, v in est.items()}, filled_at=f.filled_at,
                )  # fmt: skip
        for c in ctx.adapter.charges(now.date()):
            if c.broker_order_id == ack.broker_order_id:
                store.record_broker_charge(
                    broker_account_id=deployment.broker_account_id, trade_date=c.trade_date.isoformat(),
                    order_id=order.id, source=c.source, breakdown=c.breakdown, total=c.total, raw=c.raw,
                )  # fmt: skip
        ctx.live.commit()
        orders.append(order)
    return orders, None


def _filled_legs(store: LiveStore, intent: TradeIntent) -> list[dict]:
    legs = []
    for order in store.orders_for_intent(intent.id):
        fills = store.fills_for_order(order.id)
        qty = sum(f.quantity for f in fills)
        if not qty:
            continue
        legs.append(
            {
                "side": order.side,
                "option_type": order.option_type,
                "strike": float(order.strike),
                "expiry": order.expiry,
                "tradingsymbol": order.tradingsymbol,
                "quantity": qty,
                "price": float(sum(f.price * f.quantity for f in fills) / qty),
                "quote": float(order.limit_price),
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


def _close_position(
    ctx: CrewContext, state: TickState, intent: TradeIntent, legs: list[dict], fired: Check, checks, partial=False
):
    """Close `legs` (all or, with `partial`, some of the position's open quantity). The trade carries
    the closed share of the opening costs and target."""
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
    if not _still_started(ctx):
        return None, "deployment stopped before the exit was sent"
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
    close_orders, error = _send_orders(ctx, state, close_intent, close_legs)
    if error:
        halt(ctx, f"exit failed, position still open: {error}")
        return None, error

    exit_legs = _filled_legs(store, close_intent)
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
    exit_prices = {(leg["strike"], leg["option_type"]): leg["price"] for leg in exit_legs}
    trade = store.close_live_trade(
        deployment_id=ctx.deployment_id,
        open_intent_id=intent.id,
        close_intent_id=close_intent.id,
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
    _write_lesson(ctx, state, intent, trade)
    return trade, None


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
        checks = exit_checks(rules, mark)
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
    fired = fired_exit(exit_checks(state["rules"], mark))
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
    """Measure drawdown from peak equity and apply the ladder. Runs after exits, so it never blocks one."""
    ctx, deployment = state["ctx"], state["deployment"]
    policy = drawdown.policy_for(deployment)
    ladder = drawdown.effective_ladder(policy, ctx.settings.max_drawdown_pct)
    if not ladder:
        return {}
    store = LiveStore(ctx.live)
    eq = equity_state(store, ctx.settings, [deployment], include_unrealized=policy is None or policy.basis == "mtm")
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
    return {"equity": eq, "ladder": decision}


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
    on_hit=close_all, close every open position). Runs after exits, so it never blocks one."""
    ctx, deployment = state["ctx"], state["deployment"]
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
    try:
        proposal = build_entry_proposal(rules, market, ctx.settings, max_lots)
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
    if ladder and lots:
        lots = drawdown.scaled_lots(lots, ladder.size_factor)
    lots = max(lots, 1)  # a sizing check that failed still records the entry, as blocked
    if lots != proposal["lots"]:
        proposal = resize_proposal(rules, proposal, lots)
    return {
        "entry_checks": checks,
        "proposal": proposal,
        "max_lots": max_lots,
        "sizing_check": sizing_check,
        "unscaled_lots": unscaled,
    }


def _risk(state: TickState) -> TickState:
    checks = _risk_checks(state, state["proposal"])
    approval = "auto" if all(c.passed for c in checks) else "blocked"
    return {"risk_checks": checks, "approval": approval}


def _risk_checks(state: TickState, proposal: dict) -> list[Check]:
    ctx, rules, deployment = state["ctx"], state["rules"], state["deployment"]
    store = LiveStore(ctx.live)
    today = ctx.clock().date()
    portfolio_open = sum(len(store.open_intents(d.id)) for d in store.active_deployments())
    account = AccountState(
        open_positions=state["open_positions"],
        pnl_today=float(sum(t.net for t in store.trades_closed_on(today, ctx.deployment_id))),
        portfolio_open_positions=portfolio_open,
        portfolio_pnl_today=float(sum(t.net for t in store.trades_closed_on(today))),
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
    checks += [c for c in (state.get("sizing_check"), state.get("daily_check")) if c is not None]
    return checks


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
        ctx.settings, context, state["proposal"], state["entry_checks"], state["risk_checks"], lessons
    )
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
        if ladder is not None and rules_lots < state.get("unscaled_lots", rules_lots):
            default += (
                f" Drawdown level {ladder.level} ({ladder.drawdown_pct:.2f}% from peak) cut "
                f"{state['unscaled_lots']} to {rules_lots} lots."
            )
        if state.get("rules_lots", state["proposal"]["lots"]) != state["proposal"]["lots"]:
            default += f" The LLM opinion reduced {state['rules_lots']} to {state['proposal']['lots']} lots: {why}."
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
    if error:
        halt(ctx, f"entry failed: {error}")
        return {"outcome": "halted", "detail": error, "intent_id": intent.id}
    return {"outcome": "entered", "intent_id": intent.id}


def _next(node: str):
    return lambda state: END if state.get("stop") else node


def build_trade_graph():
    graph = StateGraph(TickState)
    for name, fn in (
        ("verify", _verify),
        ("gate", _gate),
        ("observe", _observe),
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
    graph.add_conditional_edges("gate", _next("observe"))
    graph.add_conditional_edges("observe", _next("exits"))
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
