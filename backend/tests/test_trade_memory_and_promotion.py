"""Synthetic engineering fixtures only; never market-validation evidence."""
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from app.trading import promotion as p, trade_memory as tm, walk_forward as wf
from app.db.hashing import canonical_json, sha256_hex
from tests.test_walk_forward_and_ablation import _make_candidate_event, _wf_cfg

NOW = datetime.fromisoformat('2026-11-06T16:00:00+05:30')
BINDING = {k: (sha256_hex(k) if k.endswith('sha256') or k == 'parameter_hash' else k + '-fixture') for k in p.BINDING_KEYS}
TRUST = frozenset({'test-validator'})


def artifacts(until='WALK_FORWARD_VALIDATED'):
    out = []
    for stage in p.STATES[:p.STATES.index(until) + 1]:
        if stage == 'PAPER_APPROVED':
            continue
        out.append(p.Evidence.create({'stage': stage, 'binding': BINDING, 'passed': True, 'complete': True,
            'issuer': 'test-validator', 'observed_at': (NOW - timedelta(days=100)).isoformat(),
            'valid_until': (NOW + timedelta(days=100)).isoformat(), 'synthetic': False,
            'verified_market_data': True, 'data_authorized': True,
            'causal': True, 'oos_folds': 5, 'leakage_checked': True}))
    return out


def evaluate(current='WALK_FORWARD_VALIDATED', target='PAPER_APPROVED', **kwargs):
    args = dict(current=current, target=target, binding=BINDING, evidence=artifacts(), now=NOW,
                trusted_validators=TRUST, trusted_operators=frozenset({'operator-fixture'}))
    args.update(kwargs)
    return p.evaluate(**args)


def signoff(evidence):
    hashes = sorted(e.sha256 for e in evidence if e.read()['stage'] in p.STATES[:6])
    return {'operator': 'operator-fixture', 'target': 'PAPER_APPROVED',
        'signed_at': (NOW - timedelta(days=90)).isoformat(),
        'dossier_sha256': sha256_hex(canonical_json({'binding': BINDING, 'artifacts': hashes}))}


def test_signoff_is_required_and_binds_exact_artifacts():
    es = artifacts()
    assert 'EXPLICIT_OPERATOR_SIGNOFF_REQUIRED' in evaluate().blockers
    assert evaluate(operator_signoff=signoff(es)).allowed
    altered = p.Evidence.create({**es[0].read(), 'new_evidence': True})
    assert not evaluate(evidence=[altered, *es[1:]], operator_signoff=signoff(es)).allowed
    assert not evaluate(trusted_validators=frozenset(), operator_signoff=signoff(es)).allowed
    assert not evaluate(current='EXTRACTED', target='PAPER_APPROVED').allowed


@pytest.mark.parametrize('change', [
    {'synthetic': True}, {'verified_market_data': False}, {'data_authorized': False},
    {'complete': False}, {'passed': False}, {'issuer': 'unknown'},
    {'observed_at': '2026-11-07T10:00:00+05:30'}, {'valid_until': '2026-01-01T10:00:00+05:30'},
    {'binding': {**BINDING, 'parameter_hash': 'changed'}},
])
def test_promotion_invalidates_bad_validation_evidence(change):
    es = artifacts()
    es[-1] = p.Evidence.create({**es[-1].read(), **change})
    assert not evaluate(evidence=es, operator_signoff=signoff(es)).allowed


def test_hash_tamper_missing_evidence_and_ordinary_reports_fail_closed():
    es = artifacts()
    es[-1] = replace(es[-1], payload_json='{}')
    assert not evaluate(evidence=es).allowed
    assert not evaluate(evidence=[]).allowed
    assert not evaluate(evidence=[p.Evidence.create({'review': 'Profitable'})]).allowed


def test_forward_requires_actual_reconciled_sessions_after_approval():
    es = artifacts('PAPER_VALIDATED')
    approval = signoff(es)
    forward = {**es[-1].read(), 'observed_at': NOW.isoformat()}
    def check(sessions):
        evidence = [*es[:-1], p.Evidence.create({**forward, 'sessions': sessions})]
        return evaluate(current='PAPER_APPROVED', target='PAPER_VALIDATED', evidence=evidence,
                        operator_signoff=approval)
    assert not check([]).allowed
    days = [NOW.date() - timedelta(days=i) for i in range(1, 21)]
    sessions = [{'market_day': day.isoformat(), 'actual_forward_session': True,
                 'reconciliation': 'EXACT_0.00', 'unresolved_exposure': False, 'closed_trades': 2, 'account_id': BINDING['account_id']} for day in days]
    # Fixture declarations test gates, not proof these sessions occurred.
    assert check(sessions).allowed
    for key, value in [('actual_forward_session', False), ('reconciliation', 'UNKNOWN'),
                       ('unresolved_exposure', True), ('closed_trades', 0)]:
        assert not check([{**s, key: value} for s in sessions]).allowed
    assert not check([sessions[0]] * 20).allowed


