"""Adversarial accounting tests; all books and Greeks here are synthetic fixtures."""
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

from app.trading import walk_forward as wf
from tests.test_walk_forward_and_ablation import _make_candidate_event, _wf_cfg


def test_future_sale_fees_cannot_change_entry_admission():
    event = _make_candidate_event(1, '2026-11-06')
    ex = wf.simulate_option_execution(event, config=_wf_cfg())
    decisions = []
    for fee in (0, 2000):
        tracker = wf.ReplayRiskTracker()
        out = tracker.check_and_apply(event, replace(ex, exit_charges_inr=fee))
        decisions.append((out.rejection_reason, tracker.cash,
                          tracker.open_positions[event.event_id]['full_premium_loss']))
    assert decisions[0] == decisions[1]


def test_incomplete_label_retains_entry_and_blocks_next_day():
    event = replace(_make_candidate_event(1, '2026-11-06'), label_end_time=None,
                    exit_quote=None, exit_spot=None, incomplete_label=True)
    cfg = _wf_cfg()
    ex = wf.simulate_option_execution(event, config=cfg, fail_closed_on_missing=False)
    tracker = wf.ReplayRiskTracker()
    tracker.check_and_apply(event, ex)
    assert tracker.cash < Decimal('50000')
    assert event.event_id in tracker.open_positions
    before = tracker.cash
    tracker._update_positions(datetime.fromisoformat('2026-11-09T10:00:00+05:30'))
    assert tracker.cash == before
    assert tracker.daily_losses == {}
    later = _make_candidate_event(2, '2026-11-06', decision_clock='11:00:00', exit_clock='11:45:00')
    result = tracker.check_and_apply(later, wf.simulate_option_execution(later, config=cfg))
    assert result.rejection_reason == 'MAX_POSITIONS_EXCEEDED'


def test_default_greek_trust_is_empty():
    assert wf.WalkForwardConfig().trusted_delta_sources == frozenset()


def test_quote_refresh_does_not_refresh_greek():
    event = _make_candidate_event(1, '2026-11-06')
    q = replace(event.entry_quote, delta_observed_at=event.entry_quote.quote_time - timedelta(seconds=11))
    ex = wf.simulate_option_execution(replace(event, entry_quote=q), config=_wf_cfg(), fail_closed_on_missing=False)
    assert ex.rejection_reason == 'STOP_RISK_UNVERIFIABLE'


def test_entry_reservation_and_cash_reconcile_exactly():
    event = _make_candidate_event(1, '2026-11-06')
    ex = wf.simulate_option_execution(event, config=_wf_cfg())
    tracker = wf.ReplayRiskTracker()
    admitted = tracker.check_and_apply(event, ex)
    assert admitted.admitted
    debit = Decimal(str(ex.entry_price_inr)) * ex.qty + Decimal(str(ex.entry_charges_inr))
    assert tracker.cash == Decimal('50000.00') - debit
    assert tracker.open_positions[event.event_id]['full_premium_loss'] == debit + Decimal(ex.conservative_exit_reserve_inr)
    tracker._update_positions(datetime.fromisoformat(event.label_end_time))
    assert tracker.cash == Decimal('50000.00') + Decimal(str(ex.net_pnl_inr))
    assert tracker.open_positions == {}


def test_window_boundary_preserves_unresolved_without_reading_oos_exit():
    event = _make_candidate_event(1, '2026-11-06', label_end_day='2026-11-09')
    events, incomplete, _ = wf._filter_and_control_events_for_window(
        [event], allowed_dates={'2026-11-06'}, window_end_date='2026-11-06',
        allow_overlapping_labels=False, strict_leakage_check=True)
    assert incomplete == [event.event_id]
    assert events[0].exit_quote is None and events[0].label_end_time is None
    tracker = wf.ReplayRiskTracker()
    ex = tracker.check_and_apply(events[0], wf.simulate_option_execution(events[0], config=_wf_cfg()))
    assert ex.admitted and ex.unresolved and ex.net_pnl_inr is None
    assert tracker.open_positions[event.event_id]['exit_time'] is None


