"""Versioned rebuildable projections; source evidence and financial journals are never changed."""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any, Mapping

from app.db.hashing import canonical_json, normalize_json, sha256_hex
from app.trading import paper_accounting as pa

SCHEMA_VERSION = 'trade-memory-v1'

# Classifications describe observed evidence, never a guessed cause of a loss.
REASONS = {
    'FULL_PREMIUM_LIMIT': 'RISK_REJECTION', 'STOP_RISK_LIMIT': 'RISK_REJECTION',
    'DAILY_EXPOSURE_LIMIT': 'RISK_REJECTION', 'MAX_POSITIONS_EXCEEDED': 'RISK_REJECTION',
    'INSUFFICIENT_CAPITAL': 'RISK_REJECTION', 'STOP_RISK_UNVERIFIABLE': 'DATA_FAILURE',
    'QUOTE_MISSING': 'DATA_FAILURE', 'QUOTE_STALE': 'DATA_FAILURE',
    'NO_BID_ASK': 'DATA_FAILURE', 'MISSING_EXIT_QUOTE': 'DATA_FAILURE',
    'MISSING_FUTURE_BARS': 'DATA_FAILURE', 'ENTRY_MISSED_AFTER_RESTART': 'OPERATIONAL_FAILURE',
    'EXPIRED_UNRESOLVED': 'OPERATIONAL_FAILURE', 'RULE_VIOLATION': 'STRATEGY_FAILURE',
    'SIMULATED_FILL_REJECTED': 'EXECUTION_FAILURE', 'REGIME_NOT_ALLOWED': 'REGIME_MISMATCH',
}


def classify_failures(reasons: list[str]) -> list[dict]:
    found = [{'category': REASONS[r], 'evidence': r} for r in sorted(set(reasons)) if r in REASONS]
    return found or [{'category': 'UNKNOWN', 'evidence': 'INSUFFICIENT_CAUSAL_EVIDENCE'}]


@dataclass(frozen=True)
class CanonicalTradeMemoryRecord:
    """Strings keep both the source snapshot and projection immutable after construction."""
    memory_id: str
    source_sha256: str
    projection_sha256: str
    evidence_json: str
    projection_json: str
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {'schema_version': self.schema_version, 'memory_id': self.memory_id,
                'source_sha256': self.source_sha256, 'projection_sha256': self.projection_sha256,
                **json.loads(self.projection_json)}


def _record(kind: str, trade_id: str, account: str, evidence: Mapping, projection: Mapping):
    source = canonical_json(evidence)
    body = canonical_json({'source_kind': kind, 'source_id': trade_id, 'account_id': account,
        'mae_inr': None, 'mfe_inr': None, 'excursion_status': 'NO_COMPLETE_EXECUTABLE_OPTION_PATH',
        **projection})
    return CanonicalTradeMemoryRecord(
        sha256_hex(canonical_json([SCHEMA_VERSION, kind, account, trade_id])), sha256_hex(source),
        sha256_hex(canonical_json([SCHEMA_VERSION, sha256_hex(source), json.loads(body)])), source, body)


