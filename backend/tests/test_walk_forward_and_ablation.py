"""Stage B Phase B3 Verification Suite:
Deterministic Walk-Forward Engine, Tiered Options Execution Simulation,
Recency & Regime Four-Arm Ablation, and Pre-Implementation Quarantine/State Verification.
"""

from __future__ import annotations

import ast
import copy
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.config import get_settings
from app.db import crew_journal as cj
from app.db import crew_session as cs
from app.db import paper_ledger as pl
from app.live import approvals
from app.provenance import report_inputs
from app.reports.store import ReportStore
from app.strategies.normalizer import (
    NORMALIZER_VERSION,
    normalize_markdown,
    normalize_pdc_15ema_v0_benchmark,
    normalize_scanner_v0_family,
    normalize_strategy_document,
)
from app.strategies.schema import parse_strategy_markdown
from app.strategies.store import StrategyStore
from app.trading import admission as adm
from app.trading import paper_fees
from app.trading import paper_registry as reg
from app.trading import paper_risk as pr
from app.trading import recency as rc
from app.trading import walk_forward as wf
from app.trading.option_quote import IST, ContractQuote
from tests.conftest import APPROVABLE_MARKDOWN
from tests.test_paper_trading import CAPITAL, SETTINGS, quote
from tests.test_research_runtime import DAY, Clock, bars


def _trading_calendar(start: date = date(2025, 1, 1), count: int = 135) -> list[str]:
    """Generate `count` chronological weekday trading dates within the verified 2025 fee window."""
    out: list[str] = []
    cur = start
    while len(out) < count:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def _wf_cfg(**kwargs) -> wf.WalkForwardConfig:
    """WalkForwardConfig helper with historical brokerage assumption enabled for 2025 test calendar."""
    kwargs.setdefault("allow_historical_brokerage_assumption", True)
    kwargs.setdefault("trusted_delta_sources", frozenset({"nse_historical_book"}))
    return wf.WalkForwardConfig(**kwargs)


def _make_verified_quote(
    day_str: str,
    *,
    time_str: str = "10:00:00",
    bid: float = 40.0,
    ask: float = 40.4,
    strike: float = 24000.0,
    option_type: str = "CE",
    expiry: str = "2026-12-24",
    lot_size: int = 75,
    source: str = "nse_historical_book",
) -> ContractQuote:
    ts = datetime.fromisoformat(f"{day_str}T{time_str}+05:30")
    return ContractQuote(
        underlying="NIFTY",
        expiry=expiry,
        strike=strike,
        option_type=option_type,
        provider_instrument_id=f"NSE_{strike:.0f}_{option_type}",
        tradingsymbol=f"NIFTY{strike:.0f}{option_type}",
        lot_size=lot_size,
        ltp=Decimal(str(round((bid + ask) / 2.0, 2))),
        bid=Decimal(str(bid)),
        ask=Decimal(str(ask)),
        quote_time=ts,
        fetched_at=ts,
        source=source,
        delta=0.50,
        delta_observed_at=ts,
        delta_source=source,
        delta_contract=f"NIFTY{strike:.0f}{option_type}",
    )


def _make_candidate_event(
    idx: int,
    day_str: str,
    *,
    is_winner: bool = True,
    regime: str = "TREND",
    entry_iv: float = 0.16,
    exit_iv: float = 0.16,
    dte_days: float = 7.0,
    label_end_day: str | None = None,
    incomplete_label: bool = False,
    decision_clock: str = "10:00:00",
    exit_clock: str = "10:45:00",
    with_verified_quotes: bool = True,
    quote_source: str = "nse_historical_book",
    features: tuple = (),
    bars_seq: tuple = (),
    metadata: dict | None = None,
) -> wf.CandidateSignalEvent:
    end_day = label_end_day or day_str
    dec_ts = f"{day_str}T{decision_clock}+05:30"
    end_ts = None if incomplete_label else f"{end_day}T{exit_clock}+05:30"
    entry_spot = 24000.0
    stop_spot = 23980.0
    target_spot = 24040.0
    exit_spot = 24045.0 if is_winner else 23978.0

    entry_q = (
        _make_verified_quote(
            day_str,
            time_str=decision_clock,
            bid=40.0,
            ask=40.4,
            source=quote_source,
        )
        if with_verified_quotes
        else None
    )
    exit_q = (
        _make_verified_quote(
            end_day,
            time_str=exit_clock,
            bid=52.0 if is_winner else 32.0,
            ask=52.4 if is_winner else 32.4,
            source=quote_source,
        )
        if with_verified_quotes
        else None
    )
    return wf.CandidateSignalEvent(
        event_id=f"EV_{idx:04d}_{day_str}",
        market_day=day_str,
        decision_time=dec_ts,
        label_end_time=end_ts,
        underlying="NIFTY",
        direction="CALL",
        entry_spot=entry_spot,
        stop_spot=stop_spot,
        target_spot=target_spot,
        exit_spot=exit_spot,
        strike=24000.0,
        option_type="CE",
        expiry="2026-12-24",
        lot_size=75,
        lots=1,
        regime=regime,
        entry_quote=entry_q,
        exit_quote=exit_q,
        entry_iv=entry_iv,
        exit_iv=exit_iv,
        dte_days=dte_days,
        holding_minutes=45.0,
        features=features,
        bars=bars_seq,
        incomplete_label=incomplete_label,
        metadata=metadata or {},
    )


# -------------------------------------------------------------------------------------------------
# PRE-IMPLEMENTATION VERIFICATION & REQUIREMENT 12/13:
# Paper-admission quarantine enforcement, distinct strategy lifecycle states, provenance binding,
# and frozen V0 compatibility.
# -------------------------------------------------------------------------------------------------