def test_trade_memory_rebuild_is_immutable_and_account_specific():
    event = _make_candidate_event(1, '2026-11-06')
    ex = wf.ReplayRiskTracker().check_and_apply(event, wf.simulate_option_execution(event, config=_wf_cfg()))
    def record(account='IND:test', event=event, execution=ex):
        return tm.project_research(event, execution, strategy_id='fixture', strategy_version='v1',
            parameter_hash='fixture-param', account_id=account, validation_binding=BINDING)
    a = record()
    assert a == record()
    assert a.memory_id != record('SHARED').memory_id
    assert a.source_sha256 != record(event=replace(event, regime='OTHER')).source_sha256
    view = a.to_dict()
    assert view['mae_inr'] is None and view['mfe_inr'] is None
    view['state'] = 'tampered'
    assert a.to_dict()['state'] == 'REALIZED'
    unresolved = replace(ex, priced=False, unresolved=True, net_pnl_inr=None)
    assert record(execution=unresolved).to_dict()['realized_pnl_inr'] is None
    assert tm.classify_failures(['LOSING_TRADE']) == [{'category': 'UNKNOWN', 'evidence': 'INSUFFICIENT_CAUSAL_EVIDENCE'}]
    assert tm.classify_failures(['STOP_RISK_LIMIT'])[0]['category'] == 'RISK_REJECTION'


def test_promotion_specification_is_read_only_and_authenticated(client):
    response = client.get('/api/live/crew/promotion/specification')
    assert response.status_code in (401, 403)


def test_v2_memory_reports_rebuild_recovery_and_account_isolation(tmp_path):
    from dataclasses import replace
    from decimal import Decimal
    from app.db import crew_journal as cj, paper_ledger as pl, crew_reader
    from app.trading import research_runtime as rr, paper_notify as pn
    from app.trading.paper_risk import DeltaEvidence
    from tests.test_paper_trading import PaperFeed, SETTINGS, IND, quote, paper
    from tests.test_research_runtime import Clock, setup, rows
    path = tmp_path / 'crew_research.db'
    clock = Clock('10:19')
    base = PaperFeed([setup(stop=23995)], bid=10, ask=10.4)
    def feed(now):
        contract = quote(now, bid=10, ask=10.4).tradingsymbol
        return replace(base(now), delta_evidence={contract: DeltaEvidence(Decimal('0.5'), now, contract, 'synthetic')})
    rt = rr.ResearchRuntime(path, feed, settings=SETTINGS, clock=clock,
                           trusted_delta_sources=frozenset({'synthetic'}))
    rt.start()
    try:
        rt.tick()
        before = paper(path)
        with crew_reader.snapshot(path) as conn:
            first = tm.report(conn, account_id=IND)
            assert first == tm.report(conn, account_id=IND)
            assert set(first['account_outcomes']) == {IND}
            assert first['records'][0]['quantity'] > 0
            assert first['records'][0]['realized_pnl_inr'] == '0.00'
            assert first['records'][0]['unrealized_pnl_inr'] is None
            shared = tm.report(conn, account_id='SHARED')
            assert set(shared['account_outcomes']) == {'SHARED'}
            assert first['records'][0]['memory_id'] != shared['records'][0]['memory_id']
        assert paper(path) == before
        trade = rows(path, 'SELECT * FROM crew_paper_trades WHERE account_id=?', (IND,))[0]
        pl.close(path, lease=rt.lease, model_trade_id=trade['model_trade_id'], chain=None,
                 index_spot=24000, reason='MISSING_EXIT_QUOTE', now=clock(), settings=SETTINGS)
        with crew_reader.snapshot(path) as conn:
            unresolved = tm.report(conn, account_id=IND)
            assert unresolved['records'][0]['state'] == 'EXIT_UNPRICED'
            assert unresolved['records'][0]['reserved_worst_case_inr'] == first['records'][0]['reserved_worst_case_inr']
            assert unresolved['records'][0]['source_sha256'] != first['records'][0]['source_sha256']
        pn.sync(path, lease=rt.lease, now=clock())
        publications = rows(path, 'SELECT * FROM crew_outbox ORDER BY publication_id')
        pn.sync(path, lease=rt.lease, now=clock())
        assert publications == rows(path, 'SELECT * FROM crew_outbox ORDER BY publication_id')
        assert pl.check_consistency(path) == []
    finally:
        rt.stop()
    rt2 = rr.ResearchRuntime(path, PaperFeed(), settings=SETTINGS, clock=clock)
    rt2.start()
    rt2.stop()
    with crew_reader.snapshot(path) as conn:
        assert tm.report(conn, account_id=IND) == unresolved


