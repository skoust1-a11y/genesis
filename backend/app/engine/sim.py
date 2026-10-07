"""Day-by-day simulation of a strategy's own rules.

Each trading day, in the same order as a Trade Crew tick:
1. mark open positions and close those whose exit rules fire (profit target, stop loss, DTE,
   expiry settlement);
2. if nothing closed, check entry, then risk, and open a position at that day's quotes.

Entry, legs, economics, exits and risk come from `app.trading.decisions`, the code the Trade Crew
uses, so a simulated run and a paper run of the same rules on the same data take the same trades.

With a `Management` (a deployment's trade policy and drawdown ladder), the same policy code as the
Trade Crew also runs: booking and time exits after the strategy's exits, then the drawdown ladder
and daily profit target, then sizing at entry. A pause stops entries for the rest of the window,
since nobody restarts a backtest.

Assumptions (also written into every report):
- end-of-day data: rules are checked at the close and fills happen at the close, so a stop breached
  during the day is filled at the close and can lose more than the stop;
- gross P&L is measured at quotes; slippage is charged as a cost on every fill;
- there is no margin model; returns are measured against `default_capital_inr`.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field, fields, replace
from datetime import date
from typing import Any

from app.agents.trade_crew.legs import signed
from app.config import Settings
from app.engine.costs import CostBreakdown, CostProfile, exercise_costs, leg_costs
from app.live import drawdown
from app.market.feed import MarketFeed, MarketSnapshot, intrinsic
from app.strategies.rules import AccountState, Check, RiskLimits, StrategyRules, check_entry
from app.strategies.schema import StrategyDocument
from app.trading import policy as trade_policy
from app.trading.decisions import (
    MissingMarketConfig,
    build_entry_proposal,
    describe_check,
    entry_risk_checks,
    entry_rule_text,
    exit_checks,
    fired_exit,
    mark_position,
    proposal_lots,
    resize_proposal,
)

ENGINE_VERSION = "3"
SCRATCH_PCT_OF_TARGET = 0.01
BACKTEST_SHARE = 0.7
WINDOW_END_RULE = "exit.window_end: closed at the last day of the test window"
ASSUMPTIONS = [
    "End-of-day data: rules are checked and orders filled at the daily close, so a stop breached "
    "during the day is filled at the close and can lose more than the stop.",
    "Gross P&L is measured at quotes; slippage is charged as a cost on every fill.",
    "No margin model: returns are measured against the configured capital.",
    "Positions still open at the end of a test window are closed at that day's quotes.",
]


@dataclass
class Scenario:
    """One variant of the strategy's rules."""

    label: str
    rules: dict[str, Any]
    lots: int

    @property
    def parsed(self) -> StrategyRules:
        return StrategyRules.model_validate(self.rules)

    def threshold(self, field_name: str, ops: tuple[str, ...]) -> float | None:
        for c in self.rules.get("entry", []):
            if c["field"] == field_name and c["op"] in ops:
                return c["value"]
        return None

    @property
    def iv_rank_min(self) -> float | None:
        return self.threshold("iv_rank", (">=", ">"))

    @property
    def dte_min(self) -> int | None:
        v = self.threshold("dte", (">=", ">"))
        return int(v) if v is not None else None

    @property
    def dte_max(self) -> int | None:
        v = self.threshold("dte", ("<=", "<"))
        return int(v) if v is not None else None

    @property
    def entry_rule(self) -> str:
        return " and ".join(f"{c['field']} {c['op']} {c['value']:g}" for c in self.rules.get("entry", []))


@dataclass
class Condition:
    field: str
    op: str
    threshold: float
    actual: float
    passed: bool


@dataclass
class TradeResult:
    date: str
    gross: float
    costs: float
    net: float
    # Why
    entry_rule: str = ""
    entry_conditions: list[Condition] = field(default_factory=list)
    reason_text: str = ""
    edge: float = 0.0  # IV-rank margin over the entry threshold, in rank points
    market_snapshot: dict = field(default_factory=dict)
    # How
    lots: int = 0
    credit: float = 0.0
    max_loss: float = 0.0
    notional: float = 0.0
    orders: int = 0
    cost_breakdown: CostBreakdown | None = None
    legs: list[dict] = field(default_factory=list)
    expiry: str | None = None
    exit_date: str | None = None
    exit_rule: str = ""
    exit_reason: str = ""
    holding_bars: int = 0
    # Vision
    target_pnl: float = 0.0
    stop_loss_pnl: float = 0.0
    risk_reward: float = 0.0
    # Outcome
    pnl_vs_target: float = 0.0
    cost_ratio: float | None = None
    outcome_label: str = ""
    equity_after: float = 0.0
    drawdown_after: float = 0.0
    partial: bool = False  # some lots closed by a partial booking; the rest stayed open


