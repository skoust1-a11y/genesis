"""Evidence-bound research lifecycle. This module grants no order or delivery authority."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Mapping, Sequence

from app.db.hashing import canonical_json, sha256_hex
from app.trading.paper_fees import FeePolicyUnavailable

STATES = ('EXTRACTED', 'NORMALIZED', 'RESEARCH_CANDIDATE', 'BACKTESTED',
          'OUT_OF_SAMPLE_VALIDATED', 'WALK_FORWARD_VALIDATED', 'PAPER_APPROVED',
          'PAPER_VALIDATED', 'OPERATOR_REVIEW')
BINDING_KEYS = frozenset({'strategy_id', 'strategy_version', 'spec_sha256', 'parameter_hash',
    'dataset_sha256', 'engine_version', 'risk_policy_sha256', 'fee_policy_sha256', 'account_id'})
VERIFIED_STATES = frozenset(STATES[4:])


def _aware(value):
    stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(stamp, datetime) or stamp.utcoffset() is None:
        raise ValueError('evidence time must be timezone-aware')
    return stamp


def binding_blockers(binding: Mapping) -> list[str]:
    return ['INCOMPLETE_BINDING'] if (set(binding) != BINDING_KEYS or
        any(not isinstance(v, str) or not v.strip() for v in binding.values())) else []


@dataclass(frozen=True)
class Evidence:
    """Immutable content-addressed artifact. Hash identity is not a claim of authenticity."""
    payload_json: str
    sha256: str

    @classmethod
    def create(cls, payload: Mapping):
        text = canonical_json(payload)
        return cls(text, sha256_hex(text))

    def read(self) -> dict:
        if sha256_hex(self.payload_json) != self.sha256:
            raise ValueError('ARTIFACT_HASH_MISMATCH')
        payload = json.loads(self.payload_json)
        if not isinstance(payload, dict):
            raise ValueError('INVALID_ARTIFACT')
        return payload


@dataclass(frozen=True)
class PromotionDecision:
    state: str
    target: str
    allowed: bool
    blockers: tuple[str, ...]
    evidence_hashes: tuple[str, ...]
    dossier_sha256: str
    orders_authorized: bool = False

    def to_dict(self):
        from dataclasses import asdict
        return asdict(self)


def evaluate(*, current: str, target: str, binding: Mapping, evidence: Sequence[Evidence],
             now: datetime, trusted_validators: frozenset[str] = frozenset(),
             operator_signoff: Mapping | None = None,
             trusted_operators: frozenset[str] = frozenset()) -> PromotionDecision:
    """Revalidate every prerequisite on every transition; ordinary review reports are insufficient.

    Trust sets must come from operator-controlled configuration, not an artifact's self claims.
    An operator signoff binds the complete evidence dossier and exact policy/data/strategy identity.
    """
    now = _aware(now)
    blockers = binding_blockers(binding)
    if current not in STATES or target not in STATES or STATES.index(target) != STATES.index(current) + 1:
        blockers.append('INVALID_TRANSITION')
        return PromotionDecision(current, target, False, tuple(blockers), (), '')
    hashes = tuple(sorted(e.sha256 for e in evidence))
    dossier = sha256_hex(canonical_json({'binding': dict(binding), 'artifacts': hashes}))
    required = STATES[:STATES.index(target) + 1]
    by_state = {}
    for artifact in evidence:
        try:
            p = artifact.read()
            stage = p.get('stage')
            if stage in by_state:
                blockers.append('DUPLICATE_STAGE_EVIDENCE')
            by_state[stage] = p
            if (p.get('binding') != dict(binding) or p.get('passed') is not True
                    or p.get('complete') is not True):
                blockers.append('MISMATCHED_OR_INCOMPLETE_EVIDENCE')
            if not _aware(p.get('observed_at')) <= now <= _aware(p.get('valid_until')):
                blockers.append('STALE_OR_FUTURE_EVIDENCE')
            if p.get('issuer') not in trusted_validators:
                blockers.append('UNTRUSTED_VALIDATOR')
            if (stage in VERIFIED_STATES or (stage == "BACKTESTED" and target in VERIFIED_STATES)) and (p.get('synthetic') is not False
                    or p.get('verified_market_data') is not True
                    or p.get('data_authorized') is not True):
                blockers.append('VERIFIED_AUTHORIZED_MARKET_EVIDENCE_REQUIRED')
            if stage == 'WALK_FORWARD_VALIDATED' and (p.get('causal') is not True
                    or type(p.get('oos_folds')) is not int or p.get('oos_folds', 0) < 1 or p.get('leakage_checked') is not True):
                blockers.append('WALK_FORWARD_EVIDENCE_REQUIRED')
        except (ValueError, TypeError, KeyError):
            blockers.append('INVALID_ARTIFACT')
    # PAPER_APPROVED is a signed gate, not an ordinary report artifact.
    for stage in required:
        if stage in ('PAPER_APPROVED', 'OPERATOR_REVIEW'):
            continue
        if stage not in by_state:
            blockers.append(f'MISSING_EVIDENCE:{stage}')
    if STATES.index(target) >= STATES.index('PAPER_APPROVED'):
        signoff = operator_signoff or {}
        try:
            # A later forward artifact must not invalidate the prior paper approval signature.
            approval_hashes = sorted(e.sha256 for e in evidence if e.read().get('stage') in STATES[:6])
            approval_dossier = sha256_hex(canonical_json({'binding': dict(binding), 'artifacts': approval_hashes}))
            if (signoff.get('operator') not in trusted_operators or signoff.get('target') != 'PAPER_APPROVED'
                    or signoff.get('dossier_sha256') != approval_dossier
                    or not max(_aware(a['observed_at']) for stage, a in by_state.items()
                               if stage in STATES[:6]) <= _aware(signoff.get('signed_at')) <= now):
                blockers.append('EXPLICIT_OPERATOR_SIGNOFF_REQUIRED')
        except (ValueError, TypeError, KeyError):
            blockers.append('EXPLICIT_OPERATOR_SIGNOFF_REQUIRED')
    if STATES.index(target) >= STATES.index('PAPER_VALIDATED'):
        forward = by_state.get('PAPER_VALIDATED', {})
        try:
            sessions = forward.get('sessions', [])
            days = [date.fromisoformat(s['market_day']) for s in sessions]
            sign_time = _aware((operator_signoff or {}).get('signed_at'))
            if (len(days) != len(set(days)) or len(days) < 20
                    or any(d <= sign_time.date() or d >= now.date() for d in days)
                    or any(d >= _aware(forward.get('observed_at')).date() for d in days)
                    or any(s.get('actual_forward_session') is not True
                           or s.get('reconciliation') != 'EXACT_0.00'
                           or s.get('unresolved_exposure') is not False
                           or s.get('account_id') != binding.get('account_id')
                           or type(s.get('closed_trades')) is not int or s['closed_trades'] < 0 for s in sessions)
                    or sum(s['closed_trades'] for s in sessions) < 30):
                blockers.append('ACTUAL_RECONCILED_FORWARD_SESSIONS_REQUIRED')
        except (ValueError, TypeError, KeyError):
            blockers.append('ACTUAL_RECONCILED_FORWARD_SESSIONS_REQUIRED')
    if target == 'OPERATOR_REVIEW':
        # This is a review queue, never a broker authorization or automatic deployment.
        pass
    return PromotionDecision(current, target, not blockers, tuple(sorted(set(blockers))), hashes, dossier)


def specification():
    return {'schema_version': 'promotion-v1', 'states': list(STATES),
        'binding_keys': sorted(BINDING_KEYS), 'trust_defaults': 'EMPTY',
        'paper_approval': 'EXPLICIT_OPERATOR_SIGNOFF_ON_EXACT_DOSSIER',
        'paper_validation': '20_ACTUAL_RECONCILED_SESSIONS_AND_30_CLOSED_TRADES',
        'current_blockers': ['NO_VERIFIED_PROMOTION_DOSSIER', 'STAGE_C_EXTERNAL_GATES_BLOCKED'],
        'orders_authorized': False, 'delivery_authorized': False}


def approval_dossier_blockers(report_payload, spec, *, settings=None):
    """Deployment boundary: self-declared report fields cannot establish validator/operator trust.

    Only artifacts and signoffs explicitly pinned in operator configuration are accepted here.
    Empty defaults keep real activation closed. No API writes these pins.
    """
    from app.config import get_settings
    from app.db.types import utcnow
    settings = settings or get_settings()
    dossier = report_payload.get('promotion_dossier') if isinstance(report_payload, dict) else None
    if not isinstance(dossier, dict):
        return ['B4_PROMOTION_DOSSIER_REQUIRED']
    try:
        binding = dossier['binding']
        for key in ('strategy_id', 'strategy_version', 'spec_sha256', 'parameter_hash'):
            if binding[key] != getattr(spec, key):
                return ['B4_PROMOTION_BINDING_MISMATCH']
        from app.trading import paper_registry, paper_fees
        from app.trading.walk_forward import WALK_FORWARD_ENGINE_VERSION
        from app.trading.option_quote import IST
        if (binding['risk_policy_sha256'] != sha256_hex(canonical_json(paper_registry.RISK_POLICY))
                or binding['engine_version'] != WALK_FORWARD_ENGINE_VERSION):
            return ['B4_PROMOTION_RUNTIME_POLICY_MISMATCH']
        intended_day = dossier['intended_market_day']
        if date.fromisoformat(intended_day) < utcnow().astimezone(IST).date():
            return ['B4_PROMOTION_FORWARD_DATE_REQUIRED']
        fee = paper_fees.resolve_policy(intended_day, policy_id=dossier.get('cost_policy_id'))
        if binding['fee_policy_sha256'] != sha256_hex(canonical_json(fee.snapshot())):
            return ['B4_PROMOTION_FEE_POLICY_MISMATCH']
        evidence = [Evidence(**e) for e in dossier['evidence']]
        pins = frozenset(settings.promotion_artifact_hashes.split(',')) - {''}
        signoff = dossier['operator_signoff']
        sign_pins = frozenset(settings.promotion_signoff_hashes.split(',')) - {''}
        if any(e.sha256 not in pins for e in evidence) or sha256_hex(canonical_json(signoff)) not in sign_pins:
            return ['B4_PROMOTION_UNTRUSTED_ARTIFACT_OR_SIGNOFF']
        decision = evaluate(current='WALK_FORWARD_VALIDATED', target='PAPER_APPROVED',
            binding=binding, evidence=evidence, now=utcnow(), operator_signoff=signoff,
            trusted_validators=frozenset(settings.promotion_validator_ids.split(',')) - {''},
            trusted_operators=frozenset(settings.promotion_operator_ids.split(',')) - {''})
        return list(decision.blockers)
    except FeePolicyUnavailable as exc:
        return [f'B4_PROMOTION:{exc}']
    except (ValueError, TypeError, KeyError, AttributeError):
        return ['B4_PROMOTION_INVALID_DOSSIER']