def test_b3_precheck_direct_paper_ledger_cannot_bypass_quarantine(tmp_path: Path):
    """Verify no direct `paper_ledger.enter` or `check_paper_admission_eligibility` path can bypass quarantine."""
    db = tmp_path / "b3_quarantine_ledger.db"
    cj.migrate(db)
    clock = Clock("09:45")
    lease = cs.acquire(db, now=clock())
    pl.ensure_accounts(db, lease=lease, capital=CAPITAL, now=clock())

    q_quote = quote(clock(), bid=40.0, ask=40.4)
    cand = adm.SignalCandidate(
        market_day=DAY,
        instrument="NIFTY",
        strategy_id="Liquidity Sweep [5M]",
        strategy_version="scanner-v0",
        signal_candle=clock().isoformat(),
        direction="CALL",
        setup_id="5m|24000.0",
    )
    evaluated = adm.evaluate(cand, q_quote, now=clock(), settings=SETTINGS)
    persisted = adm.persist(db, evaluated, lease=lease, now=clock())
    assert persisted is not None
    opened = adm.open_research_trade(
        db,
        persisted,
        lease=lease,
        model_lots=2,
        entry_spot=24000.0,
        stop_spot=23980.0,
        target_spot=24040.0,
        execution_policy="scanner-v0-hop-v1",
        now=clock(),
    )

    quarantined_md = APPROVABLE_MARKDOWN.replace(
        "## Gaps\n",
        "## Gaps\n- Assumed, not in the source: stop loss defaulted by extractor.\n",
    )
    quarantined_doc = parse_strategy_markdown(quarantined_md)

    # 1. Direct `pl.enter` with a quarantined StrategyDocument spec is rejected even though strategy_id is registered!
    decisions = pl.enter(
        db,
        lease=lease,
        admission=persisted["admission"],
        model_trade=opened["trade"],
        timeframe="5m",
        quote=q_quote,
        df_5m=bars((24000.0, 24005.0, 24010.0), start="09:30"),
        now=clock(),
        settings=SETTINGS,
        cutoff="15:10",
        delta_evidence=None,
        trusted_delta_sources=frozenset({"computed_bsm"}),
        spec=quarantined_doc,
    )
    assert all(d["outcome"] == "REJECTED" for d in decisions)
    assert all("STRATEGY_QUARANTINED" in d["reasons"] for d in decisions)

    # 2. Check all spec representations at `check_paper_admission_eligibility`
    ok_md, r_md = reg.check_paper_admission_eligibility("Liquidity Sweep [5M]", spec=quarantined_md)
    assert ok_md is False and "STRATEGY_QUARANTINED" in r_md

    ok_dict, r_dict = reg.check_paper_admission_eligibility(
        "Liquidity Sweep [5M]",
        spec={"fidelity_tier": "TIER_2_SYNTHETIC", "paper_promotion_eligible": False},
    )
    assert ok_dict is False and "SYNTHETIC_TIER2_NOT_ELIGIBLE_FOR_PAPER" in r_dict

    ok_unknown, r_unknown = reg.check_paper_admission_eligibility("Liquidity Sweep [5M]", spec=object())
    assert ok_unknown is False and "STRATEGY_QUARANTINED" in r_unknown

    ok_pdc, r_pdc = reg.check_paper_admission_eligibility("PDC_15EMA_V0")
    assert ok_pdc is False and any(x.startswith("STRATEGY_DISABLED:") for x in r_pdc)


def test_b3_precheck_distinct_strategy_states_and_provenance_binding():
    """Verify structurally complete, backtest-eligible, independently validated, and paper-approved states are distinct,
    and source provenance, normalizer version, rules hash, and parameter hash are independently bound."""
    # State 1: Quarantined (incomplete rules / unresolved gap)
    q_md = APPROVABLE_MARKDOWN.replace("## Gaps\n", "## Gaps\n- What stop loss should be used?\n")
    s_quarantined = normalize_markdown(q_md, strategy_id="s-state", strategy_version="v1", strict_gaps=True)
    assert s_quarantined.structurally_complete is False
    assert s_quarantined.backtest_eligible is False
    assert s_quarantined.independently_validated is False
    assert s_quarantined.paper_approved is False
    assert s_quarantined.strategy_state == "QUARANTINED"

    # State 2: Structurally complete & backtest-eligible, not yet independently validated
    s_backtest = normalize_markdown(
        APPROVABLE_MARKDOWN,
        strategy_id="s-state",
        strategy_version="v1",
        source_sha256="prov-sha-123",
        has_validated_report=False,
    )
    assert s_backtest.structurally_complete is True
    assert s_backtest.backtest_eligible is True
    assert s_backtest.independently_validated is False
    assert s_backtest.paper_approved is False
    assert s_backtest.strategy_state == "BACKTEST_ELIGIBLE"

    # State 2b: Validated only with Tier 2 synthetic simulation -> stays BACKTEST_ELIGIBLE, not independently validated
    s_tier2 = normalize_markdown(
        APPROVABLE_MARKDOWN,
        strategy_id="s-state",
        strategy_version="v1",
        source_sha256="prov-sha-123",
        has_validated_report=True,
        validation_fidelity_tier="TIER_2_SYNTHETIC",
    )
    assert s_tier2.structurally_complete is True
    assert s_tier2.backtest_eligible is True
    assert s_tier2.independently_validated is False
    assert s_tier2.paper_eligible is False
    assert s_tier2.promotable is False
    assert s_tier2.paper_approved is False
    assert s_tier2.strategy_state == "BACKTEST_ELIGIBLE"

    # State 3: Independently validated (Tier 1 verified report), not yet paper-approved
    s_validated = normalize_markdown(
        APPROVABLE_MARKDOWN,
        strategy_id="s-state",
        strategy_version="v1",
        source_sha256="prov-sha-123",
        has_validated_report=True,
        validation_fidelity_tier="TIER_1_VERIFIED",
        paper_approval_granted=False,
    )
    assert s_validated.structurally_complete is True
    assert s_validated.backtest_eligible is True
    assert s_validated.independently_validated is True
    assert s_validated.paper_approved is False
    assert s_validated.strategy_state == "INDEPENDENTLY_VALIDATED"

    # State 4: Paper-approved
    s_approved = normalize_markdown(
        APPROVABLE_MARKDOWN,
        strategy_id="s-state",
        strategy_version="v1",
        source_sha256="prov-sha-123",
        has_validated_report=True,
        validation_fidelity_tier="TIER_1_VERIFIED",
        paper_approval_granted=True,
    )
    assert s_approved.structurally_complete is True
    assert s_approved.backtest_eligible is True
    assert s_approved.independently_validated is True
    assert s_approved.paper_approved is True
    assert s_approved.strategy_state == "PAPER_APPROVED"

    # Verify independent provenance, normalizer version, rules hash, and parameter hash binding
    binding = s_validated.validation_binding()
    assert binding["normalizer_version"] == NORMALIZER_VERSION
    assert binding["source_provenance"]["source_sha256"] == "prov-sha-123"
    assert binding["provenance_sha256"] == s_validated.provenance_sha256
    assert binding["rules_sha256"] == s_validated.rules_sha256
    assert binding["rules_v2_sha256"] == s_validated.rules_v2_sha256
    assert binding["parameter_hash"] == s_validated.parameter_hash
    assert len(binding["binding_sha256"]) == 64

    # Verify report_inputs also binds provenance, normalizer version, rules hash, and parameter hash
    doc = parse_strategy_markdown(APPROVABLE_MARKDOWN)
    doc_norm = normalize_strategy_document(doc, strategy_version="v1")
    rep_in = report_inputs(get_settings(), doc, 1, s_validated.content_sha256)
    assert rep_in["normalizer_version"] == NORMALIZER_VERSION
    assert rep_in["rules_sha256"] == doc_norm.rules_sha256
    assert rep_in["rules_v2_sha256"] == doc_norm.rules_v2_sha256
    assert rep_in["parameter_hash"] == doc_norm.parameter_hash
    assert rep_in["provenance_sha256"] == doc_norm.provenance_sha256


