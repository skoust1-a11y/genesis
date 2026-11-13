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
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from app.config import Settings
from app.db import crew_journal as cj
from app.trading import paper_accounting as pa
from app.trading import paper_exec as px
from app.trading import paper_registry as reg
from app.trading import paper_risk as risk_v2
from app.trading import paper_fees
from app.trading.option_quote import IST
from app.trading.market_validation import closed_bars

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
CONTRACT_LOT_UNVERIFIED = "CONTRACT_LOT_UNVERIFIED"
_OPEN_SQL = "('OPEN','PARTIALLY_EXITED')"
# The committed NSE-circular table, not DATA_DIR: the broker's lot must agree with an independent source.
LOT_TABLE = Path(__file__).resolve().parents[2] / "data" / "lot_sizes" / "nse.yaml"


def lot_verified(underlying: str, market_day: str, broker_lot: int) -> bool:
    """The broker's contract lot agrees with the dated reference table for that day (see ``contract_lot_size``)."""
    from app.market.nse import contract_lot_size, load_lot_sizes  # the read-only crew API must not load app.market

    try:
        return contract_lot_size(load_lot_sizes(LOT_TABLE), underlying, market_day, quoted=broker_lot) == broker_lot
    except ValueError:
        return False


def paper_trade_id_for(account_id: str, market_day: str, signal_id: str) -> str:
    return f"P_{market_day.replace('-', '')}_{hashlib.sha256(f'{account_id}|{signal_id}'.encode()).hexdigest()[:12]}"


def ensure_accounts(db_path: Path | str, *, lease: cj.Lease, capital: Decimal, now: datetime) -> list[dict]:
    """Open every registered account once. A later start with another capital or risk policy is refused."""
    if not isinstance(capital, Decimal) or not capital.is_finite() or capital <= 0:
        raise cj.InvalidTransition("paper capital must be a positive decimal")
    if capital != Decimal("50000"):
        raise cj.ConsistencyError([{"kind": "paper_capital_not_approved", "approved_capital": "50000"}])
    policy = reg.RISK_POLICY["risk_policy_id"]

    def op(conn: sqlite3.Connection) -> list[dict]:
        for account_id in reg.account_ids():
            row = conn.execute("SELECT * FROM crew_paper_accounts WHERE account_id=?", (account_id,)).fetchone()
            if row is None:
                conn.execute("INSERT INTO crew_paper_accounts (account_id, capital_inr, risk_policy_id, opened_at)"
                             " VALUES (?, ?, ?, ?)", (account_id, str(capital), policy, now.isoformat()))
                conn.execute("INSERT INTO crew_paper_account_identity VALUES (?, ?, ?, ?, ?)",
                             (account_id, reg.REGISTRY_HASH, reg.STRATEGY_VERSION, now.isoformat(), "ACCOUNT_CREATED"))
            elif Decimal(row["capital_inr"]) != capital or row["risk_policy_id"] != policy:
                raise cj.ConsistencyError([{"kind": "paper_account_mismatch", "account_id": account_id,
                                            "stored_capital": row["capital_inr"], "stored_policy": row["risk_policy_id"]}])
            identity = conn.execute("SELECT * FROM crew_paper_account_identity WHERE account_id=?",
                                    (account_id,)).fetchone()
            if identity is None:
                raise cj.ConsistencyError([{"kind": "paper_registry_migration_required", "account_id": account_id}])
            if identity["registry_hash"] != reg.REGISTRY_HASH or identity["strategy_version"] != reg.STRATEGY_VERSION:
                raise cj.ConsistencyError([{"kind": "paper_registry_mismatch", "account_id": account_id}])
        return [dict(r) for r in conn.execute("SELECT * FROM crew_paper_accounts ORDER BY account_id")]

    return cj._fenced(db_path, lease, now, op)


