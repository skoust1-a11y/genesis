"""Paper-trading ledger over the crew journal's v4 ``crew_paper_*`` tables. Simulation only: no order, no broker.

Every write takes the session ``Lease`` and runs in one fenced BEGIN IMMEDIATE transaction (``cj._fenced``).
A signal is decided for all of its accounts in one transaction, so a crash leaves all or none of them.
Cash is derived, never stored: capital - every BUY (premium + charges) + every SELL (premium - charges).
A trade whose exit could not be priced therefore keeps its premium out of cash: the worst case, never a guess.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from app.config import Settings
from app.db import crew_journal as cj
from app.trading import paper_exec as px
from app.trading import paper_registry as reg
from app.trading.option_quote import IST

OPEN = ("OPEN", "PARTIALLY_EXITED")
STRATEGY_NOT_REGISTERED = "STRATEGY_NOT_REGISTERED"
TIMEFRAME_NOT_CLOSED_BAR_SAFE = "TIMEFRAME_NOT_CLOSED_BAR_SAFE"
MAX_POSITIONS = "MAX_POSITIONS"
DUPLICATE_EXPOSURE = "DUPLICATE_EXPOSURE"
CONFLICTING_EXPOSURE = "CONFLICTING_EXPOSURE"
DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
ENTRY_MISSED_AFTER_RESTART = "ENTRY_MISSED_AFTER_RESTART"
EXIT_MISSED_AFTER_RESTART = "EXIT_MISSED_AFTER_RESTART"
PARTIAL_MISSED_AFTER_RESTART = "PARTIAL_MISSED_AFTER_RESTART"
ENTRY_AFTER_CUTOFF = "ENTRY_AFTER_CUTOFF"
_OPEN_SQL = "('OPEN','PARTIALLY_EXITED')"


def paper_trade_id_for(account_id: str, market_day: str, signal_id: str) -> str:
    return f"P_{market_day.replace('-', '')}_{hashlib.sha256(f'{account_id}|{signal_id}'.encode()).hexdigest()[:12]}"


def ensure_accounts(db_path: Path | str, *, lease: cj.Lease, capital: Decimal, now: datetime) -> list[dict]:
    """Open every registered account once. A later start with another capital or risk policy is refused."""
    if not isinstance(capital, Decimal) or not capital.is_finite() or capital <= 0:
        raise cj.InvalidTransition("paper capital must be a positive decimal")
    policy = reg.RISK_POLICY["risk_policy_id"]

    def op(conn: sqlite3.Connection) -> list[dict]:
        for account_id in reg.account_ids():
            row = conn.execute("SELECT * FROM crew_paper_accounts WHERE account_id=?", (account_id,)).fetchone()
            if row is None:
                conn.execute("INSERT INTO crew_paper_accounts (account_id, capital_inr, risk_policy_id, opened_at)"
                             " VALUES (?, ?, ?, ?)", (account_id, str(capital), policy, now.isoformat()))
            elif Decimal(row["capital_inr"]) != capital or row["risk_policy_id"] != policy:
                raise cj.ConsistencyError([{"kind": "paper_account_mismatch", "account_id": account_id,
                                            "stored_capital": row["capital_inr"], "stored_policy": row["risk_policy_id"]}])
        return [dict(r) for r in conn.execute("SELECT * FROM crew_paper_accounts ORDER BY account_id")]

    return cj._fenced(db_path, lease, now, op)


def enter(db_path: Path | str, *, lease: cj.Lease, admission: dict, model_trade: dict, timeframe: str, quote,
          df_5m, now: datetime, settings: Settings, cutoff: str) -> list[dict]:
    """Decide one RESEARCH_ONLY signal for each of its accounts; open a paper trade where it fits.
    At or after ``cutoff`` (IST HH:MM, the model's intraday close) nothing opens: it would be closed at once."""
    inputs = admission["inputs"]
    strategy_id, version = inputs["strategy_id"], inputs["strategy_version"]
    family = reg.family_of(strategy_id, version)
    day = admission["market_day"]
    late = now.astimezone(IST).strftime("%H:%M") >= cutoff
    priced, unpriced = px.executable(quote, now, settings)
    label = px.regime(df_5m)
    risk = reg.RISK_POLICY

    def op(conn: sqlite3.Connection) -> list[dict]:
        out = []
        for account_id in reg.accounts_for(strategy_id, version):
            prior = conn.execute("SELECT * FROM crew_paper_decisions WHERE account_id=? AND admission_id=?",
                                 (account_id, admission["admission_id"])).fetchone()
            if prior is not None:
                out.append({**dict(prior), "replayed": True})
                continue
            outcome, reasons, entry = "REJECTED", [], None
            if family is None:
                reasons = [STRATEGY_NOT_REGISTERED]
            elif timeframe not in reg.PAPER_TIMEFRAMES:
                reasons = [TIMEFRAME_NOT_CLOSED_BAR_SAFE]
            elif late:
                reasons = [ENTRY_AFTER_CUTOFF]
            elif priced is None:
                outcome, reasons = "UNPRICED", [unpriced]
            else:
                reasons = _risk_gate(conn, account_id, day, model_trade, priced)
                if not reasons:
                    entry, why = px.size(priced, now=now, settings=settings, ticks=risk["slippage_ticks"],
                                         max_lots=risk["max_lots"], loss_cap=Decimal(risk["max_loss_per_trade_inr"]),
                                         funds=cash(conn, account_id))
                    reasons = [] if entry else [why]
            ptid = None
            if entry is not None:
                outcome, ptid = "OPENED", paper_trade_id_for(account_id, day, admission["signal_id"])
                _open(conn, ptid, account_id=account_id, admission=admission, model_trade=model_trade,
                      family=family, quote=priced, entry=entry, regime=label, lease=lease, now=now, settings=settings)
            conn.execute(
                "INSERT INTO crew_paper_decisions (account_id, admission_id, market_day, strategy_id, family,"
                " registry_hash, outcome, reasons, paper_trade_id, decided_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (account_id, admission["admission_id"], day, strategy_id, family, reg.REGISTRY_HASH, outcome,
                 json.dumps(reasons), ptid, now.isoformat()))
            out.append({"account_id": account_id, "outcome": outcome, "reasons": reasons, "paper_trade_id": ptid,
                        "replayed": False})
        return out

    return cj._fenced(db_path, lease, now, op)


