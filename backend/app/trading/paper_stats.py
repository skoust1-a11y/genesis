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
    capital = Decimal(conn.execute("SELECT capital_inr FROM crew_paper_accounts WHERE account_id=?",
                                   (account_id,)).fetchone()[0])
    decisions = [dict(r) for r in conn.execute(
        f"SELECT outcome, reasons FROM crew_paper_decisions WHERE account_id=? {day_sql}", (account_id, *day_args))]
    trades = [dict(r) for r in conn.execute(
        f"SELECT * FROM crew_paper_trades WHERE account_id=? {day_sql} ORDER BY closed_at, paper_trade_id",
        (account_id, *day_args))]
    closed = [t for t in trades if t["state"] == "CLOSED"]
    for t in closed:
        _money(conn, t)
    nets = [t["net"] for t in closed]
    wins, losses = [n for n in nets if n > 0], [n for n in nets if n <= 0]
    curve, peak, drawdown = capital, capital, _ZERO
    for n in nets:
        curve += n
        peak = max(peak, curve)
        drawdown = max(drawdown, peak - curve)
    reasons = Counter(r for d in decisions if d["outcome"] != "OPENED" for r in json.loads(d["reasons"]))
    return {
        "capital_inr": str(capital),
        "cash_inr": str(pl.cash(conn, account_id)) if market_day is None else None,
        "signals": len(decisions),
        "accepted": sum(d["outcome"] == "OPENED" for d in decisions),
        "rejected": sum(d["outcome"] == "REJECTED" for d in decisions),
        "unpriced_signals": sum(d["outcome"] == "UNPRICED" for d in decisions),
        "unpriced_exits": [t["paper_trade_id"] for t in trades if t["state"] == "EXIT_UNPRICED"],
        "expired_unresolved": [t["paper_trade_id"] for t in trades if t["state"] == "EXPIRED_UNRESOLVED"],
        "open": [t["paper_trade_id"] for t in trades if t["state"] in pl.OPEN],
        "rejection_reasons": dict(sorted(reasons.items())),
        "completed": len(closed),
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
