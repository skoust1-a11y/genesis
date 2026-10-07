"""Trade management policy: position sizing, profit booking, time exits and a daily profit target.

Set at approval (`risk_limits.policy`) and frozen with the deployment, like the drawdown ladder. A
policy can only make a deployment more careful than its strategy rules:

- sizing chooses how many lots to trade, never more than the approved max_lots; with compounding,
  a share of realized profit is added to the capital that risk-based sizing works from;
- booking exits close all or part of a position before the strategy's profit target; the strategy's
  own exits (expiry, stop loss, profit target, DTE) are checked first and always apply; for debit
  structures the `*_pct_of_credit` thresholds are percentages of the premium paid;
- time exits close a position some days before expiry (optionally from a time of day, IST) or
  before a scheduled event;
- the daily profit target stops new entries, or closes everything, once the day's P&L reaches it;
- VIX sizing trades fewer lots when India VIX is high.

Pure functions shared by the Trade Crew and the simulator.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.strategies.rules import Check
from app.trading.decisions import PositionMark

IST = ZoneInfo("Asia/Kolkata")
SIZING_CHECK = "policy.sizing"
DAILY_TARGET_CHECK = "policy.daily_profit_target"
EventKind = Literal["rbi_policy", "budget", "fomc", "election", "results", "board_meeting", "other"]  # EVENT_KINDS


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Sizing(_Strict):
    """fixed: `lots` (default the approved max_lots). risk_pct / risk_inr: as many lots as keep the
    trade's max loss within the budget, capped by max_lots; below `min_lots` the entry is blocked."""

    mode: Literal["fixed", "risk_pct", "risk_inr"] = "fixed"
    lots: int | None = Field(default=None, ge=1)
    risk_pct: float | None = Field(default=None, gt=0, le=100)
    risk_inr: float | None = Field(default=None, gt=0)
    min_lots: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def _fields_for_mode(self) -> Sizing:
        for name in ("risk_pct", "risk_inr"):
            if (self.mode == name) != (getattr(self, name) is not None):
                raise ValueError(f"{name} is required for {name} sizing and only allowed there")
        if self.lots is not None and self.mode != "fixed":
            raise ValueError("lots is only allowed for fixed sizing")
        return self


class PartialBooking(_Strict):
    at_pct_of_credit: float = Field(gt=0, le=100)
    close_pct: float = Field(gt=0, lt=100)  # of the lots opened


class LockIn(_Strict):
    """Once profit reaches `after_pct_of_credit`, close if it falls back to `lock_pct_of_credit`."""

    after_pct_of_credit: float = Field(gt=0, le=100)
    lock_pct_of_credit: float = Field(ge=0, le=100)

    @model_validator(mode="after")
    def _below(self) -> LockIn:
        if self.lock_pct_of_credit >= self.after_pct_of_credit:
            raise ValueError("lock_pct_of_credit must be below after_pct_of_credit")
        return self


class Trailing(_Strict):
    """Once profit reaches `after_pct_of_credit`, close if it gives back `giveback_pct` of its peak."""

    after_pct_of_credit: float = Field(gt=0, le=100)
    giveback_pct: float = Field(gt=0, lt=100)


class Booking(_Strict):
    partial: list[PartialBooking] = Field(default_factory=list, max_length=5)
    lock_in: LockIn | None = None
    trailing: Trailing | None = None

    @model_validator(mode="after")
    def _ordered(self) -> Booking:
        for lower, upper in zip(self.partial, self.partial[1:]):
            if upper.at_pct_of_credit <= lower.at_pct_of_credit:
                raise ValueError("partial at_pct_of_credit values must increase")
        if sum(p.close_pct for p in self.partial) >= 100:
            raise ValueError("partial close_pct values must add up to less than 100")
        return self


class TimeExit(_Strict):
    days_before_expiry: int | None = Field(default=None, ge=0, le=60)
    at: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")  # IST, on that day
    before_events: list[EventKind] = Field(default_factory=list)
    event_days_before: int = Field(default=1, ge=0, le=14)

    @model_validator(mode="after")
    def _something(self) -> TimeExit:
        if self.at is not None and self.days_before_expiry is None:
            raise ValueError("at needs days_before_expiry")
        if self.days_before_expiry is None and not self.before_events:
            raise ValueError("set days_before_expiry or before_events")
        return self


class DailyTarget(_Strict):
    profit_inr: float = Field(gt=0)
    on_hit: Literal["stop_new_entries", "close_all"] = "stop_new_entries"