def partial(db_path: Path | str, *, lease: cj.Lease, model_trade_id: str, chain: dict | None, index_spot: float,
            reason: str, now: datetime, settings: Settings) -> list[dict]:
    """Mirror a model half-exit: sell half the paper lots at the bid. One lot, or no book, holds the position."""
    def act(conn, trade):
        half = trade["lots_open"] // 2
        quote, unpriced = _book(trade, chain, now, settings)
        if half == 0 or quote is None:
            why = "SINGLE_LOT" if half == 0 else f"UNPRICED:{unpriced}"
            return _event(conn, trade, "PARTIAL_SKIPPED", trade["state"], f"{reason}:{why}", trade["lots_open"],
                          key=f"{trade['paper_trade_id']}:partial", index_spot=index_spot, lease=lease, now=now)
        sold = px.fill(quote, "SELL", half, now=now, settings=settings, ticks=reg.RISK_POLICY["slippage_ticks"])
        return _event(conn, trade, "PARTIAL_EXIT", "PARTIALLY_EXITED", reason, trade["lots_open"] - half,
                      key=f"{trade['paper_trade_id']}:partial", fill=sold, index_spot=index_spot, lease=lease, now=now)

    return _each_open(db_path, lease, now, model_trade_id, act)


def close(db_path: Path | str, *, lease: cj.Lease, model_trade_id: str, chain: dict | None, index_spot: float,
          reason: str, now: datetime, settings: Settings) -> list[dict]:
    """Mirror a model exit: sell every open paper lot at the bid, or record EXIT_UNPRICED. Never priced later."""
    def act(conn, trade):
        quote, unpriced = _book(trade, chain, now, settings)
        key = f"{trade['paper_trade_id']}:exit"
        if quote is None:
            # ponytail: priced on the exit tick only; add a short re-quote window if too many exits go unpriced.
            return _event(conn, trade, "EXIT_UNPRICED", "EXIT_UNPRICED", f"{reason}:{unpriced}", trade["lots_open"],
                          key=key, index_spot=index_spot, lease=lease, now=now)
        sold = px.fill(quote, "SELL", trade["lots_open"], now=now, settings=settings,
                       ticks=reg.RISK_POLICY["slippage_ticks"])
        return _event(conn, trade, "EXIT", "CLOSED", reason, 0, key=key, fill=sold, index_spot=index_spot,
                      lease=lease, now=now)

    return _each_open(db_path, lease, now, model_trade_id, act)