def bind_legacy_accounts(db_path: Path | str, *, lease: cj.Lease, expected_registry_hash: str,
                         reason: str, now: datetime) -> list[str]:
    """Explicit fenced migration; attest empty accounts and verify every recorded registry.

    Never changes capital, risk policy, trades or events. A different risk policy
    needs a separate simulation scenario, not relabeling an old account.
    """
    if expected_registry_hash != reg.REGISTRY_HASH or not isinstance(reason, str) or not reason.strip():
        raise cj.InvalidTransition("explicit current registry hash and migration reason required")

    def op(conn):
        accounts = list(conn.execute("SELECT * FROM crew_paper_accounts ORDER BY account_id"))
        changed = []
        for account in accounts:
            aid = account["account_id"]
            if (aid not in reg.account_ids() or Decimal(account["capital_inr"]) != Decimal("50000")
                    or account["risk_policy_id"] != reg.RISK_POLICY["risk_policy_id"]):
                raise cj.ConsistencyError([{"kind": "legacy_account_incompatible", "account_id": aid}])
            hashes = {r[0] for r in conn.execute(
                "SELECT registry_hash FROM crew_paper_decisions WHERE account_id=? UNION "
                "SELECT registry_hash FROM crew_paper_trades WHERE account_id=?", (aid, aid))}
            versions = {r[0] for r in conn.execute(
                "SELECT strategy_version FROM crew_paper_trades WHERE account_id=?", (aid,))}
            if hashes - {expected_registry_hash} or versions - {reg.STRATEGY_VERSION}:
                raise cj.ConsistencyError([{"kind": "legacy_registry_conflict", "account_id": aid}])
            old = conn.execute("SELECT * FROM crew_paper_account_identity WHERE account_id=?", (aid,)).fetchone()
            if old:
                if old["registry_hash"] != expected_registry_hash or old["strategy_version"] != reg.STRATEGY_VERSION:
                    raise cj.ConsistencyError([{"kind": "paper_registry_mismatch", "account_id": aid}])
                continue
            provenance = json.dumps({"migration": "bind-v4-registry-v1", "reason": reason.strip(),
                                     "evidence": "RECORDED_HASH" if hashes else "EXPLICIT_EMPTY_ACCOUNT_ATTESTATION",
                                     "session_id": lease.session_id}, sort_keys=True)
            conn.execute("INSERT INTO crew_paper_account_identity VALUES (?, ?, ?, ?, ?)",
                         (aid, expected_registry_hash, reg.STRATEGY_VERSION, now.isoformat(), provenance))
            changed.append(aid)
        return changed

    return cj._fenced(db_path, lease, now, op)


