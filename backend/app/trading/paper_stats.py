"""Paper-trading performance per virtual account. Read-only over the crew journal.

Each ``IND:<family>`` account is one strategy family's isolated ₹ account; ``SHARED`` is the competing portfolio.
They are reported side by side and never summed. Rupee figures come only from priced fills of CLOSED trades;
unpriced signals and EXIT_UNPRICED trades are counted, listed, and kept out of every rupee metric.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime
from decimal import Decimal

from app.db import paper_ledger as pl
from app.trading import paper_exec as px
from app.trading import paper_registry as reg
from app.trading.option_quote import IST

MIN_SAMPLE = 30
_ZERO = Decimal(0)


def report(conn: sqlite3.Connection, *, market_day: str | None = None) -> dict:
    accounts = [r[0] for r in conn.execute("SELECT account_id FROM crew_paper_accounts ORDER BY account_id")]
    return {
        "units": "INR from simulated bid/ask fills; independent accounts are never summed",
        "registry_hash": reg.REGISTRY_HASH, "risk_policy": reg.RISK_POLICY, "market_day": market_day,
        "accounts": {a: account_stats(conn, a, market_day=market_day) for a in accounts},
    }


def account_stats(conn: sqlite3.Connection, account_id: str, *, market_day: str | None = None) -> dict:
    day_sql, day_args = ("AND market_day=?", (market_day,)) if market_day else ("", ())
    account = conn.execute("SELECT capital_inr, risk_policy_id FROM crew_paper_accounts WHERE account_id=?",
                           (account_id,)).fetchone()
    capital = Decimal(account["capital_inr"])
    decisions = [dict(r) for r in conn.execute(
        f"SELECT outcome, reasons FROM crew_paper_decisions WHERE account_id=? {day_sql}", (account_id, *day_args))]
    trades = [dict(r) for r in conn.execute(
        "SELECT * FROM crew_paper_trades WHERE account_id=? ORDER BY opened_at, paper_trade_id", (account_id,))]
    if market_day:
        historical = []
        for trade in trades:
            events = [dict(r) for r in conn.execute(
                "SELECT * FROM crew_paper_events WHERE paper_trade_id=? ORDER BY seq", (trade["paper_trade_id"],))
                if datetime.fromisoformat(r["occurred_at"]).astimezone(IST).date().isoformat() <= market_day]
            if not events:
                continue
            last = events[-1]
            trade.update(state=last["to_state"], lots_open=last["lots_after"],
                         closed_at=last["occurred_at"] if last["to_state"] not in pl.OPEN else None)
            historical.append(trade)
        trades = historical
    closed = sorted((t for t in trades if t["state"] == "CLOSED" and
                     (market_day is None or datetime.fromisoformat(t["closed_at"]).astimezone(IST).date().isoformat() == market_day)),
                    key=lambda t: (t["closed_at"], t["paper_trade_id"]))
    for t in closed:
        _money(conn, t)
    nets = [t["net"] for t in closed]
    wins, losses = [n for n in nets if n > 0], [n for n in nets if n < 0]
    curve, peak, drawdown = capital, capital, _ZERO
    for n in nets:
        curve += n
        peak = max(peak, curve)
        drawdown = max(drawdown, peak - curve)
    model_exits = {p["paper_trade_id"]: p["model_exit_at"] for p in pl.pending_exits(conn)}
    reasons = Counter(r for d in decisions if d["outcome"] != "OPENED" for r in json.loads(d["reasons"]))
    projection = pl.account_projection(conn, account_id, market_day=market_day)
    orphaned = []
    for trade in trades:
        if trade["state"] not in pl.OPEN:
            continue
        owner = conn.execute("SELECT owner_session_id FROM crew_trades WHERE trade_id=?",
                             (trade["model_trade_id"],)).fetchone()
        latest = conn.execute("SELECT session_id FROM crew_sessions WHERE market_day=? ORDER BY generation DESC LIMIT 1",
                              (trade["market_day"],)).fetchone()
        if owner and latest and owner[0] != latest[0]:
            orphaned.append(trade["paper_trade_id"])
    return {
        "accounting": {
            "as_of_market_day": market_day,
            "cash_inr": str(projection.cash),
            "committed_capital_inr": str(projection.committed),
            "realized_pnl_inr": str(projection.realized),
            "daily_realized_pnl_inr": str(projection.daily_realized),
            "daily_charges_inr": str(projection.daily_charges),
            "daily_slippage_inr": str(projection.daily_slippage),
            "cumulative_charges_inr": str(projection.total_charges),
            "cumulative_slippage_inr": str(projection.total_slippage),
            "gross_daily_realized_loss_inr": str(projection.gross_daily_loss),
            "reserved_worst_case_inr": str(projection.reserved),
            "equity_inr": None if projection.equity is None else str(projection.equity),
            "unrealized_pnl_inr": None if projection.unrealized is None else str(projection.unrealized),
            "capital_lower_bound_inr": str(projection.capital_lower_bound),
            "effective_capital_inr": str(projection.effective_capital),
            "stop_risk_budget_inr": str(projection.stop_risk_budget),
            "unpriced_positions": list(projection.unpriced),
            "charge_reconciliation": "DISCREPANCY" if projection.charge_discrepancies else "EXACT",
            "charge_discrepancies": [{"paper_trade_id": key, "seq": seq}
                                     for key, seq in projection.charge_discrepancies],
            "reservation_source": "persisted_entry_max_loss_less_entry_debit",
            "risk_policy_id": account["risk_policy_id"],
            "policy_status": "V2_ADMISSION_POLICY" if account["risk_policy_id"] == "paper-risk-v2"
                             else "LEGACY_ACCOUNT_PROJECTION",
        },
        "capital_inr": str(capital),
        "cash_inr": str(pl.cash(conn, account_id)) if market_day is None else None,
        "signals": len(decisions),
        "accepted": sum(d["outcome"] == "OPENED" for d in decisions),
        "rejected": sum(d["outcome"] == "REJECTED" for d in decisions),
        "unpriced_signals": sum(d["outcome"] == "UNPRICED" for d in decisions),
        "unpriced_exits": [t["paper_trade_id"] for t in trades if t["state"] == "EXIT_UNPRICED"],
        "expired_unresolved": [t["paper_trade_id"] for t in trades if t["state"] == "EXPIRED_UNRESOLVED"],
        "open": [t["paper_trade_id"] for t in trades if t["state"] in pl.OPEN],
        "pending_exits": [t["paper_trade_id"] for t in trades if t["state"] in pl.OPEN and t["paper_trade_id"] in model_exits
                          and (market_day is None or datetime.fromisoformat(model_exits[t["paper_trade_id"]])
                               .astimezone(IST).date().isoformat() <= market_day)],
        "orphaned_open": orphaned,
        "rejection_reasons": dict(sorted(reasons.items())),
        "completed": len(closed),
        "wins": len(wins), "losses": len(losses), "breakeven": sum(n == 0 for n in nets),
        "performance_scope": "completed trades by closing day; partial realized P&L is in accounting",
        "drawdown_scope": "closed-trade net P&L curve; not marked equity drawdown",
        "confidence_limitation": "sample threshold is descriptive, not statistical or strategy validation",
        "win_rate": _ratio(len(wins), len(closed)),
        "avg_win_inr": _mean(wins),
        "avg_loss_inr": _mean(losses),
        "expectancy_inr": _mean(nets),
        "profit_factor": _ratio(sum(wins, _ZERO), -sum(losses, _ZERO)) if losses and sum(losses, _ZERO) else None,
        "gross_pnl_inr": str(sum((t["gross"] for t in closed), _ZERO)),
        "net_pnl_inr": str(sum(nets, _ZERO)),
        "charges_inr": str(sum((t["charges"] for t in closed), _ZERO)),
        "slippage_inr": str(sum((t["slippage"] for t in closed), _ZERO)),
        "max_drawdown_inr": str(drawdown),
        "max_drawdown_pct": _ratio(drawdown * 100, capital),
        "avg_holding_minutes": _mean([Decimal(t["minutes"]) for t in closed]),
        "by_regime": _groups(closed, "regime"),
        "by_time_of_day": _groups(closed, "bucket"),
        "by_family": _groups(closed, "family") if account_id == reg.SHARED else None,
        "sample_warning": "INSUFFICIENT_SAMPLE" if len(closed) < MIN_SAMPLE else None,
    }


def _money(conn, t: dict) -> None:
    events = [dict(r) for r in conn.execute(
        "SELECT * FROM crew_paper_events WHERE paper_trade_id=? AND side IS NOT NULL ORDER BY seq", (t["paper_trade_id"],))]
    signed = [(Decimal(e["price_inr"]) * e["qty"]) * (-1 if e["side"] == "BUY" else 1) for e in events]
    t["gross"] = sum(signed, _ZERO)
    t["charges"] = sum((Decimal(e["charges_inr"]) for e in events), _ZERO)
    t["slippage"] = sum((Decimal(e["slippage_inr"]) for e in events), _ZERO)
    t["net"] = t["gross"] - t["charges"]
    t["minutes"] = (datetime.fromisoformat(events[-1]["occurred_at"])
                    - datetime.fromisoformat(events[0]["occurred_at"])).total_seconds() / 60
    t["bucket"] = px.time_bucket(t["signal_candle"])


def _groups(trades: list[dict], key: str) -> dict:
    out: dict[str, dict] = {}
    for t in trades:
        g = out.setdefault(t[key], {"trades": 0, "wins": 0, "net_pnl_inr": _ZERO})
        g["trades"] += 1
        g["wins"] += t["net"] > 0
        g["net_pnl_inr"] += t["net"]
    return {k: {**v, "net_pnl_inr": str(v["net_pnl_inr"])} for k, v in sorted(out.items())}


def _mean(values: list[Decimal]) -> str | None:
    return str((sum(values, _ZERO) / len(values)).quantize(px.PAISA)) if values else None


def _ratio(num, den) -> str | None:
    return str((Decimal(num) / Decimal(den)).quantize(Decimal("0.0001"))) if den else None
