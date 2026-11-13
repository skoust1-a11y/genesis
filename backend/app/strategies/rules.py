"""Machine-checkable strategy rules: the `rules:` block in a strategy's frontmatter.

Everything the Trade Crew decides comes from these checks. They are pure functions with no
LLM involvement, so the same inputs always produce the same decision.
"""

from __future__ import annotations

import operator
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.db.hashing import canonical_json, normalize_json, sha256_hex

STRATEGY_RULES_V2_VERSION = "strategy-rules-v2"
_HHMM_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
INTRADAY_TIMEFRAMES = ("1m", "5m", "15m")
SUPPORTED_TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h", "1d")

EntryField = Literal[
    "iv_rank", "dte", "spot", "vix", "pct_from_prev_high", "pct_from_prev_low",
    "pct_high_from_prev_high", "pct_low_from_prev_low",
]  # fmt: skip
ENTRY_FIELDS = (
    "iv_rank", "dte", "spot", "vix", "pct_from_prev_high", "pct_from_prev_low",
    "pct_high_from_prev_high", "pct_low_from_prev_low",
)  # fmt: skip
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
CAPITAL_EXITS = ("stop_loss_pct_of_capital", "profit_target_r_multiple")
EXIT_KINDS = (*CREDIT_EXITS, *DEBIT_EXITS, *CAPITAL_EXITS, "dte_at_or_below")
MAX_STOP_PCT_OF_CAPITAL = 20
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
        "profit_target_pct_of_debit", "stop_loss_pct_of_debit",
        "stop_loss_pct_of_capital", "profit_target_r_multiple", "dte_at_or_below",
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
    max_losses_per_day: int | None = Field(default=None, ge=1)  # losing trades closed today that stop new entries
    min_minutes_between_trades: int | None = Field(default=None, ge=1, le=375)  # since the last entry or exit

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
        if kinds & set(CAPITAL_EXITS):
            if missing := set(CAPITAL_EXITS) - kinds:
                raise ValueError(f"exit needs {', '.join(sorted(missing))}: the capital stop and R target go together")
            if mixed := kinds & set(CREDIT_EXITS + DEBIT_EXITS):
                raise ValueError(f"{', '.join(sorted(mixed))} cannot be combined with a stop as % of capital")
            if not 0 < self.stop_loss_pct_of_capital <= MAX_STOP_PCT_OF_CAPITAL:
                raise ValueError(f"stop_loss_pct_of_capital must be above 0 and at most {MAX_STOP_PCT_OF_CAPITAL}")
            if self.profit_target_r_multiple <= 0:
                raise ValueError("profit_target_r_multiple must be above 0")
            return self
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
    def uses_capital_stop(self) -> bool:
        """Stop as % of capital and target as a multiple of it, instead of % of the premium."""
        return self.stop_loss_pct_of_capital is not None

    @property
    def stop_loss_pct_of_capital(self) -> float | None:
        return self.exit_value("stop_loss_pct_of_capital")

    @property
    def profit_target_r_multiple(self) -> float | None:
        return self.exit_value("profit_target_r_multiple")

    @property
    def profit_target_pct(self) -> float | None:
        """Profit target as % of the premium received (credit structures) or paid (debit structures);
        None with a stop as % of capital."""
        return self.exit_value(f"profit_target_pct_of_{'debit' if self.instrument.is_debit else 'credit'}")

    @property
    def stop_loss_pct(self) -> float | None:
        return self.exit_value(f"stop_loss_pct_of_{'debit' if self.instrument.is_debit else 'credit'}")


def risk_reward_ratio(rules: StrategyRules) -> float | None:
    """Target over stop: the R multiple, or the premium target % over the premium stop % (90/30 = 3)."""
    if rules.profit_target_r_multiple is not None:
        return rules.profit_target_r_multiple
    if rules.profit_target_pct is None or not rules.stop_loss_pct:
        return None
    return rules.profit_target_pct / rules.stop_loss_pct


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