def enter(db_path: Path | str, *, lease: cj.Lease, admission: dict, model_trade: dict, timeframe: str, quote,
          df_5m, now: datetime, settings: Settings, cutoff: str,
          delta_evidence: risk_v2.DeltaEvidence | None = None,
          trusted_delta_sources: frozenset[str] = frozenset(),
          cost_policy_id: str | None = None,
          allow_historical_brokerage_assumption: bool = False,
          spec: object | None = None) -> list[dict]:
    """Decide one RESEARCH_ONLY signal for each of its accounts; open a paper trade where it fits.
    At or after ``cutoff`` (IST HH:MM, the model's intraday close) nothing opens: it would be closed at once."""
    inputs = admission["inputs"]
    strategy_id, version = inputs["strategy_id"], inputs["strategy_version"]
    family = reg.family_of(strategy_id, version)
    effective_spec = spec if spec is not None else inputs.get("spec")
    eligible, elig_reasons = reg.check_paper_admission_eligibility(
        strategy_id,
        version,
        spec=effective_spec,
    )
    if eligible and (
        inputs.get("quarantined") is True
        or inputs.get("paper_eligible") is False
        or inputs.get("paper_promotion_eligible") is False
        or str(inputs.get("fidelity_tier") or inputs.get("validation_fidelity_tier") or "").upper() == "TIER_2_SYNTHETIC"
    ):
        eligible = False
        extra_reasons = [str(r) for r in (inputs.get("quarantine_reasons") or []) if str(r)]
        elig_reasons = ["STRATEGY_QUARANTINED", *(extra_reasons or ["QUARANTINED_IN_ADMISSION_INPUTS"])]
    day = admission["market_day"]
    late = now.astimezone(IST).strftime("%H:%M") >= min(cutoff, "15:10")
    from app.market.hours import market_status
    session_open, session_reason = market_status(now, settings)
    priced, unpriced = px.executable(quote, now, settings)
    label = px.regime(df_5m)
    regime_vec = px.regime_v2(df_5m, now=now)
    risk = reg.RISK_POLICY
    strategy_lots = reg.strategy_max_lots(family) if family else None
    candles_valid = risk["risk_policy_id"] != risk_v2.POLICY_ID or not closed_bars(df_5m, now).empty

    def op(conn: sqlite3.Connection) -> list[dict]:
        persisted = conn.execute("SELECT inputs,market_day,signal_id,class FROM crew_admissions WHERE admission_id=?",
                                 (admission["admission_id"],)).fetchone()
        if (persisted is None or json.loads(persisted["inputs"]) != inputs
                or persisted["market_day"] != day or persisted["signal_id"] != admission["signal_id"]
                or persisted["class"] != "RESEARCH_ONLY"):
            raise cj.ConsistencyError([{"kind": "paper_admission_evidence", "admission_id": admission["admission_id"]}])
        model = conn.execute("SELECT * FROM crew_trades WHERE trade_id=?", (model_trade["trade_id"],)).fetchone()
        if (model is None or model["admission_id"] != admission["admission_id"]
                or any(model[key] != model_trade[key] for key in
                       ("direction", "entry_spot", "stop_spot", "target_spot"))):
            raise cj.ConsistencyError([{"kind": "paper_model_evidence", "trade_id": model_trade["trade_id"]}])
        out = []
        for account_id in reg.accounts_for(strategy_id, version):
            identity = conn.execute(
                "SELECT a.capital_inr, a.risk_policy_id, i.registry_hash, i.strategy_version "
                "FROM crew_paper_accounts a LEFT JOIN crew_paper_account_identity i USING(account_id) "
                "WHERE a.account_id=?", (account_id,)).fetchone()
            if (identity is None or Decimal(identity["capital_inr"]) != Decimal("50000")
                    or identity["risk_policy_id"] != risk["risk_policy_id"]
                    or identity["registry_hash"] != reg.REGISTRY_HASH
                    or identity["strategy_version"] != reg.STRATEGY_VERSION):
                raise cj.ConsistencyError([{"kind": "paper_admission_identity", "account_id": account_id}])
            prior = conn.execute("SELECT * FROM crew_paper_decisions WHERE account_id=? AND admission_id=?",
                                 (account_id, admission["admission_id"])).fetchone()
            if prior is not None:
                out.append({**dict(prior), "replayed": True})
                continue
            outcome, reasons, entry = "REJECTED", [], None
            projection, sized = None, None
            if model["owner_session_id"] != lease.session_id or model["state"] != "TRACKING":
                reasons = ["MODEL_NOT_OWNED_TRACKING"]
            elif not eligible:
                reasons = list(elig_reasons)
            elif family is None:
                reasons = [STRATEGY_NOT_REGISTERED]
            elif timeframe not in reg.PAPER_TIMEFRAMES:
                reasons = [TIMEFRAME_NOT_CLOSED_BAR_SAFE]
            elif late:
                reasons = [ENTRY_AFTER_CUTOFF]
            elif not session_open:
                reasons = [f"MARKET_CLOSED:{session_reason}"]
            elif not candles_valid:
                reasons = ["INVALID_OR_STALE_CANDLES"]
            elif priced is None:
                outcome, reasons = "UNPRICED", [unpriced]
            elif (priced.option_type != {"CALL": "CE", "PUT": "PE"}.get(model_trade["direction"])
                  or priced.underlying != inputs["instrument"]):
                reasons = ["CONTRACT_IDENTITY_MISMATCH"]
            elif not priced.tradingsymbol or not priced.provider_instrument_id:
                reasons = ["CONTRACT_IDENTITY_MISSING"]
            elif any(getattr(priced, key) != (inputs.get("contract") or {}).get(key) for key in
                     ("underlying", "expiry", "strike", "option_type", "tradingsymbol",
                      "provider_instrument_id", "lot_size")):
                reasons = ["CONTRACT_IDENTITY_MISMATCH"]
            elif not lot_verified(priced.underlying, day, priced.lot_size):
                reasons = [CONTRACT_LOT_UNVERIFIED]
            elif strategy_lots is None:
                reasons = ["STRATEGY_LOT_LIMIT_AMBIGUOUS"]
            else:
                reasons = _risk_gate(conn, account_id, day, model_trade, priced)
                if not reasons:
                    if risk["risk_policy_id"] == risk_v2.POLICY_ID:
                        projection = account_projection(conn, account_id, market_day=day)
                        if projection.charge_discrepancies:
                            raise cj.ConsistencyError([{"kind": "paper_charge_discrepancy", "account_id": account_id}])
                        sized = risk_v2.size(
                            quote=priced, evidence=delta_evidence, trusted_sources=trusted_delta_sources,
                            now=now, settings=settings, underlying_entry=Decimal(str(model_trade["entry_spot"])),
                            underlying_stop=Decimal(str(model_trade["stop_spot"])),
                            stop_budget=projection.stop_risk_budget, gross_daily_loss=projection.gross_daily_loss,
                            existing_reservations=projection.reserved,
                            available_cash=projection.spendable_cash, max_lots=strategy_lots,
                            cost_policy_id=cost_policy_id,
                            allow_historical_brokerage_assumption=allow_historical_brokerage_assumption)
                        entry, why = sized.entry, sized.reason
                    elif risk["risk_policy_id"] == "paper-risk-v1":
                        entry, why = px.size(priced, now=now, settings=settings, ticks=risk["slippage_ticks"],
                                             max_lots=min(risk["max_lots"], strategy_lots), loss_cap=Decimal(risk["max_loss_per_trade_inr"]),
                                             funds=cash(conn, account_id))
                    else:
                        raise cj.ConsistencyError([{"kind": "paper_risk_policy_unknown", "account_id": account_id}])
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
            if risk["risk_policy_id"] == risk_v2.POLICY_ID:
                if entry is not None:
                    active_policy_id = (entry.charges.get("policy") or {}).get("policy_id")
                else:
                    try:
                        active_policy_id = paper_fees.resolve_policy(
                            day,
                            policy_id=cost_policy_id,
                            allow_historical_brokerage_assumption=allow_historical_brokerage_assumption,
                        ).policy_id
                    except paper_fees.FeePolicyUnavailable:
                        active_policy_id = cost_policy_id or paper_fees.POLICY_ID
                evidence = {
                    "registry_hash": reg.REGISTRY_HASH, "strategy_version": version,
                    "rule_source_hashes": reg.RULE_SOURCE_HASHES,
                    "parameter_hash": reg.PARAMETER_HASH, "parameters": reg.STRATEGY_PARAMETERS,
                    "outcome": outcome, "reasons": reasons,
                    "underlying_entry": str(model_trade["entry_spot"]),
                    "underlying_stop": str(model_trade["stop_spot"]),
                    "quote": asdict(quote) if quote is not None else None,
                    "delta": asdict(delta_evidence) if delta_evidence is not None else None,
                    "trusted_delta_sources": sorted(trusted_delta_sources),
                    "limits": {"full_premium": str(risk_v2.FULL_PREMIUM_LIMIT),
                               "daily_exposure": str(risk_v2.DAILY_EXPOSURE_LIMIT),
                               "max_lots": risk_v2.MAX_LOTS, "strategy_max_lots": strategy_lots,
                               "max_positions": reg.max_positions(account_id),
                               "delta_max_age_seconds": risk_v2.DELTA_MAX_AGE_SECONDS,
                               "slippage_ticks": risk_v2.SLIPPAGE_TICKS},
                    "account_before": None if projection is None else {
                        "cash": str(projection.cash), "effective_capital": str(projection.effective_capital),
                        "cash_after_exit_reserves": str(projection.spendable_cash),
                        "stop_budget": str(projection.stop_risk_budget),
                        "gross_daily_loss": str(projection.gross_daily_loss),
                        "reserved": str(projection.reserved)},
                    "sizing": None if sized is None else {
                        "lots": entry.lots if entry is not None else None,
                        "full_premium_loss": sized.full_premium_loss,
                        "estimated_stop_risk": sized.estimated_stop_risk, "reason": sized.reason},
                    "cost_policy_id": active_policy_id,
                    "regime_v2": regime_vec.to_dict(),
                }
                conn.execute("INSERT INTO crew_paper_risk_decisions VALUES (?, ?, ?, ?, ?)",
                             (account_id, admission["admission_id"], risk_v2.POLICY_ID,
                              json.dumps(evidence, sort_keys=True, default=str, allow_nan=False), now.isoformat()))
            out.append({"account_id": account_id, "outcome": outcome, "reasons": reasons, "paper_trade_id": ptid,
                        "replayed": False})
        return out

    return cj._fenced(db_path, lease, now, op)