def recover(db_path: Path | str, *, lease: cj.Lease, now: datetime) -> dict:
    """Startup, after crew recovery: refuse an inconsistent ledger, expire prior days, and close the gaps a crash
    leaves (an exit or an entry the paper ledger never saw) without pricing anything after the fact."""
    def op(conn: sqlite3.Connection) -> dict:
        findings = consistency(conn)
        if findings:
            raise cj.ConsistencyError(findings)
        report = {"expired": [], "exit_missed": [], "partial_missed": [], "entry_missed": []}
        for trade in _rows(conn, f"SELECT * FROM crew_paper_trades WHERE state IN {_OPEN_SQL} ORDER BY paper_trade_id"):
            model = conn.execute("SELECT state FROM crew_trades WHERE trade_id=?", (trade["model_trade_id"],)).fetchone()
            if trade["market_day"] < lease.market_day:
                _event(conn, trade, "EXPIRED_UNRESOLVED", "EXPIRED_UNRESOLVED", "PRIOR_MARKET_DAY", trade["lots_open"],
                       key=f"{trade['paper_trade_id']}:expired", lease=lease, now=now)
                report["expired"].append(trade["paper_trade_id"])
            elif model["state"] not in ("TRACKING", "PARTIALLY_EXITED"):
                _event(conn, trade, "EXIT_UNPRICED", "EXIT_UNPRICED", EXIT_MISSED_AFTER_RESTART, trade["lots_open"],
                       key=f"{trade['paper_trade_id']}:exit", lease=lease, now=now)
                report["exit_missed"].append(trade["paper_trade_id"])
            elif model["state"] == "PARTIALLY_EXITED":
                # The model half-exited but the paper ledger never saw it; the lots are held, never priced late.
                done = _event(conn, trade, "PARTIAL_SKIPPED", trade["state"], PARTIAL_MISSED_AFTER_RESTART,
                              trade["lots_open"], key=f"{trade['paper_trade_id']}:partial", lease=lease, now=now)
                if not done["replayed"]:
                    report["partial_missed"].append(trade["paper_trade_id"])
        missing = _rows(conn, "SELECT a.admission_id, a.market_day, json_extract(a.inputs, '$.strategy_id') AS sid,"
                              " json_extract(a.inputs, '$.strategy_version') AS ver FROM crew_trades t"
                              " JOIN crew_admissions a ON a.admission_id = t.admission_id"
                              " WHERE t.market_day = ? AND t.admission_class = 'RESEARCH_ONLY'"
                              " ORDER BY a.decided_at, a.admission_id", (lease.market_day,))
        for row in missing:
            family = reg.family_of(row["sid"], row["ver"])
            for account_id in reg.accounts_for(row["sid"], row["ver"]):
                if conn.execute("SELECT 1 FROM crew_paper_decisions WHERE account_id=? AND admission_id=?",
                                (account_id, row["admission_id"])).fetchone():
                    continue
                conn.execute(
                    "INSERT INTO crew_paper_decisions (account_id, admission_id, market_day, strategy_id, family,"
                    " registry_hash, outcome, reasons, paper_trade_id, decided_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, 'REJECTED', ?, NULL, ?)",
                    (account_id, row["admission_id"], row["market_day"], row["sid"], family, reg.REGISTRY_HASH,
                     json.dumps([ENTRY_MISSED_AFTER_RESTART]), now.isoformat()))
                report["entry_missed"].append({"account_id": account_id, "admission_id": row["admission_id"]})
        return report

    return cj._fenced(db_path, lease, now, op)