class ProvenanceSpec(_Strict):
    source_type: str = Field(default="manual", min_length=1)
    source_ref: str | None = None
    source_sha256: str | None = None
    ingest_run_id: str | None = None
    author_hints_applied: bool = False
    extraction_mode: Literal["deterministic", "llm_extracted", "manual", "frozen_benchmark"] = "manual"
    citations: list[str] = Field(default_factory=list)


class TimeframeSpec(_Strict):
    primary: Literal["1m", "5m", "15m", "1h", "4h", "1d"]
    structure: Literal["5m", "15m", "1h", "4h", "1d"] | None = None
    reference: Literal["15m", "1h", "4h", "1d", "1w"] | None = None


class SessionWindowSpec(_Strict):
    timezone: Literal["Asia/Kolkata"] = "Asia/Kolkata"
    entry_start: str = "09:15"
    entry_cutoff: str = "15:10"
    square_off: str = "15:15"
    blackout_windows: list[tuple[str, str]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_session_times(self) -> SessionWindowSpec:
        for label, hhmm in (
            ("entry_start", self.entry_start),
            ("entry_cutoff", self.entry_cutoff),
            ("square_off", self.square_off),
        ):
            if not _HHMM_RE.match(hhmm):
                raise ValueError(f"{label} must be HH:MM (got {hhmm!r})")
        if not ("09:15" <= self.entry_start <= self.entry_cutoff <= self.square_off <= "15:30"):
            raise ValueError(
                "session_window must satisfy 09:15 <= entry_start <= entry_cutoff <= square_off <= 15:30"
            )
        if self.entry_cutoff > "15:10":
            raise ValueError("session_window.entry_cutoff must not exceed 15:10 IST")
        for start, end in self.blackout_windows:
            if not (_HHMM_RE.match(start) and _HHMM_RE.match(end)) or start >= end:
                raise ValueError(f"invalid blackout window ({start!r}, {end!r})")
        return self


class IntradayEntryPredicate(_Strict):
    kind: Literal[
        "ema_relation",
        "vwap_relation",
        "pdc_zone_reaction",
        "pdh_pdl_sweep",
        "or30_breakout_retest",
        "trendline_breakout_3rd_touch",
        "equal_hl_sweep",
        "candle_body_ratio",
        "custom_predicate",
    ]
    timeframe: Literal["1m", "5m", "15m", "1h", "4h", "1d"] = "5m"
    direction: Literal["LONG", "SHORT", "BOTH"] = "BOTH"
    params: dict[str, float | int | str | bool] = Field(default_factory=dict)
    requires_closed_bar: bool = True

    @model_validator(mode="after")
    def _require_closed_bar(self) -> IntradayEntryPredicate:
        if not self.requires_closed_bar:
            raise ValueError("intraday_entry predicates must operate on closed bars only (requires_closed_bar=True)")
        return self


class StopLossSpec(_Strict):
    mode: Literal[
        "pct_of_credit",
        "pct_of_debit",
        "pct_of_capital",
        "wick_touch",
        "candle_close",
        "swing_extreme_buffer",
        "index_points",
    ]
    value: float = Field(gt=0)
    min_points: float | None = Field(default=None, gt=0)
    max_points: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _validate_bounds(self) -> StopLossSpec:
        if self.min_points is not None and self.max_points is not None and self.min_points > self.max_points:
            raise ValueError("stop_loss.min_points cannot exceed max_points")
        if self.mode == "pct_of_debit" and self.value > 100:
            raise ValueError("stop_loss pct_of_debit cannot exceed 100")
        if self.mode == "pct_of_capital" and self.value > MAX_STOP_PCT_OF_CAPITAL:
            raise ValueError(f"stop_loss pct_of_capital cannot exceed {MAX_STOP_PCT_OF_CAPITAL}")
        return self


class TargetSpec(_Strict):
    mode: Literal[
        "pct_of_credit",
        "pct_of_debit",
        "r_multiple",
        "opposing_liquidity_level",
        "index_points",
    ]
    value: float = Field(gt=0)
    allow_liquidity_hop: bool = False


class PartialExitRule(_Strict):
    at_r_multiple: float = Field(gt=0)
    book_fraction: float = Field(gt=0, lt=1)
    move_stop_to: Literal["entry", "structure_trail", "plus_1r", "unchanged"] = "entry"


class PositionSizingSpec(_Strict):
    risk_policy_id: str = Field(default="paper-risk-v2", min_length=1)
    capital_inr: float = Field(default=50000.0, gt=0)
    max_lots: int = Field(default=1, ge=1)
    lot_step_mode: Literal["fixed_lots", "risk_budget_lots"] = "risk_budget_lots"


class IndicatorDependency(_Strict):
    name: Literal[
        "ema",
        "vwap",
        "adx",
        "choppiness",
        "atr",
        "iv_rank",
        "vix",
        "or30",
        "pdh_pdl_pdc",
        "swing_levels",
    ]
    timeframe: Literal["1m", "5m", "15m", "1h", "4h", "1d"] = "5m"
    period: int | None = Field(default=None, gt=0)
    params: dict[str, float | int | str | bool] = Field(default_factory=dict)


class ParameterDef(_Strict):
    name: str = Field(min_length=1)
    type: Literal["int", "float", "decimal", "str", "bool"]
    value: int | float | str | bool
    min_value: float | None = None
    max_value: float | None = None
    description: str | None = None
    mutable_in_research: bool = True

    @model_validator(mode="after")
    def _validate_parameter_type_and_bounds(self) -> ParameterDef:
        if self.type == "bool":
            if not isinstance(self.value, bool):
                raise ValueError(f"parameter {self.name!r} declared as bool but value is {type(self.value).__name__}")
        elif self.type == "int":
            if isinstance(self.value, bool) or not isinstance(self.value, int):
                raise ValueError(f"parameter {self.name!r} declared as int but value is {self.value!r}")
        elif self.type in ("float", "decimal"):
            if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
                raise ValueError(f"parameter {self.name!r} declared as {self.type} but value is {self.value!r}")
        elif self.type == "str":
            if not isinstance(self.value, str) or not self.value.strip():
                raise ValueError(f"parameter {self.name!r} declared as str requires non-empty string")
        if self.min_value is not None and self.max_value is not None and self.min_value > self.max_value:
            raise ValueError(f"parameter {self.name!r} min_value ({self.min_value}) > max_value ({self.max_value})")
        if self.type in ("int", "float", "decimal") and not isinstance(self.value, bool):
            num = float(self.value)
            if self.min_value is not None and num < self.min_value:
                raise ValueError(f"parameter {self.name!r} value ({num}) < min_value ({self.min_value})")
            if self.max_value is not None and num > self.max_value:
                raise ValueError(f"parameter {self.name!r} value ({num}) > max_value ({self.max_value})")
        return self


class StrategyRulesV2(_Strict):
    schema_version: Literal["strategy-rules-v2"] = STRATEGY_RULES_V2_VERSION
    strategy_id: str = Field(min_length=1)
    strategy_version: str = Field(default="v1", min_length=1)
    market: Literal["nse_options", "nse_futures", "nse_equity"] = "nse_options"
    timeframes: TimeframeSpec
    instrument: Instrument
    entry: list[EntryCondition] = Field(default_factory=list)
    intraday_entry: list[IntradayEntryPredicate] = Field(default_factory=list)
    entry_composition: Literal["ALL", "ANY"] | None = None
    exit: list[ExitRule] = Field(default_factory=list)
    stop_loss: StopLossSpec | None = None
    target: TargetSpec | None = None
    partial_exits: list[PartialExitRule] = Field(default_factory=list)
    risk: RiskRules
    position_sizing: PositionSizingSpec = Field(default_factory=PositionSizingSpec)
    session_window: SessionWindowSpec | None = None
    indicators: list[IndicatorDependency] = Field(default_factory=list)
    data_dependencies: list[str] = Field(default_factory=list)
    entry_blackout: EntryBlackout | None = None
    parameters: dict[str, ParameterDef] = Field(default_factory=dict)
    provenance: ProvenanceSpec = Field(default_factory=ProvenanceSpec)
    unresolved_gaps: list[str] = Field(default_factory=list)

    @field_validator("strategy_id", "strategy_version")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("must not be blank")
        return s

    @model_validator(mode="after")
    def _validate_v2_contract(self) -> StrategyRulesV2:
        if not self.entry and not self.intraday_entry:
            raise ValueError("entry conditions required: provide at least one of entry or intraday_entry")
        if self.timeframes.primary in INTRADAY_TIMEFRAMES and self.session_window is None:
            raise ValueError(f"session_window is required for intraday timeframe {self.timeframes.primary}")
        for key, param in self.parameters.items():
            if param.name != key:
                raise ValueError(f"parameters[{key!r}].name ({param.name!r}) must match dictionary key")
        # Validate legacy exit block if provided, and synthesize/verify stop_loss & target
        if self.exit:
            StrategyRules(
                instrument=self.instrument,
                entry=self.entry or [EntryCondition(field="dte", op=">=", value=0)],
                exit=self.exit,
                risk=self.risk,
                entry_blackout=self.entry_blackout,
            )
            if self.stop_loss is None or self.target is None:
                derived_stop, derived_target = _derive_stop_and_target_from_exits(self.exit)
                if self.stop_loss is None:
                    self.stop_loss = derived_stop
                if self.target is None:
                    self.target = derived_target
        if self.stop_loss is None:
            raise ValueError("stop_loss rule is required")
        if self.target is None:
            raise ValueError("target rule is required")
        if self.partial_exits:
            total_frac = sum(p.book_fraction for p in self.partial_exits)
            if total_frac >= 1.0:
                raise ValueError("sum of partial_exits.book_fraction must be < 1.0")
        return self

    @property
    def is_quarantined(self) -> bool:
        return bool(self.unresolved_gaps)

    def to_legacy_rules(self) -> StrategyRules | None:
        if (self.timeframes.primary != "1d" or self.intraday_entry or self.partial_exits
                or self.session_window is not None or self.timeframes.structure is not None):
            return None
        if not self.entry or not self.exit:
            return None
        return StrategyRules(
            instrument=self.instrument,
            entry=self.entry,
            exit=self.exit,
            risk=self.risk,
            entry_blackout=self.entry_blackout,
        )

    def rules_v2_sha256(self) -> str:
        payload = self.model_dump(mode="json", exclude={"provenance"})
        return sha256_hex(canonical_json(normalize_json(payload)))

    def parameter_hash(self) -> str:
        params_payload = {
            k: v.model_dump(mode="json")
            for k, v in sorted(self.parameters.items())
        }
        return sha256_hex(canonical_json(normalize_json(params_payload)))

    def identity_hash(self) -> str:
        payload = {
            "schema_version": self.schema_version,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "market": self.market,
            "underlying": self.instrument.underlying,
            "structure": self.instrument.structure,
            "primary_timeframe": self.timeframes.primary,
            "rules_v2_sha256": self.rules_v2_sha256(),
            "parameter_hash": self.parameter_hash(),
        }
        return sha256_hex(canonical_json(normalize_json(payload)))


def _derive_stop_and_target_from_exits(exits: list[ExitRule]) -> tuple[StopLossSpec | None, TargetSpec | None]:
    stop_spec: StopLossSpec | None = None
    target_spec: TargetSpec | None = None
    for rule in exits:
        if rule.kind == "stop_loss_pct_of_credit" and rule.value > 0:
            stop_spec = StopLossSpec(mode="pct_of_credit", value=rule.value)
        elif rule.kind == "stop_loss_pct_of_debit" and rule.value > 0:
            stop_spec = StopLossSpec(mode="pct_of_debit", value=rule.value)
        elif rule.kind == "stop_loss_pct_of_capital" and rule.value > 0:
            stop_spec = StopLossSpec(mode="pct_of_capital", value=rule.value)
        elif rule.kind == "profit_target_pct_of_credit" and rule.value > 0:
            target_spec = TargetSpec(mode="pct_of_credit", value=rule.value)
        elif rule.kind == "profit_target_pct_of_debit" and rule.value > 0:
            target_spec = TargetSpec(mode="pct_of_debit", value=rule.value)
        elif rule.kind == "profit_target_r_multiple" and rule.value > 0:
            target_spec = TargetSpec(mode="r_multiple", value=rule.value)
    return stop_spec, target_spec


def parse_rules_v2(frontmatter: dict) -> tuple[StrategyRulesV2 | None, list[str]]:
    raw = frontmatter.get("rules_v2")
    if raw is None:
        return None, ["rules_v2: block missing"]
    try:
        return StrategyRulesV2.model_validate(raw), []
    except ValidationError as exc:
        return None, [_format_error(e).replace("rules.", "rules_v2.", 1) for e in exc.errors()]


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


def check_exit(
    rules: StrategyRules, position: PositionState, snapshot: Any, frozen: tuple[float, float] | None = None
) -> list[Check]:
    """One check per exit rule; `passed` means the exit is triggered. Targets and stops are percentages
    of the premium received or paid, or, for a stop as % of capital, the rupee (target, stop) fixed at
    entry and passed as `frozen`. Without `frozen` those two checks cannot fire."""
    premium = abs(position.credit)
    target_inr, stop_inr = frozen if frozen else (None, None)
    checks = []
    for e in rules.exit:
        if e.kind in ("profit_target_pct_of_credit", "profit_target_pct_of_debit"):
            checks.append(_compare("exit.profit_target", "pnl", ">=", premium * e.value / 100, position.pnl))
        elif e.kind in ("stop_loss_pct_of_credit", "stop_loss_pct_of_debit"):
            checks.append(_compare("exit.stop_loss", "pnl", "<=", -premium * e.value / 100, position.pnl))
        elif e.kind == "profit_target_r_multiple":
            checks.append(_compare("exit.profit_target", "pnl", ">=", target_inr, position.pnl))
        elif e.kind == "stop_loss_pct_of_capital":
            checks.append(_compare("exit.stop_loss", "pnl", "<=", stop_inr, position.pnl))
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
    losses_today: int = 0  # closed trades with a net loss today
    minutes_since_last_trade: float | None = None  # since the latest entry or exit; None before the first


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
    of the two wins: fewer entries, a shorter losing streak, a longer cooldown, a smaller loss cap, fewer
    losses a day, a longer gap between trades."""
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
    if "max_losses_per_day" in common:
        update["max_losses_per_day"] = int(_strictest(r.max_losses_per_day, common["max_losses_per_day"]))
    if "min_minutes_between_trades" in common:
        update["min_minutes_between_trades"] = int(
            max(r.min_minutes_between_trades or 0, common["min_minutes_between_trades"])
        )
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
    if r.max_losses_per_day is not None:
        checks.append(
            _compare("risk.max_losses_per_day", "losses_today", "<", r.max_losses_per_day, account.losses_today)
        )
    if r.min_minutes_between_trades is not None:
        gap, n = account.minutes_since_last_trade, r.min_minutes_between_trades
        gap = round(gap, 1) if gap is not None else None  # None: no trade yet, so nothing to wait for
        checks.append(
            Check("risk.min_minutes_between_trades", "minutes_since_last_trade", ">=", n, gap, gap is None or gap >= n)
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
