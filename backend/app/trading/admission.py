"""S1-P3 admission: classify a signal candidate as REJECTED, RESEARCH_ONLY or ACTIONABLE.

D6 quote grades. FRESH_REFERENCE: a valid contract, last price above zero, chain fetched (and quote
time, when given) no more than MAX_QUOTE_AGE_SECONDS before evaluation. It allows RESEARCH_ONLY.
EXECUTABLE: ``quote_decision`` returns ``executable``. It is necessary for ACTIONABLE but not enough.

ACTIONABLE also needs verified capital, cash and maximum loss. Those sources belong to S2/S3 and do
not exist yet, so every admission carries CAPITAL_UNAVAILABLE, CASH_UNAVAILABLE and
MAX_LOSS_UNVERIFIED and ACTIONABLE is never returned. This module places no orders and sends nothing.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from app.config import Settings
from app.db import crew_journal as cj
from app.trading.option_quote import IST, MAX_QUOTE_AGE_SECONDS, ContractQuote, quote_decision

# RESEARCH_ONLY reasons: exactly the P1 schema's _RESEARCH_REASONS.
QUOTE_NOT_EXECUTABLE = "QUOTE_NOT_EXECUTABLE"
CAPITAL_UNAVAILABLE = "CAPITAL_UNAVAILABLE"
CASH_UNAVAILABLE = "CASH_UNAVAILABLE"
MAX_LOSS_UNVERIFIED = "MAX_LOSS_UNVERIFIED"
# REJECTED reasons. The schema accepts any non-empty strings for REJECTED.
SIGNAL_INVALID = "SIGNAL_INVALID"
SIGNAL_STALE = "SIGNAL_STALE"
CONTRACT_INVALID = "CONTRACT_INVALID"
QUOTE_MISSING = "QUOTE_MISSING"
QUOTE_STALE = "QUOTE_STALE"
LTP_INVALID = "LTP_INVALID"
MARKET_DATA_CORRUPT = "MARKET_DATA_CORRUPT"

# ponytail: no verified capital, cash or max-loss source exists until S2/S3; replace with their evidence then.
_UNVERIFIED_RISK = (CAPITAL_UNAVAILABLE, CASH_UNAVAILABLE, MAX_LOSS_UNVERIFIED)
_OPTION_TYPE = {"CALL": "CE", "PUT": "PE"}


@dataclass(frozen=True)
class SignalCandidate:
    market_day: str
    instrument: str
    strategy_id: str
    strategy_version: str
    signal_candle: str
    direction: str
    setup_id: str


@dataclass(frozen=True)
class Admission:
    admission_class: str
    reasons: tuple[str, ...]
    signal_id: str | None
    market_day: str
    instrument: str
    strategy_id: str
    strategy_version: str
    setup_id: str
    signal_candle: str
    direction: str
    contract: dict | None
    quote: dict | None
    capital: dict
    evaluated_at: str

    def inputs(self) -> dict:
        return {"contract": self.contract, "quote": self.quote, "capital": self.capital,
                "evaluated_at": self.evaluated_at}


def evaluate(candidate: SignalCandidate, quote: ContractQuote | None, *, now: datetime,
             settings: Settings) -> Admission:
    """Pure classification. Reads no database and calls no network."""
    clock = now.astimezone(IST).replace(microsecond=0) if _aware(now) else None
    evaluated_at = clock.isoformat() if clock else str(now)
    capital = {"verified": False, "source": None, "reasons": list(_UNVERIFIED_RISK)}
    try:
        parts = dict(line.split("=", 1) for line in cj.canonical_signal_fingerprint(
            market_day=candidate.market_day, instrument=candidate.instrument,
            strategy_id=candidate.strategy_id, strategy_version=candidate.strategy_version,
            signal_candle=candidate.signal_candle, direction=candidate.direction, setup_id=candidate.setup_id,
        ).split("\n"))
        signal_id = cj.signal_id_for(**parts)
    except (cj.JournalError, TypeError, ValueError):
        return Admission("REJECTED", (SIGNAL_INVALID,), None, str(candidate.market_day),
                         str(candidate.instrument), str(candidate.strategy_id), str(candidate.strategy_version),
                         str(candidate.setup_id), str(candidate.signal_candle), str(candidate.direction),
                         None, None, capital, evaluated_at)

    def outcome(cls: str, reasons, contract=None, quote_ev=None) -> Admission:
        return Admission(cls, tuple(reasons), signal_id, parts["market_day"], parts["instrument"],
                         parts["strategy_id"], parts["strategy_version"], parts["setup_id"],
                         parts["signal_candle"], parts["direction"], contract, quote_ev, capital, evaluated_at)

    if clock is None:
        return outcome("REJECTED", [MARKET_DATA_CORRUPT])
    if clock.date().isoformat() != parts["market_day"]:
        return outcome("REJECTED", [SIGNAL_STALE])
    if datetime.fromisoformat(parts["signal_candle"]) > clock:
        return outcome("REJECTED", [MARKET_DATA_CORRUPT])
    if quote is None:
        return outcome("REJECTED", [QUOTE_MISSING])
    if not isinstance(quote, ContractQuote):
        return outcome("REJECTED", [MARKET_DATA_CORRUPT])
    try:
        return _grade(quote, parts, clock, settings, outcome)
    except (TypeError, ValueError, AttributeError, ArithmeticError):
        return outcome("REJECTED", [MARKET_DATA_CORRUPT])


def _grade(quote: ContractQuote, parts: dict, clock: datetime, settings: Settings, outcome) -> Admission:
    contract = {
        "underlying": quote.underlying, "expiry": quote.expiry, "strike": quote.strike,
        "option_type": quote.option_type, "tradingsymbol": quote.tradingsymbol,
        "provider_instrument_id": quote.provider_instrument_id, "lot_size": quote.lot_size,
    }
    money = (quote.ltp, quote.bid, quote.ask)
    if any(v is not None and (not isinstance(v, Decimal) or not v.is_finite()) for v in money):
        return outcome("REJECTED", [MARKET_DATA_CORRUPT], contract)
    stamps = [quote.fetched_at] + ([quote.quote_time] if quote.quote_time is not None else [])
    if not all(_aware(s) for s in stamps):
        return outcome("REJECTED", [MARKET_DATA_CORRUPT], contract)
    ages = [(clock - s).total_seconds() for s in stamps]
    quote_ev = {
        "source": quote.source, "ltp": _str(quote.ltp), "bid": _str(quote.bid), "ask": _str(quote.ask),
        "fetched_at": quote.fetched_at.isoformat(),
        "quote_time": quote.quote_time.isoformat() if quote.quote_time is not None else None,
        "age_seconds": max(ages), "max_age_seconds": MAX_QUOTE_AGE_SECONDS, "grade": None,
        "decision": None, "decision_reason": None,
    }
    if min(ages) < 0:
        return outcome("REJECTED", [MARKET_DATA_CORRUPT], contract, quote_ev)
    if not _valid_contract(quote, parts):
        return outcome("REJECTED", [CONTRACT_INVALID], contract, quote_ev)
    if max(ages) > MAX_QUOTE_AGE_SECONDS:
        return outcome("REJECTED", [QUOTE_STALE], contract, quote_ev)
    if quote.ltp is None or quote.ltp <= 0:
        return outcome("REJECTED", [LTP_INVALID], contract, quote_ev)
    decision = quote_decision(quote, clock, settings)
    executable = decision.status == "executable"
    quote_ev.update(grade="EXECUTABLE" if executable else "FRESH_REFERENCE",
                    decision=decision.status, decision_reason=decision.reason)
    reasons = ([] if executable else [QUOTE_NOT_EXECUTABLE]) + list(_UNVERIFIED_RISK)
    return outcome("RESEARCH_ONLY", reasons, contract, quote_ev)


def _valid_contract(quote: ContractQuote, parts: dict) -> bool:
    lot = quote.lot_size
    strike = quote.strike
    try:
        expiry = date.fromisoformat(quote.expiry) if isinstance(quote.expiry, str) else None
    except ValueError:
        expiry = None
    return (
        expiry is not None and expiry.isoformat() == quote.expiry
        and expiry >= date.fromisoformat(parts["market_day"])
        and quote.option_type == _OPTION_TYPE[parts["direction"]]
        and isinstance(quote.underlying, str) and quote.underlying.strip().upper() == parts["instrument"]
        and type(lot) is int and lot > 0
        and isinstance(strike, (int, float)) and not isinstance(strike, bool)
        and math.isfinite(strike) and strike > 0
    )


def persist(db_path: Path | str, admission: Admission, *, lease: cj.Lease, now: datetime | None = None) -> dict | None:
    """Record the admission once per signal and day. The first decision is canonical.

    Returns None, and writes nothing, when the identity is invalid or the evaluation is off the market day.
    A later evaluation of the same signal returns the stored admission instead of a second row.
    """
    if admission.signal_id is None or admission.evaluated_at[:10] != admission.market_day:
        return None
    try:
        return cj.record_admission(
            db_path, lease=lease, now=now, admission_class=admission.admission_class,
            reasons=list(admission.reasons), market_day=admission.market_day,
            instrument=admission.instrument, strategy_id=admission.strategy_id,
            strategy_version=admission.strategy_version, signal_candle=admission.signal_candle,
            direction=admission.direction, setup_id=admission.setup_id,
            inputs=json.loads(json.dumps(admission.inputs())), decided_at=admission.evaluated_at,
        )
    except cj.AdmissionConflict:
        conn = cj.connect_readonly(db_path)
        try:
            row = conn.execute("SELECT * FROM crew_admissions WHERE signal_id=? AND market_day=?",
                               (admission.signal_id, admission.market_day)).fetchone()
        finally:
            conn.close()
        if row is None:
            raise
        stored = dict(row)
        stored["reasons"], stored["inputs"] = json.loads(stored["reasons"]), json.loads(stored["inputs"])
        return {"replayed": True, "admission": stored}


def open_research_trade(db_path: Path | str, persisted: dict, *, lease: cj.Lease, model_lots: int,
                        entry_spot: float, stop_spot: float, target_spot: float,
                        execution_policy: str | None = None, now: datetime | None = None) -> dict:
    """Open a hypothetical model trade from a stored RESEARCH_ONLY admission. Not an order."""
    stored = persisted["admission"]
    if stored["class"] != "RESEARCH_ONLY":
        raise cj.InvalidTransition(f"admission {stored['admission_id']} is {stored['class']}, not RESEARCH_ONLY")
    contract = stored["inputs"]["contract"]
    return cj.open_trade(
        db_path, lease=lease, now=now, admission_id=stored["admission_id"], model_lots=model_lots,
        entry_spot=entry_spot,
        stop_spot=stop_spot, target_spot=target_spot,
        contract=contract["tradingsymbol"] or contract["provider_instrument_id"]
        or f"{contract['underlying']} {contract['expiry']} {contract['strike']:g} {contract['option_type']}",
        expiry=contract["expiry"], lot_size_ref=contract["lot_size"], execution_policy=execution_policy,
    )


def _aware(value) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def _str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)