@dataclass
class SimResult:
    scenario: Scenario
    mode: str
    trades: list[TradeResult]
    gross_pnl: float
    total_costs: float
    net_pnl: float
    win_rate: float
    max_drawdown: float  # on closed-trade equity
    window_start: str | None = None
    window_end: str | None = None
    bars_in_window: int = 0
    equity_curve: list[dict] = field(default_factory=list)  # daily mark-to-market
    max_drawdown_mtm: float = 0.0
    max_drawdown_days: int = 0
    data_source: str = ""
    by_year: dict[str, dict] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)  # entry signals not taken, by reason
    management: dict = field(default_factory=dict)  # what the policy and drawdown ladder did, when given


@dataclass
class Management:
    """A deployment's risk limits as they apply in a backtest."""

    policy: trade_policy.TradePolicy | None = None
    drawdown: drawdown.DrawdownPolicy | None = None
    max_drawdown_pct: float = 0
    capital_inr: float | None = None
    max_lots: int | None = None

    @classmethod
    def from_limits(cls, risk_limits: dict[str, Any], max_drawdown_pct: float) -> Management:
        raw = risk_limits.get("drawdown")
        return cls(
            policy=trade_policy.policy_from_limits(risk_limits),
            drawdown=drawdown.DrawdownPolicy.model_validate(raw) if raw else None,
            max_drawdown_pct=max_drawdown_pct,
            capital_inr=risk_limits.get("capital_inr"),
            max_lots=risk_limits.get("max_lots"),
        )


def outcome_label(net: float, target: float) -> str:
    if abs(net) <= abs(target) * SCRATCH_PCT_OF_TARGET:
        return "scratch"
    return "win" if net > 0 else "loss"


# Scenarios


def _variant(raw: dict, label: str, change) -> tuple[str, dict] | None:
    out = copy.deepcopy(raw)
    if not change(out):
        return None
    try:
        StrategyRules.model_validate(out)
    except ValueError:
        return None
    return label, out