def doc_norm_prov_hash(doc) -> str:
    return normalize_strategy_document(doc, strategy_version="v1").provenance_sha256


# -------------------------------------------------------------------------------------------------
# REQUIREMENT 1, 2, 3, 4, 5:
# Future-data leakage, purge & embargo correctness, incomplete & overlapping labels,
# IS/OOS separation, parameter freezing, and insufficient sample handling.
# -------------------------------------------------------------------------------------------------


def test_future_data_and_survivorship_leakage_rejected():
    """Verify causal timestamp guards reject future features, future bars, future recency evidence, and survivorship bias."""
    decision_ts = "2025-05-12T10:15:00+05:30"

    # Causal feature <= decision_ts succeeds
    valid_obs = wf.verify_no_lookahead(
        decision_time=decision_ts,
        features=[
            wf.FeatureObservation(
                name="ema_15",
                value=24010.5,
                observed_at="2025-05-12T10:15:00+05:30",
                available_at="2025-05-12T10:15:00+05:30",
            )
        ],
        bars=[{"bar_close_at": "2025-05-12T10:15:00+05:30", "close": 24012.0}],
    )
    assert len(valid_obs) == 1

    # Feature available 1 minute in the future raises LookAheadLeakageError
    with pytest.raises(wf.LookAheadLeakageError, match="LOOKAHEAD_FEATURE_LEAKAGE"):
        wf.verify_no_lookahead(
            decision_time=decision_ts,
            features=[
                {
                    "name": "future_vwap",
                    "value": 24050.0,
                    "observed_at": "2025-05-12T10:15:00+05:30",
                    "available_at": "2025-05-12T10:16:00+05:30",
                }
            ],
        )

    # Bar closing in the future raises LookAheadLeakageError
    with pytest.raises(wf.LookAheadLeakageError, match="LOOKAHEAD_BAR_LEAKAGE"):
        wf.verify_no_lookahead(
            decision_time=decision_ts,
            bars=[{"bar_close_at": "2025-05-12T10:20:00+05:30", "close": 24050.0}],
        )

    # Non-monotonic trading dates raise LookAheadLeakageError
    with pytest.raises(wf.LookAheadLeakageError, match="NON_MONOTONIC_TRADING_DATES"):
        wf.generate_walk_forward_folds(["2025-01-02", "2025-01-02", "2025-01-03"])

    # Survivorship-filtered dataset raises SurvivorshipBiasError
    spec = normalize_scanner_v0_family("LIQ_SWEEP_PDHL")
    with pytest.raises(wf.SurvivorshipBiasError, match="SURVIVORSHIP_LEAKAGE"):
        wf.run_walk_forward_validation(
            spec,
            trading_dates=_trading_calendar(count=90),
            events=[],
            universe_metadata={"survivorship_filtered": True, "point_in_time": False},
        )

    # Future recency evidence raises LookAheadLeakageError
    future_item = rc.EvidenceItem(
        evidence_id="ev_future",
        factor_category="pa_5m",
        observed_at="2025-05-12T10:20:00+05:30",
        direction=1,
    )
    with pytest.raises(wf.LookAheadLeakageError, match="RECENCY_FUTURE_EVIDENCE_LEAKAGE"):
        rc.score_evidence_item(future_item, decision_time=decision_ts)


def test_purge_embargo_and_is_oos_chronological_separation():
    """Verify 60D IS / 5D purge & embargo / 20D OOS windows and expansion with label horizon / cooldown."""
    cal = _trading_calendar(count=125)
    cfg = wf.WalkForwardConfig()
    assert cfg.is_trading_days == 60
    assert cfg.oos_trading_days == 20
    assert cfg.purge_days == 5
    assert cfg.embargo_days == 5
    assert cfg.effective_purge_days == 5
    assert cfg.effective_embargo_days == 5

    folds = wf.generate_walk_forward_folds(cal, cfg)
    assert len(folds) == 3  # 125 days -> fold 0 (0..84), fold 1 (20..104), fold 2 (40..124)

    for idx, fold in enumerate(folds):
        assert fold.fold_index == idx
        assert len(fold.is_raw_dates) == 60
        assert len(fold.is_dates) == 55  # 60 raw IS minus 5 trailing purged days
        assert len(fold.purged_dates) == 5
        assert len(fold.embargo_dates) == 5
        assert len(fold.oos_dates) == 20
        # Strict chronological ordering and disjointness
        assert fold.is_dates[-1] < fold.purged_dates[0]
        assert fold.purged_dates[-1] < fold.embargo_dates[0]
        assert fold.embargo_dates[-1] < fold.oos_dates[0]
        assert set(fold.is_dates).isdisjoint(fold.purged_dates)
        assert set(fold.is_dates).isdisjoint(fold.embargo_dates)
        assert set(fold.is_dates).isdisjoint(fold.oos_dates)
        assert set(fold.embargo_dates).isdisjoint(fold.oos_dates)

    # Expanding purge/embargo when label_horizon_days or cooldown_days exceeds 5
    cfg_long = wf.WalkForwardConfig(label_horizon_days=9, cooldown_days=7)
    assert cfg_long.effective_purge_days == 8
    assert cfg_long.effective_embargo_days == 8
    folds_long = wf.generate_walk_forward_folds(cal, cfg_long)
    assert len(folds_long[0].purged_dates) == 8
    assert len(folds_long[0].embargo_dates) == 8