def partial(db_path: Path | str, *, lease: cj.Lease, model_trade_id: str, chain: dict | None, index_spot: float,
            reason: str, now: datetime, settings: Settings) -> list[dict]:
    """Mirror a model half-exit: sell half the paper lots at the bid. One lot, or no book, holds the position."""
    def act(conn, trade):
        half = trade["lots_open"] // 2
        quote, unpriced = _book(conn, trade, chain, now, settings)
        if half == 0 or quote is None:
            why = "SINGLE_LOT" if half == 0 else f"UNPRICED:{unpriced}"
            return _event(conn, trade, "PARTIAL_SKIPPED", trade["state"], f"{reason}:{why}", trade["lots_open"],
                          key=f"{trade['paper_trade_id']}:partial", index_spot=index_spot, lease=lease, now=now)
        policy_id, historical_flag = _entry_cost_policy(conn, trade)
        try:
            sold = px.fill(quote, "SELL", half, now=now, settings=settings, ticks=reg.RISK_POLICY["slippage_ticks"],
                           cost_policy_id=policy_id, allow_historical_brokerage_assumption=historical_flag)
        except paper_fees.FeePolicyUnavailable as exc:
            return _event(conn, trade, "PARTIAL_SKIPPED", trade["state"], f"{reason}:{exc}", trade["lots_open"],
                          key=f"{trade['paper_trade_id']}:partial", index_spot=index_spot, lease=lease, now=now)
        return _event(conn, trade, "PARTIAL_EXIT", "PARTIALLY_EXITED", reason, trade["lots_open"] - half,
                      key=f"{trade['paper_trade_id']}:partial", fill=sold, index_spot=index_spot, lease=lease, now=now)

    return _each_open(db_path, lease, now, model_trade_id, act)


