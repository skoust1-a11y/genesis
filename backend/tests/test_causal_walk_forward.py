"""Regression tests for B3 Causal Signal Regeneration and Parameter Optimization.

Proves:
1. Parameter changes affect executed strategy behavior.
2. Selection uses IS data only (and leaky OOS selection fails closed).
3. Future candles cannot affect earlier decisions (strict causality).
4. Results and report/fold hashes are reproducible.
5. Option quote, cost, and risk gates remain strictly enforced (Tier 1 & Tier 2).
6. Frozen PDC V0 benchmark and rule files remain unchanged.
7. Fixed-event replay parameter optimization remains blocked (`FIXED_EVENT_REPLAY_ONLY`).
8. Unsupported strategy rules and quarantined specifications are rejected explicitly.
"""

from __future__ import annotations

import copy
import hashlib
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

import pandas as pd
import pytest

from tests.candle_integrity_fixtures import declaration, selection_view, synthetic_calendar

pytestmark = pytest.mark.usefixtures("synthetic_calendar")

from app.config import Settings
from app.strategies.normalizer import (
    NormalizedStrategySpec,
    normalize_markdown,
    normalize_pdc_15ema_v0_benchmark,
)
from app.strategies.rules import (
    Instrument,
    IntradayEntryPredicate,
    ParameterDef,
    ProvenanceSpec,
    RiskRules,
    SessionWindowSpec,
    StopLossSpec,
    StrategyRulesV2,
    TargetSpec,
    TimeframeSpec,
)
from app.trading import paper_exec as px
from app.trading import walk_forward as wf
from app.trading.option_quote import IST, ContractQuote
from app.trading.paper_registry import RULE_SOURCE_HASHES

DAY = "2026-11-06"


def _trading_calendar(count: int = 85, start: str = "2026-05-04") -> list[str]:
    """Generate chronological weekday trading dates."""
    cur = date.fromisoformat(start)
    dates: list[str] = []
    while len(dates) < count:
        if cur.weekday() < 5:
            dates.append(cur.isoformat())
        cur += timedelta(days=1)
    return dates


def _wf_cfg(**kwargs) -> wf.WalkForwardConfig:
    """WalkForwardConfig helper with historical brokerage assumption enabled for test calendars."""
    kwargs.setdefault("allow_historical_brokerage_assumption", True)
    kwargs.setdefault("trusted_delta_sources", frozenset({"nse_historical"}))
    kwargs.setdefault("is_trading_days", 60)
    kwargs.setdefault("oos_trading_days", 20)
    kwargs.setdefault("step_trading_days", 20)
    kwargs.setdefault("purge_days", 5)
    kwargs.setdefault("embargo_days", 5)
    kwargs.setdefault("min_oos_folds", 1)
    kwargs.setdefault("min_oos_trades", 1)
    return wf.WalkForwardConfig(**kwargs)


