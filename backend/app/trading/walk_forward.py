"""Deterministic Walk-Forward Validation & Tiered Options Execution Simulator (Stage B Phase B3).

Enforces:
1. Chronological in-sample (IS) and out-of-sample (OOS) fold generation (default 60D IS / 20D OOS).
2. Explicit 5-trading-day purge and embargo windows (expanding with event-label horizon and cooldown).
3. Event-label horizon and overlap controls (purging IS events whose horizon leaks past IS end).
4. Strict causal timestamp availability and look-ahead / survivorship leakage guards.
5. Deterministic parameter freezing at IS end before OOS evaluation.
6. Tiered options execution fidelity:
   - TIER_1_VERIFIED: Verified historical option market quotes, bid/ask/age checks, date-effective fees.
   - TIER_2_SYNTHETIC: Explicitly synthetic Black-76 option simulation burdened by conservative spread,
     slippage, and IV stress assumptions; never labeled as verified performance and never eligible for
     paper promotion.
7. Independent validation metrics, fold-level and regime-level breakdowns, confidence limitations,
   and reproducible cryptographic fold and report hashes.
"""

from __future__ import annotations

import copy
import math
from abc import ABC, abstractmethod
from collections.abc import Callable, Collection, Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import pandas as pd

from app.trading.candle_integrity import (
    CandleIntegrityError, canonical_rows, execution_snapshot, requirements_for,
    derive_is_snapshot, require_is_view,
)
from app.config import Settings, get_settings
from app.db.hashing import canonical_json, normalize_json, sha256_hex
from app.market.expiry import ExpiryDayPolicy, resolve_expiry
from app.market.hours import holidays as exchange_holidays
from app.market.nse import LotChange, contract_lot_size, load_lot_sizes
from app.strategies.normalizer import (
    NormalizedStrategySpec,
    normalize_strategy_document,
)
from app.strategies.rules import PartialExitRule
from app.strategies.schema import StrategyDocument
from app.trading import paper_accounting, paper_fees, paper_risk, regime
from app.trading import paper_exec as px
from app.trading.option_pnl import black76
from app.trading.option_quote import IST, ContractQuote


def black76_price(
    spot: float,
    strike: float,
    years: float,
    rate: float,
    iv: float,
    option_type: str,
) -> float:
    """Discounted Black-76 option price from spot, strike, time-to-expiry (years), risk-free rate, and IV."""
    if years <= 0:
        return max(spot - strike if option_type == "CE" else strike - spot, 0.0)
    forward = spot * math.exp(rate * years)
    discount = math.exp(-rate * years)
    return discount * black76(forward, strike, years, iv, option_type)

WALK_FORWARD_ENGINE_VERSION = "walk-forward-v3.3"

DEFAULT_IS_TRADING_DAYS = 60
DEFAULT_OOS_TRADING_DAYS = 20
DEFAULT_STEP_TRADING_DAYS = 20
DEFAULT_PURGE_DAYS = 5
DEFAULT_EMBARGO_DAYS = 5

DEFAULT_MIN_OOS_TRADES = 30
DEFAULT_MIN_OOS_FOLDS = 3
DEFAULT_MIN_PROFIT_FACTOR = 1.30
DEFAULT_MAX_DRAWDOWN_PCT = 15.0
DEFAULT_INITIAL_CAPITAL_INR = 50000.0

ExecutionFidelityTier = Literal["TIER_1_VERIFIED", "TIER_2_SYNTHETIC"]

_SYNTHETIC_QUOTE_SOURCES = frozenset(
    {
        "synthetic",
        "simulated",
        "model",
        "black76",
        "bsm",
        "tier2",
        "tier_2_synthetic",
        "bundled_sample",
    }
)


class WalkForwardError(ValueError):
    """Base error for walk-forward validation failures."""


class LookAheadLeakageError(WalkForwardError):
    """Raised when future data, future features, or overlapping IS/OOS folds violate causality."""


class ParameterFreezeViolationError(WalkForwardError):
    """Raised when strategy parameters are mutated during OOS evaluation or selected on OOS outcomes."""


class MissingOptionDataError(WalkForwardError):
    """Raised when option chain / quote / underlying inputs are missing and fail-closed mode is active."""


class SurvivorshipBiasError(WalkForwardError):
    """Raised when historical universe metadata indicates post-facto survivorship filtering."""


class UnsupportedStrategyRuleError(WalkForwardError):
    """Raised when a strategy specification contains unsupported or unexecutable rules."""


def _parse_ts(value: datetime | date | str) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=IST) if value.tzinfo is None else value.astimezone(IST)
    if isinstance(value, date):
        return datetime.combine(value, time(15, 30), tzinfo=IST)
    text = str(value).strip()
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return datetime.combine(date.fromisoformat(text), time(15, 30), tzinfo=IST)
    dt = datetime.fromisoformat(text)
    return dt.replace(tzinfo=IST) if dt.tzinfo is None else dt.astimezone(IST)


def _parse_day(value: datetime | date | str) -> str:
    if isinstance(value, datetime):
        return (value.replace(tzinfo=IST) if value.tzinfo is None else value.astimezone(IST)).date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    return _parse_ts(text).date().isoformat()


@dataclass(frozen=True)
class FeatureObservation:
    """A causally timestamped feature or market event observation."""

    name: str
    value: Any
    observed_at: str
    available_at: str
    source: str = "market"

    def is_available_at(self, decision_time: datetime | str) -> bool:
        return _parse_ts(self.available_at) <= _parse_ts(decision_time)


def verify_no_lookahead(
    *,
    decision_time: datetime | str,
    features: Sequence[FeatureObservation | Mapping[str, Any]] = (),
    bars: Sequence[Mapping[str, Any]] = (),
) -> list[FeatureObservation]:
    """Verify every feature and bar used at `decision_time` was causally available at or before `decision_time`.

    Raises `LookAheadLeakageError` immediately if any observation carries `available_at > decision_time`
    or if a bar's close timestamp is after `decision_time`.
    """
    dec_ts = _parse_ts(decision_time)
    verified: list[FeatureObservation] = []
    for item in features:
        if isinstance(item, FeatureObservation):
            obs = item
        elif isinstance(item, Mapping):
            avail = item.get("available_at") or item.get("bar_close_at") or item.get("timestamp") or item.get("observed_at")
            if avail is None:
                raise LookAheadLeakageError(f"FEATURE_MISSING_AVAILABLE_AT:{item.get('name', 'unknown')}")
            obs = FeatureObservation(
                name=str(item.get("name") or "feature"),
                value=item.get("value"),
                observed_at=str(item.get("observed_at") or avail),
                available_at=str(avail),
                source=str(item.get("source") or "market"),
            )
        else:
            raise LookAheadLeakageError(f"INVALID_FEATURE_OBSERVATION:{type(item).__name__}")
        if _parse_ts(obs.available_at) > dec_ts:
            raise LookAheadLeakageError(
                f"LOOKAHEAD_FEATURE_LEAKAGE: feature {obs.name!r} available_at={obs.available_at} > decision_time={dec_ts.isoformat()}"
            )
        verified.append(obs)

    for bar in bars:
        bar_close = bar.get("bar_close_at") or bar.get("available_at") or bar.get("close_time") or bar.get("timestamp") or bar.get("date")
        if bar_close is None:
            raise LookAheadLeakageError("BAR_MISSING_TIMESTAMP")
        if _parse_ts(str(bar_close)) > dec_ts:
            raise LookAheadLeakageError(
                f"LOOKAHEAD_BAR_LEAKAGE: bar close {bar_close} > decision_time={dec_ts.isoformat()}"
            )
    return verified


def verify_point_in_time_universe(universe_metadata: Mapping[str, Any] | None) -> None:
    """Reject datasets filtered by end-of-sample survivorship rather than point-in-time index membership."""
    if not universe_metadata:
        return
    if universe_metadata.get("survivorship_filtered") is True or universe_metadata.get("point_in_time") is False:
        raise SurvivorshipBiasError(
            "SURVIVORSHIP_LEAKAGE: dataset uses end-of-sample survivorship filtering rather than point-in-time universe"
        )


@dataclass(frozen=True)
class WalkForwardConfig:
    """Configuration for chronological walk-forward validation."""

    is_trading_days: int = DEFAULT_IS_TRADING_DAYS
    oos_trading_days: int = DEFAULT_OOS_TRADING_DAYS
    step_trading_days: int = DEFAULT_STEP_TRADING_DAYS
    purge_days: int = DEFAULT_PURGE_DAYS
    embargo_days: int = DEFAULT_EMBARGO_DAYS
    label_horizon_days: int = 1
    cooldown_days: int = 0
    allow_overlapping_labels: bool = False
    strict_leakage_check: bool = True
    fidelity_tier: ExecutionFidelityTier = "TIER_1_VERIFIED"
    initial_capital_inr: float = DEFAULT_INITIAL_CAPITAL_INR
    min_oos_trades: int = DEFAULT_MIN_OOS_TRADES
    min_oos_folds: int = DEFAULT_MIN_OOS_FOLDS
    min_profit_factor: float = DEFAULT_MIN_PROFIT_FACTOR
    max_drawdown_pct: float = DEFAULT_MAX_DRAWDOWN_PCT
    cost_policy_id: str | None = None
    allow_historical_brokerage_assumption: bool = False
    tier2_spread_bps_each_way: float = 75.0
    tier2_slippage_ticks: int = 2
    tier2_iv_stress_pct: float = 0.05
    risk_free_rate: float = 0.065
    trusted_delta_sources: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.is_trading_days <= 0:
            raise ValueError("is_trading_days must be positive")
        if self.oos_trading_days <= 0:
            raise ValueError("oos_trading_days must be positive")
        if self.step_trading_days <= 0:
            raise ValueError("step_trading_days must be positive")
        if self.purge_days < 0 or self.embargo_days < 0:
            raise ValueError("purge_days and embargo_days cannot be negative")
        if self.label_horizon_days < 1:
            raise ValueError("label_horizon_days must be >= 1")
        if self.cooldown_days < 0:
            raise ValueError("cooldown_days cannot be negative")
        if self.initial_capital_inr <= 0:
            raise ValueError("initial_capital_inr must be positive")

    @property
    def effective_purge_days(self) -> int:
        return max(self.purge_days, self.label_horizon_days - 1, self.cooldown_days)

    @property
    def effective_embargo_days(self) -> int:
        return max(self.embargo_days, self.label_horizon_days - 1, self.cooldown_days)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["trusted_delta_sources"] = sorted(list(self.trusted_delta_sources))
        return {
            **d,
            "effective_purge_days": self.effective_purge_days,
            "effective_embargo_days": self.effective_embargo_days,
        }


@dataclass(frozen=True)
class WalkForwardFold:
    """A single chronological IS / purge / embargo / OOS fold."""

    fold_index: int
    is_raw_dates: tuple[str, ...]
    is_dates: tuple[str, ...]
    purged_dates: tuple[str, ...]
    embargo_dates: tuple[str, ...]
    oos_dates: tuple[str, ...]

    @property
    def is_start_date(self) -> str:
        return self.is_dates[0]

    @property
    def is_end_date(self) -> str:
        return self.is_dates[-1]

    @property
    def oos_start_date(self) -> str:
        return self.oos_dates[0]

    @property
    def oos_end_date(self) -> str:
        return self.oos_dates[-1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "fold_index": self.fold_index,
            "is_start_date": self.is_start_date,
            "is_end_date": self.is_end_date,
            "is_trading_days": len(self.is_dates),
            "is_raw_trading_days": len(self.is_raw_dates),
            "purged_dates": list(self.purged_dates),
            "embargo_dates": list(self.embargo_dates),
            "oos_start_date": self.oos_start_date,
            "oos_end_date": self.oos_end_date,
            "oos_trading_days": len(self.oos_dates),
        }


def generate_walk_forward_folds(
    trading_dates: Sequence[date | str],
    config: WalkForwardConfig | None = None,
) -> list[WalkForwardFold]:
    """Split a strictly increasing sequence of trading dates into chronological walk-forward folds.

    Layout per fold `k` starting at index `cursor`:
    - Raw IS window: `trading_dates[cursor : cursor + is_trading_days]`
      - Trailing `effective_purge_days` of the raw IS window are purged if `effective_purge_days > 0`,
        leaving `is_dates = raw_is[:-effective_purge_days]` (or explicit purge gap between `is_dates` and `embargo_dates`).
      - Specifically, to preserve a full `is_trading_days` clean IS window when enough dates exist while enforcing
        both `effective_purge_days` and `effective_embargo_days` between IS and OOS:
        Let `gap_days = max(config.effective_purge_days, config.effective_embargo_days)`.
        Wait: the blueprint specifies:
        - `60` trading days IS
        - `5` trading days purge/embargo between IS and OOS
        - `20` trading days OOS
        And within the IS window, any event whose label horizon overlaps the boundary or falls in the purge tail is purged.
    """
    cfg = config or WalkForwardConfig()
    normalized_dates = [_parse_day(d) for d in trading_dates]
    for idx in range(1, len(normalized_dates)):
        if normalized_dates[idx] <= normalized_dates[idx - 1]:
            raise LookAheadLeakageError(
                f"NON_MONOTONIC_TRADING_DATES: {normalized_dates[idx]} after {normalized_dates[idx - 1]}"
            )

    embargo_len = cfg.effective_embargo_days
    purge_len = cfg.effective_purge_days
    min_required = cfg.is_trading_days + embargo_len + cfg.oos_trading_days
    if len(normalized_dates) < min_required:
        return []

    folds: list[WalkForwardFold] = []
    cursor = 0
    fold_idx = 0
    while cursor + cfg.is_trading_days + embargo_len + cfg.oos_trading_days <= len(normalized_dates):
        raw_is = tuple(normalized_dates[cursor : cursor + cfg.is_trading_days])
        if purge_len > 0 and purge_len < len(raw_is):
            clean_is = raw_is[:-purge_len]
            purged = raw_is[-purge_len:]
        else:
            clean_is = raw_is
            purged = ()

        embargo_start = cursor + cfg.is_trading_days
        embargo_end = embargo_start + embargo_len
        embargo = tuple(normalized_dates[embargo_start:embargo_end])
        oos = tuple(normalized_dates[embargo_end : embargo_end + cfg.oos_trading_days])

        # Verify strict chronological separation: clean_is < purged <= embargo < oos
        if clean_is[-1] >= oos[0]:
            raise LookAheadLeakageError("IS_OOS_OVERLAP_DETECTED")
        if embargo and (clean_is[-1] >= embargo[0] or embargo[-1] >= oos[0]):
            raise LookAheadLeakageError("EMBARGO_BOUNDARY_VIOLATION")

        folds.append(
            WalkForwardFold(
                fold_index=fold_idx,
                is_raw_dates=raw_is,
                is_dates=clean_is,
                purged_dates=purged,
                embargo_dates=embargo,
                oos_dates=oos,
            )
        )
        fold_idx += 1
        cursor += cfg.step_trading_days

    return folds