def test_ordinary_bound_report_cannot_bypass_b4_deployment_gate():
    from tests.test_causal_walk_forward import _make_test_strategy_spec
    spec = _make_test_strategy_spec()
    assert p.approval_dossier_blockers({'validation_binding': spec.validation_binding()}, spec) == ['B4_PROMOTION_DOSSIER_REQUIRED']


def test_v2_concrete_mock_preview_is_redacted_and_never_delivered(tmp_path):
    from dataclasses import replace
    from decimal import Decimal
    from app.trading import research_runtime as rr
    from app.trading.paper_risk import DeltaEvidence
    from app.telegram_transport import TelegramTransport
    from tests.test_paper_trading import PaperFeed, SETTINGS, quote
    from tests.test_research_runtime import Clock, setup, rows
    path = tmp_path / 'crew_research.db'
    clock = Clock('10:19')
    base = PaperFeed([setup(stop=23995)], bid=10, ask=10.4)
    def feed(now):
        contract = quote(now, bid=10, ask=10.4).tradingsymbol
        return replace(base(now), delta_evidence={contract: DeltaEvidence(Decimal('0.5'), now, contract, 'synthetic')})
    rt = rr.ResearchRuntime(path, feed, settings=SETTINGS, clock=clock,
                           trusted_delta_sources=frozenset({'synthetic'}))
    rt.start()
    try:
        rt.tick()
        messages = rows(path, "SELECT body, chat_id FROM crew_outbox WHERE purpose LIKE '%PAPER_ENTRY_FILLED%' ORDER BY publication_id")
        assert messages
        for message in messages:
            assert message['chat_id'].startswith('mock:')
            assert 'SYNTHETIC ENGINEERING FIXTURE' in message['body']
            assert 'Greek observation:' in message['body']
            assert 'Full-premium worst-case loss:' in message['body']
            outcome = TelegramTransport('fixture-no-credential').send(message['chat_id'], message['body'])
            assert outcome.exc_class == 'PreSendValidation'
        # Export an explicitly synthetic concrete preview; no real journal, credentials or market data.
        from pathlib import Path
        Path('/tmp/genesis-telegram-mock-preview.txt').write_text(
            'SYNTHETIC LOCAL TEST PREVIEW — NOT MARKET DATA\n\n' + messages[0]['body'] + '\n')
    finally:
        rt.stop()


def test_operator_cannot_sign_before_validation_observed():
    es = artifacts()
    signed = {**signoff(es), 'signed_at': (NOW - timedelta(days=110)).isoformat()}
    assert not evaluate(evidence=es, operator_signoff=signed).allowed


def test_forward_evidence_cannot_predate_observed_sessions():
    es = artifacts('PAPER_VALIDATED')
    sessions = [{'market_day': (NOW.date() - timedelta(days=i)).isoformat(),
        'actual_forward_session': True, 'reconciliation': 'EXACT_0.00',
        'unresolved_exposure': False, 'closed_trades': 2, 'account_id': BINDING['account_id']} for i in range(1, 21)]
    es[-1] = p.Evidence.create({**es[-1].read(), 'sessions': sessions})
    assert not evaluate(current='PAPER_APPROVED', target='PAPER_VALIDATED',
        evidence=es, operator_signoff=signoff(es)).allowed


def test_deployment_dossier_cannot_bind_a_different_runtime_risk_policy():
    from tests.test_causal_walk_forward import _make_test_strategy_spec
    spec = _make_test_strategy_spec()
    binding = {**BINDING, **{k: getattr(spec, k) for k in
               ('strategy_id', 'strategy_version', 'spec_sha256', 'parameter_hash')}}
    payload = {'promotion_dossier': {'binding': binding, 'evidence': [], 'operator_signoff': {}}}
    assert p.approval_dossier_blockers(payload, spec) == ['B4_PROMOTION_RUNTIME_POLICY_MISMATCH']