import pytest


@pytest.mark.parametrize('field,value', [
    ('delta_observed_at', None), ('delta_contract', 'different-contract'),
    ('delta_source', 'unauthorized'), ('delta_source', 'computed_bsm'),
    ('delta', None), ('delta', float('nan')),
])
def test_invalid_greek_evidence_rejected(field, value):
    event = _make_candidate_event(1, '2026-11-06')
    event = replace(event, entry_quote=replace(event.entry_quote, **{field: value}))
    ex = wf.simulate_option_execution(event, config=_wf_cfg(), fail_closed_on_missing=False)
    assert ex.rejection_reason == 'STOP_RISK_UNVERIFIABLE'
    assert not ex.entry_executable


def test_future_dated_greek_rejected():
    event = _make_candidate_event(1, '2026-11-06')
    event = replace(event, entry_quote=replace(event.entry_quote,
        delta_observed_at=event.entry_quote.quote_time + timedelta(seconds=1)))
    assert wf.simulate_option_execution(event, config=_wf_cfg(), fail_closed_on_missing=False).rejection_reason == 'STOP_RISK_UNVERIFIABLE'


def test_future_exit_corruption_cannot_reject_executable_entry():
    event = _make_candidate_event(1, '2026-11-06')
    for exit_quote in (None, {'quote_time': 'invalid'}, replace(event.exit_quote, strike=25000)):
        ex = wf.simulate_option_execution(replace(event, exit_quote=exit_quote), config=_wf_cfg())
        tracker = wf.ReplayRiskTracker()
        result = tracker.check_and_apply(event, ex)
        assert result.admitted and result.unresolved
        assert result.net_pnl_inr is None
        assert tracker.cash == Decimal('46941.29')


def test_replay_idempotency_rebuild_and_account_isolation():
    events = [_make_candidate_event(i, '2026-11-06', decision_clock=f'{10+i}:00:00',
              exit_clock=f'{10+i}:45:00') for i in range(2)]
    a, b = wf.ReplayRiskTracker(), wf.ReplayRiskTracker()
    for ev in events:
        ex = wf.simulate_option_execution(ev, config=_wf_cfg())
        a.check_and_apply(ev, ex)
        snapshot = a.snapshot()
        a.check_and_apply(ev, ex)
        assert a.snapshot() == snapshot
        b.check_and_apply(ev, ex)
    assert a.snapshot() == b.snapshot()
    untouched = wf.ReplayRiskTracker()
    assert untouched.cash == Decimal('50000.00') and not untouched.open_positions


def test_synthetic_account_does_not_bypass_hard_limits():
    event = _make_candidate_event(1, '2026-11-06')
    ex = wf.simulate_option_execution(event, config=_wf_cfg(fidelity_tier='TIER_2_SYNTHETIC'))
    assert Decimal(str(ex.entry_price_inr)) * ex.qty > Decimal('5000')
    result = wf.ReplayRiskTracker().check_and_apply(event, ex)
    assert not result.admitted and result.rejection_reason == 'FULL_PREMIUM_LIMIT'


def test_explicit_composition_and_unsupported_rules_fail_closed():
    from tests.test_causal_walk_forward import _make_body_ratio_strategy_spec, _make_session_candles
    from app.strategies.rules import IntradayEntryPredicate
    # Ensure the CALL predicate can pass: the former fixture only produced
    # PUT events despite declaring long_call, which hid the structure bug.
    spec = _make_body_ratio_strategy_spec('composition-evidence', body_ratio_min=0.30)
    spec.rules_v2.intraday_entry.append(IntradayEntryPredicate(kind='vwap_relation', direction='SHORT'))
    gen = wf.RuleBasedCausalSignalGenerator()
    candles = _make_session_candles('2026-11-06')
    with pytest.raises(wf.UnsupportedStrategyRuleError, match='AMBIGUOUS_ENTRY_COMPOSITION'):
        gen.generate_signals(spec, {}, candles, allowed_dates=['2026-11-06'])
    spec.rules_v2.entry_composition = 'ALL'
    assert not gen.generate_signals(spec, {}, candles, allowed_dates=['2026-11-06'])
    spec.rules_v2.entry_composition = 'ANY'
    any_events = gen.generate_signals(spec, {}, candles, allowed_dates=['2026-11-06'])
    assert any_events and all(e.direction == 'CALL' for e in any_events)
    spec.rules_v2.intraday_entry[0].kind = 'or30_breakout_retest'
    with pytest.raises(wf.UnsupportedStrategyRuleError, match='UNSUPPORTED_OR30_RETEST_SEMANTICS'):
        gen.generate_signals(spec, {}, candles, allowed_dates=['2026-11-06'])


