"""Machine-checkable strategy rules: the `rules:` block in a strategy's frontmatter.

Everything the Trade Crew decides comes from these checks. They are pure functions with no
LLM involvement, so the same inputs always produce the same decision.
"""

from __future__ import annotations

import operator
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.db.hashing import canonical_json, normalize_json, sha256_hex

EntryField = Literal["iv_rank", "dte", "spot", "vix", "pct_from_prev_high", "pct_from_prev_low"]
ENTRY_FIELDS = ("iv_rank", "dte", "spot", "vix", "pct_from_prev_high", "pct_from_prev_low")
OPS = {">=": operator.ge, "<=": operator.le, ">": operator.gt, "<": operator.lt, "==": operator.eq}
Structure = Literal[
    "iron_condor", "short_strangle", "short_straddle", "long_call", "long_put", "bull_call_spread", "bear_put_spread"
]
CREDIT_STRUCTURES = ("iron_condor", "short_strangle", "short_straddle")
DEBIT_STRUCTURES = ("long_call", "long_put", "bull_call_spread", "bear_put_spread")
STRUCTURES = CREDIT_STRUCTURES + DEBIT_STRUCTURES
DEFINED_RISK = ("iron_condor", *DEBIT_STRUCTURES)
NEEDS_SHORT_DISTANCE = ("iron_condor", "short_strangle")
NEEDS_WING_WIDTH = ("iron_condor", "bull_call_spread", "bear_put_spread")
CREDIT_EXITS = ("profit_target_pct_of_credit", "stop_loss_pct_of_credit")
DEBIT_EXITS = ("profit_target_pct_of_debit", "stop_loss_pct_of_debit")
EXIT_KINDS = (*CREDIT_EXITS, *DEBIT_EXITS, "dte_at_or_below")
EVENT_KINDS = ("rbi_policy", "budget", "fomc", "election", "results", "board_meeting", "other")
BLACKOUT_MAX_DAYS_BEFORE = 14  # the event calendar lookup always covers at least this far ahead
BLACKOUT_MAX_DAYS_AFTER = 7  # and this far back


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Instrument(_Strict):
    underlying: str
    structure: Structure
    short_distance: float | None = Field(default=None, gt=0)  # credit structures: spot to the short strikes
    wing_width: float | None = Field(default=None, gt=0)  # short to long strike (condor), bought to sold (spreads)
    strike_offset: float | None = None  # debit structures: ATM to the bought strike, + out of the money, - in it
    lot_size: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _structure_fields(self) -> Instrument:
        if self.structure in NEEDS_SHORT_DISTANCE and self.short_distance is None:
            raise ValueError(f"short_distance is required for {self.structure}")
        if self.structure in NEEDS_WING_WIDTH and self.wing_width is None:
            raise ValueError(f"wing_width is required for {self.structure}")
        if self.structure in DEBIT_STRUCTURES and self.short_distance is not None:
            raise ValueError(f"short_distance does not apply to {self.structure}; use strike_offset")
        if self.structure in CREDIT_STRUCTURES and self.strike_offset is not None:
            raise ValueError(f"strike_offset only applies to {', '.join(DEBIT_STRUCTURES)}")
        return self

    @property
    def is_debit(self) -> bool:
        return self.structure in DEBIT_STRUCTURES


class EntryCondition(_Strict):
    field: EntryField
    op: Literal[">=", "<=", ">", "<", "=="]
    value: float


class ExitRule(_Strict):
    kind: Literal[
        "profit_target_pct_of_credit", "stop_loss_pct_of_credit",
        "profit_target_pct_of_debit", "stop_loss_pct_of_debit", "dte_at_or_below",
    ]  # fmt: skip
    value: float = Field(ge=0)


class RiskRules(_Strict):
    max_lots: int = Field(ge=1)
    max_daily_loss_pct: float = Field(gt=0)
    max_open_positions: int = Field(default=1, ge=1)
    min_risk_reward: float | None = Field(default=None, gt=0)
    max_entries_per_day: int | None = Field(default=None, ge=1)
    max_consecutive_losses: int | None = Field(default=None, ge=1)
    cooldown_days: int | None = Field(default=None, ge=1, le=30)  # trading days without entries after the streak
    max_loss_pct_of_capital: float | None = Field(default=None, gt=0, le=100)  # 100 = never risk more than the account

    @model_validator(mode="after")
    def _streak_and_cooldown(self) -> RiskRules:
        if (self.max_consecutive_losses is None) != (self.cooldown_days is None):
            raise ValueError("max_consecutive_losses and cooldown_days go together")
        return self


class EntryBlackout(_Strict):
    """No new entries from `days_before` an event until `days_after` it."""

    kinds: list[Literal["rbi_policy", "budget", "fomc", "election", "results", "board_meeting", "other"]] = Field(
        min_length=1
    )
    days_before: int = Field(default=1, ge=0, le=BLACKOUT_MAX_DAYS_BEFORE)
    days_after: int = Field(default=0, ge=0, le=BLACKOUT_MAX_DAYS_AFTER)