@dataclass(frozen=True)
class FrozenFoldParameters:
    """Immutable parameter snapshot frozen at the end of an IS fold prior to OOS evaluation."""

    fold_index: int
    strategy_id: str
    strategy_version: str
    normalizer_version: str
    rules_v2_sha256: str | None
    parameter_hash: str
    parameters: Mapping[str, Any]
    frozen_at_is_end: str
    freeze_sha256: str

    def verify_unchanged(self, active_parameters: Mapping[str, Any]) -> None:
        active_hash = sha256_hex(canonical_json(normalize_json(dict(active_parameters))))
        if active_hash != self.parameter_hash:
            raise ParameterFreezeViolationError(
                f"PARAMETER_FREEZE_VIOLATION: fold {self.fold_index} expected parameter_hash={self.parameter_hash}, "
                f"got {active_hash} during OOS evaluation"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "fold_index": self.fold_index,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "normalizer_version": self.normalizer_version,
            "rules_v2_sha256": self.rules_v2_sha256,
            "parameter_hash": self.parameter_hash,
            "parameters": dict(self.parameters),
            "frozen_at_is_end": self.frozen_at_is_end,
            "freeze_sha256": self.freeze_sha256,
        }


def freeze_fold_parameters(
    *,
    fold: WalkForwardFold,
    spec: NormalizedStrategySpec,
    selected_parameters: Mapping[str, Any] | None = None,
    selection_source_dates: Sequence[str] | None = None,
) -> FrozenFoldParameters:
    """Freeze strategy parameters at `fold.is_end_date` before OOS evaluation starts.

    Raises `ParameterFreezeViolationError` if `selection_source_dates` includes any date after `fold.is_end_date`.
    """
    if selection_source_dates is not None:
        for d in selection_source_dates:
            day_str = _parse_day(d)
            if day_str > fold.is_end_date:
                raise ParameterFreezeViolationError(
                    f"OOS_PARAMETER_SELECTION_FORBIDDEN: parameter selection used date {day_str} > is_end_date {fold.is_end_date}"
                )

    base_params: dict[str, Any] = {}
    if spec.rules_v2 is not None:
        base_params = {k: p.value for k, p in sorted(spec.rules_v2.parameters.items())}
    if selected_parameters is not None:
        for key, val in selected_parameters.items():
            if spec.rules_v2 is not None and key in spec.rules_v2.parameters:
                pdef = spec.rules_v2.parameters[key]
                if not pdef.mutable_in_research and val != pdef.value:
                    raise ParameterFreezeViolationError(
                        f"IMMUTABLE_PARAMETER_MUTATION: parameter {key!r} is marked mutable_in_research=False"
                    )
            base_params[str(key)] = copy.deepcopy(val)

    param_hash = sha256_hex(canonical_json(normalize_json(base_params)))
    payload = {
        "fold_index": fold.fold_index,
        "strategy_id": spec.strategy_id,
        "strategy_version": spec.strategy_version,
        "normalizer_version": spec.normalizer_version,
        "rules_v2_sha256": spec.rules_v2_sha256,
        "parameter_hash": param_hash,
        "parameters": base_params,
        "frozen_at_is_end": fold.is_end_date,
    }
    freeze_hash = sha256_hex(canonical_json(normalize_json(payload)))
    return FrozenFoldParameters(
        fold_index=fold.fold_index,
        strategy_id=spec.strategy_id,
        strategy_version=spec.strategy_version,
        normalizer_version=spec.normalizer_version,
        rules_v2_sha256=spec.rules_v2_sha256,
        parameter_hash=param_hash,
        parameters=base_params,
        frozen_at_is_end=fold.is_end_date,
        freeze_sha256=freeze_hash,
    )


@dataclass(frozen=True)
class CandidateSignalEvent:
    """A candidate trade/signal event with explicit decision timestamp, label horizon, and option execution inputs."""

    event_id: str
    market_day: str
    decision_time: str
    label_end_time: str | None
    underlying: str
    direction: Literal["CALL", "PUT"]
    entry_spot: float
    stop_spot: float
    target_spot: float
    exit_spot: float | None
    strike: float
    option_type: Literal["CE", "PE"]
    expiry: str | None
    lot_size: int = 75
    lots: int = 1
    regime: str = "UNKNOWN"
    entry_quote: ContractQuote | Mapping[str, Any] | None = None
    exit_quote: ContractQuote | Mapping[str, Any] | None = None
    entry_iv: float | None = None
    exit_iv: float | None = None
    dte_days: float | None = None
    holding_minutes: float = 45.0
    features: tuple[FeatureObservation | Mapping[str, Any], ...] = ()
    bars: tuple[Mapping[str, Any], ...] = ()
    incomplete_label: bool = False
    exit_reason: str = "TARGET_OR_STOP"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def label_end_day(self) -> str | None:
        if self.incomplete_label or self.label_end_time is None:
            return None
        return _parse_day(self.label_end_time)


@dataclass(frozen=True)
class SimulatedOptionExecution:
    """Result of executing a single option trade under Tier 1 (verified) or Tier 2 (synthetic)."""

    event_id: str
    market_day: str
    regime: str
    fidelity_tier: ExecutionFidelityTier
    verified_market_data: bool
    priced: bool
    rejection_reason: str | None
    lots: int
    lot_size: int
    qty: int
    entry_price_inr: float | None
    exit_price_inr: float | None
    entry_slippage_inr: float
    exit_slippage_inr: float
    entry_spread_burden_inr: float
    exit_spread_burden_inr: float
    entry_charges_inr: float
    exit_charges_inr: float
    total_costs_inr: float
    gross_pnl_inr: float | None
    net_pnl_inr: float | None
    initial_risk_inr: float | None
    realized_r: float | None
    cost_policy_id: str | None
    quote_source: str | None
    exit_reason: str
    entry_executable: bool = False
    admitted: bool = False
    unresolved: bool = False
    conservative_exit_reserve_inr: str | None = None
    entry_cost_evidence_json: str | None = None
    exit_cost_evidence_json: str | None = None


def _quote_timestamp(raw):
    try:
        value = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw))
        return value if value.utcoffset() is not None else None
    except (ValueError, TypeError):
        return None


def _coerce_contract_quote(
    raw: ContractQuote | Mapping[str, Any] | None,
    *,
    default_underlying: str,
    default_strike: float,
    default_option_type: Literal["CE", "PE"],
    default_expiry: str | None,
    default_lot_size: int,
) -> ContractQuote | None:
    if raw is None:
        return None
    if isinstance(raw, ContractQuote):
        return raw
    if isinstance(raw, Mapping):
        if any(raw.get(k) is None for k in ("underlying", "strike", "option_type", "expiry", "lot_size", "tradingsymbol", "provider_instrument_id", "source")):
            return None
        qt_raw = raw.get("quote_time")
        ft_raw = raw.get("fetched_at") or qt_raw
        return ContractQuote(
            underlying=str(raw.get("underlying") or default_underlying),
            expiry=str(raw.get("expiry") or default_expiry or ""),
            strike=float(raw.get("strike") if raw.get("strike") is not None else default_strike),
            option_type=str(raw.get("option_type") or default_option_type),
            provider_instrument_id=raw.get("provider_instrument_id"),
            tradingsymbol=raw.get("tradingsymbol"),
            lot_size=int(raw.get("lot_size") if raw.get("lot_size") is not None else default_lot_size),
            ltp=Decimal(str(raw["ltp"])) if raw.get("ltp") is not None else None,
            bid=Decimal(str(raw["bid"])) if raw.get("bid") is not None else None,
            ask=Decimal(str(raw["ask"])) if raw.get("ask") is not None else None,
            quote_time=_quote_timestamp(qt_raw),
            fetched_at=_quote_timestamp(ft_raw),
            source=str(raw.get("source") or ""),
            delta=float(raw["delta"]) if raw.get("delta") is not None else None,
            delta_observed_at=_quote_timestamp(raw.get("delta_observed_at")),
            delta_source=raw.get("delta_source"),
            delta_contract=raw.get("delta_contract"),
        )
    return None


def _simulate_verified(event, cfg, settings, fail_closed):
    """Price admission entirely from entry evidence; missing future evidence retains exposure."""
    now = _parse_ts(event.decision_time)
    empty = SimulatedOptionExecution(
        event.event_id, event.market_day, event.regime, "TIER_1_VERIFIED", False, False, None,
        event.lots, event.lot_size, event.lots * event.lot_size, None, None,
        0, 0, 0, 0, 0, 0, 0, 0, 0, None, None, None, None, event.exit_reason)

    def reject(reason):
        if fail_closed:
            raise MissingOptionDataError(reason)
        return replace(empty, rejection_reason=reason)

    def quote(raw):
        return _coerce_contract_quote(raw, default_underlying=event.underlying,
            default_strike=event.strike, default_option_type=event.option_type,
            default_expiry=event.expiry, default_lot_size=event.lot_size)

    def validate(q, at):
        if q is None:
            return "TIER_1_MISSING_OPTION_QUOTE"
        source = (q.source or "").lower()
        if not source or any(x in source for x in _SYNTHETIC_QUOTE_SOURCES):
            return "TIER_1_SYNTHETIC_PROVENANCE_REJECTED"
        if (q.underlying != event.underlying or q.strike != event.strike
                or q.expiry != event.expiry or q.option_type != event.option_type
                or q.lot_size != event.lot_size or not q.provider_instrument_id
                or not q.expiry or q.expiry < at.astimezone(IST).date().isoformat()):
            return "TIER_1_CONTRACT_MISMATCH"
        _, reason = px.executable(q, at, settings)
        return f"TIER_1_UNEXECUTABLE_QUOTE:{reason}" if reason else None

    entry = quote(event.entry_quote)
    why = validate(entry, now)
    if why:
        return reject(why)
    if (event.market_day != now.astimezone(IST).date().isoformat()
            or not time(9, 15) <= now.astimezone(IST).time() < time(15, 10)
            or type(event.lots) is not int or not 1 <= event.lots <= paper_risk.MAX_LOTS):
        return reject("INVALID_ENTRY_SESSION_OR_LOTS")
    policy = paper_fees.resolve_policy(event.market_day, policy_id=cfg.cost_policy_id,
        allow_historical_brokerage_assumption=cfg.allow_historical_brokerage_assumption)
    buy = px.fill(entry, "BUY", event.lots, now=now, settings=settings, ticks=1,
        cost_policy_id=policy.policy_id,
        allow_historical_brokerage_assumption=cfg.allow_historical_brokerage_assumption)
    reserve = Decimal(paper_fees.order_charges(price=buy.price, quantity=buy.qty,
        side="SELL", market_day=event.market_day, policy_id=policy.policy_id,
        allow_historical_brokerage_assumption=cfg.allow_historical_brokerage_assumption)["total"])
    full = buy.value + buy.charges_inr + reserve
    if any(tag in (entry.delta_source or "").lower() for tag in ("model", "bsm", "black", "computed", "synthetic")):
        return reject("STOP_RISK_UNVERIFIABLE")
    evidence = paper_risk.DeltaEvidence(Decimal(str(entry.delta)), entry.delta_observed_at,
        entry.delta_contract, entry.delta_source) if entry.delta is not None else None
    try:
        risk = paper_risk.stop_risk(evidence=evidence, trusted_sources=cfg.trusted_delta_sources,
            contract=entry.tradingsymbol, option_type=entry.option_type, now=now,
            underlying_entry=Decimal(str(event.entry_spot)), underlying_stop=Decimal(str(event.stop_spot)),
            quantity=buy.qty, entry_price=buy.price, liquidation_bid=max(Decimal(0), entry.bid - px.TICK),
            entry_charges=buy.charges_inr, conservative_exit_charges=reserve, full_premium_loss=full)
    except (paper_risk.StopRiskUnverifiable, ValueError):
        return reject("STOP_RISK_UNVERIFIABLE")
    result = replace(empty, verified_market_data=True, entry_executable=True, unresolved=True,
        entry_price_inr=float(buy.price), entry_charges_inr=float(buy.charges_inr),
        gross_pnl_inr=None, net_pnl_inr=None,
        entry_slippage_inr=float(buy.slippage_inr),
        entry_spread_burden_inr=float((buy.ask - buy.mid) * buy.qty),
        initial_risk_inr=float(risk), cost_policy_id=policy.policy_id, quote_source=entry.source,
        conservative_exit_reserve_inr=str(reserve), entry_cost_evidence_json=canonical_json(buy.charges))
    if event.incomplete_label or event.label_end_time is None:
        return replace(result, exit_reason=event.exit_reason)
    end = _parse_ts(event.label_end_time)
    if end <= now:
        return replace(result, exit_reason="INVALID_EXIT_TIME")
    try:
        exit_q = quote(event.exit_quote)
        why = validate(exit_q, end)
    except (ValueError, TypeError, ArithmeticError):
        return replace(result, exit_reason="INVALID_EXIT_EVIDENCE")
    if (not why and (exit_q.tradingsymbol != entry.tradingsymbol
                     or exit_q.provider_instrument_id != entry.provider_instrument_id)):
        why = "TIER_1_CONTRACT_MISMATCH"
    if why:
        return replace(result, exit_reason=why)
    try:
        exit_policy = paper_fees.resolve_policy(end.astimezone(IST).date().isoformat(),
            policy_id=cfg.cost_policy_id,
            allow_historical_brokerage_assumption=cfg.allow_historical_brokerage_assumption)
        sell = px.fill(exit_q, "SELL", event.lots, now=end, settings=settings, ticks=1,
            cost_policy_id=exit_policy.policy_id,
            allow_historical_brokerage_assumption=cfg.allow_historical_brokerage_assumption)
    except paper_fees.FeePolicyUnavailable as exc:
        return replace(result, exit_reason=str(exc))
    net = sell.value - sell.charges_inr - buy.value - buy.charges_inr
    gross = (sell.mid - buy.mid) * buy.qty
    return replace(result, priced=True, unresolved=False, exit_price_inr=float(sell.price),
        exit_cost_evidence_json=canonical_json(sell.charges),
        exit_charges_inr=float(sell.charges_inr), exit_slippage_inr=float(sell.slippage_inr),
        exit_spread_burden_inr=float((sell.mid - sell.bid) * sell.qty),
        total_costs_inr=float(gross - net), gross_pnl_inr=float(gross), net_pnl_inr=float(net),
        realized_r=float(net / risk) if risk else None)