def test_future_exit_cannot_suppress_earlier_candidate_signals():
    from tests.test_causal_walk_forward import _make_body_ratio_strategy_spec, _make_session_candles
    spec = _make_body_ratio_strategy_spec('candidate-causality')
    bars = _make_session_candles('2026-11-06')
    gen = wf.RuleBasedCausalSignalGenerator()
    base = gen.generate_signals(spec, {}, bars, allowed_dates=['2026-11-06'])
    truncated = gen.generate_signals(spec, {}, bars[:20], allowed_dates=['2026-11-06'])
    cutoff = '2026-11-06T10:50:00+05:30'
    def early(events):
        return [(e.event_id, e.decision_time, e.entry_spot, e.stop_spot) for e in events if e.decision_time <= cutoff]
    assert early(base) == early(truncated)


def test_risk_rejection_is_not_reported_as_missing_quote_evidence():
    from tests.test_walk_forward_and_ablation import _trading_calendar
    from app.strategies.normalizer import normalize_scanner_v0_family
    calendar = _trading_calendar(count=85)
    day = wf.generate_walk_forward_folds(calendar)[0].oos_dates[0]
    event = replace(_make_candidate_event(1, day), lots=2)
    report = wf.run_walk_forward_validation(normalize_scanner_v0_family('LIQ_SWEEP_PDHL'),
        trading_dates=calendar, events=[event], config=_wf_cfg())
    ex = report.folds[0].oos_executions[0]
    assert ex.rejection_reason == 'FULL_PREMIUM_LIMIT' and not ex.admitted
    assert report.oos_performance.trade_count == 0
    assert report.data_fidelity_and_provenance['coverage_ratio'] == 1.0
    assert not report.verified_market_performance


def test_declared_option_structure_cannot_be_replaced_by_opposite_signal():
    import pytest
    from tests.test_causal_walk_forward import _make_test_strategy_spec, _make_session_candles
    spec = _make_test_strategy_spec('structure-conflict')
    rules = spec.rules_v2.model_copy(deep=True)
    rules.instrument.structure = 'long_put'
    spec = replace(spec, rules_v2=rules)
    with pytest.raises(wf.UnsupportedStrategyRuleError, match='ENTRY_DIRECTION_STRUCTURE_MISMATCH'):
        wf.RuleBasedCausalSignalGenerator().generate_signals(
            spec, {}, _make_session_candles('2026-11-06'), allowed_dates=['2026-11-06'])


def test_bidirectional_predicate_respects_declared_option_structure():
    from tests.test_causal_walk_forward import _make_test_strategy_spec, _make_session_candles
    spec = _make_test_strategy_spec('structure-both')
    rules = spec.rules_v2.model_copy(deep=True)
    rules.instrument.structure = 'long_put'
    rules.intraday_entry[0].direction = 'BOTH'
    rules.intraday_entry[0].params['relation'] = 'cross'
    spec = replace(spec, rules_v2=rules)
    events = wf.RuleBasedCausalSignalGenerator().generate_signals(
        spec, {}, _make_session_candles('2026-11-06'), allowed_dates=['2026-11-06'])
    assert all(event.direction == 'PUT' for event in events)
    bearish = wf.RuleBasedCausalSignalGenerator().generate_signals(
        spec, {}, _make_session_candles('2026-11-06', pattern='trending_down'),
        allowed_dates=['2026-11-06'])
    assert bearish and all(event.direction == 'PUT' for event in bearish)