def test_insufficient_historical_samples_never_reports_oos_result():
    """Verify no OOS result is reported when historical samples cannot form a valid OOS fold."""
    spec = normalize_scanner_v0_family("LIQ_SWEEP_PDHL")
    short_cal = _trading_calendar(count=70)  # Needs 60 IS + 5 embargo + 20 OOS = 85 days
    ev = _make_candidate_event(1, short_cal[10], is_winner=True)

    report = wf.run_walk_forward_validation(spec, trading_dates=short_cal, events=[ev])
    assert report.has_valid_oos_folds is False
    assert report.fold_count == 0
    assert report.folds == ()
    assert report.oos_performance is None
    assert report.validation_passed is False
    assert report.paper_promotion_eligible is False
    assert "NO_VALID_OOS_FOLDS" in report.failed_validation_criteria
    assert "INSUFFICIENT_HISTORICAL_SAMPLES" in report.failed_validation_criteria


def test_incomplete_and_overlapping_labels_purged_and_controlled():
    """Verify IS events whose horizon overflows IS end are purged, and incomplete/overlapping OOS labels are controlled."""
    spec = normalize_scanner_v0_family("LIQ_SWEEP_PDHL")
    cal = _trading_calendar(count=85)  # 1 fold: raw IS cal[0:60] (clean IS cal[0:55], purged cal[55:60]), embargo cal[60:65], OOS cal[65:85]
    fold0 = wf.generate_walk_forward_folds(cal)[0]

    # 1. Valid IS event on cal[10]
    ev_is_valid = _make_candidate_event(1, fold0.is_dates[10], is_winner=True)
    # 2. IS event on last clean IS day whose label horizon extends into purged/embargo window (fold0.purged_dates[1])
    ev_is_overflow = _make_candidate_event(
        2,
        fold0.is_dates[-1],
        label_end_day=fold0.purged_dates[1],
        is_winner=True,
    )
    # 3. Event inside trailing purge window
    ev_in_purge = _make_candidate_event(3, fold0.purged_dates[0], is_winner=True)
    # 4. Event inside embargo window
    ev_in_embargo = _make_candidate_event(4, fold0.embargo_dates[2], is_winner=True)
    # 5. Valid OOS event on oos_dates[2] (10:00 -> 10:45)
    ev_oos_1 = _make_candidate_event(5, fold0.oos_dates[2], decision_clock="10:00:00", exit_clock="10:45:00", is_winner=True)
    # 6. Overlapping OOS event on same day oos_dates[2] (10:15 -> 11:00 while ev_oos_1 is still open)
    ev_oos_overlap = _make_candidate_event(6, fold0.oos_dates[2], decision_clock="10:15:00", exit_clock="11:00:00", is_winner=True)
    # 7. Incomplete OOS event (missing exit / incomplete_label=True)
    ev_oos_incomplete = _make_candidate_event(7, fold0.oos_dates[5], incomplete_label=True, is_winner=True)
    # 8. OOS event whose label horizon overflows past fold0.oos_end_date
    ev_oos_overflow = _make_candidate_event(8, fold0.oos_dates[-1], label_end_day="2025-12-31", is_winner=True)

    report = wf.run_walk_forward_validation(
        spec,
        trading_dates=cal,
        events=[
            ev_is_valid,
            ev_is_overflow,
            ev_in_purge,
            ev_in_embargo,
            ev_oos_1,
            ev_oos_overlap,
            ev_oos_incomplete,
            ev_oos_overflow,
        ],
        config=_wf_cfg(allow_overlapping_labels=False),
    )
    assert report.fold_count == 1
    f_res = report.folds[0]

    # IS evaluated only ev_is_valid; both ev_in_purge and ev_is_overflow were purged
    assert f_res.is_performance.trade_count == 1
    assert ev_in_purge.event_id in f_res.purged_is_event_ids
    assert ev_is_overflow.event_id in f_res.purged_is_event_ids

    # Embargoed event tracked and excluded from both IS and OOS
    assert ev_in_embargo.event_id in f_res.embargoed_event_ids

    # OOS evaluated only ev_oos_1; overlap and incomplete/overflow events were excluded and recorded
    assert f_res.oos_performance.trade_count == 1
    executions = {e.event_id: e for e in f_res.oos_executions}
    assert executions[ev_oos_overlap.event_id].rejection_reason == "MAX_POSITIONS_EXCEEDED"
    assert executions[ev_oos_incomplete.event_id].admitted
    assert executions[ev_oos_incomplete.event_id].unresolved
    assert executions[ev_oos_overflow.event_id].rejection_reason == "MAX_POSITIONS_EXCEEDED"
    assert ev_oos_incomplete.event_id in f_res.incomplete_oos_event_ids
    assert ev_oos_overflow.event_id in f_res.incomplete_oos_event_ids