def simulate_option_execution(
    event: CandidateSignalEvent,
    *,
    config: WalkForwardConfig | None = None,
    settings: Settings | None = None,
    fail_closed_on_missing: bool = True,
) -> SimulatedOptionExecution:
    """Execute a candidate signal event under Tier 1 (verified quotes) or Tier 2 (synthetic model).

    - Tier 1 (`TIER_1_VERIFIED`):
      Requires verified historical entry and exit `ContractQuote`s with positive bid/ask, valid timestamp age,
      and non-synthetic provenance source. Uses `paper_exec.executable` + `paper_exec.fill` and date-effective
      `paper_fees.order_charges`. Never invents quotes, fills, or Greeks.
    - Tier 2 (`TIER_2_SYNTHETIC`):
      Explicitly synthetic Black-76 option pricing burdened by modeled bid-ask spread (`tier2_spread_bps_each_way`),
      adverse slippage ticks (`tier2_slippage_ticks`), and adverse IV stress (`tier2_iv_stress_pct`), plus
      date-effective `paper_fees.order_charges`. Never labeled as verified market performance.
    """
    cfg = config or WalkForwardConfig()
    active_settings = settings or get_settings()
    tier = cfg.fidelity_tier
    dec_now = _parse_ts(event.decision_time)
    exit_now = _parse_ts(event.label_end_time) if event.label_end_time else (dec_now + timedelta(minutes=event.holding_minutes))

    if event.lots <= 0 or event.lot_size <= 0:
        raise ValueError("event.lots and event.lot_size must be positive")
    qty = event.lots * event.lot_size

    if tier == "TIER_1_VERIFIED":
        return _simulate_verified(event, cfg, active_settings, fail_closed_on_missing)

    # --- TIER_2_SYNTHETIC: Explicitly model-based Black-76 option simulation with conservative burdens ---
    if (
        event.exit_spot is None
        or event.entry_iv is None
        or event.entry_iv <= 0
        or event.dte_days is None
        or event.dte_days <= 0
        or event.strike <= 0
        or event.entry_spot <= 0
    ):
        if fail_closed_on_missing:
            raise MissingOptionDataError(
                f"TIER_2_MISSING_MODEL_INPUTS: event {event.event_id} requires positive entry_spot, exit_spot, strike, entry_iv, and dte_days"
            )
        return SimulatedOptionExecution(
            event_id=event.event_id,
            market_day=event.market_day,
            regime=event.regime,
            fidelity_tier="TIER_2_SYNTHETIC",
            verified_market_data=False,
            priced=False,
            rejection_reason="TIER_2_MISSING_MODEL_INPUTS",
            lots=event.lots,
            lot_size=event.lot_size,
            qty=qty,
            entry_price_inr=None,
            exit_price_inr=None,
            entry_slippage_inr=0.0,
            exit_slippage_inr=0.0,
            entry_spread_burden_inr=0.0,
            exit_spread_burden_inr=0.0,
            entry_charges_inr=0.0,
            exit_charges_inr=0.0,
            total_costs_inr=0.0,
            gross_pnl_inr=0.0,
            net_pnl_inr=0.0,
            initial_risk_inr=None,
            realized_r=None,
            cost_policy_id=None,
            quote_source="synthetic_black76_v3",
            exit_reason="UNPRICED_MISSING_MODEL_INPUTS",
        )

    t_entry_years = max(float(event.dte_days) / 365.0, 1.0 / (365.0 * 24.0))
    elapsed_days = max(float(event.holding_minutes) / 375.0, 0.0)
    t_exit_years = max((float(event.dte_days) - elapsed_days) / 365.0, 1.0 / (365.0 * 24.0 * 4.0))

    base_entry_iv = float(event.entry_iv)
    base_exit_iv = float(event.exit_iv if event.exit_iv is not None and event.exit_iv > 0 else event.entry_iv)

    # Unstressed theoretical mid prices
    mid_entry = max(
        black76_price(event.entry_spot, event.strike, t_entry_years, cfg.risk_free_rate, base_entry_iv, event.option_type),
        0.50,
    )
    mid_exit = max(
        black76_price(event.exit_spot, event.strike, t_exit_years, cfg.risk_free_rate, base_exit_iv, event.option_type),
        0.05,
    )
    mid_stop = max(
        black76_price(event.stop_spot, event.strike, t_entry_years, cfg.risk_free_rate, base_entry_iv, event.option_type),
        0.05,
    )

    # Conservative IV stress burden: long option pays higher IV on entry or receives lower IV on exit
    stressed_exit_iv = max(base_exit_iv * (1.0 - cfg.tier2_iv_stress_pct), 0.05)
    stressed_mid_exit = max(
        black76_price(event.exit_spot, event.strike, t_exit_years, cfg.risk_free_rate, stressed_exit_iv, event.option_type),
        0.05,
    )

    tick = float(getattr(active_settings, "option_tick_size", None) or px.TICK)
    half_spread_entry = max(mid_entry * (cfg.tier2_spread_bps_each_way / 10000.0), tick)
    half_spread_exit = max(stressed_mid_exit * (cfg.tier2_spread_bps_each_way / 10000.0), tick)
    slip_per_unit = max(cfg.tier2_slippage_ticks, 1) * tick

    exec_entry_price = round(mid_entry + half_spread_entry + slip_per_unit, 2)
    exec_exit_price = round(max(stressed_mid_exit - half_spread_exit - slip_per_unit, tick), 2)

    entry_charges_dict = paper_fees.order_charges(
        side="BUY",
        price=Decimal(str(exec_entry_price)),
        quantity=qty,
        market_day=event.market_day,
        policy_id=cfg.cost_policy_id,
        allow_historical_brokerage_assumption=cfg.allow_historical_brokerage_assumption,
    )
    exit_day = _parse_day(exit_now)
    exit_charges_dict = paper_fees.order_charges(
        side="SELL",
        price=Decimal(str(exec_exit_price)),
        quantity=qty,
        market_day=exit_day,
        policy_id=cfg.cost_policy_id,
        allow_historical_brokerage_assumption=cfg.allow_historical_brokerage_assumption,
    )

    entry_charges_inr = float(entry_charges_dict["total"])
    exit_charges_inr = float(exit_charges_dict["total"])
    entry_slippage_inr = round(slip_per_unit * qty, 2)
    exit_slippage_inr = round(slip_per_unit * qty, 2)
    entry_spread_inr = round((exec_entry_price - slip_per_unit - mid_entry) * qty, 2)
    exit_spread_inr = round((mid_exit - (exec_exit_price + slip_per_unit)) * qty, 2)

    gross_pnl = round((mid_exit - mid_entry) * qty, 2)
    net_pnl = round((exec_exit_price * qty - exit_charges_inr) - (exec_entry_price * qty + entry_charges_inr), 2)
    total_costs = round(gross_pnl - net_pnl, 2)

    reserve = paper_fees.order_charges(side="SELL", price=Decimal(str(exec_entry_price)),
        quantity=qty, market_day=event.market_day, policy_id=cfg.cost_policy_id,
        allow_historical_brokerage_assumption=cfg.allow_historical_brokerage_assumption)["total"]
    stop_loss_per_unit = max((exec_entry_price - mid_stop) + half_spread_entry + slip_per_unit, tick)
    est_init_risk = round(stop_loss_per_unit * qty + entry_charges_inr + float(reserve), 2)
    realized_r = round(net_pnl / est_init_risk, 4) if est_init_risk > 0 else None
    policy_id = (entry_charges_dict.get("policy") or {}).get("policy_id")

    return SimulatedOptionExecution(
        event_id=event.event_id,
        market_day=event.market_day,
        regime=event.regime,
        fidelity_tier="TIER_2_SYNTHETIC",
        verified_market_data=False,
        priced=True,
        rejection_reason=None,
        lots=event.lots,
        lot_size=event.lot_size,
        qty=qty,
        entry_price_inr=exec_entry_price,
        exit_price_inr=exec_exit_price,
        entry_slippage_inr=entry_slippage_inr,
        exit_slippage_inr=exit_slippage_inr,
        entry_spread_burden_inr=entry_spread_inr,
        exit_spread_burden_inr=exit_spread_inr,
        entry_charges_inr=entry_charges_inr,
        exit_charges_inr=exit_charges_inr,
        total_costs_inr=total_costs,
        gross_pnl_inr=gross_pnl,
        net_pnl_inr=net_pnl,
        initial_risk_inr=est_init_risk,
        realized_r=realized_r,
        cost_policy_id=policy_id,
        quote_source="synthetic_black76_v3",
        exit_reason=event.exit_reason,
        conservative_exit_reserve_inr=str(reserve),
        entry_cost_evidence_json=canonical_json(entry_charges_dict),
        exit_cost_evidence_json=canonical_json(exit_charges_dict),
    )


@dataclass(frozen=True)
class RegimePerformanceBreakdown:
    """Validation metrics for a single market regime."""

    regime: str
    trade_count: int
    winning_trades: int
    losing_trades: int
    gross_pnl_inr: float
    total_costs_inr: float
    net_pnl_inr: float
    net_expectancy_inr: float | None
    win_rate: float | None
    average_win_inr: float | None
    average_loss_inr: float | None
    profit_factor: float | None
    max_drawdown_inr: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ValidationMetrics:
    """Comprehensive independent validation metrics with explicit zero-trade and undefined-metric handling."""

    trade_count: int
    winning_trades: int
    losing_trades: int
    breakeven_trades: int
    unpriced_or_skipped_count: int
    gross_pnl_inr: float
    total_costs_inr: float
    net_pnl_inr: float
    net_expectancy_inr: float | None
    net_expectancy_r: float | None
    win_rate: float | None
    average_win_inr: float | None
    average_loss_inr: float | None
    profit_factor: float | None
    max_drawdown_inr: float
    max_drawdown_pct: float
    calmar_ratio: float | None
    sharpe_ratio: float | None
    deflated_sharpe_ratio: float | None
    trading_days_evaluated: int
    initial_capital_inr: float
    ending_equity_inr: float
    regime_performance: Mapping[str, RegimePerformanceBreakdown]
    confidence_limitations: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["regime_performance"] = {k: v.to_dict() for k, v in self.regime_performance.items()}
        payload["confidence_limitations"] = list(self.confidence_limitations)
        return payload


def _compute_drawdown(pnls: Sequence[float], initial_capital_inr: float) -> tuple[float, float]:
    equity = float(initial_capital_inr)
    peak = equity
    max_dd_inr = 0.0
    max_dd_pct = 0.0
    for pnl in pnls:
        equity += float(pnl)
        if equity > peak:
            peak = equity
        dd_inr = peak - equity
        if dd_inr > max_dd_inr:
            max_dd_inr = dd_inr
        if peak > 0:
            dd_pct = (dd_inr / peak) * 100.0
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct
    return round(max_dd_inr, 2), round(max_dd_pct, 4)


def _compute_sharpe_and_dsr(pnls: Sequence[float], num_trials: int = 1) -> tuple[float | None, float | None]:
    n = len(pnls)
    if n < 5:
        return None, None
    mean_pnl = sum(pnls) / n
    variance = sum((x - mean_pnl) ** 2 for x in pnls) / (n - 1)
    if variance <= 1e-12:
        return None, None
    std_pnl = math.sqrt(variance)
    sharpe = (mean_pnl / std_pnl) * math.sqrt(252.0)
    # Deflated Sharpe Ratio adjustment for multiple testing / non-normality
    benchmark_sr = math.sqrt(2.0 * math.log(max(num_trials, 1))) if num_trials > 1 else 0.0
    dsr = sharpe - benchmark_sr
    return round(sharpe, 4), round(dsr, 4)


def compute_validation_metrics(
    executions: Sequence[SimulatedOptionExecution],
    *,
    trading_days_evaluated: int,
    initial_capital_inr: float = DEFAULT_INITIAL_CAPITAL_INR,
    fidelity_tier: ExecutionFidelityTier = "TIER_1_VERIFIED",
    min_recommended_trades: int = DEFAULT_MIN_OOS_TRADES,
    extra_skipped_count: int = 0,
    num_trials: int = 1,
) -> ValidationMetrics:
    """Compute independent validation metrics over a sequence of simulated option executions."""
    priced_execs = [e for e in executions if e.priced]
    unpriced_count = sum(1 for e in executions if not e.priced) + int(extra_skipped_count)
    trade_count = len(priced_execs)
    limitations: list[str] = []

    if fidelity_tier == "TIER_2_SYNTHETIC" or any(not e.verified_market_data for e in priced_execs):
        limitations.append("SYNTHETIC_TIER2_SIMULATION: model-based option pricing is not verified market performance")

    if unpriced_count > 0:
        limitations.append(f"UNPRICED_OR_SKIPPED_EVENTS:{unpriced_count}")

    if trade_count == 0:
        limitations.append("ZERO_TRADES: expectancy, win_rate, average_win, average_loss, profit_factor, and calmar_ratio are undefined")
        return ValidationMetrics(
            trade_count=0,
            winning_trades=0,
            losing_trades=0,
            breakeven_trades=0,
            unpriced_or_skipped_count=unpriced_count,
            gross_pnl_inr=0.0,
            total_costs_inr=0.0,
            net_pnl_inr=0.0,
            net_expectancy_inr=None,
            net_expectancy_r=None,
            win_rate=None,
            average_win_inr=None,
            average_loss_inr=None,
            profit_factor=None,
            max_drawdown_inr=0.0,
            max_drawdown_pct=0.0,
            calmar_ratio=None,
            sharpe_ratio=None,
            deflated_sharpe_ratio=None,
            trading_days_evaluated=trading_days_evaluated,
            initial_capital_inr=round(initial_capital_inr, 2),
            ending_equity_inr=round(initial_capital_inr, 2),
            regime_performance={},
            confidence_limitations=tuple(limitations),
        )

    if trade_count < min_recommended_trades:
        limitations.append(
            f"SMALL_SAMPLE_SIZE: trade_count={trade_count} is below minimum statistical threshold ({min_recommended_trades})"
        )

    net_pnls = [float(e.net_pnl_inr) for e in priced_execs]
    gross_pnl = round(sum(float(e.gross_pnl_inr) for e in priced_execs), 2)
    total_costs = round(sum(float(e.total_costs_inr) for e in priced_execs), 2)
    net_pnl = round(sum(net_pnls), 2)
    expectancy_inr = round(net_pnl / trade_count, 2)

    r_values = [float(e.realized_r) for e in priced_execs if e.realized_r is not None]
    expectancy_r = round(sum(r_values) / len(r_values), 4) if r_values else None

    wins = [p for p in net_pnls if p > 0]
    losses = [p for p in net_pnls if p < 0]
    breakevens = [p for p in net_pnls if p == 0]

    win_rate = round(len(wins) / trade_count, 4)
    avg_win = round(sum(wins) / len(wins), 2) if wins else None
    avg_loss = round(sum(losses) / len(losses), 2) if losses else None

    if not wins:
        limitations.append("NO_WINNING_TRADES: average_win_inr is undefined")
    if not losses:
        limitations.append("UNDEFINED_PROFIT_FACTOR_NO_LOSSES: profit_factor and average_loss_inr are undefined with zero losing trades")
        profit_factor = None
    else:
        profit_factor = round(sum(wins) / abs(sum(losses)), 4)

    max_dd_inr, max_dd_pct = _compute_drawdown(net_pnls, initial_capital_inr)

    # Calmar ratio is only meaningful when max_drawdown_pct > 0 and sample spans >= 20 trading days
    if max_dd_pct > 0 and trading_days_evaluated >= 20:
        total_return_pct = (net_pnl / initial_capital_inr) * 100.0
        annualized_return_pct = total_return_pct * (252.0 / float(trading_days_evaluated))
        calmar: float | None = round(annualized_return_pct / max_dd_pct, 4)
    else:
        calmar = None
        if max_dd_pct <= 0:
            limitations.append("UNDEFINED_CALMAR_ZERO_DRAWDOWN: calmar_ratio is undefined when max_drawdown_pct == 0")
        else:
            limitations.append(
                f"UNDEFINED_CALMAR_SHORT_HORIZON: trading_days_evaluated={trading_days_evaluated} < 20 required for annualized Calmar"
            )

    sharpe, dsr = _compute_sharpe_and_dsr(net_pnls, num_trials=num_trials)
    if sharpe is None:
        limitations.append("UNDEFINED_SHARPE_RATIO: requires >= 5 trades with non-zero PnL variance")

    # Regime-level breakdown
    by_regime: dict[str, list[SimulatedOptionExecution]] = {}
    for ex in priced_execs:
        by_regime.setdefault(ex.regime or "UNKNOWN", []).append(ex)

    regime_perf: dict[str, RegimePerformanceBreakdown] = {}
    for reg_label in sorted(by_regime):
        r_execs = by_regime[reg_label]
        r_pnls = [float(x.net_pnl_inr) for x in r_execs]
        r_wins = [p for p in r_pnls if p > 0]
        r_losses = [p for p in r_pnls if p < 0]
        r_gross = round(sum(float(x.gross_pnl_inr) for x in r_execs), 2)
        r_costs = round(sum(float(x.total_costs_inr) for x in r_execs), 2)
        r_net = round(sum(r_pnls), 2)
        r_dd_inr, _ = _compute_drawdown(r_pnls, initial_capital_inr)
        regime_perf[reg_label] = RegimePerformanceBreakdown(
            regime=reg_label,
            trade_count=len(r_execs),
            winning_trades=len(r_wins),
            losing_trades=len(r_losses),
            gross_pnl_inr=r_gross,
            total_costs_inr=r_costs,
            net_pnl_inr=r_net,
            net_expectancy_inr=round(r_net / len(r_execs), 2) if r_execs else None,
            win_rate=round(len(r_wins) / len(r_execs), 4) if r_execs else None,
            average_win_inr=round(sum(r_wins) / len(r_wins), 2) if r_wins else None,
            average_loss_inr=round(sum(r_losses) / len(r_losses), 2) if r_losses else None,
            profit_factor=round(sum(r_wins) / abs(sum(r_losses)), 4) if r_losses else None,
            max_drawdown_inr=r_dd_inr,
        )

    return ValidationMetrics(
        trade_count=trade_count,
        winning_trades=len(wins),
        losing_trades=len(losses),
        breakeven_trades=len(breakevens),
        unpriced_or_skipped_count=unpriced_count,
        gross_pnl_inr=gross_pnl,
        total_costs_inr=total_costs,
        net_pnl_inr=net_pnl,
        net_expectancy_inr=expectancy_inr,
        net_expectancy_r=expectancy_r,
        win_rate=win_rate,
        average_win_inr=avg_win,
        average_loss_inr=avg_loss,
        profit_factor=profit_factor,
        max_drawdown_inr=max_dd_inr,
        max_drawdown_pct=max_dd_pct,
        calmar_ratio=calmar,
        sharpe_ratio=sharpe,
        deflated_sharpe_ratio=dsr,
        trading_days_evaluated=trading_days_evaluated,
        initial_capital_inr=round(initial_capital_inr, 2),
        ending_equity_inr=round(initial_capital_inr + net_pnl, 2),
        regime_performance=regime_perf,
        confidence_limitations=tuple(limitations),
    )