class StrategyRules(_Strict):
    instrument: Instrument
    entry: list[EntryCondition] = Field(min_length=1)
    exit: list[ExitRule] = Field(min_length=1)
    risk: RiskRules
    entry_blackout: EntryBlackout | None = None

    @model_validator(mode="after")
    def _target_and_stop(self) -> StrategyRules:
        kinds = {e.kind for e in self.exit}
        debit = self.instrument.is_debit
        wanted, other = (DEBIT_EXITS, CREDIT_EXITS) if debit else (CREDIT_EXITS, DEBIT_EXITS)
        missing = set(wanted) - kinds
        if missing:
            raise ValueError(f"exit needs {', '.join(sorted(missing))}")
        if wrong := kinds & set(other):
            raise ValueError(f"{', '.join(sorted(wrong))} does not apply to {self.instrument.structure}")
        if debit and self.stop_loss_pct > 100:
            raise ValueError("stop_loss_pct_of_debit cannot exceed 100: a bought position loses at most its debit")
        return self

    def exit_value(self, kind: str) -> float | None:
        return next((e.value for e in self.exit if e.kind == kind), None)

    @property
    def profit_target_pct(self) -> float:
        """Profit target as % of the premium received (credit structures) or paid (debit structures)."""
        return self.exit_value(f"profit_target_pct_of_{'debit' if self.instrument.is_debit else 'credit'}")

    @property
    def stop_loss_pct(self) -> float:
        return self.exit_value(f"stop_loss_pct_of_{'debit' if self.instrument.is_debit else 'credit'}")


def _format_error(err: dict) -> str:
    loc = ".".join(str(p) for p in err["loc"])
    msg = err["msg"].removeprefix("Value error, ")
    return f"rules.{loc}: {msg}" if loc else f"rules: {msg}"


def parse_rules(frontmatter: dict) -> tuple[StrategyRules | None, list[str]]:
    raw = frontmatter.get("rules")
    if raw is None:
        return None, ["rules: block missing"]
    try:
        return StrategyRules.model_validate(raw), []
    except ValidationError as exc:
        return None, [_format_error(e) for e in exc.errors()]


def rules_sha256(raw_rules: Any) -> str:
    """Hash of the rules block exactly as written in the strategy."""
    return sha256_hex(canonical_json(normalize_json(raw_rules)))


# Checks


@dataclass
class Check:
    rule: str
    field: str
    op: str
    threshold: float | None
    actual: float | None
    passed: bool


def _compare(rule: str, field: str, op: str, threshold: float | None, actual: float | None) -> Check:
    passed = actual is not None and threshold is not None and OPS[op](actual, threshold)
    return Check(rule, field, op, threshold, actual, passed)


def check_entry(rules: StrategyRules, snapshot: Any) -> list[Check]:
    """One check per entry condition; the entry signal fires only if every check passes."""
    checks = [
        _compare(f"entry.{c.field}", c.field, c.op, c.value, getattr(snapshot, c.field, None))
        for c in rules.entry
    ]
    if rules.entry_blackout is not None:
        checks.append(check_blackout(rules.entry_blackout, getattr(snapshot, "events", None)))
    return checks


def blackout_events(blackout: EntryBlackout, events: list[dict]) -> list[dict]:
    return [
        e for e in events
        if e["kind"] in blackout.kinds and -blackout.days_after <= e["days_from"] <= blackout.days_before
    ]  # fmt: skip


def check_blackout(blackout: EntryBlackout, events: list[dict] | None) -> Check:
    """Fails safe: with no event calendar loaded (`events is None`) the check does not pass."""
    hits = None if events is None else len(blackout_events(blackout, events))
    return _compare("entry.blackout", "events_in_window", "==", 0, hits)


@dataclass
class PositionState:
    credit: float  # net premium when the position was opened, in INR: received is positive, paid negative
    pnl: float  # unrealized P&L at current quotes, in INR


def check_exit(rules: StrategyRules, position: PositionState, snapshot: Any) -> list[Check]:
    """One check per exit rule; `passed` means the exit is triggered. Targets and stops are percentages
    of the premium received or paid."""
    premium = abs(position.credit)
    checks = []
    for e in rules.exit:
        if e.kind in ("profit_target_pct_of_credit", "profit_target_pct_of_debit"):
            checks.append(_compare("exit.profit_target", "pnl", ">=", premium * e.value / 100, position.pnl))
        elif e.kind in ("stop_loss_pct_of_credit", "stop_loss_pct_of_debit"):
            checks.append(_compare("exit.stop_loss", "pnl", "<=", -premium * e.value / 100, position.pnl))
        else:
            checks.append(_compare("exit.dte", "dte", "<=", e.value, getattr(snapshot, "dte", None)))
    return checks