def test_parameter_freezing_and_mutation_guards():
    """Verify strategy parameters are frozen at IS end and cannot be selected on OOS dates or mutated during OOS."""
    doc = parse_strategy_markdown(APPROVABLE_MARKDOWN)
    spec = normalize_strategy_document(doc, strategy_id="condor-freeze", strategy_version="v1")
    cal = _trading_calendar(count=85)
    fold0 = wf.generate_walk_forward_folds(cal)[0]
    ev_oos = _make_candidate_event(1, fold0.oos_dates[1], is_winner=True)

    # 1. Selecting parameters using an OOS date raises ParameterFreezeViolationError
    def leaky_selector(fold, is_events, is_execs, norm_spec):
        return {"max_lots": 2}, [fold.is_end_date, fold.oos_start_date]

    with pytest.raises(wf.ParameterFreezeViolationError, match="OOS_PARAMETER_SELECTION_FORBIDDEN"):
        wf.run_walk_forward_validation(
            spec,
            trading_dates=cal,
            events=[ev_oos],
            parameter_selector=leaky_selector,
        )

    def valid_selector(fold, is_events, is_execs, norm_spec):
        return {"max_lots": 2}, [fold.is_end_date]

    with pytest.raises(wf.ParameterFreezeViolationError, match="FIXED_EVENT_REPLAY_ONLY"):
        wf.run_walk_forward_validation(
            spec, trading_dates=cal, events=[ev_oos], parameter_selector=valid_selector)

    # 2. Mutating a parameter marked `mutable_in_research=False` (scanner-v0 family) raises ParameterFreezeViolationError
    scanner_spec = normalize_scanner_v0_family("LIQ_SWEEP_PDHL")
    with pytest.raises(wf.ParameterFreezeViolationError, match="IMMUTABLE_PARAMETER_MUTATION"):
        wf.freeze_fold_parameters(
            fold=fold0,
            spec=scanner_spec,
            selected_parameters={"ema_period": 25},
        )

    # 3. Direct verification on FrozenFoldParameters detects any post-freeze parameter drift
    frozen = wf.freeze_fold_parameters(fold=fold0, spec=spec, selected_parameters={"max_lots": 2})
    frozen.verify_unchanged({**dict(frozen.parameters), "max_lots": 2})
    with pytest.raises(wf.ParameterFreezeViolationError, match="PARAMETER_FREEZE_VIOLATION"):
        frozen.verify_unchanged({**dict(frozen.parameters), "max_lots": 3})


# -------------------------------------------------------------------------------------------------
# REQUIREMENT 7, 8, 9, 10, 11:
# Tier 1 / Tier 2 provenance, fee-date dispatch, missing option data, deterministic hashes,
# and promotion ineligibility for synthetic-only evidence.
# -------------------------------------------------------------------------------------------------


def test_tier1_vs_tier2_provenance_and_synthetic_promotion_ineligibility(session, client, paper_account):
    """Verify Tier 1 requires verified market quotes, Tier 2 applies conservative burdens, and Tier 2 never qualifies for paper promotion."""
    spec = normalize_scanner_v0_family("LIQ_SWEEP_PDHL")
    cal = _trading_calendar(count=125)  # 3 folds
    folds = wf.generate_walk_forward_folds(cal)

    # Create 36 events across the 3 OOS windows (12 per OOS fold: 9 winners, 3 losers)
    events: list[wf.CandidateSignalEvent] = []
    ev_counter = 1
    for fold in folds:
        # Also populate IS events
        for i in range(6):
            events.append(_make_candidate_event(ev_counter, fold.is_dates[i * 5], is_winner=(i % 3 != 0)))
            ev_counter += 1
        for i in range(12):
            day = fold.oos_dates[i]
            events.append(
                _make_candidate_event(
                    ev_counter,
                    day,
                    is_winner=(i % 4 != 0),
                    regime="TREND" if i % 2 == 0 else "RANGE_EXPANSION",
                )
            )
            ev_counter += 1

    # One signal per decision time; overlapping folds can otherwise duplicate fixture signals.
    events = list({(e.market_day, e.decision_time): e for e in events}.values())
    # 1. Tier 1 Verified evaluation passes all criteria and is eligible for paper promotion
    rep_tier1 = wf.run_walk_forward_validation(
        spec,
        trading_dates=cal,
        events=events,
        config=_wf_cfg(fidelity_tier="TIER_1_VERIFIED", min_oos_trades=30, min_oos_folds=3),
    )
    assert rep_tier1.fidelity_tier == "TIER_1_VERIFIED"
    assert rep_tier1.verified_market_performance is True
    assert rep_tier1.performance_label == "VERIFIED_MARKET_DATA"
    assert rep_tier1.has_valid_oos_folds is True
    assert rep_tier1.oos_performance is not None
    assert rep_tier1.oos_performance.trade_count == 36
    assert rep_tier1.oos_performance.net_expectancy_inr is not None and rep_tier1.oos_performance.net_expectancy_inr > 0
    assert rep_tier1.oos_performance.profit_factor is not None and rep_tier1.oos_performance.profit_factor > 1.30
    assert rep_tier1.oos_performance.calmar_ratio is not None and rep_tier1.oos_performance.calmar_ratio > 0
    assert "TREND" in rep_tier1.oos_performance.regime_performance
    assert "RANGE_EXPANSION" in rep_tier1.oos_performance.regime_performance
    assert rep_tier1.validation_passed is True
    assert rep_tier1.paper_promotion_eligible is True
    assert rep_tier1.strategy_state == "INDEPENDENTLY_VALIDATED"

    # 2. Tier 2 Synthetic evaluation on the exact same events is NEVER labeled verified and NEVER eligible for paper promotion
    rep_tier2 = wf.run_walk_forward_validation(
        spec,
        trading_dates=cal,
        events=events,
        config=_wf_cfg(fidelity_tier="TIER_2_SYNTHETIC", min_oos_trades=30, min_oos_folds=3),
    )
    assert rep_tier2.fidelity_tier == "TIER_2_SYNTHETIC"
    assert rep_tier2.verified_market_performance is False
    assert rep_tier2.performance_label == "SYNTHETIC_MODEL_SIMULATION_ONLY"
    assert rep_tier2.paper_promotion_eligible is False
    assert rep_tier2.strategy_state == "BACKTEST_ELIGIBLE"
    assert "SYNTHETIC_TIER_2_INELIGIBLE_FOR_PAPER_PROMOTION" in rep_tier2.failed_validation_criteria
    assert any("SYNTHETIC_TIER2_SIMULATION" in lim for lim in rep_tier2.confidence_limitations)

    # 3. Tier 1 rejects synthetic quote provenance (`source="synthetic"`)
    bad_ev = _make_candidate_event(999, cal[70], quote_source="synthetic")
    with pytest.raises(wf.MissingOptionDataError, match="TIER_1_SYNTHETIC_PROVENANCE_REJECTED"):
        wf.simulate_option_execution(bad_ev, config=wf.WalkForwardConfig(fidelity_tier="TIER_1_VERIFIED"))

    # 4. Approvals preview blocks paper promotion if report has Tier 2 synthetic walk-forward payload
    s_store = StrategyStore(session)
    saved = s_store.save(APPROVABLE_MARKDOWN.replace("name: approvable-condor\n", "name: tier2-blocked-strat\n"))
    r_store = ReportStore(session)
    r_store.create_success(
        strategy_id=saved.strategy_id,
        version=saved.version,
        payload={
            "verdict": {"label": "Profitable", "scenarios_tested": 1, "scenarios_passing": 1},
            "fidelity_tier": "TIER_2_SYNTHETIC",
            "walk_forward": rep_tier2.to_dict(),
        },
        sim_runs=[],
        reviews=[],
        improvements=[],
        inputs={"test": True},
        started_at=datetime.now(IST),
    )
    session.commit()

    prev = approvals.preview(session, saved.strategy_id, saved.version.version, mode="paper")
    assert prev["independently_validated"] is False
    assert any("Tier 2 synthetic walk-forward validation does not qualify" in b for b in prev["blockers"])