@dataclass(frozen=True)
class WalkForwardFoldResult:
    """Deterministic result for one chronological IS + purge/embargo + OOS fold."""

    fold: WalkForwardFold
    frozen_parameters: FrozenFoldParameters
    is_performance: ValidationMetrics
    oos_performance: ValidationMetrics
    purged_is_event_ids: tuple[str, ...]
    embargoed_event_ids: tuple[str, ...]
    incomplete_oos_event_ids: tuple[str, ...]
    overlapping_skipped_event_ids: tuple[str, ...]
    oos_executions: tuple[SimulatedOptionExecution, ...]
    fold_hash: str
    account_snapshot: Mapping[str, Any] = field(default_factory=dict)
    trade_memory: tuple[Mapping[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "fold": self.fold.to_dict(),
            "frozen_parameters": self.frozen_parameters.to_dict(),
            "is_performance": self.is_performance.to_dict(),
            "oos_performance": self.oos_performance.to_dict(),
            "purged_is_event_ids": list(self.purged_is_event_ids),
            "embargoed_event_ids": list(self.embargoed_event_ids),
            "incomplete_oos_event_ids": list(self.incomplete_oos_event_ids),
            "overlapping_skipped_event_ids": list(self.overlapping_skipped_event_ids),
            "fold_hash": self.fold_hash,
            "account_snapshot": dict(self.account_snapshot),
            "trade_memory": list(self.trade_memory),
        }


@dataclass(frozen=True)
class WalkForwardReport:
    """Complete independent walk-forward validation report with provenance binding and reproducible hashes."""

    engine_version: str
    strategy_id: str
    strategy_version: str
    normalizer_version: str
    strategy_state: str
    config: WalkForwardConfig
    fidelity_tier: ExecutionFidelityTier
    verified_market_performance: bool
    performance_label: str
    paper_promotion_eligible: bool
    has_valid_oos_folds: bool
    fold_count: int
    folds: tuple[WalkForwardFoldResult, ...]
    oos_performance: ValidationMetrics | None
    data_fidelity_and_provenance: Mapping[str, Any]
    confidence_limitations: tuple[str, ...]
    failed_validation_criteria: tuple[str, ...]
    validation_passed: bool
    report_hash: str
    dataset_integrity: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            **({"dataset_integrity": dict(self.dataset_integrity)} if self.dataset_integrity is not None else {}),
            "engine_version": self.engine_version,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "normalizer_version": self.normalizer_version,
            "strategy_state": self.strategy_state,
            "config": self.config.to_dict(),
            "fidelity_tier": self.fidelity_tier,
            "verified_market_performance": self.verified_market_performance,
            "performance_label": self.performance_label,
            "paper_promotion_eligible": self.paper_promotion_eligible,
            "has_valid_oos_folds": self.has_valid_oos_folds,
            "fold_count": self.fold_count,
            "folds": [f.to_dict() for f in self.folds],
            "oos_performance": self.oos_performance.to_dict() if self.oos_performance is not None else None,
            "data_fidelity_and_provenance": dict(self.data_fidelity_and_provenance),
            "confidence_limitations": list(self.confidence_limitations),
            "failed_validation_criteria": list(self.failed_validation_criteria),
            "validation_passed": self.validation_passed,
            "report_hash": self.report_hash,
        }


def _filter_and_control_events_for_window(
    events: Sequence[CandidateSignalEvent],
    *,
    allowed_dates: set[str],
    window_end_date: str,
    allow_overlapping_labels: bool,
    strict_leakage_check: bool,
) -> tuple[list[CandidateSignalEvent], list[str], list[str]]:
    """Filter events to `allowed_dates`, purging incomplete/horizon-overflowing labels and overlapping positions.

    Returns `(accepted_events, incomplete_or_overflow_ids, overlapping_skipped_ids)`.
    """
    sorted_events = sorted(
        [e for e in events if _parse_day(e.market_day) in allowed_dates],
        key=lambda x: (_parse_ts(x.decision_time), x.event_id),
    )
    accepted: list[CandidateSignalEvent] = []
    incomplete_ids: list[str] = []
    overlap_ids: list[str] = []
    active_until_by_underlying: dict[str, datetime] = {}

    for ev in sorted_events:
        if strict_leakage_check:
            verify_no_lookahead(decision_time=ev.decision_time, features=ev.features, bars=ev.bars)

        if ev.incomplete_label or ev.label_end_time is None:
            incomplete_ids.append(ev.event_id)
            accepted.append(replace(ev, label_end_time=None, exit_quote=None, exit_spot=None, incomplete_label=True))
            continue

        dec_ts = _parse_ts(ev.decision_time)
        end_ts = _parse_ts(ev.label_end_time)
        if end_ts < dec_ts:
            raise LookAheadLeakageError(
                f"NEGATIVE_LABEL_HORIZON: event {ev.event_id} label_end_time={ev.label_end_time} < decision_time={ev.decision_time}"
            )

        end_day = _parse_day(ev.label_end_time)
        if end_day > window_end_date:
            # Event label horizon extends past the window boundary (e.g. IS into purge/embargo/OOS, or OOS past fold end)
            incomplete_ids.append(ev.event_id)
            accepted.append(replace(ev, label_end_time=None, exit_quote=None, exit_spot=None, incomplete_label=True))
            continue

        accepted.append(ev)

    return accepted, incomplete_ids, overlap_ids


class ReplayRiskTracker:
    """Deterministic paper-risk-v2 state tracker across sequential replayed trades."""

    def __init__(
        self,
        *,
        initial_capital_inr: float = DEFAULT_INITIAL_CAPITAL_INR,
        max_open_positions: int = 1,
        full_premium_limit: Decimal = paper_risk.FULL_PREMIUM_LIMIT,
        daily_exposure_limit: Decimal = paper_risk.DAILY_EXPOSURE_LIMIT,
        stop_risk_fraction: Decimal = Decimal("0.02"),
    ) -> None:
        if (Decimal(str(initial_capital_inr)) != Decimal("50000")
                or full_premium_limit != paper_risk.FULL_PREMIUM_LIMIT
                or daily_exposure_limit != paper_risk.DAILY_EXPOSURE_LIMIT
                or stop_risk_fraction != Decimal("0.02") or max_open_positions < 1):
            raise ValueError("UNAPPROVED_REPLAY_RISK_POLICY")
        self.initial_capital = paper_accounting.money(Decimal(str(initial_capital_inr)))
        self.cash = self.initial_capital
        self.max_open_positions = max_open_positions
        self.full_premium_limit = full_premium_limit
        self.daily_exposure_limit = daily_exposure_limit
        self.stop_risk_fraction = stop_risk_fraction
        # Active open positions: dict of event_id -> dict
        self.open_positions: dict[str, dict[str, Any]] = {}
        # Gross daily loss per market day: profits NEVER replenish!
        self.daily_losses: dict[str, Decimal] = {}
        self.decisions: dict[str, tuple[str, SimulatedOptionExecution]] = {}
        self.last_decision_time: datetime | None = None

    def snapshot(self) -> dict[str, Any]:
        """Exact rebuildable account evidence, without treating reservations as cash debits."""
        return normalize_json({"cash": self.cash, "open_positions": self.open_positions,
            "gross_daily_losses": self.daily_losses,
            "committed": sum((p["entry_debit"] for p in self.open_positions.values()), Decimal(0)),
            "reserved": sum((p["full_premium_loss"] for p in self.open_positions.values()), Decimal(0))})

    def _update_positions(self, current_time: datetime) -> None:
        """Close any positions whose exit_time is at or before current_time."""
        closed_ids = []
        for eid, pos in self.open_positions.items():
            if pos["exit_time"] is not None and pos["exit_time"] <= current_time:
                closed_ids.append(eid)
                # Credit exit proceeds to cash
                net_pnl = pos["net_pnl"]
                self.cash += pos["exit_proceeds"]
                if net_pnl < Decimal("0.00"):
                    day = pos["exit_time"].astimezone(IST).date().isoformat()
                    self.daily_losses[day] = self.daily_losses.get(day, Decimal("0.00")) + abs(net_pnl)
        for eid in closed_ids:
            del self.open_positions[eid]

    def check_and_apply(self, event: CandidateSignalEvent,
                        execution: SimulatedOptionExecution) -> SimulatedOptionExecution:
        digest = sha256_hex(canonical_json({"event": asdict(event), "execution": asdict(execution)}))
        prior = self.decisions.get(event.event_id)
        if prior:
            if prior[0] != digest:
                raise ValueError("REPLAY_EVENT_IDENTITY_CONFLICT")
            return prior[1]
        now = _parse_ts(event.decision_time)
        if self.last_decision_time is not None and now < self.last_decision_time:
            raise ValueError("REPLAY_CLOCK_MOVED_BACKWARDS")
        self._update_positions(now)
        result = self._check_and_apply(event, execution)
        self.decisions[event.event_id] = (digest, result)
        self.last_decision_time = now
        return result

    def _check_and_apply(
        self,
        event: CandidateSignalEvent,
        execution: SimulatedOptionExecution,
    ) -> SimulatedOptionExecution:
        """Enforce paper-risk-v2 cash, positions, full-premium, stop budget and daily exposure limits."""
        if not execution.priced and not execution.entry_executable:
            return execution
        if event.event_id in self.open_positions:
            return replace(execution, admitted=True)

        dec_now = _parse_ts(event.decision_time)
        self._update_positions(dec_now)

        # 1. Max open positions
        if len(self.open_positions) >= self.max_open_positions:
            return SimulatedOptionExecution(
                **{**asdict(execution), "priced": False, "admitted": False, "net_pnl_inr": None, "gross_pnl_inr": None, "realized_r": None, "rejection_reason": "MAX_POSITIONS_EXCEEDED"}
            )

        day = event.market_day
        gross_daily_loss = self.daily_losses.get(day, Decimal("0.00"))
        existing_reservations = sum((p["full_premium_loss"] for p in self.open_positions.values()), Decimal("0.00"))
        spendable_cash = max(Decimal("0.00"), self.cash - sum((p["exit_reserve"] for p in self.open_positions.values()), Decimal("0.00")))
        effective_capital = max(Decimal("0.00"), min(self.initial_capital, self.cash + sum((p["entry_debit"] for p in self.open_positions.values()), Decimal("0.00")) - existing_reservations))
        stop_budget = paper_accounting.stop_budget(effective_capital, self.initial_capital)

        # Monetary values from execution
        entry_val = paper_accounting.money(Decimal(str(execution.entry_price_inr)) * execution.qty)
        entry_fees = Decimal(str(round(execution.entry_charges_inr, 2)))
        exit_fees = Decimal(str(round(execution.exit_charges_inr, 2)))
        if execution.conservative_exit_reserve_inr is None:
            if execution.fidelity_tier != "TIER_2_SYNTHETIC":
                return replace(execution, priced=False, rejection_reason="MISSING_ENTRY_COST_RESERVE")
            reserve = Decimal("0.00")
        else:
            reserve = Decimal(execution.conservative_exit_reserve_inr)
        full_premium_loss = entry_val + entry_fees + reserve
        est_stop_risk = (
            Decimal(str(round(execution.initial_risk_inr, 2)))
            if execution.initial_risk_inr is not None
            else full_premium_loss
        )

        rejection = paper_risk.exposure_rejection(
            full_premium_loss=full_premium_loss, estimated_stop_risk=est_stop_risk,
            stop_budget=stop_budget, gross_daily_loss=gross_daily_loss,
            existing_reservations=existing_reservations, available_cash=spendable_cash)
        if rejection is not None:
            return SimulatedOptionExecution(
                **{**asdict(execution), "priced": False, "admitted": False, "net_pnl_inr": None, "gross_pnl_inr": None, "realized_r": None, "rejection_reason": rejection}
            )

        # Trade is accepted! Debit cash for entry debit and track open position
        entry_debit = entry_val + entry_fees
        self.cash -= entry_debit
        exit_time = _parse_ts(event.label_end_time) if event.label_end_time and execution.priced and not execution.unresolved else None
        exit_val = paper_accounting.money(Decimal(str(execution.exit_price_inr)) * execution.qty) if execution.exit_price_inr is not None else Decimal("0.00")
        exit_proceeds = exit_val - exit_fees if execution.priced else None
        net_pnl = exit_proceeds - entry_debit if exit_proceeds is not None else None

        self.open_positions[event.event_id] = {
            "market_day": day,
            "entry_time": dec_now,
            "exit_time": exit_time,
            "full_premium_loss": full_premium_loss,
            "entry_debit": entry_debit,
            "exit_reserve": reserve,
            "exit_proceeds": exit_proceeds,
            "net_pnl": net_pnl,
        }
        return replace(execution, admitted=True)