def _make_session_candles(
    day: str,
    pattern: str = "trending_up",
    base_price: float = 24000.0,
) -> list[dict[str, Any]]:
    """Generate 75 5-minute intraday bars (09:15 to 15:25) for a given session."""
    t0 = datetime.fromisoformat(f"{day}T09:15:00+05:30")
    bars: list[dict[str, Any]] = []
    cur_p = base_price

    for i in range(75):
        bar_time = t0 + timedelta(minutes=5 * i)
        if pattern == "trending_up":
            if i < 15:
                # Early consolidation
                delta = 2.0 * ((i % 3) - 1)
            elif i < 45:
                # Strong upward trend
                delta = 8.0
            else:
                # High plateau / slow drift
                delta = 1.0 * ((i % 3) - 1)
        elif pattern == "trending_down":
            if i < 15:
                delta = -2.0 * ((i % 3) - 1)
            elif i < 45:
                delta = -8.0
            else:
                delta = -1.0 * ((i % 3) - 1)
        else:  # range
            delta = 6.0 if (i // 5) % 2 == 0 else -6.0

        o = cur_p
        c = cur_p + delta
        h = max(o, c) + 5.0
        l = min(o, c) - 5.0
        cur_p = c

        bars.append(
            {
                "timestamp": bar_time.isoformat(),
                "open": round(o, 2),
                "high": round(h, 2),
                "low": round(l, 2),
                "close": round(c, 2),
                "volume": 1500.0,
                "underlying": "NIFTY",
            }
        )
    return bars


def _make_test_strategy_spec(
    strategy_id: str = "causal-ema-strategy",
    ema_period: int = 15,
    stop_loss_points: float = 25.0,
    target_r_multiple: float = 2.0,
) -> NormalizedStrategySpec:
    """Build a valid, unquarantined NormalizedStrategySpec with StrategyRulesV2."""
    md = f"""---
name: {strategy_id}
rules_v2:
  strategy_id: {strategy_id}
  strategy_version: v1
  market: nse_options
  timeframes:
    primary: 5m
  instrument:
    underlying: NIFTY
    structure: long_call
    strike_offset: 0
    lot_size: 75
  intraday_entry:
    - kind: ema_relation
      timeframe: 5m
      direction: LONG
      params:
        relation: cross_above
        ema_period: {ema_period}
      requires_closed_bar: true
  stop_loss:
    mode: index_points
    value: {stop_loss_points}
    min_points: 10.0
    max_points: 100.0
  target:
    mode: r_multiple
    value: {target_r_multiple}
  risk:
    max_lots: 1
    max_daily_loss_pct: 2.0
    max_open_positions: 1
  session_window:
    timezone: Asia/Kolkata
    entry_start: "09:15"
    entry_cutoff: "15:10"
    square_off: "15:25"
  parameters:
    iv: {{name: iv, type: float, value: 0.02, min_value: 0.01, max_value: 0.5}}
    ema_period:
      name: ema_period
      type: int
      value: {ema_period}
      min_value: 5
      max_value: 50
    stop_loss_points:
      name: stop_loss_points
      type: float
      value: {stop_loss_points}
      min_value: 10.0
      max_value: 100.0
    target_r_multiple:
      name: target_r_multiple
      type: float
      value: {target_r_multiple}
      min_value: 1.0
      max_value: 5.0
---
# Thesis
Causal EMA strategy for walk-forward validation.

## Gaps
"""
    return normalize_markdown(md, strategy_id=strategy_id, strategy_version="v1")


# -------------------------------------------------------------------------------------------------
# 1. PARAMETER CHANGES AFFECT EXECUTED STRATEGY BEHAVIOR
# -------------------------------------------------------------------------------------------------


def test_parameter_changes_affect_executed_strategy_behavior():
    """Verify that frozen parameter differences produce measurably distinct OOS executions and performance."""
    cal = _trading_calendar(count=85)
    all_candles: list[dict[str, Any]] = []
    for d in cal:
        all_candles.extend(_make_session_candles(d, pattern="trending_up", base_price=24000.0))

    cfg = _wf_cfg(fidelity_tier="TIER_2_SYNTHETIC")

    # Strategy A: tight stop (15 pts), quick target (1.2 R), fast EMA (10)
    spec_a = _make_test_strategy_spec("spec-a", ema_period=10, stop_loss_points=15.0, target_r_multiple=1.2)
    rep_a = wf.run_causal_walk_forward_validation(spec_a, trading_dates=cal, candles=all_candles, config=cfg, dataset_declaration=declaration(cal),)

    # Strategy B: wide stop (50 pts), ambitious target (3.5 R), slow EMA (30)
    spec_b = _make_test_strategy_spec("spec-b", ema_period=30, stop_loss_points=50.0, target_r_multiple=3.5)
    rep_b = wf.run_causal_walk_forward_validation(spec_b, trading_dates=cal, candles=all_candles, config=cfg, dataset_declaration=declaration(cal),)

    assert rep_a.has_valid_oos_folds and rep_b.has_valid_oos_folds
    assert rep_a.oos_performance is not None and rep_b.oos_performance is not None

    # Performance and trades must measurably differ
    assert (
        rep_a.oos_performance.trade_count != rep_b.oos_performance.trade_count
        or rep_a.oos_performance.net_pnl_inr != rep_b.oos_performance.net_pnl_inr
    )

    # Fold parameters must verify exact frozen values
    fold_a = rep_a.folds[0]
    fold_b = rep_b.folds[0]
    assert fold_a.frozen_parameters.parameters["stop_loss_points"] == 15.0
    assert fold_b.frozen_parameters.parameters["stop_loss_points"] == 50.0
    assert fold_a.frozen_parameters.parameters["target_r_multiple"] == 1.2
    assert fold_b.frozen_parameters.parameters["target_r_multiple"] == 3.5


# -------------------------------------------------------------------------------------------------
# 2. SELECTION USES IS DATA ONLY
# -------------------------------------------------------------------------------------------------


def test_selection_uses_is_data_only():
    """Verify parameter selection evaluates strictly inside fold.is_dates; leaky selection fails closed."""
    cal = _trading_calendar(count=85)
    all_candles: list[dict[str, Any]] = []
    for d in cal:
        all_candles.extend(_make_session_candles(d, pattern="trending_up", base_price=24000.0))

    cfg = _wf_cfg(fidelity_tier="TIER_2_SYNTHETIC")
    spec = _make_test_strategy_spec("spec-selection")

    # 1. Valid GridSearchParameterSelector: searches 3 parameter sets across IS only
    selector = wf.GridSearchParameterSelector(
        param_grid=[
            {"stop_loss_points": 20.0, "target_r_multiple": 1.5, "ema_period": 10},
            {"stop_loss_points": 35.0, "target_r_multiple": 2.5, "ema_period": 15},
            {"stop_loss_points": 50.0, "target_r_multiple": 3.0, "ema_period": 20},
        ],
        metric="net_pnl_inr",
        min_is_trades=1,
    )

    rep = wf.run_causal_walk_forward_validation(
        spec,
        trading_dates=cal,
        candles=all_candles,
        config=cfg,
        parameter_selector=selector,
     dataset_declaration=declaration(cal),)
    assert rep.has_valid_oos_folds
    for f in rep.folds:
        is_dates_set = set(f.fold.is_dates)
        oos_dates_set = set(f.fold.oos_dates)
        embargo_dates_set = set(f.fold.embargo_dates)

        # Selection dates must be entirely contained within IS dates
        assert f.frozen_parameters.frozen_at_is_end == f.fold.is_end_date
        best_p, sel_dates = selector.select_parameters(
            f.fold, spec, wf.RuleBasedCausalSignalGenerator(), selection_view(all_candles, cal, f.fold), cfg
        )
        assert set(sel_dates).issubset(is_dates_set)
        assert not any(d in oos_dates_set for d in sel_dates)
        assert not any(d in embargo_dates_set for d in sel_dates)

    # 2. Leaky selector attempting to select parameters using an OOS date must fail closed
    class LeakyOOSSelector(wf.CausalParameterSelector):
        def select_parameters(self, fold, spec, generator, candles, config, settings=None, option_quote_provider=None):
            # Leaking OOS start date into selection dates
            return {"stop_loss_points": 20.0}, [fold.is_end_date, fold.oos_start_date]

    with pytest.raises(wf.ParameterFreezeViolationError, match="OOS_PARAMETER_SELECTION_FORBIDDEN"):
        wf.run_causal_walk_forward_validation(
            spec,
            trading_dates=cal,
            candles=all_candles,
            config=cfg,
            parameter_selector=LeakyOOSSelector(),
         dataset_declaration=declaration(cal),)


# -------------------------------------------------------------------------------------------------
# 3. FUTURE CANDLES CANNOT AFFECT EARLIER DECISIONS (STRICT CAUSALITY)
# -------------------------------------------------------------------------------------------------


def test_future_candles_cannot_affect_earlier_decisions():
    """Verify mutating afternoon or next-day bars does not alter earlier morning trade entry/exit decisions."""
    day = "2026-11-06"
    base_bars = _make_session_candles(day, pattern="trending_up", base_price=24000.0)
    spec = _make_test_strategy_spec("causal-causality-test", ema_period=10, stop_loss_points=20.0, target_r_multiple=1.5)

    gen = wf.RuleBasedCausalSignalGenerator()
    params = {"ema_period": 10, "stop_loss_points": 20.0, "target_r_multiple": 1.5, "max_lots": 1}

    # Run on clean base bars
    events_base = gen.generate_signals(spec, params, base_bars, allowed_dates=[day])
    assert len(events_base) >= 1
    first_ev_base = events_base[0]

    # Find the index of the bar when the first trade completed
    exit_ts = datetime.fromisoformat(first_ev_base.label_end_time)

    # Create mutated bars where all bars after the first trade's exit are drastically altered
    mutated_bars: list[dict[str, Any]] = []
    for b in base_bars:
        b_copy = dict(b)
        bar_t = datetime.fromisoformat(b["timestamp"])
        if bar_t > exit_ts:
            # Inject extreme price crash in the afternoon
            b_copy["open"] = 21000.0
            b_copy["high"] = 21050.0
            b_copy["low"] = 20500.0
            b_copy["close"] = 20600.0
        mutated_bars.append(b_copy)

    events_mutated = gen.generate_signals(spec, params, mutated_bars, allowed_dates=[day])
    assert len(events_mutated) >= 1
    first_ev_mutated = events_mutated[0]

    # The first morning trade decision, price levels, and exit must be 100% identical byte-for-byte
    assert first_ev_base.event_id == first_ev_mutated.event_id
    assert first_ev_base.decision_time == first_ev_mutated.decision_time
    assert first_ev_base.direction == first_ev_mutated.direction
    assert first_ev_base.entry_spot == first_ev_mutated.entry_spot
    assert first_ev_base.stop_spot == first_ev_mutated.stop_spot
    assert first_ev_base.target_spot == first_ev_mutated.target_spot
    assert first_ev_base.exit_spot == first_ev_mutated.exit_spot
    assert first_ev_base.label_end_time == first_ev_mutated.label_end_time
    assert first_ev_base.exit_reason == first_ev_mutated.exit_reason


# -------------------------------------------------------------------------------------------------
# 4. RESULTS AND HASHES ARE REPRODUCIBLE
# -------------------------------------------------------------------------------------------------


def test_causal_walk_forward_results_and_hashes_reproducible():
    """Verify repeat runs produce byte-for-byte identical reports, metrics, and cryptographic hashes."""
    cal = _trading_calendar(count=85)
    all_candles: list[dict[str, Any]] = []
    for d in cal:
        all_candles.extend(_make_session_candles(d, pattern="trending_up", base_price=24000.0))

    cfg = _wf_cfg(fidelity_tier="TIER_2_SYNTHETIC")
    spec = _make_test_strategy_spec("reproducible-spec")

    rep1 = wf.run_causal_walk_forward_validation(spec, trading_dates=cal, candles=all_candles, config=cfg, dataset_declaration=declaration(cal),)
    rep2 = wf.run_causal_walk_forward_validation(spec, trading_dates=cal, candles=copy.deepcopy(all_candles), config=cfg, dataset_declaration=declaration(cal),)

    assert rep1.report_hash == rep2.report_hash
    assert rep1.oos_performance is not None and rep2.oos_performance is not None
    assert rep1.oos_performance.net_pnl_inr == rep2.oos_performance.net_pnl_inr
    assert [f.fold_hash for f in rep1.folds] == [f.fold_hash for f in rep2.folds]
    assert [f.frozen_parameters.parameter_hash for f in rep1.folds] == [
        f.frozen_parameters.parameter_hash for f in rep2.folds
    ]


# -------------------------------------------------------------------------------------------------
# 5. OPTION QUOTE, COST AND RISK GATES REMAIN ENFORCED (TIER 1 & TIER 2)
# -------------------------------------------------------------------------------------------------


def test_option_quote_cost_and_risk_gates_enforced():
    """Verify Tier 1 verified quotes enforce fees/slippage and fail closed on missing/synthetic data; Tier 2 blocks promotion."""
    cal = _trading_calendar(count=85)
    all_candles: list[dict[str, Any]] = []
    for d in cal:
        all_candles.extend(_make_session_candles(d, pattern="trending_up", base_price=24000.0))

    spec = _make_test_strategy_spec("quote-gate-spec", stop_loss_points=20.0)

    # 1. Tier 1 Verified Mode with authentic quotes
    def verified_quote_provider(underlying, strike, opt_type, expiry, timestamp):
        # Return verified non-synthetic quote
        return ContractQuote(
            tradingsymbol=f"{underlying}{strike:.0f}{opt_type}",
            provider_instrument_id=f"NSE_{strike:.0f}_{opt_type}",
            underlying=underlying,
            strike=strike,
            option_type=opt_type,
            expiry=expiry,
            lot_size=65,  # NIFTY's lot on these 2026 dates
            ltp=Decimal("50.0"),
            bid=Decimal("49.5"),
            ask=Decimal("50.5"),
            quote_time=timestamp,
            fetched_at=timestamp,
            source="nse_historical",
            delta=0.50 if opt_type == "CE" else -0.50,
            delta_observed_at=timestamp,
            delta_source="nse_historical",
            delta_contract=f"{underlying}{strike:.0f}{opt_type}",
        )

    cfg_tier1 = _wf_cfg(fidelity_tier="TIER_1_VERIFIED")

    rep_tier1 = wf.run_causal_walk_forward_validation(
        spec,
        trading_dates=cal,
        candles=all_candles,
        config=cfg_tier1,
        option_quote_provider=verified_quote_provider,
     dataset_declaration=declaration(cal),)
    assert rep_tier1.has_valid_oos_folds
    assert rep_tier1.fidelity_tier == "TIER_1_VERIFIED"
    assert rep_tier1.verified_market_performance is True
    # Verify statutory fees (brokerage, exchange, STT, IPFT, GST) were applied
    first_exec = rep_tier1.folds[0].oos_executions[0]
    assert first_exec.verified_market_data is True
    assert first_exec.total_costs_inr > 0.0

    # 2. Tier 1 Missing Quote fails closed
    with pytest.raises(wf.MissingOptionDataError, match="TIER_1_MISSING_OPTION_QUOTE"):
        wf.run_causal_walk_forward_validation(
            spec,
            trading_dates=cal,
            candles=all_candles,
            config=cfg_tier1,
            option_quote_provider=lambda *args: None,  # Missing quotes
            fail_closed_on_missing_option_data=True,
         dataset_declaration=declaration(cal),)

    # 3. Tier 1 Synthetic Quote fails closed with provenance rejection
    def synthetic_quote_provider(underlying, strike, opt_type, expiry, timestamp):
        return ContractQuote(
            tradingsymbol=f"{underlying}{strike:.0f}{opt_type}",
            provider_instrument_id=f"NSE_{strike:.0f}_{opt_type}",
            underlying=underlying,
            strike=strike,
            option_type=opt_type,
            expiry=expiry,
            lot_size=75,
            ltp=Decimal("121.0"),
            bid=Decimal("120.0"),
            ask=Decimal("122.0"),
            quote_time=timestamp,
            fetched_at=timestamp,
            source="synthetic",  # Disallowed synthetic provenance in Tier 1
            delta=0.50 if opt_type == "CE" else -0.50,
            delta_observed_at=timestamp,
            delta_source="synthetic",
            delta_contract=f"{underlying}{strike:.0f}{opt_type}",
        )

    with pytest.raises(wf.MissingOptionDataError, match="TIER_1_SYNTHETIC_PROVENANCE_REJECTED"):
        wf.run_causal_walk_forward_validation(
            spec,
            trading_dates=cal,
            candles=all_candles,
            config=cfg_tier1,
            option_quote_provider=synthetic_quote_provider,
            fail_closed_on_missing_option_data=True,
         dataset_declaration=declaration(cal),)

    # 4. Tier 2 Synthetic Mode blocks paper promotion
    cfg_tier2 = _wf_cfg(fidelity_tier="TIER_2_SYNTHETIC")
    rep_tier2 = wf.run_causal_walk_forward_validation(
        spec,
        trading_dates=cal,
        candles=all_candles,
        config=cfg_tier2,
     dataset_declaration=declaration(cal),)
    assert rep_tier2.has_valid_oos_folds
    assert rep_tier2.fidelity_tier == "TIER_2_SYNTHETIC"
    assert rep_tier2.verified_market_performance is False
    assert rep_tier2.paper_promotion_eligible is False
    assert rep_tier2.performance_label == "SYNTHETIC_MODEL_SIMULATION_ONLY"
    assert "SYNTHETIC_TIER_2_INELIGIBLE_FOR_PAPER_PROMOTION" in rep_tier2.failed_validation_criteria


# -------------------------------------------------------------------------------------------------
# 6. FIXED-EVENT PARAMETER OPTIMIZATION REMAINS BLOCKED
# -------------------------------------------------------------------------------------------------


def test_fixed_event_parameter_optimization_remains_blocked():
    """Verify supplied fixed events fail closed when parameter selectors are used without a causal generator."""
    cal = _trading_calendar(count=85)
    spec = _make_test_strategy_spec("fixed-event-block-test")
    fold0 = wf.generate_walk_forward_folds(cal)[0]

    # Pre-packaged fixed event
    ev = wf.CandidateSignalEvent(
        event_id="fixed-ev-1",
        market_day=fold0.oos_dates[1],
        decision_time=f"{fold0.oos_dates[1]}T10:15:00+05:30",
        label_end_time=f"{fold0.oos_dates[1]}T10:45:00+05:30",
        underlying="NIFTY",
        direction="CALL",
        entry_spot=24000.0,
        stop_spot=23975.0,
        target_spot=24050.0,
        exit_spot=24050.0,
        strike=24000.0,
        option_type="CE",
        expiry=fold0.oos_dates[1],
        entry_iv=0.15,
        exit_iv=0.15,
        dte_days=4.0,
    )

    def selector(fold, is_events, is_execs, norm_spec):
        return {"stop_loss_points": 30.0}, fold.is_dates

    with pytest.raises(wf.ParameterFreezeViolationError, match="FIXED_EVENT_REPLAY_ONLY"):
        wf.run_walk_forward_validation(
            spec,
            trading_dates=cal,
            events=[ev],
            parameter_selector=selector,
            signal_generator=None,  # Fixed-event replay without causal generator
        )


# -------------------------------------------------------------------------------------------------
# 7. UNSUPPORTED STRATEGY RULES REJECTED EXPLICITLY
# -------------------------------------------------------------------------------------------------


def test_unsupported_strategy_rules_rejected_explicitly():
    """Verify quarantined strategies and unsupported predicates/modes raise UnsupportedStrategyRuleError."""
    gen = wf.RuleBasedCausalSignalGenerator()
    bars = _make_session_candles(DAY, pattern="trending_up", base_price=24000.0)

    # 1. Quarantined benchmark raises UnsupportedStrategyRuleError
    pdc_spec = normalize_pdc_15ema_v0_benchmark()
    assert pdc_spec.quarantined is True
    with pytest.raises(wf.UnsupportedStrategyRuleError, match="STRATEGY_QUARANTINED"):
        gen.generate_signals(pdc_spec, {}, bars, allowed_dates=[DAY])

    # 2. Unsupported entry predicate kind raises UnsupportedStrategyRuleError
    bad_pred_spec = _make_test_strategy_spec("bad-pred-spec")
    bad_pred_spec.rules_v2.entry_composition = "ALL"
    bad_pred_spec.rules_v2.intraday_entry.append(
        IntradayEntryPredicate(
            kind="custom_predicate",
            timeframe="5m",
            direction="LONG",
        )
    )
    # Monkey-patch an unsupported kind
    object.__setattr__(bad_pred_spec.rules_v2.intraday_entry[0], "kind", "unsupported_ml_classifier")
    with pytest.raises(wf.UnsupportedStrategyRuleError, match="UNSUPPORTED_ENTRY_PREDICATE"):
        gen.generate_signals(bad_pred_spec, {}, bars, allowed_dates=[DAY])

    # 3. Unsupported stop loss mode raises UnsupportedStrategyRuleError
    bad_sl_spec = _make_test_strategy_spec("bad-sl-spec")
    object.__setattr__(bad_sl_spec.rules_v2.stop_loss, "mode", "unsupported_trailing_parabolic_sar")
    with pytest.raises(wf.UnsupportedStrategyRuleError, match="UNSUPPORTED_STOP_LOSS_MODE"):
        gen.generate_signals(bad_sl_spec, {}, bars, allowed_dates=[DAY])

    # 4. Unsupported target mode raises UnsupportedStrategyRuleError
    bad_tgt_spec = _make_test_strategy_spec("bad-tgt-spec")
    object.__setattr__(bad_tgt_spec.rules_v2.target, "mode", "unsupported_moon_phase_target")
    with pytest.raises(wf.UnsupportedStrategyRuleError, match="UNSUPPORTED_TARGET_MODE"):
        gen.generate_signals(bad_tgt_spec, {}, bars, allowed_dates=[DAY])


# -------------------------------------------------------------------------------------------------
# 8. FROZEN PDC V0 INTEGRITY PRESERVED
# -------------------------------------------------------------------------------------------------


def test_frozen_pdc_v0_integrity_preserved():
    """Verify all 5 frozen benchmark and rule files retain their exact pinned SHA-256 digests."""
    backend_root = Path(__file__).resolve().parent.parent

    for rel_path, expected_digest in RULE_SOURCE_HASHES.items():
        full_path = backend_root / rel_path
        assert full_path.is_file(), f"Frozen file missing: {full_path}"
        actual_digest = hashlib.sha256(full_path.read_bytes()).hexdigest()
        assert actual_digest == expected_digest, f"Digest mismatch on frozen file {rel_path}"


# -------------------------------------------------------------------------------------------------
# 9. ADVERSARIAL: ENTRY CUTOFF STRICTLY BEFORE EARLIER OF STRATEGY AND 15:10 IST
# -------------------------------------------------------------------------------------------------


def _make_body_ratio_strategy_spec(
    strategy_id: str = "body-ratio-spec",
    entry_cutoff: str = "15:10",
    stop_loss_points: float = 20.0,
    target_r_multiple: float = 2.0,
    primary_timeframe: str = "5m",
    body_ratio_min: float = 0.50,
) -> NormalizedStrategySpec:
    md = f"""---
name: {strategy_id}
rules_v2:
  strategy_id: {strategy_id}
  strategy_version: v1
  market: nse_options
  timeframes:
    primary: {primary_timeframe}
  instrument:
    underlying: NIFTY
    structure: long_call
    strike_offset: 0
    lot_size: 75
  intraday_entry:
    - kind: candle_body_ratio
      timeframe: 5m
      direction: LONG
      params:
        body_ratio_min: {body_ratio_min}
      requires_closed_bar: true
  stop_loss:
    mode: index_points
    value: {stop_loss_points}
    min_points: 10.0
    max_points: 100.0
  target:
    mode: r_multiple
    value: {target_r_multiple}
  risk:
    max_lots: 1
    max_daily_loss_pct: 2.0
    max_open_positions: 1
  session_window:
    timezone: Asia/Kolkata
    entry_start: "09:15"
    entry_cutoff: "{entry_cutoff}"
    square_off: "15:25"
  parameters:
    body_ratio_min:
      name: body_ratio_min
      type: float
      value: {body_ratio_min}
      min_value: 0.10
      max_value: 0.90
    stop_loss_points:
      name: stop_loss_points
      type: float
      value: {stop_loss_points}
      min_value: 10.0
      max_value: 100.0
    target_r_multiple:
      name: target_r_multiple
      type: float
      value: {target_r_multiple}
      min_value: 1.0
      max_value: 5.0
---
# Thesis
Body ratio strategy.

## Gaps
"""
    return normalize_markdown(md, strategy_id=strategy_id, strategy_version="v1")


def test_adversarial_entry_cutoff_strictly_before_cutoff():
    """Verify entry decisions are enforced strictly before min(cutoff, 15:10) on decision timestamps."""
    gen = wf.RuleBasedCausalSignalGenerator()

    def _single_candle(day: str, time_str: str, base_price: float = 24000.0) -> dict[str, Any]:
        dt = datetime.fromisoformat(f"{day}T{time_str}:00+05:30")
        return {
            "timestamp": dt.isoformat(),
            "open": base_price,
            "high": base_price + 10.0,
            "low": base_price - 2.0,
            "close": base_price + 8.0,
            "volume": 1000.0,
            "underlying": "NIFTY",
        }

    # 1. Standard session with cutoff 15:10
    spec = _make_body_ratio_strategy_spec("cutoff-test-1510", entry_cutoff="15:10")
    params = {"body_ratio_min": 0.50, "stop_loss_points": 20.0, "target_r_multiple": 2.0, "max_lots": 1}

    # Candle A: 15:00-15:05 (decision time 15:05) -> strictly before 15:10 -> ACCEPTED
    bars_before = [_single_candle(DAY, "15:00"), _single_candle(DAY, "15:05"), _single_candle(DAY, "15:10")]
    evs_before = gen.generate_signals(spec, params, bars_before, allowed_dates=[DAY])
    assert len(evs_before) >= 1
    assert "15:05" in evs_before[0].decision_time

    # Candle B: 15:05-15:10 (decision time 15:10) -> exactly at cutoff 15:10 -> REJECTED
    bars_at = [_single_candle(DAY, "15:05"), _single_candle(DAY, "15:10"), _single_candle(DAY, "15:15")]
    evs_at = gen.generate_signals(spec, params, bars_at, allowed_dates=[DAY])
    # Must NOT enter at 15:10 decision time
    assert not any("15:10" in e.decision_time for e in evs_at)

    # Candle C: 15:10-15:15 (decision time 15:15) -> after cutoff 15:10 -> REJECTED
    bars_after = [_single_candle(DAY, "15:10"), _single_candle(DAY, "15:15"), _single_candle(DAY, "15:20")]
    evs_after = gen.generate_signals(spec, params, bars_after, allowed_dates=[DAY])
    assert len(evs_after) == 0

    # 2. Early strategy cutoff: 14:30
    spec_early = _make_body_ratio_strategy_spec("early-cutoff-spec", entry_cutoff="14:30")
    # Decision at 14:25 (14:20-14:25 bar) -> strictly before 14:30 -> ACCEPTED
    bars_1420 = [_single_candle(DAY, "14:20"), _single_candle(DAY, "14:25"), _single_candle(DAY, "14:30")]
    evs_1425 = gen.generate_signals(spec_early, params, bars_1420, allowed_dates=[DAY])
    assert len(evs_1425) >= 1
    assert "14:25" in evs_1425[0].decision_time

    # Decision at 14:30 (14:25-14:30 bar) -> exactly at cutoff 14:30 -> REJECTED
    bars_1425 = [_single_candle(DAY, "14:25"), _single_candle(DAY, "14:30"), _single_candle(DAY, "14:35")]
    evs_1430 = gen.generate_signals(spec_early, params, bars_1425, allowed_dates=[DAY])
    assert not any("14:30" in e.decision_time for e in evs_1430)

    # Decision at 14:35 (14:30-14:35 bar) -> after cutoff 14:30 -> REJECTED
    bars_1430 = [_single_candle(DAY, "14:30"), _single_candle(DAY, "14:35"), _single_candle(DAY, "14:40")]
    evs_1435 = gen.generate_signals(spec_early, params, bars_1430, allowed_dates=[DAY])
    assert len(evs_1435) == 0


# -------------------------------------------------------------------------------------------------
# 10. ADVERSARIAL: RISK ENFORCEMENT & PAPER-RISK-V2 INTEGRATION
# -------------------------------------------------------------------------------------------------


def test_adversarial_risk_enforcement_paper_risk_v2():
    """Verify Tier 1 rejects unverifiable stop risk, enforces sizing and daily exposure limits across replayed trades."""
    # 1. Tier 1 rejects unverifiable stop risk explicitly when delta is missing or stale
    ev = wf.CandidateSignalEvent(
        event_id="test-ev-no-delta",
        market_day=DAY,
        decision_time=f"{DAY}T10:00:00+05:30",
        label_end_time=f"{DAY}T10:30:00+05:30",
        underlying="NIFTY",
        direction="CALL",
        entry_spot=24000.0,
        stop_spot=23975.0,
        target_spot=24050.0,
        exit_spot=24050.0,
        strike=24000.0,
        option_type="CE",
        expiry=DAY,
        lot_size=75,
        lots=1,
        entry_quote=ContractQuote(
            tradingsymbol="NIFTY24000CE",
            provider_instrument_id="NSE_24000_CE",
            underlying="NIFTY",
            strike=24000.0,
            option_type="CE",
            expiry=DAY,
            lot_size=75,
            ltp=Decimal("100.0"),
            bid=Decimal("99.0"),
            ask=Decimal("101.0"),
            quote_time=datetime.fromisoformat(f"{DAY}T10:00:00+05:30"),
            fetched_at=datetime.fromisoformat(f"{DAY}T10:00:00+05:30"),
            source="nse_historical",
            delta=None,  # MISSING DELTA!
        ),
        exit_quote=ContractQuote(
            tradingsymbol="NIFTY24000CE",
            provider_instrument_id="NSE_24000_CE",
            underlying="NIFTY",
            strike=24000.0,
            option_type="CE",
            expiry=DAY,
            lot_size=75,
            ltp=Decimal("120.0"),
            bid=Decimal("119.0"),
            ask=Decimal("121.0"),
            quote_time=datetime.fromisoformat(f"{DAY}T10:30:00+05:30"),
            fetched_at=datetime.fromisoformat(f"{DAY}T10:30:00+05:30"),
            source="nse_historical",
            delta=0.60,
            delta_observed_at=datetime.fromisoformat(f"{DAY}T10:30:00+05:30"),
            delta_source="nse_historical",
            delta_contract="NIFTY24000CE",
        ),
    )

    cfg = _wf_cfg(fidelity_tier="TIER_1_VERIFIED")
    with pytest.raises(wf.MissingOptionDataError, match="STOP_RISK_UNVERIFIABLE|UNVERIFIABLE_STOP_RISK"):
        wf.simulate_option_execution(ev, config=cfg, fail_closed_on_missing=True)

    unpriced_exec = wf.simulate_option_execution(ev, config=cfg, fail_closed_on_missing=False)
    assert unpriced_exec.priced is False
    assert "STOP_RISK_UNVERIFIABLE" in (unpriced_exec.rejection_reason or "")

    # 2. Replay risk enforcement across multiple trades on same day:
    # Trade 1 has a loss of Rs 4,500.
    # Trade 2 on same day would exceed Rs 5,000 daily exposure limit -> REJECTED with DAILY_EXPOSURE_LIMIT!
    cal = _trading_calendar(count=85)
    spec = _make_test_strategy_spec("risk-replay-spec")
    fold0 = wf.generate_walk_forward_folds(cal)[0]
    oos_day = fold0.oos_dates[1]

    # Build 2 sequential trades on oos_day:
    # Quote with verified delta
    q_entry1 = ContractQuote(
        tradingsymbol="NIFTY24000CE",
        provider_instrument_id="NSE_24000_CE",
        underlying="NIFTY",
        strike=24000.0,
        option_type="CE",
        expiry=oos_day,
        lot_size=75,
        ltp=Decimal("60.0"),
        bid=Decimal("59.5"),
        ask=Decimal("60.5"),
        quote_time=datetime.fromisoformat(f"{oos_day}T09:30:00+05:30"),
        fetched_at=datetime.fromisoformat(f"{oos_day}T09:30:00+05:30"),
        source="nse_historical",
        delta=0.50,
        delta_observed_at=datetime.fromisoformat(f"{oos_day}T09:30:00+05:30"),
        delta_source="nse_historical",
        delta_contract="NIFTY24000CE",
    )
    # Stop exit fill: drops to 5.0 (loss of ~4,200)
    q_exit1 = ContractQuote(
        tradingsymbol="NIFTY24000CE",
        provider_instrument_id="NSE_24000_CE",
        underlying="NIFTY",
        strike=24000.0,
        option_type="CE",
        expiry=oos_day,
        lot_size=75,
        ltp=Decimal("5.0"),
        bid=Decimal("4.95"),
        ask=Decimal("5.05"),
        quote_time=datetime.fromisoformat(f"{oos_day}T10:00:00+05:30"),
        fetched_at=datetime.fromisoformat(f"{oos_day}T10:00:00+05:30"),
        source="nse_historical",
        delta=0.05,
        delta_observed_at=datetime.fromisoformat(f"{oos_day}T10:00:00+05:30"),
        delta_source="nse_historical",
        delta_contract="NIFTY24000CE",
    )
    ev1 = wf.CandidateSignalEvent(
        event_id="loss-trade-1",
        market_day=oos_day,
        decision_time=f"{oos_day}T09:30:00+05:30",
        label_end_time=f"{oos_day}T10:00:00+05:30",
        underlying="NIFTY",
        direction="CALL",
        entry_spot=24000.0,
        stop_spot=23980.0,
        target_spot=24060.0,
        exit_spot=23980.0,
        strike=24000.0,
        option_type="CE",
        expiry=oos_day,
        lot_size=75,
        lots=1,
        entry_quote=q_entry1,
        exit_quote=q_exit1,
    )

    # Trade 2 on same day at 10:30:
    q_entry2 = ContractQuote(
        tradingsymbol="NIFTY24000CE",
        provider_instrument_id="NSE_24000_CE",
        underlying="NIFTY",
        strike=24000.0,
        option_type="CE",
        expiry=oos_day,
        lot_size=75,
        ltp=Decimal("50.0"),
        bid=Decimal("49.5"),
        ask=Decimal("50.5"),
        quote_time=datetime.fromisoformat(f"{oos_day}T10:30:00+05:30"),
        fetched_at=datetime.fromisoformat(f"{oos_day}T10:30:00+05:30"),
        source="nse_historical",
        delta=0.50,
        delta_observed_at=datetime.fromisoformat(f"{oos_day}T10:30:00+05:30"),
        delta_source="nse_historical",
        delta_contract="NIFTY24000CE",
    )
    q_exit2 = ContractQuote(
        tradingsymbol="NIFTY24000CE",
        provider_instrument_id="NSE_24000_CE",
        underlying="NIFTY",
        strike=24000.0,
        option_type="CE",
        expiry=oos_day,
        lot_size=75,
        ltp=Decimal("60.0"),
        bid=Decimal("59.5"),
        ask=Decimal("60.5"),
        quote_time=datetime.fromisoformat(f"{oos_day}T11:00:00+05:30"),
        fetched_at=datetime.fromisoformat(f"{oos_day}T11:00:00+05:30"),
        source="nse_historical",
        delta=0.60,
        delta_observed_at=datetime.fromisoformat(f"{oos_day}T11:00:00+05:30"),
        delta_source="nse_historical",
        delta_contract="NIFTY24000CE",
    )
    ev2 = wf.CandidateSignalEvent(
        event_id="trade-2-exceeds-daily",
        market_day=oos_day,
        decision_time=f"{oos_day}T10:30:00+05:30",
        label_end_time=f"{oos_day}T11:00:00+05:30",
        underlying="NIFTY",
        direction="CALL",
        entry_spot=24000.0,
        stop_spot=23980.0,
        target_spot=24060.0,
        exit_spot=24060.0,
        strike=24000.0,
        option_type="CE",
        expiry=oos_day,
        lot_size=75,
        lots=1,
        entry_quote=q_entry2,
        exit_quote=q_exit2,
    )

    rep = wf.run_walk_forward_validation(
        spec,
        trading_dates=cal,
        events=[ev1, ev2],
        config=cfg,
        fail_closed_on_missing_option_data=False,
    )
    oos_execs = rep.folds[0].oos_executions
    assert len(oos_execs) == 2
    assert oos_execs[0].priced is True
    assert oos_execs[0].net_pnl_inr < -4000.0
    # Trade 2 must be rejected by daily exposure limit
    assert oos_execs[1].priced is False
    assert oos_execs[1].rejection_reason in ("DAILY_EXPOSURE_LIMIT", "INSUFFICIENT_CAPITAL")


# -------------------------------------------------------------------------------------------------
# 11. ADVERSARIAL: UNSUPPORTED RULE SUBSTITUTION REJECTED
# -------------------------------------------------------------------------------------------------


def test_adversarial_unsupported_rule_substitution():
    """Verify pct_of_debit, pct_of_capital, unhandled partials, and non-5m timeframes are rejected rather than silently substituted."""
    gen = wf.RuleBasedCausalSignalGenerator()
    bars = _make_session_candles(DAY, pattern="trending_up", base_price=24000.0)

    # 1. pct_of_debit stop loss without bar option quotes must raise UnsupportedStrategyRuleError
    spec_debit = _make_test_strategy_spec("debit-sl-spec")
    object.__setattr__(spec_debit.rules_v2.stop_loss, "mode", "pct_of_debit")
    with pytest.raises(wf.UnsupportedStrategyRuleError, match="UNSUPPORTED_STOP_LOSS_MODE"):
        gen.generate_signals(spec_debit, {}, bars, allowed_dates=[DAY])

    # 2. pct_of_capital stop loss without bar option quotes must raise UnsupportedStrategyRuleError
    spec_cap = _make_test_strategy_spec("cap-sl-spec")
    object.__setattr__(spec_cap.rules_v2.stop_loss, "mode", "pct_of_capital")
    with pytest.raises(wf.UnsupportedStrategyRuleError, match="UNSUPPORTED_STOP_LOSS_MODE"):
        gen.generate_signals(spec_cap, {}, bars, allowed_dates=[DAY])

    # 3. Non-empty partial_exits must raise UnsupportedStrategyRuleError rather than being silently omitted
    spec_partial = _make_test_strategy_spec("partial-spec")
    spec_partial.rules_v2.partial_exits.append(
        wf.PartialExitRule(at_r_multiple=1.0, book_fraction=0.5, move_stop_to="entry")
    )
    with pytest.raises(wf.UnsupportedStrategyRuleError, match="UNSUPPORTED_PARTIAL_EXITS"):
        gen.generate_signals(spec_partial, {}, bars, allowed_dates=[DAY])

    # 4. Primary timeframe not '5m' must raise UnsupportedStrategyRuleError
    spec_15m = _make_test_strategy_spec("15m-spec")
    object.__setattr__(spec_15m.rules_v2.timeframes, "primary", "15m")
    with pytest.raises(wf.UnsupportedStrategyRuleError, match="UNSUPPORTED_TIMEFRAME"):
        gen.generate_signals(spec_15m, {}, bars, allowed_dates=[DAY])

    # 5. Session blackout windows must be enforced (no entry during blackout window)
    spec_blackout = _make_body_ratio_strategy_spec("blackout-spec")
    object.__setattr__(spec_blackout.rules_v2.session_window, "blackout_windows", [("10:00", "12:00")])
    evs_blackout = gen.generate_signals(
        spec_blackout,
        {"body_ratio_min": 0.50, "stop_loss_points": 20.0, "target_r_multiple": 2.0, "max_lots": 1},
        bars,
        allowed_dates=[DAY],
    )
    for e in evs_blackout:
        dec_t = datetime.fromisoformat(e.decision_time).strftime("%H:%M")
        assert not ("10:00" <= dec_t < "12:00"), f"Entry occurred inside blackout window at {dec_t}"


# -------------------------------------------------------------------------------------------------
# 12. ADVERSARIAL: ENTRY PREDICATE COMPOSITION (AND / CONFLICT / MISSING HANDLER)
# -------------------------------------------------------------------------------------------------


def test_adversarial_entry_predicate_composition():
    """Verify conjunctive composition, conflict rejection, and unavailable custom handler rejection."""
    gen = wf.RuleBasedCausalSignalGenerator()
    bars = _make_session_candles(DAY, pattern="trending_up", base_price=24000.0)

    # 1. Conjunctive AND: When 2 predicates are required, if only 1 passes, NO entry should fire
    spec_two_pred = _make_body_ratio_strategy_spec("two-pred-spec")
    # Predicate 1 is candle_body_ratio (passes on trending_up)
    # Predicate 2: vwap_relation with direction SHORT (close is above VWAP on trending_up, so SHORT fails!)
    spec_two_pred.rules_v2.entry_composition = "ALL"
    spec_two_pred.rules_v2.intraday_entry.append(
        IntradayEntryPredicate(
            kind="vwap_relation",
            timeframe="5m",
            direction="SHORT",
            requires_closed_bar=True,
        )
    )
    evs_conj = gen.generate_signals(
        spec_two_pred,
        {"body_ratio_min": 0.50, "stop_loss_points": 20.0, "target_r_multiple": 2.0, "max_lots": 1},
        bars,
        allowed_dates=[DAY],
    )
    assert len(evs_conj) == 0, "Conjunctive composition failed: entered when only 1 of 2 predicates passed!"

    # 2. Conflicting conditions: One predicate says CALL, the other says PUT -> NO entry
    # Suppose both candle_body_ratio (LONG) and an opposing condition are satisfied or conflicting
    spec_conflict = _make_body_ratio_strategy_spec("conflict-pred-spec")
    # Add a second predicate configured to produce PUT on the same bar
    # (e.g. custom predicate returning PUT, or conflicting direction)
    spec_conflict.rules_v2.entry_composition = "ALL"
    spec_conflict.rules_v2.intraday_entry.append(
        IntradayEntryPredicate(
            kind="custom_predicate",
            timeframe="5m",
            direction="SHORT",
            params={"handler_name": "always_put"},
            requires_closed_bar=True,
        )
    )
    evs_conflict = gen.generate_signals(
        spec_conflict,
        {
            "body_ratio_min": 0.50,
            "stop_loss_points": 20.0,
            "target_r_multiple": 2.0,
            "max_lots": 1,
            "custom_handlers": {"always_put": lambda bars, p: "PUT"},
        },
        bars,
        allowed_dates=[DAY],
    )
    assert len(evs_conflict) == 0, "Conflicting predicates must not produce an entry!"

    # 3. Custom predicate with unavailable/non-callable handler raises UnsupportedStrategyRuleError
    spec_no_handler = _make_body_ratio_strategy_spec("no-handler-spec")
    spec_no_handler.rules_v2.entry_composition = "ALL"
    spec_no_handler.rules_v2.intraday_entry.append(
        IntradayEntryPredicate(
            kind="custom_predicate",
            timeframe="5m",
            direction="LONG",
            params={"handler_name": "missing_handler"},
            requires_closed_bar=True,
        )
    )
    with pytest.raises(wf.UnsupportedStrategyRuleError, match="UNAVAILABLE_CUSTOM_PREDICATE_HANDLER"):
        gen.generate_signals(spec_no_handler, {}, bars, allowed_dates=[DAY])


# -------------------------------------------------------------------------------------------------
# 13. ADVERSARIAL: END-OF-DATA & CONTRACT INTEGRITY
# -------------------------------------------------------------------------------------------------


def test_adversarial_end_of_data_and_contract_integrity():
    """Verify end-of-data does not fabricate completed exits, and guessed strikes are not treated as verified contracts."""
    gen = wf.RuleBasedCausalSignalGenerator()

    # 1. Truncate bars mid-day at 13:00 (before square_off at 15:25) on trending_up
    bars_full = _make_session_candles(DAY, pattern="trending_up", base_price=24000.0)
    bars_truncated = [b for b in bars_full if b["timestamp"] <= f"{DAY}T13:00:00+05:30"]

    spec = _make_body_ratio_strategy_spec("end-of-data-spec", stop_loss_points=95.0, target_r_multiple=4.5, body_ratio_min=0.40)
    # Stop and target are so far that neither will trigger before 13:00; body_ratio_min=0.40 triggers in trending_up
    evs = gen.generate_signals(
        spec,
        {"body_ratio_min": 0.40, "stop_loss_points": 95.0, "target_r_multiple": 4.5, "max_lots": 1},
        bars_truncated,
        allowed_dates=[DAY],
    )
    assert len(evs) >= 1
    for ev in evs:
        # Must be marked incomplete_label = True and NOT have exit_reason="INTRADAY_CLOSE"
        assert ev.incomplete_label is True, f"Event {ev.event_id} was fabricated as a completed exit!"
        assert ev.label_end_time is None or ev.exit_reason == "MISSING_FUTURE_BARS"


def test_ema_and_body_ratio_replay_as_different_strategies():
    """Two entry predicates share the generator and keep distinct, repeatable signals."""
    candles = _make_session_candles(DAY, pattern="trending_up")
    ema = _make_test_strategy_spec("replay-ema-cross", ema_period=10)
    body = _make_body_ratio_strategy_spec("replay-body-ratio", body_ratio_min=0.40)
    gen = wf.RuleBasedCausalSignalGenerator()
    ema_once = gen.generate_signals(ema, {"ema_period": 10}, candles, allowed_dates=[DAY])
    ema_twice = gen.generate_signals(ema, {"ema_period": 10}, candles, allowed_dates=[DAY])
    body_events = gen.generate_signals(body, {"body_ratio_min": 0.40}, candles, allowed_dates=[DAY])
    ema_keys = [(e.decision_time, round(e.entry_spot, 2), e.direction) for e in ema_once]
    body_keys = [(e.decision_time, round(e.entry_spot, 2), e.direction) for e in body_events]
    assert ema_once and body_events
    assert [e.event_id for e in ema_once] == [e.event_id for e in ema_twice]
    assert ema_keys != body_keys
    assert ema.rules_v2.identity_hash() != body.rules_v2.identity_hash()
    assert all(e.event_id.startswith("causal-replay-ema-cross-") for e in ema_once)
    assert all(e.event_id.startswith("causal-replay-body-ratio-") for e in body_events)



@pytest.mark.parametrize("frozen", [False, True])
def test_report_provenance_uses_bound_normalizer_version(frozen):
    spec = normalize_pdc_15ema_v0_benchmark() if frozen else _make_test_strategy_spec()
    expected = "strategy-normalizer-v2" if frozen else "strategy-normalizer-v3"
    binding = spec.validation_binding()
    dates = _trading_calendar()
    report = wf.run_walk_forward_validation(
        spec, trading_dates=dates, events=[],
        config=_wf_cfg(fidelity_tier="TIER_2_SYNTHETIC"),
        fail_closed_on_missing_option_data=False,
    )
    assert report.has_valid_oos_folds
    assert report.normalizer_version == expected
    assert report.data_fidelity_and_provenance["normalizer_version"] == expected
    assert report.data_fidelity_and_provenance["validation_binding"] == binding
    assert spec.validation_binding() == binding