def test_fee_date_dispatch_and_missing_option_data_fail_closed():
    """Verify date-effective fee policies in walk-forward simulation and fail-closed behavior on missing option data."""
    # 1. Verified date inside 2024-10-01..2026-06-30 requires allow_historical_brokerage_assumption=True
    ev_2025 = _make_candidate_event(1, "2025-03-10", is_winner=True)
    ex_2025 = wf.simulate_option_execution(
        ev_2025,
        config=wf.WalkForwardConfig(
            fidelity_tier="TIER_1_VERIFIED",
            allow_historical_brokerage_assumption=True,
            trusted_delta_sources=frozenset({"nse_historical_book"}),
        ),
    )
    assert ex_2025.priced is True
    assert ex_2025.cost_policy_id == "nse-index-option-historical-statutory-2024-10-01-to-2026-02-28-v1"

    # Without allow_historical_brokerage_assumption=True on a pre-2026-07-01 date without explicit policy_id:
    # paper_fees.resolve_policy defaults to POLICY_ID when policy_id is None, unless cost_policy_id is set or date is outside window.
    # Let's verify date outside 2024-10-01..2026-11-06 fails closed with FeePolicyUnavailable!
    ev_unverified_date = _make_candidate_event(2, "2024-05-15", is_winner=True)
    with pytest.raises(paper_fees.FeePolicyUnavailable, match="COST_POLICY_UNVERIFIED_DATE"):
        wf.simulate_option_execution(
            ev_unverified_date,
            config=wf.WalkForwardConfig(fidelity_tier="TIER_1_VERIFIED"),
        )

    with pytest.raises(paper_fees.FeePolicyUnavailable, match="COST_POLICY_UNVERIFIED_DATE"):
        wf.simulate_option_execution(
            ev_unverified_date,
            config=wf.WalkForwardConfig(fidelity_tier="TIER_2_SYNTHETIC"),
        )

    # 2. Missing option quote in Tier 1 fails closed
    ev_no_quote = _make_candidate_event(3, "2025-03-10", with_verified_quotes=False)
    with pytest.raises(wf.MissingOptionDataError, match="TIER_1_MISSING_OPTION_QUOTE"):
        wf.simulate_option_execution(
            ev_no_quote,
            config=wf.WalkForwardConfig(fidelity_tier="TIER_1_VERIFIED"),
            fail_closed_on_missing=True,
        )

    unpriced_res = wf.simulate_option_execution(
        ev_no_quote,
        config=wf.WalkForwardConfig(fidelity_tier="TIER_1_VERIFIED"),
        fail_closed_on_missing=False,
    )
    assert unpriced_res.priced is False
    assert unpriced_res.rejection_reason == "TIER_1_MISSING_OPTION_QUOTE"
    assert unpriced_res.entry_price_inr is None

    # 3. Missing IV or exit_spot in Tier 2 fails closed
    ev_no_iv = _make_candidate_event(4, "2025-03-10", entry_iv=0.0, with_verified_quotes=False)
    with pytest.raises(wf.MissingOptionDataError, match="TIER_2_MISSING_MODEL_INPUTS"):
        wf.simulate_option_execution(
            ev_no_iv,
            config=wf.WalkForwardConfig(fidelity_tier="TIER_2_SYNTHETIC"),
            fail_closed_on_missing=True,
        )


def test_deterministic_replay_and_report_hashes():
    """Verify walk-forward fold and report hashes are 100% deterministic and sensitive to any input change."""
    spec = normalize_scanner_v0_family("LIQ_SWEEP_PDHL")
    cal = _trading_calendar(count=105)
    folds = wf.generate_walk_forward_folds(cal)
    events = [
        _make_candidate_event(i + 1, folds[0].oos_dates[i], is_winner=(i % 2 == 0))
        for i in range(10)
    ]

    rep1 = wf.run_walk_forward_validation(spec, trading_dates=cal, events=events, config=_wf_cfg())
    rep2 = wf.run_walk_forward_validation(spec, trading_dates=cal, events=copy.deepcopy(events), config=_wf_cfg())

    assert rep1.report_hash == rep2.report_hash
    assert [f.fold_hash for f in rep1.folds] == [f.fold_hash for f in rep2.folds]

    # Flip one trade outcome -> fold_hash and report_hash change deterministically
    events_modified = list(events)
    events_modified[0] = _make_candidate_event(1, folds[0].oos_dates[0], is_winner=False)
    rep3 = wf.run_walk_forward_validation(spec, trading_dates=cal, events=events_modified, config=_wf_cfg())
    assert rep3.report_hash != rep1.report_hash
    assert rep3.folds[0].fold_hash != rep1.folds[0].fold_hash


# -------------------------------------------------------------------------------------------------
# REQUIREMENT 3 & 6:
# Recency decay model, deduplication, and independent four-arm ablation under identical conditions.
# -------------------------------------------------------------------------------------------------