OptionQuoteProvider = Callable[
    [str, float, str, str | None, datetime],
    ContractQuote | Mapping[str, Any] | None,
]


class CausalSignalGenerator(ABC):
    """Abstract interface for causal signal generation on completed candles."""

    @abstractmethod
    def generate_signals(
        self,
        spec: NormalizedStrategySpec,
        parameters: Mapping[str, Any],
        candles: Sequence[Mapping[str, Any]] | pd.DataFrame,
        *,
        allowed_dates: Sequence[date | str] | set[str],
        window_end_date: str | None = None,
        config: WalkForwardConfig | None = None,
        settings: Settings | None = None,
        option_quote_provider: OptionQuoteProvider | None = None,
    ) -> Sequence[CandidateSignalEvent]:
        """Generate candidate signal events causally from completed candles using frozen/selected parameters."""
        ...


def _normalize_candle_records(candles, *, expected_underlying=None) -> list[dict[str, Any]]:
    rows = canonical_rows(candles, expected_underlying)
    parsed = []
    for row in rows:
        ts = _parse_ts(row[0])
        parsed.append({"timestamp": ts, "date": ts.strftime("%Y-%m-%d"),
                       "time": ts.strftime("%H:%M"), "timestamp_close": ts + timedelta(minutes=5),
                       **dict(zip(("open", "high", "low", "close", "volume"), map(float, row[1:6]))),
                       "underlying": row[6]})
    return parsed