def close(db_path: Path | str, *, lease: cj.Lease, model_trade_id: str, chain: dict | None, index_spot: float,
          reason: str, now: datetime, settings: Settings) -> list[dict]:
    """Mirror a model exit: sell every open paper lot at the bid, or record EXIT_UNPRICED. Never priced later.
    With PAPER_EXIT_REQUOTE_MINUTES set, an unpriceable exit leaves the lots OPEN (they are still held) for
    ``retry_exits`` to sell at a later real quote; EXIT_UNPRICED is recorded once that window, counted from the
    model exit bar (``crew_trades.closed_at``, replay-deterministic), has passed."""
    final = settings.paper_exit_requote_minutes == 0
    return _each_open(db_path, lease, now, model_trade_id,
                      lambda conn, trade: _exit(conn, trade, chain, index_spot, reason, lease, now, settings, final))


def retry_exits(db_path: Path | str, *, lease: cj.Lease, chain: dict | None, index_spot: float, now: datetime,
                settings: Settings) -> list[dict]:
    """Retry this session's paper exits that ``close`` left OPEN, at an executable quote only."""
    window = settings.paper_exit_requote_minutes
    if window == 0:
        return []
    from app.market.hours import market_status

    # A fresh-looking book after the close is not executable; the window can outlast 15:30.
    closed = not market_status(now, settings)[0]

    def op(conn: sqlite3.Connection) -> list[dict]:
        out = []
        for trade in _rows(conn, "SELECT p.*, m.closed_at AS model_closed_at, json_extract(e.payload, '$.reason')"
                                 " AS model_reason FROM crew_paper_trades p"
                                 " JOIN crew_trades m ON m.trade_id = p.model_trade_id AND m.state = 'CLOSED'"
                                 " JOIN crew_events e ON e.trade_id = m.trade_id AND e.event_type = 'MODEL_EXIT'"
                                 f" WHERE p.state IN {_OPEN_SQL} AND p.market_day = ? ORDER BY p.paper_trade_id",
                                 (lease.market_day,)):
            final = (now - datetime.fromisoformat(trade["model_closed_at"])).total_seconds() >= window * 60
            out.append(_exit(conn, trade, chain, index_spot, trade["model_reason"], lease, now, settings, final,
                             late=True, closed=closed))
        return out

    return cj._fenced(db_path, lease, now, op)


