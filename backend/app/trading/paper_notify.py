"""Paper-trading notifications, enqueued into the durable crew outbox. Nothing here sends or schedules a send.

Every message is rebuilt from journal rows (admissions, paper decisions, paper events), so ``sync`` is idempotent:
a publication id is the hash of its purpose, a replay is a no-op, and a crash anywhere is healed by the next sync.
Notifications only read the paper ledger; no delivery outcome can change it. Rows are kind ``OPS`` (no trade
link), so the ACTIONABLE-only recommendation path is untouched.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from app.db import crew_journal as cj
from app.db import crew_outbox as ob
from app.db import paper_ledger as pl
from app.trading import paper_stats as ps
from app.trading.option_quote import IST

logger = logging.getLogger("genesis.paper_notify")

# ponytail: no paper channel exists; the sentinel keeps every row undeliverable until a dispatcher and a fresh
# bot token are separately approved.
CHAT_ID = "mock:paper"
FOOTER = "Mode: PAPER ONLY\nNo real broker order placed."
SUMMARY_AFTER = "15:30"
_STOPS = ("STOP_LOSS_HIT", "TRAILING_STOP_HIT")


def sync(db_path: Path | str, *, lease: cj.Lease, now: datetime) -> list[str]:
    """Enqueue every due paper notification for the lease's market day. Returns the new publication ids."""
    conn = cj.connect_readonly(db_path)
    try:
        messages = due(conn, lease.market_day, now)
        known = {r[0] for r in conn.execute("SELECT publication_id FROM crew_outbox WHERE kind='OPS'")}
    finally:
        conn.close()
    new = []
    for purpose, body in messages:
        pid = ob.publication_id_for(kind="OPS", purpose=purpose)
        if pid in known:
            continue
        try:
            ob.enqueue_publication(db_path, lease=lease, kind="OPS", purpose=purpose, body=body, chat_id=CHAT_ID,
                                   now=now)
            new.append(pid)
        except cj.InvalidTransition as e:  # a message that cannot be stored never stops the ledger
            logger.warning("paper notification %s not enqueued: %s", purpose, e)
    return new


def due(conn: sqlite3.Connection, day: str, now: datetime) -> list[tuple[str, str]]:
    """(purpose, body) for the day, in journal order. Pure function of the rows and ``now``."""
    out = []
    for a in conn.execute("SELECT * FROM crew_admissions WHERE market_day=? ORDER BY decided_at, admission_id", (day,)):
        inputs = json.loads(a["inputs"])
        if a["class"] == "REJECTED":
            out.append((f"paper|SIGNAL_REJECTED|{a['admission_id']}", _signal(a, inputs, "SIGNAL_REJECTED")))
        else:
            out.append((f"paper|SIGNAL_DETECTED|{a['admission_id']}", _signal(a, inputs, "SIGNAL_DETECTED")))
    for d in conn.execute("SELECT d.*, a.inputs FROM crew_paper_decisions d JOIN crew_admissions a"
                          " ON a.admission_id = d.admission_id WHERE d.market_day=? AND d.outcome <> 'OPENED'"
                          " ORDER BY d.decided_at, d.admission_id, d.account_id", (day,)):
        reasons = json.loads(d["reasons"])
        kind = "PAPER_TRADE_CANCELLED" if pl.ENTRY_MISSED_AFTER_RESTART in reasons else "SIGNAL_REJECTED"
        out.append((f"paper|{kind}|{d['account_id']}|{d['admission_id']}", _decision(d, reasons, kind)))
    for e in conn.execute("SELECT e.*, t.* , e.reason AS event_reason FROM crew_paper_events e JOIN crew_paper_trades t"
                          " ON t.paper_trade_id = e.paper_trade_id WHERE t.market_day=? OR e.occurred_at >= ?"
                          " ORDER BY e.occurred_at, e.event_id", (day, day)):
        kind = _event_kind(e)
        out.append((f"paper|{kind}|{e['paper_trade_id']}|{e['seq']}", _event(conn, e, kind)))
    stamp = now.astimezone(IST)
    open_today = conn.execute("SELECT 1 FROM crew_paper_trades WHERE market_day=? AND state IN ('OPEN','PARTIALLY_EXITED')",
                              (day,)).fetchone()
    if stamp.date().isoformat() == day and stamp.strftime("%H:%M") >= SUMMARY_AFTER and not open_today:
        for (account_id,) in conn.execute("SELECT account_id FROM crew_paper_accounts ORDER BY account_id").fetchall():
            out.append((f"paper|DAILY_STRATEGY_SUMMARY|{account_id}|{day}",
                        _summary(account_id, day, ps.account_stats(conn, account_id, market_day=day))))
    return out


# --- message bodies: every value from a stored row; a missing value is said, never filled in ------------------

def _signal(a, inputs: dict, kind: str) -> str:
    quote = inputs.get("quote") or {}
    contract = inputs.get("contract") or {}
    lines = [f"GENESIS — {kind.replace('_', ' ')}",
             f"Strategy: {inputs['strategy_id']} ({inputs['strategy_version']})",
             f"Market: {inputs['instrument']}   Direction: BUY {_side(inputs['direction'])}",
             f"Signal candle: {_ist(inputs['signal_candle'])}",
             f"Admission: {a['class']} ({', '.join(json.loads(a['reasons'])) or 'no reasons'})",
             f"Option: {contract.get('tradingsymbol') or 'not resolved'}",
             f"Quote grade: {quote.get('grade') or 'none'}   bid {_opt(quote.get('bid'))} / ask {_opt(quote.get('ask'))}"
             f" / last {_opt(quote.get('ltp'))}",
             f"Signal ID: {a['signal_id'][:12]}   Admission ID: {a['admission_id']}",
             f"Time: {_ist(a['decided_at'])}"]
    return "\n".join(lines + [FOOTER])