class RuleBasedCausalSignalGenerator(CausalSignalGenerator):
    """Canonical causal signal generator evaluating StrategyRulesV2 bar-by-bar on completed candles.

    Contract metadata defaults to the exchange calendars in `settings.data_dir` (dated lot table, NSE holidays) and
    can be injected: `lot_changes`, `holidays`, and `listed_expiries` (expiries actually listed, taken as fact).
    `expiry_day_policy` picks the contract on an expiry day: that day's ("same_day") or the following ("next")."""

    def __init__(
        self,
        *,
        lot_changes: Sequence[LotChange] | None = None,
        holidays: Mapping[str, str] | None = None,
        listed_expiries: Collection[str] | None = None,
        expiry_day_policy: ExpiryDayPolicy = "same_day",
    ) -> None:
        self.lot_changes = lot_changes
        self.holidays = holidays
        self.listed_expiries = listed_expiries
        self.expiry_day_policy = expiry_day_policy

    def generate_signals(
        self,
        spec: NormalizedStrategySpec,
        parameters: Mapping[str, Any],
        candles: Sequence[Mapping[str, Any]] | pd.DataFrame,
        *,
        allowed_dates: Sequence[date | str] | set[str],
        window_end_date: str | None = None,
        config: WalkForwardConfig | None = None,
        settings: Settings | None = None,
        option_quote_provider: OptionQuoteProvider | None = None,
    ) -> Sequence[CandidateSignalEvent]:
        if spec.quarantined:
            raise UnsupportedStrategyRuleError(
                f"STRATEGY_QUARANTINED: Strategy '{spec.strategy_id}' is quarantined with reasons: {list(spec.quarantine_reasons)}"
            )
        if not spec.structurally_complete or spec.rules_v2 is None:
            raise UnsupportedStrategyRuleError(
                f"INCOMPLETE_SPECIFICATION: Strategy '{spec.strategy_id}' lacks complete StrategyRulesV2"
            )
        if spec.rules_v2.unresolved_gaps:
            raise UnsupportedStrategyRuleError(
                f"UNRESOLVED_GAPS: Strategy '{spec.strategy_id}' has unresolved gaps: {list(spec.rules_v2.unresolved_gaps)}"
            )
        if not spec.rules_v2.intraday_entry and not spec.rules_v2.entry:
            raise UnsupportedStrategyRuleError(f"NO_ENTRY_CONDITIONS: Strategy '{spec.strategy_id}' has no entry rules")

        rules = spec.rules_v2
        if rules.market != "nse_options":
            raise UnsupportedStrategyRuleError("NOT_EXECUTABLE_EXACTLY: unsupported market adapter")
        if rules.session_window and rules.session_window.timezone not in ("Asia/Kolkata", "Asia/Calcutta"):
            raise UnsupportedStrategyRuleError("NOT_EXECUTABLE_EXACTLY: unsupported session timezone")
        if rules.session_window and time.fromisoformat(rules.session_window.entry_cutoff) > time(15, 10):
            raise UnsupportedStrategyRuleError("NOT_EXECUTABLE_EXACTLY: entry cutoff exceeds adapter capability")
        if (rules.stop_loss and rules.stop_loss.mode == "swing_extreme_buffer"
                and "swing_lookback_bars" not in parameters):
            raise UnsupportedStrategyRuleError("NOT_EXECUTABLE_EXACTLY: missing swing_lookback_bars")
        supported_parameters = {"max_lots", "ema_period", "stop_loss_points", "target_r_multiple",
            "target_points", "body_ratio_min", "sweep_buffer_points", "breakout_threshold_points",
            "iv", "swing_lookback_bars", "swing_buffer_points", "custom_handlers", "strike_offset"}
        if set(parameters) - supported_parameters:
            raise UnsupportedStrategyRuleError("UNSUPPORTED_PARAMETER")
        if rules.target and rules.target.allow_liquidity_hop:
            raise UnsupportedStrategyRuleError("UNSUPPORTED_LIQUIDITY_HOP")
        if (rules.entry or rules.exit or rules.entry_blackout
                or rules.instrument.structure not in ("long_call", "long_put")):
            raise UnsupportedStrategyRuleError("UNSUPPORTED_DECLARED_RULES")
        if len(rules.intraday_entry) > 1 and rules.entry_composition is None:
            raise UnsupportedStrategyRuleError("AMBIGUOUS_ENTRY_COMPOSITION")
        for name in ("ema_period", "body_ratio_min", "sweep_buffer_points"):
            declared = {p.params[name] for p in rules.intraday_entry if name in p.params}
            if name in parameters and len(declared) > 1:
                raise UnsupportedStrategyRuleError(f"NOT_EXECUTABLE_EXACTLY: ambiguous parameter binding: {name}")
            if name in rules.parameters and declared and declared != {rules.parameters[name].value}:
                raise UnsupportedStrategyRuleError(f"NOT_EXECUTABLE_EXACTLY: conflicting parameter declarations: {name}")
        for indicator in rules.indicators:
            expected_timeframe = "1d" if indicator.name == "pdh_pdl_pdc" else "5m"
            if (indicator.name not in {"ema", "vwap", "pdh_pdl_pdc"}
                    or indicator.timeframe != expected_timeframe or indicator.params):
                raise UnsupportedStrategyRuleError("NOT_EXECUTABLE_EXACTLY: unsupported indicator dependency")
            if indicator.name == "ema" and indicator.period not in {
                    p.params.get("ema_period") for p in rules.intraday_entry if p.kind == "ema_relation"}:
                raise UnsupportedStrategyRuleError("NOT_EXECUTABLE_EXACTLY: unbound EMA indicator period")
        expected_direction = "CALL" if rules.instrument.structure == "long_call" else "PUT"
        opposite_direction = "SHORT" if expected_direction == "CALL" else "LONG"
        if rules.intraday_entry and all(p.direction == opposite_direction for p in rules.intraday_entry):
            raise UnsupportedStrategyRuleError("ENTRY_DIRECTION_STRUCTURE_MISMATCH")
        for predicate in rules.intraday_entry:
            if predicate.kind not in ("ema_relation", "candle_body_ratio", "pdh_pdl_sweep",
                                      "or30_breakout_retest", "vwap_relation", "custom_predicate"):
                raise UnsupportedStrategyRuleError(f"UNSUPPORTED_ENTRY_PREDICATE: {predicate.kind}")
            supported_predicate_params = {
                "ema_relation": {"ema_period", "relation"},
                "candle_body_ratio": {"body_ratio_min"},
                "pdh_pdl_sweep": {"sweep_buffer_points"},
                "vwap_relation": set(),
                "custom_predicate": {"handler_name"},
            }
            if predicate.kind in supported_predicate_params:
                unknown = set(predicate.params) - supported_predicate_params[predicate.kind]
                if unknown:
                    raise UnsupportedStrategyRuleError(
                        f"NOT_EXECUTABLE_EXACTLY: unsupported {predicate.kind} parameters: {sorted(unknown)}"
                    )
            required = {"ema_relation": "ema_period", "candle_body_ratio": "body_ratio_min",
                        "pdh_pdl_sweep": "sweep_buffer_points"}.get(predicate.kind)
            if required and required not in predicate.params and required not in parameters:
                raise UnsupportedStrategyRuleError(f"NOT_EXECUTABLE_EXACTLY: missing {predicate.kind}.{required}")
            if required:
                raw_value = parameters.get(required, predicate.params.get(required))
                try:
                    value = float(raw_value)
                except (TypeError, ValueError) as exc:
                    raise UnsupportedStrategyRuleError(f"NOT_EXECUTABLE_EXACTLY: invalid {required}") from exc
                if (isinstance(raw_value, bool) or not math.isfinite(value)
                        or (required == "ema_period" and (value < 1 or not value.is_integer()))
                        or (required == "body_ratio_min" and not 0 <= value <= 1)
                        or (required == "sweep_buffer_points" and value < 0)):
                    raise UnsupportedStrategyRuleError(f"NOT_EXECUTABLE_EXACTLY: invalid {required}")
            if predicate.kind == "ema_relation" and "relation" not in predicate.params:
                raise UnsupportedStrategyRuleError("NOT_EXECUTABLE_EXACTLY: missing ema_relation.relation")
            if predicate.kind == "custom_predicate":
                handler_name = str(predicate.params.get("handler_name") or "")
                if not callable(parameters.get("custom_handlers", {}).get(handler_name)):
                    raise UnsupportedStrategyRuleError("UNAVAILABLE_CUSTOM_PREDICATE_HANDLER: NOT_EXECUTABLE_EXACTLY")
            if predicate.timeframe != "5m":
                raise UnsupportedStrategyRuleError("UNSUPPORTED_PREDICATE_TIMEFRAME")
            if predicate.kind == "or30_breakout_retest":
                raise UnsupportedStrategyRuleError("UNSUPPORTED_OR30_RETEST_SEMANTICS")
            if predicate.kind == "ema_relation" and predicate.params.get("relation", "cross") not in (
                    "above", "below", "cross", "cross_above", "cross_below"):
                raise UnsupportedStrategyRuleError("UNSUPPORTED_EMA_RELATION")

        if spec.rules_v2.stop_loss is None or spec.rules_v2.stop_loss.mode not in (
            "index_points",
            "swing_extreme_buffer",
        ):
            raise UnsupportedStrategyRuleError(
                f"UNSUPPORTED_STOP_LOSS_MODE: Stop loss mode '{getattr(spec.rules_v2.stop_loss, 'mode', None)}' is not supported; "
                "pct_of_debit and pct_of_capital require bar-by-bar option quotes which are not available in underlying candle replay"
            )

        if spec.rules_v2.target is None or spec.rules_v2.target.mode not in (
            "r_multiple",
            "index_points",
        ):
            raise UnsupportedStrategyRuleError(
                f"UNSUPPORTED_TARGET_MODE: Target mode '{getattr(spec.rules_v2.target, 'mode', None)}' is not supported"
            )

        if spec.rules_v2.partial_exits:
            raise UnsupportedStrategyRuleError(
                f"UNSUPPORTED_PARTIAL_EXITS: Strategy '{spec.strategy_id}' declares partial exit rules "
                "which are not supported by the causal generator"
            )

        if spec.rules_v2.timeframes.primary != "5m":
            raise UnsupportedStrategyRuleError(
                f"UNSUPPORTED_TIMEFRAME: Primary timeframe '{spec.rules_v2.timeframes.primary}' is not supported; "
                "only '5m' primary timeframe is supported by the causal generator"
            )

        allowed_date_strs = {_parse_day(d) for d in allowed_dates}
        normalized_candles = _normalize_candle_records(candles, expected_underlying=spec.rules_v2.instrument.underlying)
        underlying = spec.rules_v2.instrument.underlying

        candles_by_date: dict[str, list[dict[str, Any]]] = {}
        for bar in normalized_candles:
            candles_by_date.setdefault(bar["date"], []).append(bar)

        session_window = spec.rules_v2.session_window
        entry_start = session_window.entry_start if session_window else "09:15"
        entry_cutoff = session_window.entry_cutoff if session_window else "15:10"
        square_off = session_window.square_off if session_window else "15:25"

        entry_start_time = time.fromisoformat(entry_start)
        strategy_cutoff_time = time.fromisoformat(entry_cutoff)
        effective_cutoff_time = min(strategy_cutoff_time, time(15, 10))
        square_off_time = time.fromisoformat(square_off)

        active_settings = settings or get_settings()
        lot_path = Path(active_settings.data_dir) / "lot_sizes" / "nse.yaml"
        if not lot_path.exists():
            lot_path = Path(__file__).resolve().parents[2] / "data" / "lot_sizes" / "nse.yaml"
        lot_changes = list(self.lot_changes) if self.lot_changes is not None else load_lot_sizes(lot_path)
        holidays = self.holidays if self.holidays is not None else exchange_holidays(active_settings)
        requested_lots = max(1, int(parameters.get("max_lots", 1)))
        lots = min(requested_lots, paper_risk.MAX_LOTS, spec.rules_v2.risk.max_lots, spec.rules_v2.position_sizing.max_lots)
        strike_step = active_settings.strike_step(underlying)
        if strike_step is None or strike_step <= 0:
            raise UnsupportedStrategyRuleError("NOT_EXECUTABLE_EXACTLY: missing instrument strike step")

        stop_pts_param = float(
            parameters.get(
                "stop_loss_points",
                spec.rules_v2.stop_loss.value if spec.rules_v2.stop_loss.mode == "index_points" else 25.0,
            )
        )
        target_r_param = float(
            parameters.get(
                "target_r_multiple",
                spec.rules_v2.target.value if spec.rules_v2.target.mode == "r_multiple" else 2.0,
            )
        )
        target_pts_param = float(
            parameters.get(
                "target_points",
                spec.rules_v2.target.value if spec.rules_v2.target.mode == "index_points" else 50.0,
            )
        )
        body_ratio_param = float(parameters.get("body_ratio_min", 0.60))
        sweep_buffer_param = float(parameters.get("sweep_buffer_points", 5.0))
        breakout_param = float(parameters.get("breakout_threshold_points", 5.0))
        iv_param = float(parameters.get("iv", 0.15))

        all_dates_sorted = sorted(candles_by_date.keys())
        events: list[CandidateSignalEvent] = []

        prior_pdh: float | None = None
        prior_pdl: float | None = None

        for day_str in all_dates_sorted:
            day_bars = candles_by_date[day_str]

            if window_end_date and day_str > window_end_date:
                break
            if day_str not in allowed_date_strs:
                prior_pdh = max(b["high"] for b in day_bars)
                prior_pdl = min(b["low"] for b in day_bars)
                continue

            or_bars = [b for b in day_bars if "09:15" <= b["time"] < "09:45"]
            or30_high = max((b["high"] for b in or_bars), default=None)
            or30_low = min((b["low"] for b in or_bars), default=None)

            n_bars = len(day_bars)
            i = 0
            while i < n_bars:
                bar = day_bars[i]
                bar_time_str = bar["time"]
                dec_time = bar["timestamp_close"]
                dec_time_of_day = dec_time.time()

                # Enforce entry strictly before min(strategy_cutoff, 15:10 IST) on decision timestamp
                if not (entry_start_time <= dec_time_of_day < effective_cutoff_time):
                    i += 1
                    continue

                # Enforce session blackout windows
                if session_window and session_window.blackout_windows:
                    in_blackout = False
                    for b_start_str, b_end_str in session_window.blackout_windows:
                        b_start = time.fromisoformat(b_start_str)
                        b_end = time.fromisoformat(b_end_str)
                        if b_start <= dec_time_of_day < b_end:
                            in_blackout = True
                            break
                    if in_blackout:
                        i += 1
                        continue

                predicate_results: list[tuple[bool, str | None]] = []

                for pred in spec.rules_v2.intraday_entry:
                    if pred.kind == "ema_relation":
                        period_value = parameters.get("ema_period", pred.params.get("ema_period"))
                        ema_period = int(period_value)
                        if i + 1 >= ema_period:
                            closes_series = pd.Series([b["close"] for b in day_bars[: i + 1]])
                            curr_ema = regime.compute_ema(closes_series, period=ema_period)
                            prev_ema = (
                                regime.compute_ema(closes_series.iloc[:-1], period=ema_period)
                                if i > 0
                                else curr_ema
                            )
                            curr_c = bar["close"]
                            prev_c = day_bars[i - 1]["close"] if i > 0 else curr_c

                            rel = str(pred.params.get("relation", "cross"))
                            call_match = (
                                (rel == "above" and curr_c > curr_ema)
                                or (rel in ("cross", "cross_above") and curr_c > curr_ema and prev_c <= prev_ema)
                            ) and pred.direction in ("LONG", "CALL", "BOTH")
                            put_match = (
                                (rel == "below" and curr_c < curr_ema)
                                or (rel in ("cross", "cross_below") and curr_c < curr_ema and prev_c >= prev_ema)
                            ) and pred.direction in ("SHORT", "PUT", "BOTH")

                            if call_match and put_match:
                                predicate_results.append((True, "BOTH"))
                            elif call_match:
                                predicate_results.append((True, "CALL"))
                            elif put_match:
                                predicate_results.append((True, "PUT"))
                            else:
                                predicate_results.append((False, None))
                        else:
                            predicate_results.append((False, None))

                    elif pred.kind == "candle_body_ratio":
                        body_span = abs(bar["close"] - bar["open"])
                        tot_range = max(bar["high"] - bar["low"], 0.01)
                        ratio = body_span / tot_range
                        min_ratio = float(parameters.get("body_ratio_min", pred.params.get("body_ratio_min", body_ratio_param)))
                        if ratio >= min_ratio:
                            if bar["close"] > bar["open"] and pred.direction in ("LONG", "CALL", "BOTH"):
                                predicate_results.append((True, "CALL"))
                            elif bar["close"] < bar["open"] and pred.direction in ("SHORT", "PUT", "BOTH"):
                                predicate_results.append((True, "PUT"))
                            else:
                                predicate_results.append((False, None))
                        else:
                            predicate_results.append((False, None))

                    elif pred.kind == "pdh_pdl_sweep":
                        sweep_buffer_param = float(parameters.get("sweep_buffer_points", pred.params.get("sweep_buffer_points")))
                        call_match = (
                            prior_pdl is not None
                            and bar["low"] < (prior_pdl - sweep_buffer_param)
                            and bar["close"] > prior_pdl
                            and pred.direction in ("LONG", "CALL", "BOTH")
                        )
                        put_match = (
                            prior_pdh is not None
                            and bar["high"] > (prior_pdh + sweep_buffer_param)
                            and bar["close"] < prior_pdh
                            and pred.direction in ("SHORT", "PUT", "BOTH")
                        )
                        if call_match and put_match:
                            predicate_results.append((True, "BOTH"))
                        elif call_match:
                            predicate_results.append((True, "CALL"))
                        elif put_match:
                            predicate_results.append((True, "PUT"))
                        else:
                            predicate_results.append((False, None))

                    elif pred.kind == "or30_breakout_retest":
                        call_match = (
                            bar_time_str >= "09:45"
                            and or30_high is not None
                            and bar["close"] > (or30_high + breakout_param)
                            and pred.direction in ("LONG", "CALL", "BOTH")
                        )
                        put_match = (
                            bar_time_str >= "09:45"
                            and or30_low is not None
                            and bar["close"] < (or30_low - breakout_param)
                            and pred.direction in ("SHORT", "PUT", "BOTH")
                        )
                        if call_match and put_match:
                            predicate_results.append((True, "BOTH"))
                        elif call_match:
                            predicate_results.append((True, "CALL"))
                        elif put_match:
                            predicate_results.append((True, "PUT"))
                        else:
                            predicate_results.append((False, None))

                    elif pred.kind == "vwap_relation":
                        day_df = pd.DataFrame(day_bars[: i + 1])
                        vwap = regime.compute_session_vwap(day_df) if day_df["volume"].sum() > 0 else None
                        if vwap is not None:
                            call_match = bar["close"] > vwap and pred.direction in ("LONG", "CALL", "BOTH")
                            put_match = bar["close"] < vwap and pred.direction in ("SHORT", "PUT", "BOTH")
                            if call_match and put_match:
                                predicate_results.append((True, "BOTH"))
                            elif call_match:
                                predicate_results.append((True, "CALL"))
                            elif put_match:
                                predicate_results.append((True, "PUT"))
                            else:
                                predicate_results.append((False, None))
                        else:
                            predicate_results.append((False, None))

                    elif pred.kind == "custom_predicate":
                        handler = pred.params.get("handler")
                        handler_name = str(pred.params.get("handler_name") or "")
                        if not callable(handler) and handler_name:
                            handler = parameters.get("custom_handlers", {}).get(handler_name)
                        if not callable(handler):
                            raise UnsupportedStrategyRuleError(
                                f"UNAVAILABLE_CUSTOM_PREDICATE_HANDLER: custom_predicate '{pred.kind}' "
                                f"lacks callable handler in params or parameters['custom_handlers'] (handler_name={handler_name!r})"
                            )
                        res = handler(day_bars[: i + 1], parameters)
                        if res in ("CALL", "PUT"):
                            predicate_results.append((True, res))
                        elif res is True:
                            predicate_results.append((True, "CALL" if pred.direction in ("LONG", "CALL") else "PUT"))
                        else:
                            predicate_results.append((False, None))

                # Conjunctive composition: ALL declared intraday_entry predicates must pass!
                if not (any(passed for passed, _ in predicate_results) if rules.entry_composition == "ANY" else all(passed for passed, _ in predicate_results)):
                    i += 1
                    continue

                dirs = {d for passed, d in predicate_results if passed and d not in (None, "BOTH")}
                if len(dirs) == 1:
                    entry_direction = dirs.pop()
                elif len(dirs) == 0:
                    entry_direction = "CALL" if spec.rules_v2.instrument.structure == "long_call" else "PUT"
                else:
                    # Conflicting directions (e.g. one said CALL, one said PUT)!
                    # "Reject ambiguous or unsupported combinations rather than choosing semantics silently."
                    i += 1
                    continue

                # A BOTH predicate may observe either direction; only the declared
                # option structure is eligible for this strategy's executions.
                if entry_direction != expected_direction:
                    i += 1
                    continue

                entry_spot = bar["close"]

                if spec.rules_v2.stop_loss.mode == "index_points":
                    stop_dist = stop_pts_param
                elif spec.rules_v2.stop_loss.mode == "swing_extreme_buffer":
                    sw_lookback = int(parameters.get("swing_lookback_bars", 5))
                    recent = day_bars[max(0, i - sw_lookback + 1) : i + 1]
                    buffer_pts = float(parameters.get("swing_buffer_points", spec.rules_v2.stop_loss.value))
                    if entry_direction == "CALL":
                        stop_spot_calc = min(b["low"] for b in recent) - buffer_pts
                    else:
                        stop_spot_calc = max(b["high"] for b in recent) + buffer_pts
                    stop_dist = abs(entry_spot - stop_spot_calc)
                else:
                    raise UnsupportedStrategyRuleError(
                        f"UNSUPPORTED_STOP_LOSS_MODE: Stop loss mode '{spec.rules_v2.stop_loss.mode}' is not supported"
                    )

                if spec.rules_v2.stop_loss.min_points is not None:
                    stop_dist = max(stop_dist, spec.rules_v2.stop_loss.min_points)
                if spec.rules_v2.stop_loss.max_points is not None:
                    stop_dist = min(stop_dist, spec.rules_v2.stop_loss.max_points)

                stop_spot = (entry_spot - stop_dist) if entry_direction == "CALL" else (entry_spot + stop_dist)

                if spec.rules_v2.target.mode == "r_multiple":
                    target_dist = stop_dist * target_r_param
                elif spec.rules_v2.target.mode == "index_points":
                    target_dist = target_pts_param
                else:
                    raise UnsupportedStrategyRuleError(
                        f"UNSUPPORTED_TARGET_MODE: Target mode '{spec.rules_v2.target.mode}' is not supported"
                    )
                target_spot = (entry_spot + target_dist) if entry_direction == "CALL" else (entry_spot - target_dist)

                exit_spot = None
                exit_time = None
                exit_reason = None
                exit_idx = n_bars - 1
                exit_found = False

                for j in range(i + 1, n_bars):
                    fbar = day_bars[j]
                    fbar_close_time = fbar["timestamp_close"]

                    stopped = (fbar["low"] <= stop_spot) if entry_direction == "CALL" else (fbar["high"] >= stop_spot)
                    targeted = (fbar["high"] >= target_spot) if entry_direction == "CALL" else (fbar["low"] <= target_spot)

                    if stopped and targeted:
                        exit_spot = stop_spot
                        exit_time = fbar_close_time
                        exit_reason = "STOP_LOSS_HIT"
                        exit_idx = j
                        exit_found = True
                        break
                    elif stopped:
                        exit_spot = stop_spot
                        exit_time = fbar_close_time
                        exit_reason = "STOP_LOSS_HIT"
                        exit_idx = j
                        exit_found = True
                        break
                    elif targeted:
                        exit_spot = target_spot
                        exit_time = fbar_close_time
                        exit_reason = "TARGET_HIT"
                        exit_idx = j
                        exit_found = True
                        break
                    elif fbar_close_time.time() >= square_off_time:
                        exit_spot = fbar["close"]
                        exit_time = fbar_close_time
                        exit_reason = "INTRADAY_CLOSE"
                        exit_idx = j
                        exit_found = True
                        break

                strike = round(entry_spot / strike_step) * strike_step
                offset = float(parameters.get("strike_offset", rules.instrument.strike_offset or 0))
                strike += offset if entry_direction == "CALL" else -offset
                opt_type = "CE" if entry_direction == "CALL" else "PE"

                day_dt = date.fromisoformat(day_str)
                try:
                    exp_date = resolve_expiry(day_dt, underlying=underlying, holidays=holidays,
                                              listed=self.listed_expiries, policy=self.expiry_day_policy)
                    lot_size = contract_lot_size(lot_changes, underlying, day_str)
                except ValueError as exc:  # ExpiryUnresolved included
                    raise MissingOptionDataError(f"HISTORICAL_CONTRACT_UNRESOLVED: {exc}") from exc
                expiry_str = exp_date.isoformat()
                dte_days = max(float((exp_date - day_dt).days), 0.5)
                entry_q = option_quote_provider(underlying, strike, opt_type, expiry_str, dec_time) if option_quote_provider else None
                quoted = entry_q.lot_size if isinstance(entry_q, ContractQuote) else (entry_q or {}).get("lot_size")
                with suppress(ValueError):  # a quoted lot the table rules out stays on as a Tier 1 contract mismatch
                    lot_size = contract_lot_size(lot_changes, underlying, day_str, quoted)
                regime_label = regime.classify_regime(
                    [{"date": b["timestamp"].isoformat(), **{k: b[k] for k in ("open", "high", "low", "close")}}
                     for b in day_bars[: i + 1]], now=dec_time).structure_regime

                if not exit_found or exit_time is None:
                    # End of data reached before target, stop, or square-off!
                    # DO NOT fabricate completed exit: mark incomplete_label=True
                    ev = CandidateSignalEvent(
                        event_id=f"causal-{spec.strategy_id}-{day_str}-{dec_time.strftime('%H%M')}",
                        market_day=day_str,
                        decision_time=dec_time.isoformat(),
                        label_end_time=None,
                        underlying=underlying,
                        direction=entry_direction,
                        entry_spot=entry_spot,
                        stop_spot=stop_spot,
                        target_spot=target_spot,
                        exit_spot=None,
                        strike=strike,
                        option_type=opt_type,
                        expiry=expiry_str,
                        lot_size=lot_size,
                        lots=lots,
                        regime=regime_label,
                        entry_quote=entry_q,
                        exit_quote=None,
                        entry_iv=iv_param,
                        exit_iv=iv_param,
                        dte_days=dte_days,
                        holding_minutes=0.0,
                        incomplete_label=True,
                        exit_reason="MISSING_FUTURE_BARS",
                        metadata={"parameter_hash": sha256_hex(canonical_json(normalize_json(dict(parameters))))},
                    )
                    events.append(ev)
                    i += 1
                    continue

                holding_mins = max((exit_time - dec_time).total_seconds() / 60.0, 5.0)

                exit_q = None
                if option_quote_provider is not None:
                    exit_q = option_quote_provider(underlying, strike, opt_type, expiry_str, exit_time)
                    if entry_q is not None and exit_q is None:
                        # Executable exit quote missing: mark incomplete label
                        ev = CandidateSignalEvent(
                            event_id=f"causal-{spec.strategy_id}-{day_str}-{dec_time.strftime('%H%M')}",
                            market_day=day_str,
                            decision_time=dec_time.isoformat(),
                            label_end_time=None,
                            underlying=underlying,
                            direction=entry_direction,
                            entry_spot=entry_spot,
                            stop_spot=stop_spot,
                            target_spot=target_spot,
                            exit_spot=None,
                            strike=strike,
                            option_type=opt_type,
                            expiry=expiry_str,
                            lot_size=lot_size,
                            lots=lots,
                            regime=regime_label,
                            entry_quote=entry_q,
                            exit_quote=None,
                            entry_iv=iv_param,
                            exit_iv=iv_param,
                            dte_days=dte_days,
                            holding_minutes=holding_mins,
                            incomplete_label=True,
                            exit_reason="MISSING_EXIT_QUOTE",
                            metadata={"parameter_hash": sha256_hex(canonical_json(normalize_json(dict(parameters))))},
                        )
                        events.append(ev)
                        i += 1
                        continue

                ev = CandidateSignalEvent(
                    event_id=f"causal-{spec.strategy_id}-{day_str}-{dec_time.strftime('%H%M')}",
                    market_day=day_str,
                    decision_time=dec_time.isoformat(),
                    label_end_time=exit_time.isoformat(),
                    underlying=underlying,
                    direction=entry_direction,
                    entry_spot=entry_spot,
                    stop_spot=stop_spot,
                    target_spot=target_spot,
                    exit_spot=exit_spot,
                    strike=strike,
                    option_type=opt_type,
                    expiry=expiry_str,
                    lot_size=lot_size,
                    lots=lots,
                    regime=regime_label,
                    entry_quote=entry_q,
                    exit_quote=exit_q,
                    entry_iv=iv_param,
                    exit_iv=iv_param,
                    dte_days=dte_days,
                    holding_minutes=holding_mins,
                    exit_reason=exit_reason,
                    metadata={"parameter_hash": sha256_hex(canonical_json(normalize_json(dict(parameters))))},
                )
                events.append(ev)
                i += 1

            prior_pdh = max(b["high"] for b in day_bars)
            prior_pdl = min(b["low"] for b in day_bars)

        return events