def cash(conn: sqlite3.Connection, account_id: str) -> Decimal:
    capital = conn.execute("SELECT capital_inr FROM crew_paper_accounts WHERE account_id=?", (account_id,)).fetchone()
    if capital is None:
        raise cj.InvalidTransition(f"unknown paper account {account_id}")
    total = Decimal(capital[0])
    for side, price, qty, charges in conn.execute(
            "SELECT e.side, e.price_inr, e.qty, e.charges_inr FROM crew_paper_events e"
            " JOIN crew_paper_trades t ON t.paper_trade_id = e.paper_trade_id"
            " WHERE t.account_id=? AND e.side IS NOT NULL ORDER BY e.event_id", (account_id,)):
        value = Decimal(price) * qty
        total += -(value + Decimal(charges)) if side == "BUY" else value - Decimal(charges)
    return total


def trade_cashflow(conn: sqlite3.Connection, paper_trade_id: str) -> Decimal:
    """Net rupees received minus paid for one trade so far. Final P&L only once the trade is CLOSED."""
    net = Decimal(0)
    for side, price, qty, charges in conn.execute(
            "SELECT side, price_inr, qty, charges_inr FROM crew_paper_events WHERE paper_trade_id=? AND side IS NOT NULL",
            (paper_trade_id,)):
        value = Decimal(price) * qty
        net += -(value + Decimal(charges)) if side == "BUY" else value - Decimal(charges)
    return net


def consistency(conn: sqlite3.Connection) -> list[dict]:
    """Paper invariants. Reads only."""
    findings = []
    for trade in _rows(conn, "SELECT * FROM crew_paper_trades ORDER BY paper_trade_id"):
        tid = trade["paper_trade_id"]
        events = _rows(conn, "SELECT * FROM crew_paper_events WHERE paper_trade_id=? ORDER BY seq", (tid,))
        if [e["seq"] for e in events] != list(range(1, len(events) + 1)) or events[0]["event_type"] != "ENTRY":
            findings.append({"kind": "paper_sequence", "paper_trade_id": tid})
            continue
        last = events[-1]
        if (trade["version"], trade["state"], trade["lots_open"]) != (last["seq"], last["to_state"], last["lots_after"]):
            findings.append({"kind": "paper_projection", "paper_trade_id": tid})
        held = sum(e["qty"] if e["side"] == "BUY" else -e["qty"] for e in events if e["side"])
        if held != trade["lots_open"] * trade["lot_size"]:
            findings.append({"kind": "paper_quantity", "paper_trade_id": tid, "held": held})
        if not conn.execute("SELECT 1 FROM crew_paper_decisions WHERE paper_trade_id=? AND outcome='OPENED'",
                            (tid,)).fetchone():
            findings.append({"kind": "paper_trade_without_decision", "paper_trade_id": tid})
    for (account_id,) in conn.execute("SELECT account_id FROM crew_paper_accounts ORDER BY account_id").fetchall():
        if cash(conn, account_id) < 0:
            findings.append({"kind": "paper_negative_cash", "account_id": account_id})
    return findings


def check_consistency(db_path: Path | str) -> list[dict]:
    conn = cj.connect_readonly(db_path)
    try:
        return consistency(conn)
    finally:
        conn.close()


# --- internals -----------------------------------------------------------------------------------------

def _risk_gate(conn, account_id: str, day: str, model_trade: dict, quote) -> list[str]:
    open_rows = _rows(conn, f"SELECT * FROM crew_paper_trades WHERE account_id=? AND state IN {_OPEN_SQL}",
                      (account_id,))
    if len(open_rows) >= reg.max_positions(account_id):
        return [MAX_POSITIONS]
    for row in open_rows:
        if row["underlying"] == quote.underlying:
            same = row["direction"] == model_trade["direction"] and row["expiry"] == quote.expiry
            return [DUPLICATE_EXPOSURE if same else CONFLICTING_EXPOSURE]
    done = _rows(conn, "SELECT paper_trade_id FROM crew_paper_trades WHERE account_id=? AND market_day=?"
                       f" AND state NOT IN {_OPEN_SQL}", (account_id, day))
    realized = sum((trade_cashflow(conn, r["paper_trade_id"]) for r in done), Decimal(0))
    if realized <= -Decimal(reg.RISK_POLICY["daily_loss_limit_inr"]):
        return [DAILY_LOSS_LIMIT]
    return []