def test_recency_decay_deduplication_and_identical_four_arm_ablation(tmp_path: Path):
    """Verify 6-factor decay math, duplicate cluster collapsing, shadow hook isolation, and 4-arm ablation."""
    # 1. Verify exponential decay math for all 6 factors
    assert rc.compute_decay(20.0, 20.0) == pytest.approx(0.5, abs=1e-6)  # 1 half-life = 0.5
    assert rc.compute_decay(40.0, 20.0) == pytest.approx(0.25, abs=1e-6)  # 2 half-lives = 0.25
    assert rc.compute_decay(375.0, 375.0, floor=0.50) == pytest.approx(0.50, abs=1e-6)
    assert rc.compute_decay(900.0, 375.0, floor=0.50) == pytest.approx(0.50, abs=1e-6)  # floor holds at 0.50
    assert rc.compute_decay(10.0, 120.0, in_active_window=True) == 1.0  # active macro window = 1.0
    assert rc.compute_decay(-5.0, 20.0) == 0.0  # negative elapsed = 0.0

    # 2. Verify deduplication collapses syndicated / same-bucket items
    dec_time = "2025-06-10T10:30:00+05:30"
    dup_items = [
        rc.EvidenceItem(
            evidence_id="news_1",
            factor_category="breaking_news",
            observed_at="2025-06-10T10:22:00+05:30",
            direction=1,
            reliability=0.70,
            severity=0.80,
            cluster_id="rbi_headline_cluster",
        ),
        rc.EvidenceItem(
            evidence_id="news_2_syndicated",
            factor_category="breaking_news",
            observed_at="2025-06-10T10:24:00+05:30",
            direction=1,
            reliability=0.90,
            severity=0.80,
            cluster_id="rbi_headline_cluster",
        ),
    ]
    deduped = rc.deduplicate_and_cluster(dup_items, decision_time=dec_time)
    assert len(deduped) == 1
    assert deduped[0][0].evidence_id == "news_2_syndicated"

    # 3. Verify shadow hook writes JSONL and explicitly sets orders_sent=False, shadow_only=True
    shadow_log = tmp_path / "recency_shadow.jsonl"
    rec_out = rc.safe_shadow_hook(
        shadow_log,
        decision_time=dec_time,
        strategy_id="LIQ_SWEEP_PDHL",
        strategy_version="scanner-v0",
        regime="TREND",
        items=dup_items,
        trade_direction="CALL",
    )
    assert rec_out is not None
    assert rec_out["shadow_only"] is True
    assert rec_out["orders_sent"] is False
    assert rec_out["mutated_active_entry"] is False
    assert shadow_log.exists()

    # 4. Run 4-Arm Ablation over identical 125-trading-day period
    spec = normalize_scanner_v0_family("LIQ_SWEEP_PDHL")
    cal = _trading_calendar(count=125)
    folds = wf.generate_walk_forward_folds(cal)

    events: list[wf.CandidateSignalEvent] = []
    evidence_map: dict[str, list[rc.EvidenceItem]] = {}
    ev_id_num = 1

    for fold in folds:
        # Clean IS events
        for i in range(6):
            day = fold.is_dates[i * 5]
            ev = _make_candidate_event(ev_id_num, day, is_winner=(i % 2 == 0), regime="TREND")
            events.append(ev)
            evidence_map[ev.event_id] = [
                rc.EvidenceItem(
                    evidence_id=f"is_ev_{ev_id_num}",
                    factor_category="liq_sweep",
                    observed_at=f"{day}T09:55:00+05:30",
                    direction=1,
                    reliability=0.85,
                    severity=0.85,
                )
            ]
            ev_id_num += 1

        # OOS events:
        # - 7 winning trades in TREND regime with strong supporting recency evidence
        # - 3 losing trades in EVENT_PANIC regime (filtered by Arm 1 and Arm 3)
        # - 3 losing trades in TREND regime with strong opposing recency conflict (filtered by Arm 2 and Arm 3)
        for i in range(7):
            day = fold.oos_dates[i]
            ev = _make_candidate_event(ev_id_num, day, is_winner=True, regime="TREND")
            events.append(ev)
            evidence_map[ev.event_id] = [
                rc.EvidenceItem(
                    evidence_id=f"oos_sup1_{ev_id_num}",
                    factor_category="liq_sweep",
                    observed_at=f"{day}T09:50:00+05:30",
                    direction=1,
                    reliability=0.90,
                    severity=0.85,
                ),
                rc.EvidenceItem(
                    evidence_id=f"oos_sup2_{ev_id_num}",
                    factor_category="pa_5m",
                    observed_at=f"{day}T09:55:00+05:30",
                    direction=1,
                    reliability=0.85,
                    severity=0.80,
                ),
            ]
            ev_id_num += 1

        for i in range(7, 10):
            day = fold.oos_dates[i]
            ev = _make_candidate_event(ev_id_num, day, is_winner=False, regime="EVENT_PANIC")
            events.append(ev)
            evidence_map[ev.event_id] = [
                rc.EvidenceItem(
                    evidence_id=f"oos_panic_{ev_id_num}",
                    factor_category="pa_5m",
                    observed_at=f"{day}T09:55:00+05:30",
                    direction=1,
                    reliability=0.80,
                    severity=0.80,
                )
            ]
            ev_id_num += 1

        for i in range(10, 13):
            day = fold.oos_dates[i]
            ev = _make_candidate_event(ev_id_num, day, is_winner=False, regime="TREND")
            events.append(ev)
            evidence_map[ev.event_id] = [
                rc.EvidenceItem(
                    evidence_id=f"oos_opp_{ev_id_num}",
                    factor_category="breaking_news",
                    observed_at=f"{day}T09:55:00+05:30",
                    direction=-1,
                    reliability=0.95,
                    severity=0.90,
                )
            ]
            ev_id_num += 1

    ablation = rc.run_four_arm_ablation(
        spec,
        trading_dates=cal,
        events=events,
        walk_forward_config=_wf_cfg(fidelity_tier="TIER_1_VERIFIED", min_oos_trades=20, min_oos_folds=3),
        evidence_by_event_id=evidence_map,
    )

    assert ablation.shadow_only is True
    assert ablation.proof_of_profitability is False
    assert ablation.identical_evaluation_conditions_verified is True
    assert set(ablation.arms.keys()) == set(rc.ABLA_ARMS)

    arm0 = ablation.arms["ARM_0_FROZEN_BASELINE"].walk_forward_report.oos_performance
    arm1 = ablation.arms["ARM_1_REGIME_ONLY"].walk_forward_report.oos_performance
    arm2 = ablation.arms["ARM_2_RECENCY_ONLY"].walk_forward_report.oos_performance
    arm3 = ablation.arms["ARM_3_COMBINED_REGIME_RECENCY"].walk_forward_report.oos_performance
    assert arm0 is not None and arm1 is not None and arm2 is not None and arm3 is not None

    # Arm 0 takes all 39 OOS trades (21 winners, 18 losers)
    assert arm0.trade_count == 39
    # Arm 1 skips the 9 EVENT_PANIC losers -> 30 trades
    assert arm1.trade_count == 30
    # Arm 2 skips the 9 opposing-news losers -> 30 trades
    assert arm2.trade_count == 30
    # Arm 3 skips both sets of losers -> 21 trades (all 21 winners retained, 0 skipped winners)
    assert arm3.trade_count == 21
    assert arm3.net_expectancy_inr > arm1.net_expectancy_inr > arm0.net_expectancy_inr
    assert arm3.net_expectancy_inr > arm2.net_expectancy_inr > arm0.net_expectancy_inr

    audit3 = ablation.arms["ARM_3_COMBINED_REGIME_RECENCY"].skipped_winner_audit
    assert audit3.skipped_trades_total == 18
    assert audit3.skipped_winners_count == 0
    assert audit3.skipped_losers_count == 18
    assert audit3.skipped_winner_rate == 0.0
    assert audit3.skipped_net_pnl_foregone_inr < 0  # Negative foregone PnL = skipping saved losses!

    assert ablation.halflife_stability is not None
    assert ablation.halflife_stability.neighborhood_stable is True
    assert ablation.research_acceptance_passed is True
    assert ablation.recommended_arm == "ARM_3_COMBINED_REGIME_RECENCY"

    # Verify parameter selection on OOS dates in ablation is rejected
    with pytest.raises(wf.ParameterFreezeViolationError, match="ABLATION_OOS_PARAMETER_SELECTION_FORBIDDEN"):
        rc.run_four_arm_ablation(
            spec,
            trading_dates=cal,
            events=events,
            walk_forward_config=_wf_cfg(),
            selection_source_dates=[folds[0].oos_dates[0]],
        )