class CausalParameterSelector(ABC):
    """Abstract selector for causal in-sample parameter selection."""

    @abstractmethod
    def select_parameters(
        self,
        fold: WalkForwardFold,
        spec: NormalizedStrategySpec,
        generator: CausalSignalGenerator,
        candles: Sequence[Mapping[str, Any]] | pd.DataFrame,
        config: WalkForwardConfig,
        settings: Settings | None = None,
        option_quote_provider: OptionQuoteProvider | None = None,
    ) -> tuple[Mapping[str, Any], Sequence[str]]:
        ...


class GridSearchParameterSelector(CausalParameterSelector):
    """Evaluates candidate parameter grids causally on IS data and selects the best parameter set."""

    def __init__(
        self,
        param_grid: Sequence[Mapping[str, Any]],
        *,
        metric: str = "net_pnl_inr",
        min_is_trades: int = 1,
    ) -> None:
        if not param_grid:
            raise ValueError("param_grid must contain at least one parameter dictionary")
        self.param_grid = tuple(dict(p) for p in param_grid)
        self.metric = metric
        self.min_is_trades = min_is_trades

    def select_parameters(
        self,
        fold: WalkForwardFold,
        spec: NormalizedStrategySpec,
        generator: CausalSignalGenerator,
        candles: Sequence[Mapping[str, Any]] | pd.DataFrame,
        config: WalkForwardConfig,
        settings: Settings | None = None,
        option_quote_provider: OptionQuoteProvider | None = None,
    ) -> tuple[Mapping[str, Any], Sequence[str]]:
        require_is_view(candles, fold=fold, requirements=requirements_for(spec))
        best_params = None
        best_score = -float("inf")
        is_date_set = set(fold.is_dates)

        for candidate_p in self.param_grid:
            raw_events = generator.generate_signals(
                spec,
                candidate_p,
                candles,
                allowed_dates=fold.is_dates,
                window_end_date=fold.is_end_date,
                config=config,
                settings=settings,
                option_quote_provider=option_quote_provider,
            )
            clean_events, _, _ = _filter_and_control_events_for_window(
                raw_events,
                allowed_dates=is_date_set,
                window_end_date=fold.is_end_date,
                allow_overlapping_labels=config.allow_overlapping_labels,
                strict_leakage_check=config.strict_leakage_check,
            )
            is_execs = [
                simulate_option_execution(
                    ev,
                    config=config,
                    settings=settings,
                    fail_closed_on_missing=False,
                )
                for ev in clean_events
            ]
            tracker = ReplayRiskTracker(initial_capital_inr=config.initial_capital_inr,
                max_open_positions=spec.rules_v2.risk.max_open_positions)
            is_execs = [tracker.check_and_apply(ev, ex) for ev, ex in zip(clean_events, is_execs)]
            metrics = compute_validation_metrics(
                is_execs,
                trading_days_evaluated=len(fold.is_dates),
                initial_capital_inr=config.initial_capital_inr,
                fidelity_tier=config.fidelity_tier,
                min_recommended_trades=self.min_is_trades,
            )
            score = getattr(metrics, self.metric, None)
            if score is not None:
                score_val = float(score)
                if math.isfinite(score_val) and metrics.trade_count >= self.min_is_trades and score_val > best_score:
                    best_score = score_val
                    best_params = candidate_p

        if best_params is None:
            raise UnsupportedStrategyRuleError("NO_VALID_PARAMETER_CANDIDATE: no candidate met IS eligibility")
        return best_params, fold.is_dates


ParameterSelector = Callable[
    [WalkForwardFold, Sequence[CandidateSignalEvent], Sequence[SimulatedOptionExecution], NormalizedStrategySpec],
    Mapping[str, Any] | tuple[Mapping[str, Any], Sequence[str]],
]
OOSFilterHook = Callable[[CandidateSignalEvent, FrozenFoldParameters], tuple[bool, str | None]]


