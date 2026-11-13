"""Equity, peak and drawdown for paper and live deployments.

Equity is capital in force + realized net P&L + unrealized P&L of open positions, the last from
the position snapshots the Trade Crew writes each tick. Every capital event moves equity and peak
by the same amount,
so deposits and withdrawals never show up as gains or drawdowns. The approved capital is the
opening balance.

Unrealized P&L is marked at quotes and excludes exit costs, which are realized when the trade closes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timezone
from decimal import Decimal
from typing import Any

from app.audit import current_actor
from app.config import Settings
from app.db.models import Deployment
from app.live.store import LiveStore

CAPITAL_KINDS = ("deposit", "withdrawal")
_CAPITAL, _TRADE, _MARK = 0, 1, 2  # same-instant order: capital first, then closes, then marks


@dataclass
class EquityState:
    capital_inr: float
    realized_inr: float
    unrealized_inr: float
    equity_inr: float
    peak_inr: float
    peak_at: datetime | None
    drawdown_inr: float
    drawdown_pct: float
    max_drawdown_inr: float
    max_drawdown_pct: float
    as_of: datetime | None
    curve: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self, with_curve: bool = False) -> dict[str, Any]:
        out = asdict(self)
        if not with_curve:
            out.pop("curve")
        return out


def approved_capital(deployment: Deployment, settings: Settings) -> float:
    return float(deployment.risk_limits.get("capital_inr") or settings.default_capital_inr)


def capital_in_force(store: LiveStore, settings: Settings, deployment: Deployment) -> float:
    total = approved_capital(deployment, settings)
    for e in store.capital_events([deployment.id]):
        total += float(e.amount_inr) if e.kind == "deposit" else -float(e.amount_inr)
    return total


def _pct(drawdown: float, peak: float) -> float:
    return drawdown / peak * 100 if peak > 0 else 0.0


def equity_state(
    store: LiveStore,
    settings: Settings,
    deployments: list[Deployment],
    *,
    include_unrealized: bool = True,
    with_curve: bool = False,
    until: datetime | None = None,
) -> EquityState:
    """Replay capital, closed trades and position marks in time order for one or more deployments,
    up to (not including) `until` when given."""
    ids = [d.id for d in deployments]
    events: list[tuple[datetime, int, int, Any]] = []
    for e in store.capital_events(ids):
        events.append((e.created_at, _CAPITAL, e.id, float(e.amount_inr) * (1 if e.kind == "deposit" else -1)))
    for t in store.closed_trades(ids):
        events.append((t.closed_at, _TRADE, 0, (t.deployment_id, t.open_intent_id, float(t.net))))
    if include_unrealized:
        for s in store.position_snapshots(ids):
            events.append((s.taken_at, _MARK, s.id, (s.deployment_id, s.positions)))
    if until is not None:
        events = [e for e in events if e[0] < until]
    events.sort(key=lambda e: (e[0], e[1], e[2]))

    capital = peak = sum(approved_capital(d, settings) for d in deployments)
    realized = max_dd = max_dd_pct = 0.0
    marks: dict[tuple[str, str], float] = {}
    peak_at = min((d.created_at for d in deployments), default=None)
    as_of = None
    curve: list[dict[str, Any]] = []
    for at, kind, _, payload in events:
        if kind == _CAPITAL:
            capital += payload
            peak += payload
        elif kind == _TRADE:
            deployment_id, intent_id, net = payload
            realized += net
            marks.pop((deployment_id, intent_id), None)
        else:
            deployment_id, positions = payload
            previous = {k: marks.pop(k) for k in [k for k in marks if k[0] == deployment_id]}
            for p in positions:
                key = (deployment_id, p["open_intent_id"])
                pnl = p.get("pnl")
                marks[key] = float(pnl) if pnl is not None else previous.get(key, 0.0)
        equity = capital + realized + sum(marks.values())
        if equity > peak:
            peak, peak_at = equity, at
        drawdown = peak - equity
        max_dd, max_dd_pct = max(max_dd, drawdown), max(max_dd_pct, _pct(drawdown, peak))
        as_of = at
        if with_curve:
            curve.append(
                {"at": at, "equity_inr": round(equity, 4), "drawdown_inr": round(drawdown, 4),
                 "drawdown_pct": round(_pct(drawdown, peak), 4)}
            )  # fmt: skip

    unrealized = sum(marks.values())
    equity = capital + realized + unrealized
    drawdown = peak - equity
    return EquityState(
        capital_inr=round(capital, 4),
        realized_inr=round(realized, 4),
        unrealized_inr=round(unrealized, 4),
        equity_inr=round(equity, 4),
        peak_inr=round(peak, 4),
        peak_at=peak_at,
        drawdown_inr=round(drawdown, 4),
        drawdown_pct=round(_pct(drawdown, peak), 4),
        max_drawdown_inr=round(max_dd, 4),
        max_drawdown_pct=round(max_dd_pct, 4),
        as_of=as_of,
        curve=curve,
    )


def day_pnl(store: LiveStore, settings: Settings, deployment: Deployment, now: datetime) -> float:
    """Realized P&L plus the change in unrealized P&L since the start of `now`'s UTC day.
    Capital events are not P&L and are left out."""
    start = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
    current = equity_state(store, settings, [deployment])
    before = equity_state(store, settings, [deployment], until=start)
    return (current.realized_inr + current.unrealized_inr) - (before.realized_inr + before.unrealized_inr)


def current_positions(store: LiveStore, deployment: Deployment) -> list[dict[str, Any]]:
    """Open positions with their latest marks. A position opened since the last snapshot has no mark yet."""
    snapshot = store.latest_position_snapshot(deployment.id)
    marked = {p["open_intent_id"]: p for p in snapshot.positions} if snapshot else {}
    rows = []
    for intent in store.open_intents(deployment.id):
        if not any(store.fills_for_order(o.id) for o in store.orders_for_intent(intent.id)):
            continue
        row = marked.get(intent.id) or {"open_intent_id": intent.id, "opened_at": intent.decided_at, "pnl": None}
        rows.append({"deployment_id": deployment.id, "marked_at": snapshot.taken_at if intent.id in marked else None}
                    | row)  # fmt: skip
    return rows


def add_capital_event(
    store: LiveStore, settings: Settings, deployment_id: str, kind: str, amount_inr: float, note: str | None
):
    """Record a deposit or withdrawal. Capital in force must stay positive."""
    if current_actor() == "anonymous":
        raise ValueError("capital changes need a named actor: send the X-Actor header")
    if kind not in CAPITAL_KINDS:
        raise ValueError(f"kind must be one of {', '.join(CAPITAL_KINDS)}")
    if amount_inr <= 0:
        raise ValueError("amount_inr must be positive")
    deployment = store.deployment(deployment_id)
    if kind == "withdrawal" and capital_in_force(store, settings, deployment) - amount_inr <= 0:
        raise ValueError("a withdrawal cannot take capital in force to zero or below")
    return store.add_capital_event(
        deployment_id=deployment_id, kind=kind, amount_inr=Decimal(str(amount_inr)), note=note
    )