def test_zero_trades_and_undefined_metrics_handled_explicitly():
    """Verify zero trades and all-winning trades report None for undefined metrics with explicit confidence limitations."""
    m_zero = wf.compute_validation_metrics([], trading_days_evaluated=20)
    assert m_zero.trade_count == 0
    assert m_zero.net_expectancy_inr is None
    assert m_zero.win_rate is None
    assert m_zero.average_win_inr is None
    assert m_zero.average_loss_inr is None
    assert m_zero.profit_factor is None
    assert m_zero.calmar_ratio is None
    assert any("ZERO_TRADES" in lim for lim in m_zero.confidence_limitations)

    # Single winning trade -> profit_factor, average_loss_inr, and calmar_ratio (zero drawdown) are None, never inf/crash
    ev_win = _make_candidate_event(1, "2025-04-02", is_winner=True)
    sim_win = wf.simulate_option_execution(ev_win, config=_wf_cfg(fidelity_tier="TIER_1_VERIFIED"))
    m_one_win = wf.compute_validation_metrics([sim_win], trading_days_evaluated=25)
    assert m_one_win.trade_count == 1
    assert m_one_win.win_rate == 1.0
    assert m_one_win.average_win_inr is not None and m_one_win.average_win_inr > 0
    assert m_one_win.average_loss_inr is None
    assert m_one_win.profit_factor is None
    assert m_one_win.calmar_ratio is None
    assert any("UNDEFINED_PROFIT_FACTOR_NO_LOSSES" in lim for lim in m_one_win.confidence_limitations)
    assert any("UNDEFINED_CALMAR_ZERO_DRAWDOWN" in lim for lim in m_one_win.confidence_limitations)


def test_b3_modules_import_no_broker_or_transport_and_frozen_v0_blocked():
    """Verify walk_forward.py and recency.py obey paper isolation invariants and keep PDC_15EMA_V0 blocked."""
    forbidden_modules = (
        "app.brokers",
        "app.trading.orders",
        "app.trading.execution_crew",
        "app.telegram_transport",
        "app.alerts",
        "httpx",
        "requests",
        "urllib",
    )
    forbidden_names = {"deliver_publication", "claim_publication", "place_order", "send", "default_capital_inr"}

    for mod in (wf, rc):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        imports = [
            n.module
            for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom) and n.module
        ] + [
            alias.name
            for n in ast.walk(tree)
            if isinstance(n, ast.Import)
            for alias in n.names
        ]
        names = {
            n.id
            for n in ast.walk(tree)
            if isinstance(n, ast.Name)
        } | {
            n.attr
            for n in ast.walk(tree)
            if isinstance(n, ast.Attribute)
        }
        assert not [m for m in imports if m.startswith(forbidden_modules)]
        assert not (names & forbidden_names)
        assert "default_capital_inr" not in src

    # Frozen PDC_15EMA_V0 remains quarantined and blocked from paper promotion in walk-forward validation
    pdc_spec = normalize_pdc_15ema_v0_benchmark()
    cal = _trading_calendar(count=125)
    folds = wf.generate_walk_forward_folds(cal)
    events = [_make_candidate_event(i + 1, folds[0].oos_dates[i], is_winner=True) for i in range(10)]
    pdc_rep = wf.run_walk_forward_validation(pdc_spec, trading_dates=cal, events=events, config=_wf_cfg())
    assert pdc_rep.paper_promotion_eligible is False
    assert pdc_rep.validation_passed is False
    assert "STRATEGY_QUARANTINED" in pdc_rep.failed_validation_criteria