def project_research(event, execution, *, strategy_id: str, strategy_version: str,
                     parameter_hash: str, account_id: str, validation_binding: Mapping):
    """Project a replay result after account admission. Synthetic fidelity stays explicit."""
    ev, ex = asdict(event), asdict(execution)
    reasons = [r for r in (execution.rejection_reason, execution.exit_reason) if r]
    admitted = execution.admitted
    complete = admitted and execution.priced and not execution.unresolved
    return _record('RESEARCH', event.event_id, account_id, {'event': ev, 'execution': ex,
        'validation_binding': dict(validation_binding)}, {
        'strategy_id': strategy_id, 'strategy_version': strategy_version,
        'parameter_hash': parameter_hash, 'validation_binding': dict(validation_binding),
        'signal_context': ev, 'regime': event.regime, 'entry_rationale': event.metadata.get('entry_rationale'),
        'exit_rationale': event.exit_reason, 'quantity': execution.qty if admitted else 0,
        'state': 'REJECTED' if not admitted else 'REALIZED' if complete else 'UNRESOLVED',
        'fidelity_tier': execution.fidelity_tier, 'verified_market_data': execution.verified_market_data,
        'cost_policy_id': execution.cost_policy_id,
        'fee_provenance': {'entry': json.loads(execution.entry_cost_evidence_json) if execution.entry_cost_evidence_json else None,
                           'exit': json.loads(execution.exit_cost_evidence_json) if execution.exit_cost_evidence_json else None},
        'realized_pnl_inr': str(pa.money(Decimal(str(execution.net_pnl_inr)))) if complete else None,
        'unrealized_pnl_inr': None, 'unresolved_exposure': admitted and not complete,
        'risk_decision': {'admitted': admitted, 'reason': execution.rejection_reason},
        'costs': {k: v for k, v in ex.items() if 'charges' in k or 'slippage' in k or 'reserve' in k},
        'failure_analysis': classify_failures(reasons), 'recovery_anomalies': [],
        'promotion_eligible': False})


def _rows(conn, sql, args=()):
    return [dict(row) for row in conn.execute(sql, args)]


def project_paper(conn, trade: Mapping) -> CanonicalTradeMemoryRecord:
    """Use canonical event replay and the persisted historical reservation, never today's fees."""
    t = dict(trade)
    events = _rows(conn, 'SELECT * FROM crew_paper_events WHERE paper_trade_id=? ORDER BY seq', (t['paper_trade_id'],))
    admission = dict(conn.execute('SELECT * FROM crew_admissions WHERE admission_id=?', (t['admission_id'],)).fetchone())
    risk_row = conn.execute('SELECT evidence FROM crew_paper_risk_decisions WHERE account_id=? AND admission_id=?',
                            (t['account_id'], t['admission_id'])).fetchone()
    risk = json.loads(risk_row[0]) if risk_row else None
    entry = events[0]
    debit = pa.money(entry['price_inr']) * entry['qty'] + pa.money(entry['charges_inr'])
    p = pa.project_trade(events, liquidation_cost=pa.money(t['max_loss_inr']) - debit)
    anomalies = []
    if (p.quantity != t['lots_open'] * t['lot_size'] or p.state != t['state']
            or debit != pa.money(t['capital_committed_inr'])):
        anomalies.append('PERSISTED_PROJECTION_MISMATCH')
    if p.charge_discrepancies:
        anomalies.append('CHARGE_RECONCILIATION_FAILED')
    context = json.loads(admission['inputs'])
    return _record('PAPER', t['paper_trade_id'], t['account_id'],
        {'trade': t, 'events': events, 'admission': admission, 'risk': risk}, {
        'strategy_id': t['strategy_id'], 'strategy_version': t['strategy_version'],
        'parameter_hash': (risk or {}).get('parameter_hash', context.get('parameter_hash')), 'registry_hash': t['registry_hash'],
        'signal_context': context, 'regime': t['regime'], 'entry_rationale': entry['reason'],
        'exit_rationale': events[-1]['reason'] if len(events) > 1 else None,
        'quote_provenance': [{k: e[k] for k in ('quote_source', 'quote_time', 'quote_age_s', 'bid', 'ask')} for e in events],
        'greek_provenance': (risk or {}).get('delta'), 'risk_decision': risk,
        'quantity': p.quantity, 'entry_quantity': p.entry_quantity, 'state': p.state,
        'realized_pnl_inr': str(p.realized), 'unrealized_pnl_inr': None,
        'committed_cash_inr': str(p.remaining_basis), 'reserved_worst_case_inr': str(p.worst_case_reserved),
        'costs_inr': str(p.charges), 'slippage_inr': str(p.slippage),
        'unresolved_exposure': bool(p.quantity), 'recovery_anomalies': anomalies,
        'failure_analysis': classify_failures([e['reason'] for e in events]),
        'evidence_label': 'PAPER_SIMULATION_NOT_BROKER_EXECUTION', 'promotion_eligible': False})