def build_scenarios(strategy: StrategyDocument, settings: Settings) -> list[Scenario]:
    """The strategy's rules plus stress variants around them; none if the rules do not validate."""
    rules = strategy.rules
    if rules is None:
        return []
    base = rules.model_dump(mode="json", exclude_none=True)
    step = settings.strike_step(rules.instrument.underlying) or 50
    variants: list[tuple[str, dict] | None] = [("base", base)]

    for i, c in enumerate(base["entry"]):
        if c["field"] == "iv_rank" and c["op"] in (">=", ">"):
            for delta in (5, -5):
                value = min(max(c["value"] + delta, 0), 100)

                def bump(r, i=i, value=value):
                    if r["entry"][i]["value"] == value:
                        return False
                    r["entry"][i]["value"] = value
                    return True

                variants.append(_variant(base, f"iv_rank {c['op']} {value:g}", bump))
    distance = base["instrument"].get("short_distance")
    if distance:
        for delta in (step, -step):

            def shift(r, delta=delta):
                r["instrument"]["short_distance"] = r["instrument"]["short_distance"] + delta
                return r["instrument"]["short_distance"] > 0

            variants.append(_variant(base, f"short_distance {distance + delta:g}", shift))
    for kind, factors, cap in (("stop_loss_pct_of_credit", (0.5, 1.5), None), ("profit_target_pct_of_credit", (0.6, 1.4), 100)):
        current = rules.exit_value(kind)
        for factor in factors:
            value = round(current * factor, 2)
            if cap is not None:
                value = min(value, cap)

            def set_exit(r, kind=kind, value=value):
                for e in r["exit"]:
                    if e["kind"] == kind and e["value"] != value:
                        e["value"] = value
                        return True
                return False

            variants.append(_variant(base, f"{kind.removesuffix('_pct_of_credit')} {value:g}%", set_exit))

    seen: set[str] = set()
    scenarios: list[Scenario] = []
    for v in variants:
        if v is None:
            continue
        label, raw = v
        key = json.dumps(raw, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        scenarios.append(Scenario(label=label, rules=raw, lots=raw["risk"]["max_lots"]))
    return scenarios[: settings.max_scenario_combos]


def dte_window(rules: StrategyRules) -> tuple[int, int] | None:
    lo, hi = None, None
    for c in rules.entry:
        if c.field != "dte":
            continue
        if c.op in (">=", ">"):
            lo = int(c.value) + (1 if c.op == ">" else 0)
        elif c.op in ("<=", "<"):
            hi = int(c.value) - (1 if c.op == "<" else 0)
        elif c.op == "==":
            lo = hi = int(c.value)
    if lo is None and hi is None:
        return None
    return (lo or 0, hi if hi is not None else 10_000)


def split_dates(dates: list[str]) -> tuple[list[str], list[str]]:
    cut = int(len(dates) * BACKTEST_SHARE)
    return dates[:cut], dates[cut:]


# Simulation


@dataclass
class _Position:
    entry_date: str
    entry_index: int
    legs: list[dict]  # side, option_type, strike, expiry, quantity, price (entry quote)
    entry_costs: CostBreakdown
    proposal: dict
    entry_checks: list[Check]
    market: dict
    last_quotes: list[float]
    lots_opened: int = 0
    peak_pct: float | None = None
    partials_done: int = 0


def _scale_costs(costs: CostBreakdown, share: float) -> CostBreakdown:
    return CostBreakdown(**{f.name: getattr(costs, f.name) * share for f in fields(costs)})


def _scale_proposal(proposal: dict, lots: int, share: float) -> dict:
    e = proposal["economics"]
    economics = e and replace(
        e, credit=e.credit * share, target_pnl=e.target_pnl * share,
        stop_loss_pnl=e.stop_loss_pnl * share, max_loss=e.max_loss * share,
    )  # fmt: skip
    return proposal | {"lots": lots, "quantity": lots * proposal["lot_size"], "economics": economics}


def _split(position: _Position, lots: int) -> _Position:
    """Take `lots` lots off `position` as a position of their own, with their share of entry costs and economics."""
    total = position.proposal["lots"]
    share = lots / total
    part = replace(
        position,
        legs=[leg | {"quantity": leg["quantity"] * lots // total} for leg in position.legs],
        entry_costs=_scale_costs(position.entry_costs, share),
        proposal=_scale_proposal(position.proposal, lots, share),
    )
    position.legs = [leg | {"quantity": leg["quantity"] - p["quantity"]} for leg, p in zip(position.legs, part.legs)]
    position.entry_costs = _scale_costs(position.entry_costs, 1 - share)
    position.proposal = _scale_proposal(position.proposal, total - lots, 1 - share)
    return part


def _managed_lots(
    rules: StrategyRules, proposal: dict, policy, capital: float, realized_net: float, size_factor: float
) -> tuple[dict, Check | None]:
    """Policy sizing then the drawdown ladder's size factor, as in the Trade Crew's entry step."""
    max_lots, check = proposal["lots"], None
    lots = max_lots
    if policy is not None and policy.sizing is not None:
        e = proposal["economics"]
        capital = trade_policy.sizing_capital(policy, capital, realized_net)
        lots, check = trade_policy.size_lots(policy.sizing, max_lots, capital, e.max_loss / max_lots if e else None)
    if lots:
        lots = drawdown.scaled_lots(lots, size_factor)
    lots = max(lots, 1)
    return (resize_proposal(rules, proposal, lots) if lots != max_lots else proposal), check


def _condition(c: Check) -> Condition:
    return Condition(c.field, c.op, c.threshold, c.actual, c.passed)


def _edge(checks: list[Check]) -> float:
    iv = next((c for c in checks if c.field == "iv_rank" and c.op in (">=", ">")), None)
    return float(iv.actual - iv.threshold) if iv and iv.actual is not None else 0.0


def _snapshot_dict(market: MarketSnapshot) -> dict:
    return {
        "date": market.as_of,
        "close": market.spot,
        "iv_rank": market.iv_rank,
        "dte": market.dte,
        "expiry": market.expiry,
        "stale_quotes": len(market.stale),
    }


def _unrealized(position: _Position, quotes: list[float]) -> float:
    return sum(
        signed(leg["side"]) * (leg["price"] - q) * leg["quantity"] for leg, q in zip(position.legs, quotes, strict=True)
    )


def _close(
    position: _Position,
    quotes: list[float],
    market: MarketSnapshot,
    index: int,
    fired_rule: str,
    exit_rule: str,
    profile: CostProfile,
) -> TradeResult:
    expired = fired_rule == "exit.expiry"
    if expired:
        # Settled by the exchange: no exit orders, only STT on long legs exercised in the money.
        exit_costs = exercise_costs(
            (
                (intrinsic(market.spot, leg["strike"], leg["option_type"]), leg["quantity"])
                for leg in position.legs
                if leg["side"] == "BUY"
            ),
            profile,
        )
        orders = 0
    else:
        exit_costs = leg_costs(
            (("BUY" if leg["side"] == "SELL" else "SELL", q, leg["quantity"]) for leg, q in zip(position.legs, quotes, strict=True)),
            profile,
            market.as_of,
        )
        orders = len(position.legs)
    breakdown = position.entry_costs + exit_costs
    gross = _unrealized(position, quotes)
    costs = breakdown.total
    net = gross - costs
    economics = position.proposal["economics"]
    entry = position.entry_checks
    return TradeResult(
        date=position.entry_date,
        gross=gross,
        costs=costs,
        net=net,
        entry_rule=entry_rule_text(entry),
        entry_conditions=[_condition(c) for c in entry],
        reason_text=f"Entry signal: {'; '.join(describe_check(c) for c in entry)}. All risk checks passed.",
        edge=_edge(entry),
        market_snapshot=position.market,
        lots=position.proposal["lots"],
        credit=economics.credit,
        max_loss=economics.max_loss,
        notional=sum(leg["price"] * leg["quantity"] for leg in position.legs),
        orders=len(position.legs) + orders,
        cost_breakdown=breakdown,
        legs=[
            {k: leg[k] for k in ("side", "option_type", "strike", "expiry", "quantity")}
            | {"entry_price": leg["price"], "exit_price": q}
            for leg, q in zip(position.legs, quotes, strict=True)
        ],
        expiry=position.legs[0]["expiry"] if position.legs else None,
        exit_date=market.as_of,
        exit_rule=exit_rule,
        exit_reason=fired_rule.removeprefix("exit."),
        holding_bars=index - position.entry_index,
        target_pnl=economics.target_pnl,
        stop_loss_pnl=economics.stop_loss_pnl,
        risk_reward=economics.risk_reward,
        pnl_vs_target=net - economics.target_pnl,
        cost_ratio=(costs / abs(gross)) if gross else None,
        outcome_label=outcome_label(net, economics.target_pnl),
    )


def _drawdown_stats(curve: list[dict]) -> tuple[float, int]:
    peak, max_dd, run, longest = float("-inf"), 0.0, 0, 0
    for point in curve:
        equity = point["equity"]
        if equity >= peak:
            peak, run = equity, 0
        else:
            run += 1
            longest = max(longest, run)
        max_dd = max(max_dd, peak - equity)
        point["drawdown"] = round(peak - equity, 4)
    return max_dd, longest


def simulate(
    rules: StrategyRules,
    feed: MarketFeed,
    days: int,
    profile: CostProfile,
    settings: Settings,
    *,
    scenario: Scenario,
    mode: str,
    data_source: str = "",
    management: Management | None = None,
) -> SimResult:
    """Run `rules` over `days` snapshots from `feed`, optionally under a deployment's `management`."""
    underlying = rules.instrument.underlying
    m = management
    lots = proposal_lots(rules, m.max_lots) if m and m.max_lots else scenario.lots
    capital = m.capital_inr if m and m.capital_inr else settings.default_capital_inr
    limits = RiskLimits(
        capital_inr=capital,
        portfolio_max_open_positions=settings.portfolio_max_open_positions,
        portfolio_max_daily_loss_inr=settings.portfolio_max_daily_loss_inr,
    )
    policy = m.policy if m else None
    ladder = drawdown.effective_ladder(m.drawdown, m.max_drawdown_pct) if m else []
    restore = m.drawdown.restore_below_pct if m and m.drawdown else None
    realized_basis = bool(m and m.drawdown and m.drawdown.basis == "realized")
    level, peak_equity, paused_on = 0, capital, None
    ladder_events: list[dict] = []
    trades: list[TradeResult] = []
    open_positions: list[_Position] = []
    skipped: dict[str, int] = {}
    curve: list[dict] = []
    realized = 0.0
    last_market: MarketSnapshot | None = None
    last_index = 0
    first_day: str | None = None

    def close(position: _Position, quotes: list[float], fired: Check) -> TradeResult:
        nonlocal realized
        trade = _close(position, quotes, market, index, fired.rule, describe_check(fired), profile)
        realized += trade.gross - (trade.costs - position.entry_costs.total)
        closed_today.append(trade)
        return trade

    def close_all(fired: Check) -> None:
        for position in list(open_positions):
            close(position, position.last_quotes, fired)
            open_positions.remove(position)

    for index in range(days):
        held = sorted({(leg["strike"], leg["option_type"], leg["expiry"]) for p in open_positions for leg in p.legs})
        market = feed.snapshot(underlying, held)
        if market is None:
            continue
        first_day = first_day or market.as_of
        last_market, last_index = market, index

        closed_today: list[TradeResult] = []
        for position in list(open_positions):
            mark = mark_position(position.legs, market)
            if mark is None:
                skipped["missing_quotes_open_position"] = skipped.get("missing_quotes_open_position", 0) + 1
                continue
            position.last_quotes = mark.quotes
            fired = fired_exit(exit_checks(rules, mark))
            close_lots = None
            if policy is not None:
                position.peak_pct = trade_policy.update_peak(position.peak_pct, mark)
                booking = None if fired else trade_policy.booking_exit(
                    policy, mark, peak_pct=position.peak_pct, partials_done=position.partials_done,
                    lots_opened=position.lots_opened, lots_open=position.proposal["lots"], events=market.events,
                )  # fmt: skip
                if booking is not None:
                    fired, close_lots = booking.check, booking.close_lots
            if fired is None:
                continue
            if close_lots is not None:
                close(_split(position, close_lots), mark.quotes, fired).partial = True
                position.partials_done += 1
                continue
            close(position, mark.quotes, fired)
            open_positions.remove(position)

        extra: list[Check] = []  # the ladder's and daily target's entry checks, as in the Trade Crew
        size_factor = 1.0
        if ladder:
            closed_net = sum(t.net for t in trades + closed_today)
            unrealized = sum(_unrealized(p, p.last_quotes) for p in open_positions)
            account = capital + (closed_net if realized_basis else realized + unrealized)
            peak_equity = max(peak_equity, account)
            dd_pct = (peak_equity - account) / peak_equity * 100 if peak_equity > 0 else 0.0
            decision = drawdown.decide(ladder, restore, dd_pct, level)
            if decision.level != level:
                ladder_events.append(
                    {"date": market.as_of, "level": decision.level, "action": decision.action,
                     "drawdown_pct": decision.drawdown_pct}
                )  # fmt: skip
                level = decision.level
            if decision.pause is not None and paused_on is None:
                paused_on = market.as_of
                if decision.pause.close_open_positions:
                    close_all(Check("drawdown.pause", "drawdown_pct", ">=", decision.pause.at_pct, dd_pct, True))
            if decision.check is not None:
                extra.append(decision.check)
            size_factor = decision.size_factor
        if policy is not None and policy.daily_target is not None:
            unrealized = sum(_unrealized(p, p.last_quotes) for p in open_positions)
            day_pnl = realized + unrealized - (curve[-1]["equity"] if curve else 0.0)
            check = trade_policy.daily_target_check(policy.daily_target, day_pnl)
            if not check.passed and policy.daily_target.on_hit == "close_all":
                close_all(replace(check, op=">=", passed=True))
            extra.append(check)
        trades.extend(closed_today)

        if not closed_today and len(open_positions) < rules.risk.max_open_positions:
            entry = check_entry(rules, market)
            if all(c.passed for c in entry) and paused_on is not None:
                skipped["drawdown.paused"] = skipped.get("drawdown.paused", 0) + 1
            elif all(c.passed for c in entry):
                try:
                    proposal = build_entry_proposal(rules, market, settings, lots)
                except MissingMarketConfig:
                    skipped["missing_lot_size_or_strike_step"] = skipped.get("missing_lot_size_or_strike_step", 0) + 1
                    proposal = None
                if proposal is not None and m is not None:
                    proposal, sizing = _managed_lots(
                        rules, proposal, policy, capital, sum(t.net for t in trades), size_factor
                    )
                    extra += [sizing] if sizing else []
                if proposal is not None:
                    pnl_today = sum(t.net for t in trades if t.exit_date == market.as_of)
                    account = AccountState(
                        open_positions=len(open_positions),
                        pnl_today=pnl_today,
                        portfolio_open_positions=len(open_positions),
                        portfolio_pnl_today=pnl_today,
                    )
                    risk = entry_risk_checks(rules, limits, account, proposal) + extra
                    failed = [c.rule for c in risk if not c.passed]
                    if failed:
                        for rule in failed:
                            skipped[rule] = skipped.get(rule, 0) + 1
                    else:
                        legs = [
                            {k: leg[k] for k in ("side", "option_type", "strike", "expiry", "quantity")}
                            | {"price": leg["quote"]}
                            for leg in proposal["legs"]
                        ]
                        entry_costs = leg_costs(
                            ((leg["side"], leg["price"], leg["quantity"]) for leg in legs), profile, market.as_of
                        )
                        realized -= entry_costs.total
                        open_positions.append(
                            _Position(
                                entry_date=market.as_of,
                                entry_index=index,
                                legs=legs,
                                entry_costs=entry_costs,
                                proposal=proposal,
                                entry_checks=entry,
                                market=_snapshot_dict(market),
                                last_quotes=[leg["price"] for leg in legs],
                                lots_opened=proposal["lots"],
                            )
                        )

        unrealized = sum(_unrealized(p, p.last_quotes) for p in open_positions)
        curve.append({"date": market.as_of, "equity": round(realized + unrealized, 4)})

    if last_market is not None:
        for position in open_positions:
            trade = _close(
                position, position.last_quotes, last_market, last_index, "exit.window_end", WINDOW_END_RULE, profile
            )
            trades.append(trade)
            realized += trade.gross - (trade.costs - position.entry_costs.total)
        if open_positions and curve:
            curve[-1]["equity"] = round(realized, 4)

    trades.sort(key=lambda t: (t.exit_date or "", t.date))
    equity = peak = max_dd = 0.0
    for t in trades:
        equity += t.net
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        t.equity_after, t.drawdown_after = equity, peak - equity
    max_dd_mtm, dd_days = _drawdown_stats(curve)
    by_year: dict[str, dict] = {}
    for t in trades:
        year = by_year.setdefault((t.exit_date or t.date)[:4], {"trades": 0, "net_pnl": 0.0, "wins": 0})
        year["trades"] += 1
        year["net_pnl"] = round(year["net_pnl"] + t.net, 4)
        year["wins"] += t.net > 0
    wins = sum(1 for t in trades if t.net > 0)
    return SimResult(
        scenario=scenario,
        mode=mode,
        trades=trades,
        gross_pnl=sum(t.gross for t in trades),
        total_costs=sum(t.costs for t in trades),
        net_pnl=sum(t.net for t in trades),
        win_rate=(wins / len(trades)) if trades else 0.0,
        max_drawdown=max_dd,
        window_start=first_day,
        window_end=last_market.as_of if last_market else None,
        bars_in_window=len(curve),
        equity_curve=curve,
        max_drawdown_mtm=max_dd_mtm,
        max_drawdown_days=dd_days,
        data_source=data_source,
        by_year=by_year,
        skipped=skipped,
        management=_management_summary(trades, ladder_events, paused_on, level) if m else {},
    )


def _management_summary(trades: list[TradeResult], ladder_events: list[dict], paused_on: str | None, level: int):
    exits: dict[str, int] = {}
    for t in trades:
        if t.exit_reason.startswith(("policy.", "drawdown.")):
            exits[t.exit_reason] = exits.get(t.exit_reason, 0) + 1
    return {
        "policy_exits": exits,
        "partial_closes": sum(1 for t in trades if t.partial),
        "drawdown_events": ladder_events,
        "drawdown_level": level,
        "paused_on": paused_on,
    }


def run_window(
    scenario: Scenario,
    source,
    dates: list[str],
    profile: CostProfile,
    settings: Settings,
    mode: str,
    management: Management | None = None,
) -> SimResult:
    rules = scenario.parsed
    feed = source.feed(dates, dte_window(rules))
    return simulate(
        rules, feed, len(dates), profile, settings,
        scenario=scenario, mode=mode, data_source=source.data_source, management=management,
    )  # fmt: skip


def holding_days(trade: TradeResult) -> int:
    if not trade.exit_date:
        return 0
    return (date.fromisoformat(trade.exit_date) - date.fromisoformat(trade.date)).days