@dataclass
class RiskLimits:
    capital_inr: float
    max_lots: int | None = None
    max_daily_loss_pct: float | None = None
    portfolio_max_open_positions: int | None = None
    portfolio_max_daily_loss_inr: float | None = None


@dataclass
class AccountState:
    open_positions: int
    pnl_today: float
    portfolio_open_positions: int
    portfolio_pnl_today: float
    entries_today: int = 0
    cooldown_days_left: int = 0  # from `losing_streak_cooldown`


@dataclass
class Proposal:
    lots: int
    risk_reward: float
    max_loss: float


def losing_streak_cooldown(
    risk: RiskRules, closed: Iterable[tuple[str, float]], trading_days_since: Callable[[str], int]
) -> int:
    """Trading days of cooldown left, today included; 0 when entries are allowed.

    `closed` is (exit date, net P&L) per closed trade, in closing order. `max_consecutive_losses`
    losses in a row start a cooldown on the day of the last one: no entries that day or for the next
    `cooldown_days` trading days. `trading_days_since(day)` counts trading days after `day` up to today.
    A streak that started a cooldown is used up, so counting starts again after it.
    """
    if risk.max_consecutive_losses is None or risk.cooldown_days is None:
        return 0
    streak, start = 0, None
    for day, net in closed:
        streak = streak + 1 if net < 0 else 0
        if streak >= risk.max_consecutive_losses:
            start, streak = day, 0
    if start is None:
        return 0
    return max(0, risk.cooldown_days + 1 - trading_days_since(start))


def _strictest(*values: float | None) -> float | None:
    present = [v for v in values if v is not None]
    return min(present) if present else None


def with_common_risk(rules: StrategyRules, common: dict[str, float]) -> StrategyRules:
    """`rules` with the account-wide risk rules (`Settings.common_risk`) applied. For each key the stricter
    of the two wins: fewer entries, a shorter losing streak, a longer cooldown, a smaller loss cap."""
    if not common:
        return rules
    r = rules.risk
    update: dict[str, Any] = {}
    if "max_entries_per_day" in common:
        update["max_entries_per_day"] = int(_strictest(r.max_entries_per_day, common["max_entries_per_day"]))
    if "max_loss_pct_of_capital" in common:
        update["max_loss_pct_of_capital"] = _strictest(r.max_loss_pct_of_capital, common["max_loss_pct_of_capital"])
    if "max_consecutive_losses" in common:
        update["max_consecutive_losses"] = int(_strictest(r.max_consecutive_losses, common["max_consecutive_losses"]))
        update["cooldown_days"] = int(max(r.cooldown_days or 0, common["cooldown_days"]))
    return rules.model_copy(update={"risk": r.model_copy(update=update)})


def check_risk(rules: StrategyRules, limits: RiskLimits, account: AccountState, proposal: Proposal) -> list[Check]:
    """Every risk rule from the strategy, tightened by the deployment and portfolio limits."""
    r = rules.risk
    daily_loss_pct = -min(account.pnl_today, 0) / limits.capital_inr * 100 if limits.capital_inr else None
    checks = [
        _compare("risk.max_lots", "lots", "<=", _strictest(r.max_lots, limits.max_lots), proposal.lots),
        _compare("risk.max_open_positions", "open_positions", "<", r.max_open_positions, account.open_positions),
        _compare(
            "risk.max_daily_loss_pct", "daily_loss_pct", "<",
            _strictest(r.max_daily_loss_pct, limits.max_daily_loss_pct), daily_loss_pct,
        ),  # fmt: skip
    ]
    if r.min_risk_reward is not None:
        checks.append(_compare("risk.min_risk_reward", "risk_reward", ">=", r.min_risk_reward, proposal.risk_reward))
    if r.max_entries_per_day is not None:
        checks.append(
            _compare("risk.max_entries_per_day", "entries_today", "<", r.max_entries_per_day, account.entries_today)
        )
    if r.max_consecutive_losses is not None:
        checks.append(
            _compare("risk.losing_streak_cooldown", "cooldown_days_left", "==", 0, account.cooldown_days_left)
        )
    if r.max_loss_pct_of_capital is not None:
        cap = limits.capital_inr * r.max_loss_pct_of_capital / 100 if limits.capital_inr else None
        checks.append(_compare("risk.max_loss_pct_of_capital", "max_loss_inr", "<=", cap, round(proposal.max_loss, 2)))
    if limits.portfolio_max_open_positions is not None:
        checks.append(
            _compare(
                "portfolio.max_open_positions", "portfolio_open_positions", "<",
                limits.portfolio_max_open_positions, account.portfolio_open_positions,
            )  # fmt: skip
        )
    if limits.portfolio_max_daily_loss_inr is not None:
        checks.append(
            _compare(
                "portfolio.max_daily_loss_inr", "portfolio_daily_loss_inr", "<",
                limits.portfolio_max_daily_loss_inr, -min(account.portfolio_pnl_today, 0),
            )  # fmt: skip
        )
    return checks