def pending_exits(conn: sqlite3.Connection) -> list[dict]:
    """Paper lots still held after their model trade exited: awaiting a quote, or unresolved. Reads only."""
    return _rows(conn, "SELECT p.paper_trade_id, p.account_id, p.model_trade_id, p.market_day, p.state, p.lots_open,"
                       " m.closed_at AS model_exit_at FROM crew_paper_trades p"
                       " JOIN crew_trades m ON m.trade_id = p.model_trade_id AND m.state = 'CLOSED'"
                       f" WHERE p.state IN {_OPEN_SQL} ORDER BY p.paper_trade_id")


def _exit(conn, trade, chain, index_spot, reason, lease, now, settings, final, late=False, closed=False):
    quote, unpriced = (None, "MARKET_CLOSED") if closed else _book(conn, trade, chain, now, settings)
    key = f"{trade['paper_trade_id']}:exit"
    if quote is None:
        if not final:
            return {"paper_trade_id": trade["paper_trade_id"], "event_type": "EXIT_PENDING", "reason": unpriced,
                    "replayed": False}
        why = f"{reason}:{unpriced}" + (":REQUOTE_EXPIRED" if late else "")
        return _event(conn, trade, "EXIT_UNPRICED", "EXIT_UNPRICED", why, trade["lots_open"],
                      key=key, index_spot=index_spot, lease=lease, now=now)
    policy_id, historical_flag = _entry_cost_policy(conn, trade)
    try:
        sold = px.fill(quote, "SELL", trade["lots_open"], now=now, settings=settings,
                       ticks=reg.RISK_POLICY["slippage_ticks"], cost_policy_id=policy_id,
                       allow_historical_brokerage_assumption=historical_flag)
    except paper_fees.FeePolicyUnavailable as exc:
        return _event(conn, trade, "EXIT_UNPRICED", "EXIT_UNPRICED", f"{reason}:{exc}", trade["lots_open"],
                      key=key, index_spot=index_spot, lease=lease, now=now)
    return _event(conn, trade, "EXIT", "CLOSED", f"{reason}:REQUOTED" if late else reason, 0, key=key, fill=sold,
                  index_spot=index_spot, lease=lease, now=now)


