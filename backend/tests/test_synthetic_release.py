"""Independent synthetic release oracles. No historical performance claim."""
import json
from decimal import Decimal

from tests import test_live_replay as replay
from tests.test_live_replay import identified_synthetic_contracts, runs


def test_full_synthetic_day_expected_outcomes_and_repeatability(runs, tmp_path):
    expected_times = ['2026-11-04T10:15:05+05:30', '2026-11-04T10:45:05+05:30']
    for lane in ('LIVE', 'SYNTHETIC'):
        original = runs[lane]
        summary = original['summary']
        assert len(summary['candidates']) == 2
        assert summary['days'] == [{
            'day': '2026-11-04', 'status': 'REPLAYED', 'detail': None, 'ticks': 376,
            'skipped': {'NO_CLOSED_BAR': 5},
            'rejected': {'1m:SESSION_OPEN_BAR_MISSING': 371, '15m:SESSION_OPEN_BAR_MISSING': 361}}]
        assert [a[2] for a in summary['admissions']] == expected_times
        expected_class = 'REJECTED' if lane == 'LIVE' else 'RESEARCH_ONLY'
        expected_reasons = ['QUOTE_MISSING'] if lane == 'LIVE' else [
            'CAPITAL_UNAVAILABLE', 'CASH_UNAVAILABLE', 'MAX_LOSS_UNVERIFIED']
        assert [(a[0], a[1]) for a in summary['admissions']] == [(expected_class, expected_reasons)] * 2
        if lane == 'LIVE':
            assert summary['events'] == []
        else:
            assert [e[1] for e in summary['events']] == ['MODEL_ENTRY', 'MODEL_EXIT'] * 2
            entries = [e[2] for e in summary['events'] if e[1] == 'MODEL_ENTRY']
            assert [(e['entry_spot'], e['stop_spot'], e['target_spot']) for e in entries] == [
                (20036.0, 20005.0, 20097.0), (20060.0, 20029.0, 20147.0)]
            exits = [e[2] for e in summary['events'] if e[1] == 'MODEL_EXIT']
            # Flat for 30 minutes closes +24 points; a gap through the next stop books -31.
            assert [(e['reason'], e['model_points'], e['model_r']) for e in exits] == [
                ('STAGNATION_TIME_STOP', 24.0, 0.77), ('STOP_LOSS_HIT', -31.0, -1.0)]
        expected = [] if lane == 'LIVE' else [
            ['IND:RETEST_OR30', 'REJECTED', ['FULL_PREMIUM_LIMIT'], expected_times[0]],
            ['SHARED', 'REJECTED', ['FULL_PREMIUM_LIMIT'], expected_times[0]],
            ['IND:RETEST_PD', 'REJECTED', ['FULL_PREMIUM_LIMIT'], expected_times[1]],
            ['SHARED', 'REJECTED', ['FULL_PREMIUM_LIMIT'], expected_times[1]]]
        assert summary['paper_decisions'] == expected
        journal = original['journal']
        assert replay.lr.journal_rows(journal, 'SELECT * FROM crew_paper_trades') == []
        assert replay.lr.journal_rows(journal, 'SELECT * FROM crew_paper_events') == []
        if lane == 'SYNTHETIC':
            replay.check_paper_risk(journal)
            accounts = replay.lr.journal_rows(journal, 'SELECT * FROM crew_paper_accounts')
            assert len(accounts) == 6
            assert {Decimal(a['capital_inr']) for a in accounts} == {Decimal('50000')}
        repeat = tmp_path / (lane + '.db')
        assert replay._summary(*replay._replay(lane, repeat), repeat) == summary


def test_independent_stop_exit_accounting_golden(tmp_path):
    from tests import test_exit_lifecycle as exits
    from app.db import crew_journal, paper_ledger
    from app.trading import paper_stats

    journal = tmp_path / 'stop.db'
    tid, actions = exits.run(journal, steps=[('10:24:05', 23950.0)])
    assert actions == [('STOP_LOSS_HIT', -30.0)]
    exits._check_closed(journal, tid, 'STOP_LOSS_HIT')
    trades = exits.rows(journal, 'SELECT * FROM crew_paper_trades ORDER BY account_id')
    assert [t['account_id'] for t in trades] == ['IND:LIQ_SWEEP_PDHL', 'SHARED']
    events = exits.rows(journal, "SELECT side, qty, price_inr, charges_inr FROM crew_paper_events WHERE side IS NOT NULL ORDER BY paper_trade_id, seq")
    # One lot: (29.95 - 50.45) * 65 = -1332.50; entry + exit fees = 52.49.
    assert [(e['side'], e['qty'], Decimal(e['price_inr']), Decimal(e['charges_inr'])) for e in events] == [
        ('BUY', 65, Decimal('50.45'), Decimal('25.08')),
        ('SELL', 65, Decimal('29.95'), Decimal('27.41'))] * 2
    conn = crew_journal.connect_readonly(journal)
    try:
        for t in trades:
            accounting = paper_stats.account_stats(conn, t['account_id'])['accounting']
            assert Decimal(accounting['realized_pnl_inr']) == Decimal('-1384.99')
            assert Decimal(accounting['cash_inr']) == Decimal('48615.01')
            assert accounting['charge_reconciliation'] == 'EXACT'
    finally:
        conn.close()
    assert paper_ledger.check_consistency(journal) == []