def _decision(d, reasons: list[str], kind: str) -> str:
    inputs = json.loads(d["inputs"])
    return "\n".join([f"GENESIS — {kind.replace('_', ' ')}",
                      f"Strategy: {d['strategy_id']} ({d['family'] or 'unregistered'})",
                      f"Account: {d['account_id']}   Outcome: {d['outcome']}",
                      f"Reasons: {', '.join(reasons)}",
                      f"Market: {inputs['instrument']}   Direction: BUY {_side(inputs['direction'])}",
                      f"Admission ID: {d['admission_id']}",
                      f"Time: {_ist(d['decided_at'])}", FOOTER])


def _event_kind(e) -> str:
    kind = {"ENTRY": "PAPER_ENTRY_FILLED", "PARTIAL_EXIT": "PAPER_PARTIAL_EXIT", "PARTIAL_SKIPPED": "PAPER_PARTIAL_EXIT",
            "EXIT_UNPRICED": "SYSTEM_WARNING", "EXPIRED_UNRESOLVED": "PAPER_TRADE_CANCELLED"}.get(e["event_type"])
    if kind:
        return kind
    reason = e["event_reason"]
    return "PAPER_STOP_LOSS" if reason in _STOPS else "PAPER_TARGET_HIT" if reason == "TARGET_HIT" else "PAPER_FINAL_EXIT"


def _event(conn, e, kind: str) -> str:
    head = [f"GENESIS — {'PAPER TRADE' if kind == 'PAPER_ENTRY_FILLED' else kind.replace('_', ' ')}",
            f"Strategy: {e['strategy_id']} ({e['family']}, {e['strategy_version']})",
            f"Account: {e['account_id']}",
            f"Market: {e['underlying']}   Direction: BUY {e['option_type']}"]
    if e["event_type"] == "ENTRY":
        body = [f"Index Entry: {e['entry_spot']:g}   Index Stop: {e['stop_spot']:g}   Index Target: {e['target_spot']:g}",
                f"Option: {e['contract']} (expiry {e['expiry']}, strike {e['strike']:g} {e['option_type']}, lot {e['lot_size']})",
                _book(e),
                f"Entry Premium: {_inr(e['price_inr'])}   Quantity: {e['lots']} lots = {e['qty']}",
                f"Charges: {_inr(e['charges_inr'])}   Slippage vs mid: {_inr(e['slippage_inr'])}",
                f"Capital Committed: {_inr(e['capital_committed_inr'])}   Estimated Risk (max loss): {_inr(e['max_loss_inr'])}"]
    elif e["side"] == "SELL":
        body = [f"Reason: {e['event_reason']}   Index: {_opt(e['index_spot'])}",
                _book(e),
                f"Exit Premium: {_inr(e['price_inr'])}   Sold: {e['lots']} lots = {e['qty']}   Lots left: {e['lots_after']}",
                f"Charges: {_inr(e['charges_inr'])}   Slippage vs mid: {_inr(e['slippage_inr'])}"]
        if e["to_state"] == "CLOSED":
            body.append(f"Net P&L (trade): {_inr(str(pl.trade_cashflow(conn, e['paper_trade_id'])), signed=True)}")
    else:
        body = [f"Event: {e['event_type']}   Reason: {e['event_reason']}",
                "Premium: not priced; no rupee result is recorded for this step"]
    tail = [f"Paper Trade ID: {e['paper_trade_id']}   Model Trade ID: {e['model_trade_id']}",
            f"Time: {_ist(e['occurred_at'])}", FOOTER]
    return "\n".join(head + body + tail)


def _summary(account_id: str, day: str, s: dict) -> str:
    return "\n".join([
        "GENESIS — DAILY STRATEGY SUMMARY", f"Account: {account_id}   Day: {day}",
        f"Signals: {s['signals']}   Accepted: {s['accepted']}   Rejected: {s['rejected']}   Unpriced: {s['unpriced_signals']}",
        f"Completed: {s['completed']}   Unpriced exits: {len(s['unpriced_exits'])}   Win rate: {_opt(s['win_rate'])}",
        f"Net P&L: {_inr(s['net_pnl_inr'], signed=True)}   Charges: {_inr(s['charges_inr'])}   Slippage: {_inr(s['slippage_inr'])}",
        f"Max drawdown: {_inr(s['max_drawdown_inr'])}   Sample: {s['sample_warning'] or 'ok'}",
        FOOTER])


def _book(e) -> str:
    return (f"Quote: bid {_opt(e['bid'])} / ask {_opt(e['ask'])} / last {_opt(e['ltp'])} at {_ist(e['quote_time'])}"
            f" (age {e['quote_age_s']}s)")


def _side(direction: str) -> str:
    return {"CALL": "CE", "PUT": "PE"}.get(direction, direction)


def _inr(text: str | None, signed: bool = False) -> str:
    if text is None:
        return "not priced"
    value = Decimal(text)
    sign = "-" if value < 0 else "+" if signed else ""
    return f"{sign}₹{abs(value):,.2f}"


def _opt(value) -> str:
    return "n/a" if value is None else str(value)


def _ist(text: str | None) -> str:
    if not text:
        return "n/a"
    return datetime.fromisoformat(text).astimezone(IST).strftime("%Y-%m-%d %H:%M:%S IST")