class Compounding(_Strict):
    profit_share_pct: float = Field(gt=0, le=100)
    ceiling_inr: float | None = Field(default=None, gt=0)


class VixBand(_Strict):
    at_or_above: float = Field(gt=0)
    size_factor: float = Field(gt=0, lt=1)


class VixSizing(_Strict):
    """Fewer lots when India VIX is high: the highest band at or below today's VIX scales lots by its factor."""

    bands: list[VixBand] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def _ordered(self) -> VixSizing:
        for lower, upper in zip(self.bands, self.bands[1:]):
            if upper.at_or_above <= lower.at_or_above:
                raise ValueError("bands at_or_above values must increase")
            if upper.size_factor >= lower.size_factor:
                raise ValueError("bands size_factor values must decrease as VIX rises")
        return self


class TradePolicy(_Strict):
    sizing: Sizing | None = None
    booking: Booking | None = None
    time_exit: TimeExit | None = None
    daily_target: DailyTarget | None = None
    compounding: Compounding | None = None
    vix_sizing: VixSizing | None = None

    @model_validator(mode="after")
    def _compounding_needs_risk_sizing(self) -> TradePolicy:
        if self.compounding is not None and (self.sizing is None or self.sizing.mode == "fixed"):
            raise ValueError("compounding only applies to risk_pct or risk_inr sizing")
        return self


def _error(prefix: str, exc: ValidationError) -> ValueError:
    err = exc.errors()[0]
    loc = ".".join(str(p) for p in err["loc"])
    return ValueError(f"{prefix}{'.' + loc if loc else ''}: {err['msg'].removeprefix('Value error, ')}")


def parse_policy(raw: Any, rules: dict[str, Any] | None = None, max_lots: int | None = None) -> TradePolicy:
    """Validate a policy against the strategy it will run with: booking exits must come before the
    strategy's profit target, and fixed lots may not exceed the approved max lots."""
    try:
        policy = TradePolicy.model_validate(raw)
    except ValidationError as exc:
        raise _error("risk_limits.policy", exc) from None
    target = _profit_target(rules)
    if policy.booking is not None and target is not None:
        thresholds = [("partial", p.at_pct_of_credit) for p in policy.booking.partial]
        if policy.booking.lock_in:
            thresholds.append(("lock_in", policy.booking.lock_in.after_pct_of_credit))
        if policy.booking.trailing:
            thresholds.append(("trailing", policy.booking.trailing.after_pct_of_credit))
        for name, pct in thresholds:
            if pct >= target:
                raise ValueError(
                    f"risk_limits.policy.booking.{name}: {pct:g}% is at or past the strategy's "
                    f"{target:g}% profit target, so it would never fire"
                )
    lots = policy.sizing.lots if policy.sizing else None
    caps = [x for x in (max_lots, (rules or {}).get("risk", {}).get("max_lots")) if x]
    if lots is not None and caps and lots > min(caps):
        raise ValueError(f"risk_limits.policy.sizing.lots: {lots} is above the approved max of {min(caps)} lots")
    return policy


def _profit_target(rules: dict[str, Any] | None) -> float | None:
    for e in (rules or {}).get("exit", []):
        if e.get("kind") in ("profit_target_pct_of_credit", "profit_target_pct_of_debit"):
            return float(e["value"])
    return None


def policy_from_limits(risk_limits: dict[str, Any]) -> TradePolicy | None:
    raw = risk_limits.get("policy")
    return TradePolicy.model_validate(raw) if raw else None


# Sizing


def sizing_capital(policy: TradePolicy, capital: float, realized_net: float) -> float:
    """Capital in force, plus the compounded share of realized profit (never less than capital)."""
    c = policy.compounding
    if c is None or realized_net <= 0:
        return capital
    grown = capital + realized_net * c.profit_share_pct / 100
    return max(capital, min(grown, c.ceiling_inr)) if c.ceiling_inr else grown


def size_lots(
    sizing: Sizing | None, max_lots: int, capital: float, max_loss_per_lot: float | None
) -> tuple[int, Check | None]:
    """Lots to trade and the sizing check (None for fixed sizing). 0 lots: the budget cannot afford min_lots."""
    if sizing is None or sizing.mode == "fixed":
        return min(sizing.lots or max_lots, max_lots) if sizing else max_lots, None
    budget = capital * sizing.risk_pct / 100 if sizing.mode == "risk_pct" else sizing.risk_inr
    affordable = math.floor(budget / max_loss_per_lot) if max_loss_per_lot and max_loss_per_lot > 0 else 0
    lots = min(max_lots, affordable)
    ok = lots >= sizing.min_lots
    return (lots if ok else 0), Check(SIZING_CHECK, "affordable_lots", ">=", sizing.min_lots, affordable, ok)


