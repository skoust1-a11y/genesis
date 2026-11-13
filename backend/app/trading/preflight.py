"""Offline forward-paper diagnostics. This command grants no execution or delivery authority."""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime

from app.config import Settings, get_settings
from app.db.hashing import canonical_json, sha256_hex
from app.db.types import utcnow
from app.trading.option_quote import IST


def report(*, market_day: str, settings: Settings, now: datetime | None = None, provider: str = "groww") -> dict:
    from app.brokers import credentials, registry
    from app.brokers.groww_api import GrowwMarketData
    from app.trading import paper_fees, paper_registry
    from app.trading.walk_forward import WALK_FORWARD_ENGINE_VERSION

    intended = date.fromisoformat(market_day)
    now = now or utcnow()
    if now.utcoffset() is None:
        raise ValueError('aware clock required')
    if provider not in ('groww', 'upstox'):
        raise ValueError('unsupported provider')
    from app.brokers.upstox_api import UpstoxMarketData
    reader = GrowwMarketData if provider == 'groww' else UpstoxMarketData
    blockers = list(reader.capabilities()['blockers'])
    if intended < now.astimezone(IST).date():
        blockers.append('PAST_FORWARD_DATE')
    # This never connects to the credential database or constructs a network client.
    try:
        fields = credentials.status(None, None, registry.get_spec(provider), settings)
        configured = credentials.configured(fields)
        credential_status = [{'name': f.name, 'configured': f.source is not None, 'source': f.source} for f in fields]
        if not configured:
            blockers.append(provider.upper() + '_ACCESS_TOKEN_NOT_CONFIGURED')
    except (ValueError, OSError):
        credential_status = []
        blockers.append('LOCAL_CREDENTIAL_CONFIGURATION_INVALID')
    if not getattr(settings, provider + '_market_data_authorized'):
        blockers.append('PROVIDER_USE_NOT_ACKNOWLEDGED')
    fee_policy = None
    try:
        if provider != 'groww':
            raise paper_fees.FeePolicyUnavailable('SELECTED_BROKER_FEE_POLICY_UNVERIFIED')
        policy = paper_fees.resolve_policy(intended.isoformat())
        fee_policy = {'policy_id': policy.policy_id,
                      'sha256': sha256_hex(canonical_json(policy.snapshot()))}
    except paper_fees.FeePolicyUnavailable:
        blockers.append('FORWARD_FEE_DATE_UNVERIFIED')
    # Configuration presence cannot establish entitlement, security or real sessions.
    blockers += ['PROVIDER_ENTITLEMENT_UNVERIFIED', 'SECURITY_REVIEW_UNVERIFIED',
                 'EXACT_STRATEGY_PROMOTION_DOSSIER_REQUIRED', 'MARKET_SESSION_EVIDENCE_REQUIRED',
                 'REAL_TELEGRAM_ACTIVATION_NOT_APPROVED']
    return {'schema_version': 'forward-preflight-v1', 'provider': provider,
        'intended_market_day': intended.isoformat(), 'checked_at': now.isoformat(),
        'status': 'BLOCKED', 'forward_ready': False, 'checks_performed': 'OFFLINE_CONFIGURATION_ONLY',
        'credentials': credential_status, 'fee_policy': fee_policy,
        'engine_version': WALK_FORWARD_ENGINE_VERSION,
        'risk_policy_sha256': sha256_hex(canonical_json(paper_registry.RISK_POLICY)),
        'blockers': sorted(set(blockers)), 'network_requests': 0, 'database_connections': 0,
        'orders_authorized': False, 'telegram_delivery_authorized': False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--market-day', required=True, help='Intended session date, YYYY-MM-DD')
    parser.add_argument('--provider', choices=('groww', 'upstox'), default='groww')
    args = parser.parse_args(argv)
    try:
        result = report(market_day=args.market_day, settings=get_settings(), provider=args.provider)
    except (ValueError, OSError):
        # Pydantic validation/OS exceptions may contain private configuration details.
        result = {'status': 'BLOCKED', 'forward_ready': False,
                  'blockers': ['INVALID_LOCAL_CONFIGURATION_OR_DATE'],
                  'orders_authorized': False, 'telegram_delivery_authorized': False}
    print(json.dumps(result, sort_keys=True, indent=2))
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