def recover(db_path: Path | str, *, lease: cj.Lease, now: datetime) -> dict:
    """Startup, after crew recovery: refuse an inconsistent ledger, expire prior days, and close the gaps a crash
    leaves (an exit or an entry the paper ledger never saw) without pricing anything after the fact."""
    def op(conn: sqlite3.Connection) -> dict:
        findings = consistency(conn)
        if findings:
            raise cj.ConsistencyError(findings)
        report = {"expired": [], "expired_exit_unresolved": [], "exit_missed": [], "partial_missed": [],
                  "entry_missed": [], "orphaned": []}
        for trade in _rows(conn, f"SELECT * FROM crew_paper_trades WHERE state IN {_OPEN_SQL} ORDER BY paper_trade_id"):
            model = conn.execute("SELECT state,owner_session_id FROM crew_trades WHERE trade_id=?",
                                 (trade["model_trade_id"],)).fetchone()
            if (trade["market_day"] == lease.market_day and model["state"] in ("TRACKING", "PARTIALLY_EXITED")
                    and model["owner_session_id"] != lease.session_id):
                report["orphaned"].append({"paper_trade_id": trade["paper_trade_id"],
                                           "account_id": trade["account_id"],
                                           "owner_session_id": model["owner_session_id"]})
            if trade["market_day"] < lease.market_day:
                # A model that already exited means the paper exit itself was left unresolved, not the position.
                why = "PRIOR_MARKET_DAY:MODEL_EXITED" if model["state"] == "CLOSED" else "PRIOR_MARKET_DAY"
                _event(conn, trade, "EXPIRED_UNRESOLVED", "EXPIRED_UNRESOLVED", why, trade["lots_open"],
                       key=f"{trade['paper_trade_id']}:expired", lease=lease, now=now)
                report["expired"].append(trade["paper_trade_id"])
                if model["state"] == "CLOSED":
                    report["expired_exit_unresolved"].append(trade["paper_trade_id"])
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
                if reg.RISK_POLICY["risk_policy_id"] == risk_v2.POLICY_ID:
                    evidence = {"registry_hash": reg.REGISTRY_HASH, "strategy_version": row["ver"],
                                "outcome": "REJECTED", "reasons": [ENTRY_MISSED_AFTER_RESTART],
                                "recovery": True, "sizing": None, "account_before": None,
                                "delta": None, "quote": None}
                    conn.execute("INSERT INTO crew_paper_risk_decisions VALUES (?, ?, ?, ?, ?)",
                                 (account_id, row["admission_id"], risk_v2.POLICY_ID,
                                  json.dumps(evidence, sort_keys=True), now.isoformat()))
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


def account_projection(conn: sqlite3.Connection, account_id: str, *, market_day: str | None = None) -> pa.AccountProjection:
    """Reconstruct financial state from canonical fills, optionally as of an IST market-day end.

    No quote is supplied here: holdings remain explicitly unpriced. Exit-cost reservations come
    from each entry's persisted maximum loss, so historical charges are not recalculated.
    """
    account = conn.execute("SELECT capital_inr FROM crew_paper_accounts WHERE account_id=?", (account_id,)).fetchone()
    if account is None:
        raise cj.InvalidTransition(f"unknown paper account {account_id}")
    projections = {}
    latest_day = market_day
    try:
        for trade in _rows(conn, "SELECT * FROM crew_paper_trades WHERE account_id=?", (account_id,)):
            events = _rows(conn, "SELECT * FROM crew_paper_events WHERE paper_trade_id=? ORDER BY seq",
                           (trade["paper_trade_id"],))
            if market_day:
                events = [e for e in events if datetime.fromisoformat(e["occurred_at"]).astimezone(IST).date().isoformat()
                          <= market_day]
            if not events:
                continue
            entry_debit = pa.money(events[0]["price_inr"]) * events[0]["qty"] + pa.money(events[0]["charges_inr"])
            if entry_debit != pa.money(trade["capital_committed_inr"]):
                raise pa.AccountingError("persisted entry basis disagrees with fills")
            projection = pa.project_trade(events, liquidation_cost=pa.money(trade["max_loss_inr"]) - entry_debit)
            if market_day is None and projection.quantity != trade["lots_open"] * trade["lot_size"]:
                raise pa.AccountingError("persisted quantity disagrees with fills")
            projections[trade["paper_trade_id"]] = projection
            day = datetime.fromisoformat(events[-1]["occurred_at"]).astimezone(IST).date().isoformat()
            latest_day = max(latest_day or day, day)
        return pa.project_account(pa.money(account[0]), projections, market_day=latest_day or "1970-01-01")
    except (pa.AccountingError, ValueError) as exc:
        raise cj.ConsistencyError([{"kind": "paper_accounting", "account_id": account_id, "detail": str(exc)}]) from exc