def vix_size_factor(vix_sizing: VixSizing | None, vix: float | None) -> float:
    """Lot multiplier for today's VIX. With no VIX reading, the most careful band applies."""
    if vix_sizing is None:
        return 1.0
    if vix is None:
        return vix_sizing.bands[-1].size_factor
    return min((b.size_factor for b in vix_sizing.bands if vix >= b.at_or_above), default=1.0)


# Exits


@dataclass
class BookingExit:
    check: Check
    close_lots: int | None = None  # None closes the whole position


def pnl_pct(mark: PositionMark) -> float | None:
    """P&L as % of the premium received or, for debit structures, paid."""
    return mark.pnl / abs(mark.credit) * 100 if mark.credit else None


def update_peak(peak_pct: float | None, mark: PositionMark | None) -> float | None:
    """Highest profit as % of credit seen so far, carried between ticks for the trailing stop."""
    pct = pnl_pct(mark) if mark else None
    if pct is None:
        return peak_pct
    return round(pct if peak_pct is None else max(peak_pct, pct), 4)


def booking_exit(
    policy: TradePolicy,
    mark: PositionMark,
    *,
    peak_pct: float | None,
    partials_done: int,
    lots_opened: int,
    lots_open: int,
    now: datetime | None = None,
    events: list[dict] | None = None,
) -> BookingExit | None:
    """The first policy exit that fires: time, event, lock-in, trailing stop, then the next partial
    booking. Call only when the strategy's own exits did not fire. `now` None means end of day."""
    te = policy.time_exit
    if te is not None:
        n = te.days_before_expiry
        if n is not None and (mark.dte < n or (mark.dte == n and _past(te.at, now))):
            return BookingExit(_fired("policy.time_exit", "dte", "<=", n, mark.dte))
        if te.before_events and events is not None:
            ahead = [
                e["days_from"] for e in events
                if e["kind"] in te.before_events and 0 <= e["days_from"] <= te.event_days_before
            ]  # fmt: skip
            if ahead:
                return BookingExit(_fired("policy.event_exit", "days_to_event", "<=", te.event_days_before, min(ahead)))
    booking, pct = policy.booking, pnl_pct(mark)
    if booking is None or pct is None:
        return None
    peak = max(peak_pct if peak_pct is not None else pct, pct)
    lock = booking.lock_in
    if lock and peak >= lock.after_pct_of_credit and pct <= lock.lock_pct_of_credit:
        return BookingExit(_fired("policy.lock_in", "pnl_pct_of_credit", "<=", lock.lock_pct_of_credit, pct))
    trail = booking.trailing
    if trail and peak >= trail.after_pct_of_credit:
        floor = peak * (1 - trail.giveback_pct / 100)
        if pct <= floor:
            return BookingExit(_fired("policy.trailing_stop", "pnl_pct_of_credit", "<=", floor, pct))
    if partials_done < len(booking.partial):
        level = booking.partial[partials_done]
        lots = partial_lots(lots_opened, lots_open, level.close_pct)
        if pct >= level.at_pct_of_credit and lots:
            check = _fired("policy.partial_booking", "pnl_pct_of_credit", ">=", level.at_pct_of_credit, pct)
            return BookingExit(check, close_lots=lots)
    return None


def _fired(rule: str, field: str, op: str, threshold: float, actual: float) -> Check:
    return Check(rule, field, op, round(threshold, 4), round(actual, 4), True)


def _past(at: str | None, now: datetime | None) -> bool:
    if at is None or now is None:
        return True
    hour, minute = (int(x) for x in at.split(":"))
    return now.astimezone(IST).time() >= time(hour, minute)


def partial_lots(lots_opened: int, lots_open: int, close_pct: float) -> int:
    """Lots a partial booking closes: `close_pct` of the lots opened, at least 1, always leaving 1 open."""
    return min(max(1, round(lots_opened * close_pct / 100)), lots_open - 1) if lots_open > 1 else 0


def daily_target_check(target: DailyTarget, day_pnl: float) -> Check:
    """Passes (entries allowed) while the day's P&L is below the target."""
    pnl = round(day_pnl, 2)
    return Check(DAILY_TARGET_CHECK, "day_pnl_inr", "<", target.profit_inr, pnl, pnl < target.profit_inr)