def project_rejection(decision: Mapping):
    d = dict(decision)
    context = json.loads(d['inputs'])
    return _record('PAPER_DECISION', d['admission_id'], d['account_id'], d, {
        'strategy_id': d['strategy_id'], 'strategy_version': context.get('strategy_version'),
        'parameter_hash': context.get('parameter_hash'), 'registry_hash': d['registry_hash'],
        'signal_context': context, 'state': d['outcome'], 'quantity': 0,
        'risk_decision': {'outcome': d['outcome'], 'reasons': json.loads(d['reasons'])},
        'realized_pnl_inr': None, 'unrealized_pnl_inr': None, 'unresolved_exposure': False,
        'failure_analysis': classify_failures(json.loads(d['reasons'])),
        'recovery_anomalies': [], 'promotion_eligible': False})


def project_model(conn, trade: Mapping):
    t = dict(trade)
    events = _rows(conn, 'SELECT * FROM crew_events WHERE trade_id=? ORDER BY seq', (t['trade_id'],))
    payload = json.loads(events[0]['payload']) if events else {}
    return _record('MODEL', t['trade_id'], 'MODEL_ONLY', {'trade': t, 'events': events}, {
        'strategy_id': t['strategy'], 'strategy_version': payload.get('strategy_version'),
        'parameter_hash': payload.get('parameter_hash'), 'signal_context': payload,
        'state': t['state'], 'quantity': None, 'model_lots_open': t['model_lots_open'],
        'realized_pnl_inr': None, 'unrealized_pnl_inr': None, 'unresolved_exposure': None,
        'failure_analysis': classify_failures([json.loads(e['payload']).get('reason', '') for e in events]),
        'recovery_anomalies': [], 'evidence_label': 'UNDERLYING_MODEL_ONLY_NO_OPTION_FILLS',
        'promotion_eligible': False})


def report(conn, *, account_id: str | None = None, limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """Read-only bounded canonical projection API. Account scenarios are never added together."""
    from app.db import crew_reader, paper_ledger
    if type(limit) is not int or not 1 <= limit <= 500 or type(offset) is not int or offset < 0:
        raise ValueError('invalid memory page')
    clause, args = (' WHERE account_id=?', (account_id,)) if account_id else ('', ())
    trades = _rows(conn, 'SELECT * FROM crew_paper_trades' + clause + ' ORDER BY paper_trade_id LIMIT ? OFFSET ?', (*args, limit, offset))
    records = [project_paper(conn, t).to_dict() for t in trades]
    if account_id is None:
        records += [project_model(conn, t).to_dict() for t in _rows(conn,
            'SELECT * FROM crew_trades ORDER BY trade_id LIMIT ? OFFSET ?', (limit, offset))]
    rejection_clause = " AND d.account_id=?" if account_id else ""
    rejected = _rows(conn, "SELECT d.*, a.inputs FROM crew_paper_decisions d JOIN crew_admissions a"
        " ON d.admission_id=a.admission_id WHERE d.outcome <> 'OPENED'" + rejection_clause +
        " ORDER BY d.account_id, d.admission_id LIMIT ? OFFSET ?", (*args, limit, offset))
    records += [project_rejection(d).to_dict() for d in rejected]
    accounts = [r[0] for r in conn.execute('SELECT account_id FROM crew_paper_accounts' + clause + ' ORDER BY account_id', args)]
    summaries = {a: normalize_json(asdict(paper_ledger.account_projection(conn, a))) for a in accounts}
    failures = Counter(f['category'] for r in records for f in r['failure_analysis'])
    return crew_reader.redact({'schema_version': SCHEMA_VERSION, 'records': records,
        'account_outcomes': summaries, 'failure_counts_for_page': dict(sorted(failures.items())),
        'promotion_blockers': ['NO_VERIFIED_PROMOTION_DOSSIER', 'STAGE_C_EXTERNAL_GATES_BLOCKED'],
        'access': 'READ_ONLY', 'orders_authorized': False, 'limit': limit, 'offset': offset})