def consistency(conn: sqlite3.Connection) -> list[dict]:
    """Paper invariants. Reads only."""
    findings = []
    for decision in _rows(conn,
            "SELECT d.*, r.evidence, r.risk_policy_id AS evidence_policy FROM crew_paper_decisions d "
            "JOIN crew_paper_accounts a ON a.account_id=d.account_id "
            "LEFT JOIN crew_paper_risk_decisions r ON r.account_id=d.account_id AND r.admission_id=d.admission_id "
            "WHERE a.risk_policy_id='paper-risk-v2'"):
        evidence = json.loads(decision["evidence"]) if decision["evidence"] is not None else {}
        if (decision["evidence_policy"] != risk_v2.POLICY_ID
                or evidence.get("registry_hash") != decision["registry_hash"]
                or evidence.get("outcome") != decision["outcome"]
                or evidence.get("reasons") != json.loads(decision["reasons"])):
            findings.append({"kind": "paper_risk_evidence", "account_id": decision["account_id"],
                             "admission_id": decision["admission_id"]})
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
    for account_id, policy in conn.execute(
            "SELECT account_id,risk_policy_id FROM crew_paper_accounts ORDER BY account_id").fetchall():
        if cash(conn, account_id) < 0:
            findings.append({"kind": "paper_negative_cash", "account_id": account_id})
        if policy == risk_v2.POLICY_ID:
            try:
                projection = account_projection(conn, account_id)
                if projection.charge_discrepancies:
                    findings.append({"kind": "paper_charge_discrepancy", "account_id": account_id})
            except cj.ConsistencyError as exc:
                findings.extend(exc.findings)
    return findings


def check_consistency(db_path: Path | str) -> list[dict]:
    conn = cj.connect_readonly(db_path)
    try:
        return consistency(conn)
    finally:
        conn.close()


# --- internals -----------------------------------------------------------------------------------------

def _entry_cost_policy(conn, trade) -> tuple[str | None, bool]:
    row = conn.execute("SELECT charges FROM crew_paper_events WHERE paper_trade_id=? AND seq=1",
                       (trade["paper_trade_id"],)).fetchone()
    policy = json.loads(row[0]).get("policy") or {}
    return policy.get("policy_id"), bool(policy.get("brokerage_provenance"))

def _risk_gate(conn, account_id: str, day: str, model_trade: dict, quote) -> list[str]:
    held = "state <> 'CLOSED'" if reg.RISK_POLICY["risk_policy_id"] == risk_v2.POLICY_ID else f"state IN {_OPEN_SQL}"
    open_rows = _rows(conn, f"SELECT * FROM crew_paper_trades WHERE account_id=? AND {held}",
                      (account_id,))
    if len(open_rows) >= reg.max_positions(account_id):
        return [MAX_POSITIONS]
    for row in open_rows:
        if row["underlying"] == quote.underlying:
            same = row["direction"] == model_trade["direction"] and row["expiry"] == quote.expiry
            return [DUPLICATE_EXPOSURE if same else CONFLICTING_EXPOSURE]
    if reg.RISK_POLICY["risk_policy_id"] == risk_v2.POLICY_ID:
        return []  # Gross losses and retained reservations are enforced together by the V2 projection.
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


def _book(conn, trade, chain, now, settings):
    """The held contract's executable book. ``trade["contract"]`` is the entry trading symbol (entry requires one)."""
    quote = px.contract_quote(chain, strike=trade["strike"], option_type=trade["option_type"], expiry=trade["expiry"])
    if quote is not None:
        if not quote.tradingsymbol or not quote.underlying:
            return None, "CONTRACT_IDENTITY_MISSING"
        if ((quote.underlying, quote.tradingsymbol, quote.strike, quote.expiry, quote.option_type, quote.lot_size)
                != (trade["underlying"], trade["contract"], trade["strike"], trade["expiry"], trade["option_type"],
                    trade["lot_size"])):
            return None, "CONTRACT_IDENTITY_MISMATCH"
    evidence = conn.execute("SELECT inputs FROM crew_admissions WHERE admission_id=?",
                            (trade["admission_id"],)).fetchone()
    contract = (json.loads(evidence[0]).get("contract") or {}) if evidence else {}
    keys = ("underlying", "expiry", "strike", "option_type", "tradingsymbol", "lot_size",
            "provider_instrument_id")
    if any(not contract.get(key) for key in keys):
        return None, "CONTRACT_IDENTITY_MISSING"
    if quote is not None and any(getattr(quote, key) != contract[key] for key in keys):
        return None, "CONTRACT_IDENTITY_MISMATCH"
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