def _open(conn, ptid, *, account_id, admission, model_trade, family, quote, entry: px.Fill, regime, lease, now,
          settings) -> None:
    inputs = admission["inputs"]
    loss = px.max_loss(entry, settings, admission["market_day"])
    conn.execute(
        "INSERT INTO crew_paper_trades (paper_trade_id, account_id, admission_id, model_trade_id, market_day,"
        " strategy_id, family, strategy_version, registry_hash, underlying, direction, contract, expiry, strike,"
        " option_type, lot_size, lots_initial, lots_open, entry_spot, stop_spot, target_spot, signal_candle, regime,"
        " capital_committed_inr, max_loss_inr, state, opened_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)",
        (ptid, account_id, admission["admission_id"], model_trade["trade_id"], admission["market_day"],
         inputs["strategy_id"], family, inputs["strategy_version"], reg.REGISTRY_HASH, quote.underlying,
         model_trade["direction"], model_trade["contract"], quote.expiry, quote.strike, quote.option_type,
         quote.lot_size, entry.lots, entry.lots, model_trade["entry_spot"], model_trade["stop_spot"],
         model_trade["target_spot"], inputs["signal_candle"], regime, str(entry.value + entry.charges_inr),
         str(loss), now.isoformat()))
    _insert(conn, ptid, 1, "ENTRY", None, "OPEN", "SIGNAL_ADMITTED", entry.lots, key=f"{ptid}:entry", fill=entry,
            index_spot=model_trade["entry_spot"], lease=lease, now=now)


def _each_open(db_path, lease, now, model_trade_id, act) -> list[dict]:
    def op(conn: sqlite3.Connection) -> list[dict]:
        trades = _rows(conn, f"SELECT * FROM crew_paper_trades WHERE model_trade_id=? AND state IN {_OPEN_SQL}"
                             " ORDER BY paper_trade_id", (model_trade_id,))
        return [act(conn, t) for t in trades]

    return cj._fenced(db_path, lease, now, op)


def _book(trade, chain, now, settings):
    quote = px.contract_quote(chain, strike=trade["strike"], option_type=trade["option_type"], expiry=trade["expiry"])
    return px.executable(quote, now, settings)


def _event(conn, trade, event_type, to_state, reason, lots_after, *, key, lease, now, fill=None, index_spot=None):
    if conn.execute("SELECT 1 FROM crew_paper_events WHERE idempotency_key=?", (key,)).fetchone():
        return {"paper_trade_id": trade["paper_trade_id"], "event_type": event_type, "replayed": True}
    seq = trade["version"] + 1
    _insert(conn, trade["paper_trade_id"], seq, event_type, trade["state"], to_state, reason, lots_after, key=key,
            fill=fill, index_spot=index_spot, lease=lease, now=now)
    terminal = to_state not in OPEN
    cur = conn.execute("UPDATE crew_paper_trades SET state=?, lots_open=?, version=?, closed_at=?"
                       " WHERE paper_trade_id=? AND version=?",
                       (to_state, lots_after, seq, now.isoformat() if terminal else None, trade["paper_trade_id"],
                        trade["version"]))
    if cur.rowcount != 1:
        raise cj.VersionConflict(trade["paper_trade_id"])
    return {"paper_trade_id": trade["paper_trade_id"], "event_type": event_type, "state": to_state,
            "lots_open": lots_after, "replayed": False}


def _insert(conn, ptid, seq, event_type, from_state, to_state, reason, lots_after, *, key, lease, now, fill=None,
            index_spot=None) -> None:
    f = fill
    conn.execute(
        "INSERT INTO crew_paper_events (paper_trade_id, seq, event_type, from_state, to_state, reason, lots_after,"
        " side, lots, qty, price_inr, bid, ask, ltp, mid, quote_time, fetched_at, quote_age_s, quote_source,"
        " slippage_inr, charges, charges_inr, index_spot, session_id, occurred_at, idempotency_key)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ptid, seq, event_type, from_state, to_state, reason, lots_after,
         *((f.side, f.lots, f.qty, str(f.price), str(f.bid), str(f.ask), None if f.ltp is None else str(f.ltp),
            str(f.mid), f.quote_time, f.fetched_at, f.quote_age_s, f.quote_source, str(f.slippage_inr),
            json.dumps(f.charges, sort_keys=True), str(f.charges_inr)) if f else (None,) * 15),
         index_spot, lease.session_id, now.isoformat(), key))


def _rows(conn, sql: str, args=()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, args)]