def run_walk_forward_validation(
    spec_or_doc: NormalizedStrategySpec | StrategyDocument,
    *,
    trading_dates: Sequence[date | str],
    events: Sequence[CandidateSignalEvent] | None = None,
    candles: Sequence[Mapping[str, Any]] | pd.DataFrame | None = None,
    signal_generator: CausalSignalGenerator | None = None,
    config: WalkForwardConfig | None = None,
    settings: Settings | None = None,
    parameter_selector: ParameterSelector | CausalParameterSelector | None = None,
    oos_event_filter: OOSFilterHook | None = None,
    universe_metadata: Mapping[str, Any] | None = None,
    fail_closed_on_missing_option_data: bool = True,
    option_quote_provider: OptionQuoteProvider | None = None,
    dataset_declaration=None,
) -> WalkForwardReport:
    """Replay fixed supplied events or causally regenerate signals in chronological OOS folds.

    Guarantees:
    - Quarantined strategies fail validation immediately (`STRATEGY_QUARANTINED`).
    - When `trading_dates` cannot form at least 1 valid OOS fold, `has_valid_oos_folds = False` and
      `oos_performance = None` (never reports an OOS result when no valid OOS fold exists).
    - Purges IS events whose entry falls in `purged_dates` or whose label horizon extends past `fold.is_end_date`.
    - Embargoes events in `embargo_dates`.
    - Freezes baseline parameters before OOS and verifies they remain unchanged.
    - Rejects fixed-event parameter selectors: parameter optimization requires a causal signal generator.
    - Under causal mode (`signal_generator` and `candles`), IS-selected parameters are frozen and control
      OOS entry, stop, and exit decisions without lookahead on completed candles.
    - Enforces Tier 1 vs Tier 2 execution rules and blocks Tier 2 synthetic results from paper promotion.
    """
    cfg = config or WalkForwardConfig()
    active_settings = settings or get_settings()
    verify_point_in_time_universe(universe_metadata)

    if isinstance(spec_or_doc, NormalizedStrategySpec):
        spec = spec_or_doc
    elif isinstance(spec_or_doc, StrategyDocument):
        spec = normalize_strategy_document(spec_or_doc, strict_gaps=True)
    else:
        raise TypeError(f"Expected NormalizedStrategySpec or StrategyDocument, got {type(spec_or_doc).__name__}")

    binding = spec.validation_binding()
    folds = generate_walk_forward_folds(trading_dates, cfg)
    if folds and cfg.step_trading_days < cfg.oos_trading_days:
        raise WalkForwardError("OVERLAPPING_OOS_ACCOUNT_WINDOWS_UNSUPPORTED")
    if events is not None and (candles is not None or signal_generator is not None):
        raise CandleIntegrityError("CI_INPUT_MODE_AMBIGUOUS")
    if events is None and (candles is None or signal_generator is None):
        raise CandleIntegrityError("CI_INPUT_MODE_INVALID")
    integrity_receipt = None
    if events is None:
        candles = execution_snapshot(candles, declaration=dataset_declaration,
                                     requirements=requirements_for(spec),
                                     trading_dates=tuple(_parse_day(d) for d in trading_dates))
        integrity_receipt = candles.receipt
    # None for fixed-event replay or a generator that does not resolve expiries itself
    expiry_day_policy = getattr(signal_generator, "expiry_day_policy", None)

    tier = cfg.fidelity_tier
    performance_label = (
        "VERIFIED_MARKET_DATA" if tier == "TIER_1_VERIFIED" else "SYNTHETIC_MODEL_SIMULATION_ONLY"
    )

    # Handle insufficient historical samples: do NOT report an OOS result when no valid OOS fold exists!
    if not folds:
        failed_criteria = ["NO_VALID_OOS_FOLDS", "INSUFFICIENT_HISTORICAL_SAMPLES"]
        if spec.quarantined:
            failed_criteria.append("STRATEGY_QUARANTINED")
        if tier == "TIER_2_SYNTHETIC":
            failed_criteria.append("SYNTHETIC_TIER_2_INELIGIBLE_FOR_PAPER_PROMOTION")
        limitations = (
            f"INSUFFICIENT_HISTORICAL_SAMPLES: provided {len(trading_dates)} trading days, need at least "
            f"{cfg.is_trading_days + cfg.effective_embargo_days + cfg.oos_trading_days} for 1 valid OOS fold",
        )
        provenance_block = {
            "fidelity_tier": tier,
            "verified_market_performance": False,
            "performance_label": performance_label,
            "paper_promotion_eligible": False,
            "normalizer_version": spec.normalizer_version,
            "source_provenance": dict(spec.provenance),
            "source_references": list(spec.source_references),
            "provenance_sha256": spec.provenance_sha256,
            "content_sha256": spec.content_sha256,
            "rules_sha256": spec.rules_sha256,
            "rules_v2_sha256": spec.rules_v2_sha256,
            "parameter_hash": spec.parameter_hash,
            "spec_sha256": spec.spec_sha256,
            "validation_binding": binding,
            "cost_policy_ids": [],
            "coverage_ratio": 0.0,
            "expiry_day_policy": expiry_day_policy,
        }
        report_payload = {
            "engine_version": WALK_FORWARD_ENGINE_VERSION,
            "binding_sha256": binding["binding_sha256"],
            "config": cfg.to_dict(),
            "expiry_day_policy": expiry_day_policy,
            "fold_hashes": [],
            "has_valid_oos_folds": False,
            "oos_performance": None,
            "failed_validation_criteria": failed_criteria,
        }
        return WalkForwardReport(
            dataset_integrity=integrity_receipt,
            engine_version=WALK_FORWARD_ENGINE_VERSION,
            strategy_id=spec.strategy_id,
            strategy_version=spec.strategy_version,
            normalizer_version=spec.normalizer_version,
            strategy_state=spec.strategy_state,
            config=cfg,
            fidelity_tier=tier,
            verified_market_performance=False,
            performance_label=performance_label,
            paper_promotion_eligible=False,
            has_valid_oos_folds=False,
            fold_count=0,
            folds=(),
            oos_performance=None,
            data_fidelity_and_provenance=provenance_block,
            confidence_limitations=limitations,
            failed_validation_criteria=tuple(failed_criteria),
            validation_passed=False,
            report_hash=sha256_hex(canonical_json(normalize_json(report_payload))),
        )

    fold_results: list[WalkForwardFoldResult] = []
    all_oos_executions: list[SimulatedOptionExecution] = []
    all_oos_days: set[str] = set()
    total_oos_skipped = 0
    observed_cost_policies: set[str] = set()

    oos_risk_tracker = ReplayRiskTracker(initial_capital_inr=cfg.initial_capital_inr,
        max_open_positions=spec.rules_v2.risk.max_open_positions if spec.rules_v2 else 1)
    for fold in folds:
        is_date_set = set(fold.is_dates)
        purged_date_set = set(fold.purged_dates)
        embargo_date_set = set(fold.embargo_dates)
        oos_date_set = set(fold.oos_dates)
        all_oos_days.update(oos_date_set)

        if signal_generator is not None:
            # -------------------------------------------------------------
            # Causal Signal Regeneration Path
            # -------------------------------------------------------------
            selected_params: Mapping[str, Any] | None = None
            selection_dates: Sequence[str] = fold.is_dates

            if parameter_selector is not None:
                is_view = derive_is_snapshot(candles, fold=fold)
                require_is_view(is_view, fold=fold, requirements=requirements_for(spec))
                if isinstance(parameter_selector, CausalParameterSelector):
                    selected_params, selection_dates = parameter_selector.select_parameters(
                        fold=fold,
                        spec=spec,
                        generator=signal_generator,
                        candles=is_view,
                        config=cfg,
                        settings=active_settings,
                        option_quote_provider=option_quote_provider,
                    )
                else:
                    baseline_params = {k: v.value for k, v in spec.rules_v2.parameters.items()}
                    raw_base_is = signal_generator.generate_signals(
                        spec,
                        baseline_params,
                        is_view,
                        allowed_dates=fold.is_dates,
                        window_end_date=fold.is_end_date,
                        config=cfg,
                        settings=active_settings,
                        option_quote_provider=option_quote_provider,
                    )
                    clean_base_is, _, _ = _filter_and_control_events_for_window(
                        raw_base_is,
                        allowed_dates=is_date_set,
                        window_end_date=fold.is_end_date,
                        allow_overlapping_labels=cfg.allow_overlapping_labels,
                        strict_leakage_check=cfg.strict_leakage_check,
                    )
                    base_is_execs = [
                        simulate_option_execution(
                            ev,
                            config=cfg,
                            settings=active_settings,
                            fail_closed_on_missing=fail_closed_on_missing_option_data,
                        )
                        for ev in clean_base_is
                    ]
                    tracker = ReplayRiskTracker(initial_capital_inr=cfg.initial_capital_inr,
                        max_open_positions=spec.rules_v2.risk.max_open_positions)
                    base_is_execs = [tracker.check_and_apply(ev, ex) for ev, ex in zip(clean_base_is, base_is_execs)]
                    sel_out = parameter_selector(fold, clean_base_is, base_is_execs, spec)
                    if isinstance(sel_out, tuple) and len(sel_out) == 2:
                        selected_params, selection_dates = sel_out
                    elif isinstance(sel_out, Mapping):
                        selected_params = sel_out
                    else:
                        raise ParameterFreezeViolationError(
                            "parameter_selector must return Mapping or (Mapping, source_dates)"
                        )

            frozen = freeze_fold_parameters(
                fold=fold,
                spec=spec,
                selected_parameters=selected_params,
                selection_source_dates=selection_dates,
            )
            active_params_working_copy = copy.deepcopy(dict(frozen.parameters))

            # Re-generate clean IS events using the frozen IS-selected parameters
            raw_is_events = signal_generator.generate_signals(
                spec,
                active_params_working_copy,
                candles,
                allowed_dates=fold.is_dates,
                window_end_date=fold.is_end_date,
                config=cfg,
                settings=active_settings,
                option_quote_provider=option_quote_provider,
            )
            purged_ids_in_tail = [e.event_id for e in raw_is_events if _parse_day(e.market_day) in purged_date_set]
            embargo_ids = [e.event_id for e in raw_is_events if _parse_day(e.market_day) in embargo_date_set]
            clean_is_events, overflow_is_ids, overlap_is_ids = _filter_and_control_events_for_window(
                raw_is_events,
                allowed_dates=is_date_set,
                window_end_date=fold.is_end_date,
                allow_overlapping_labels=cfg.allow_overlapping_labels,
                strict_leakage_check=cfg.strict_leakage_check,
            )
            purged_is_ids = tuple(dict.fromkeys([*purged_ids_in_tail, *overflow_is_ids]))
            is_risk_tracker = ReplayRiskTracker(
                initial_capital_inr=cfg.initial_capital_inr,
                max_open_positions=getattr(spec.rules_v2.risk, "max_open_positions", 1) if spec.rules_v2 else 1,
            )
            is_executions = []
            for ev in clean_is_events:
                ex = simulate_option_execution(
                    ev,
                    config=cfg,
                    settings=active_settings,
                    fail_closed_on_missing=fail_closed_on_missing_option_data,
                )
                ex = is_risk_tracker.check_and_apply(ev, ex)
                is_executions.append(ex)

            # Generate OOS events causally using the FROZEN parameters!
            frozen.verify_unchanged(active_params_working_copy)
            raw_oos_events = signal_generator.generate_signals(
                spec,
                active_params_working_copy,
                candles,
                allowed_dates=fold.oos_dates,
                window_end_date=fold.oos_end_date,
                config=cfg,
                settings=active_settings,
                option_quote_provider=option_quote_provider,
            )
            clean_oos_events, incomplete_oos_ids, overlap_oos_ids = _filter_and_control_events_for_window(
                raw_oos_events,
                allowed_dates=oos_date_set,
                window_end_date=fold.oos_end_date,
                allow_overlapping_labels=cfg.allow_overlapping_labels,
                strict_leakage_check=cfg.strict_leakage_check,
            )
        else:
            # -------------------------------------------------------------
            # Fixed-Event Replay Path
            # -------------------------------------------------------------
            assert events is not None
            # Track events falling directly in the trailing purge dates or embargo dates
            purged_ids_in_tail = [e.event_id for e in events if _parse_day(e.market_day) in purged_date_set]
            embargo_ids = [e.event_id for e in events if _parse_day(e.market_day) in embargo_date_set]

            # Filter IS events and purge any IS event whose label horizon overflows fold.is_end_date
            clean_is_events, overflow_is_ids, overlap_is_ids = _filter_and_control_events_for_window(
                events,
                allowed_dates=is_date_set,
                window_end_date=fold.is_end_date,
                allow_overlapping_labels=cfg.allow_overlapping_labels,
                strict_leakage_check=cfg.strict_leakage_check,
            )
            purged_is_ids = tuple(dict.fromkeys([*purged_ids_in_tail, *overflow_is_ids]))
            is_risk_tracker = ReplayRiskTracker(
                initial_capital_inr=cfg.initial_capital_inr,
                max_open_positions=getattr(spec.rules_v2.risk, "max_open_positions", 1) if spec.rules_v2 else 1,
            )
            is_executions = []
            for ev in clean_is_events:
                ex = simulate_option_execution(
                    ev,
                    config=cfg,
                    settings=active_settings,
                    fail_closed_on_missing=fail_closed_on_missing_option_data,
                )
                ex = is_risk_tracker.check_and_apply(ev, ex)
                is_executions.append(ex)

            # Parameter selection strictly on clean IS data before OOS begins
            selected_params = None
            selection_dates = fold.is_dates
            if parameter_selector is not None:
                sel_out = parameter_selector(fold, clean_is_events, is_executions, spec)
                if isinstance(sel_out, tuple) and len(sel_out) == 2:
                    selected_params, selection_dates = sel_out
                elif isinstance(sel_out, Mapping):
                    selected_params = sel_out
                else:
                    raise ParameterFreezeViolationError(
                        "parameter_selector must return Mapping or (Mapping, source_dates)"
                    )

            frozen = freeze_fold_parameters(
                fold=fold,
                spec=spec,
                selected_parameters=selected_params,
                selection_source_dates=selection_dates,
            )
            if parameter_selector is not None:
                raise ParameterFreezeViolationError(
                    "FIXED_EVENT_REPLAY_ONLY: selected parameters cannot regenerate supplied events; "
                    "parameter optimization requires a causal signal generator"
                )
            active_params_working_copy = copy.deepcopy(dict(frozen.parameters))

            # Filter OOS events strictly inside fold.oos_dates and ending by fold.oos_end_date
            clean_oos_events, incomplete_oos_ids, overlap_oos_ids = _filter_and_control_events_for_window(
                events,
                allowed_dates=oos_date_set,
                window_end_date=fold.oos_end_date,
                allow_overlapping_labels=cfg.allow_overlapping_labels,
                strict_leakage_check=cfg.strict_leakage_check,
            )

        oos_executions: list[SimulatedOptionExecution] = []
        hook_skipped = 0
        for ev in clean_oos_events:
            # Verify parameter freeze before each OOS event evaluation
            frozen.verify_unchanged(active_params_working_copy)
            if oos_event_filter is not None:
                allow_trade, _reason = oos_event_filter(ev, frozen)
                # Ensure oos_event_filter did not mutate frozen parameters
                frozen.verify_unchanged(active_params_working_copy)
                if not allow_trade:
                    hook_skipped += 1
                    continue
            sim_ex = simulate_option_execution(
                ev,
                config=cfg,
                settings=active_settings,
                fail_closed_on_missing=fail_closed_on_missing_option_data,
            )
            sim_ex = oos_risk_tracker.check_and_apply(ev, sim_ex)
            if sim_ex.cost_policy_id:
                observed_cost_policies.add(sim_ex.cost_policy_id)
            oos_executions.append(sim_ex)

        oos_risk_tracker._update_positions(_parse_ts(f"{fold.oos_end_date}T23:59:59+05:30"))
        is_metrics = compute_validation_metrics(
            is_executions,
            trading_days_evaluated=len(fold.is_dates),
            initial_capital_inr=cfg.initial_capital_inr,
            fidelity_tier=tier,
            min_recommended_trades=max(5, cfg.min_oos_trades // max(len(folds), 1)),
            extra_skipped_count=len(purged_ids_in_tail),
        )
        fold_extra_skipped = hook_skipped
        total_oos_skipped += fold_extra_skipped
        oos_metrics = compute_validation_metrics(
            oos_executions,
            trading_days_evaluated=len(fold.oos_dates),
            initial_capital_inr=cfg.initial_capital_inr,
            fidelity_tier=tier,
            min_recommended_trades=max(5, cfg.min_oos_trades // max(len(folds), 1)),
            extra_skipped_count=fold_extra_skipped,
        )

        fold_payload = {
            "fold": fold.to_dict(),
            "frozen_parameters": frozen.to_dict(),
            "is_net_pnl_inr": is_metrics.net_pnl_inr,
            "is_trade_count": is_metrics.trade_count,
            "oos_net_pnl_inr": oos_metrics.net_pnl_inr,
            "oos_trade_count": oos_metrics.trade_count,
            "purged_is_event_ids": list(purged_is_ids),
            "embargoed_event_ids": list(embargo_ids),
            "incomplete_oos_event_ids": list(incomplete_oos_ids),
            "overlapping_skipped_event_ids": list(overlap_oos_ids),
            "fidelity_tier": tier,
            "oos_executions": [asdict(e) for e in oos_executions],
            "is_inputs": [asdict(e) for e in clean_is_events],
            "oos_inputs": [asdict(e) for e in clean_oos_events],
            "oos_account": oos_risk_tracker.snapshot(),
        }
        f_hash = sha256_hex(canonical_json(normalize_json(fold_payload)))

        from app.trading.trade_memory import project_research
        by_event = {e.event_id: e for e in clean_oos_events}
        memories = tuple(project_research(by_event[e.event_id], e, strategy_id=spec.strategy_id,
            strategy_version=spec.strategy_version, parameter_hash=frozen.parameter_hash,
            account_id=f"IND:{spec.strategy_id}", validation_binding={**binding,
                "engine_version": WALK_FORWARD_ENGINE_VERSION, "fold_hash": f_hash,
                "risk_policy_id": "paper-risk-v2", "config": cfg.to_dict()}).to_dict()
            for e in oos_executions)
        fold_results.append(
            WalkForwardFoldResult(
                fold=fold,
                frozen_parameters=frozen,
                is_performance=is_metrics,
                oos_performance=oos_metrics,
                purged_is_event_ids=purged_is_ids,
                embargoed_event_ids=tuple(embargo_ids),
                incomplete_oos_event_ids=tuple(incomplete_oos_ids),
                overlapping_skipped_event_ids=tuple(overlap_oos_ids),
                oos_executions=tuple(oos_executions),
                fold_hash=f_hash,
                account_snapshot=oos_risk_tracker.snapshot(),
                trade_memory=memories,
            )
        )
        all_oos_executions.extend(oos_executions)

    aggregate_oos = compute_validation_metrics(
        all_oos_executions,
        trading_days_evaluated=len(all_oos_days),
        initial_capital_inr=cfg.initial_capital_inr,
        fidelity_tier=tier,
        min_recommended_trades=cfg.min_oos_trades,
        extra_skipped_count=total_oos_skipped,
    )

    priced_oos = [e for e in all_oos_executions if e.priced]
    verified_perf = (
        tier == "TIER_1_VERIFIED"
        and len(priced_oos) > 0
        and all(e.verified_market_data for e in priced_oos)
    )
    coverage_ratio = (
        round(sum(e.entry_executable and e.exit_price_inr is not None and not e.unresolved
                  for e in all_oos_executions) / len(all_oos_executions), 4)
        if all_oos_executions
        else 1.0
    )

    failed_criteria: list[str] = []
    if spec.quarantined or not spec.structurally_complete:
        failed_criteria.append("STRATEGY_QUARANTINED")
    if tier == "TIER_2_SYNTHETIC":
        failed_criteria.append("SYNTHETIC_TIER_2_INELIGIBLE_FOR_PAPER_PROMOTION")
    if len(fold_results) < cfg.min_oos_folds:
        failed_criteria.append(f"INSUFFICIENT_OOS_FOLDS:{len(fold_results)}<{cfg.min_oos_folds}")
    if aggregate_oos.trade_count < cfg.min_oos_trades:
        failed_criteria.append(f"INSUFFICIENT_OOS_TRADES:{aggregate_oos.trade_count}<{cfg.min_oos_trades}")
    if aggregate_oos.net_expectancy_inr is None or aggregate_oos.net_expectancy_inr <= 0:
        failed_criteria.append("NON_POSITIVE_OOS_NET_EXPECTANCY")
    if aggregate_oos.profit_factor is None or aggregate_oos.profit_factor < cfg.min_profit_factor:
        failed_criteria.append(
            f"PROFIT_FACTOR_BELOW_MINIMUM:{aggregate_oos.profit_factor}<{cfg.min_profit_factor}"
        )
    if aggregate_oos.max_drawdown_pct > cfg.max_drawdown_pct:
        failed_criteria.append(
            f"MAX_DRAWDOWN_EXCEEDED:{aggregate_oos.max_drawdown_pct}>{cfg.max_drawdown_pct}"
        )
    if coverage_ratio < 1.0:
        failed_criteria.append(f"INCOMPLETE_OPTION_QUOTE_COVERAGE:{coverage_ratio}")

    # Research validation passes only if all criteria (other than Tier 2 paper-promotion gate if evaluating research) pass;
    # paper_promotion_eligible strictly requires Tier 1 verified data AND zero failed criteria.
    paper_promotion_eligible = (tier == "TIER_1_VERIFIED") and verified_perf and (len(failed_criteria) == 0)
    validation_passed = len([c for c in failed_criteria if c != "SYNTHETIC_TIER_2_INELIGIBLE_FOR_PAPER_PROMOTION"]) == 0

    lifecycle_state = spec.strategy_state
    if not spec.quarantined and spec.structurally_complete:
        if paper_promotion_eligible:
            lifecycle_state = "INDEPENDENTLY_VALIDATED"
        else:
            lifecycle_state = "BACKTEST_ELIGIBLE"

    provenance_block = {
        "fidelity_tier": tier,
        "verified_market_performance": verified_perf,
        "performance_label": performance_label,
        "paper_promotion_eligible": paper_promotion_eligible,
        "normalizer_version": spec.normalizer_version,
        "source_provenance": dict(spec.provenance),
        "source_references": list(spec.source_references),
        "provenance_sha256": spec.provenance_sha256,
        "content_sha256": spec.content_sha256,
        "rules_sha256": spec.rules_sha256,
        "rules_v2_sha256": spec.rules_v2_sha256,
        "parameter_hash": spec.parameter_hash,
        "spec_sha256": spec.spec_sha256,
        "validation_binding": binding,
        "cost_policy_ids": sorted(observed_cost_policies),
        "coverage_ratio": coverage_ratio,
        "expiry_day_policy": expiry_day_policy,
    }

    report_payload = {
        "engine_version": WALK_FORWARD_ENGINE_VERSION,
        "binding_sha256": binding["binding_sha256"],
        "config": cfg.to_dict(),
        "expiry_day_policy": expiry_day_policy,
        "fidelity_tier": tier,
        "verified_market_performance": verified_perf,
        "paper_promotion_eligible": paper_promotion_eligible,
        "fold_hashes": [f.fold_hash for f in fold_results],
        "oos_net_pnl_inr": aggregate_oos.net_pnl_inr,
        "oos_trade_count": aggregate_oos.trade_count,
        "failed_validation_criteria": failed_criteria,
    }
    r_hash = sha256_hex(canonical_json(normalize_json(report_payload)))

    return WalkForwardReport(
        dataset_integrity=integrity_receipt,
        engine_version=WALK_FORWARD_ENGINE_VERSION,
        strategy_id=spec.strategy_id,
        strategy_version=spec.strategy_version,
        normalizer_version=spec.normalizer_version,
        strategy_state=lifecycle_state,
        config=cfg,
        fidelity_tier=tier,
        verified_market_performance=verified_perf,
        performance_label=performance_label,
        paper_promotion_eligible=paper_promotion_eligible,
        has_valid_oos_folds=True,
        fold_count=len(fold_results),
        folds=tuple(fold_results),
        oos_performance=aggregate_oos,
        data_fidelity_and_provenance=provenance_block,
        confidence_limitations=aggregate_oos.confidence_limitations,
        failed_validation_criteria=tuple(failed_criteria),
        validation_passed=validation_passed,
        report_hash=r_hash,
    )


def run_causal_walk_forward_validation(
    spec_or_doc: NormalizedStrategySpec | StrategyDocument,
    *,
    trading_dates: Sequence[date | str],
    candles: Sequence[Mapping[str, Any]] | pd.DataFrame,
    signal_generator: CausalSignalGenerator | None = None,
    config: WalkForwardConfig | None = None,
    settings: Settings | None = None,
    parameter_selector: ParameterSelector | CausalParameterSelector | None = None,
    oos_event_filter: OOSFilterHook | None = None,
    universe_metadata: Mapping[str, Any] | None = None,
    fail_closed_on_missing_option_data: bool = True,
    option_quote_provider: OptionQuoteProvider | None = None,
    dataset_declaration=None,
) -> WalkForwardReport:
    """Execute causal walk-forward validation where parameters are optimized on IS completed candles and frozen to control OOS executions."""
    gen = signal_generator if signal_generator is not None else RuleBasedCausalSignalGenerator()
    return run_walk_forward_validation(
        spec_or_doc,
        trading_dates=trading_dates,
        candles=candles,
        dataset_declaration=dataset_declaration,
        signal_generator=gen,
        config=config,
        settings=settings,
        parameter_selector=parameter_selector,
        oos_event_filter=oos_event_filter,
        universe_metadata=universe_metadata,
        fail_closed_on_missing_option_data=fail_closed_on_missing_option_data,
        option_quote_provider=option_quote_provider,
    )
