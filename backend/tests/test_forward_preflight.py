"""Readiness diagnostics are not promotion/activation authority; no external credentials used."""
from datetime import datetime
import json

from app.brokers import credentials
from app.config import Settings
from app.trading import preflight

NOW = datetime.fromisoformat('2026-11-08T12:00:00+05:30')


def isolate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv('BROKER_GROWW_ACCESS_TOKEN', raising=False)
    credentials._dotenv.cache_clear()


def test_missing_configuration_reports_external_gates_without_io(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    result = preflight.report(market_day='2026-11-09', settings=Settings(), now=NOW)
    assert result['status'] == 'BLOCKED' and not result['forward_ready']
    assert result['network_requests'] == result['database_connections'] == 0
    assert 'GROWW_ACCESS_TOKEN_NOT_CONFIGURED' in result['blockers']
    assert 'FORWARD_FEE_DATE_UNVERIFIED' in result['blockers']
    assert not result['orders_authorized'] and not result['telegram_delivery_authorized']


def test_tokens_and_operator_pins_do_not_self_certify_readiness(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    monkeypatch.setenv('BROKER_GROWW_ACCESS_TOKEN', 'private-test-value')
    settings = Settings(groww_market_data_authorized=True,
        promotion_artifact_hashes='test-pin', promotion_signoff_hashes='test-pin')
    result = preflight.report(market_day='2026-11-09', settings=settings, now=NOW)
    assert result['credentials'][0]['configured']
    assert 'private-test-value' not in json.dumps(result)
    assert 'fingerprint' not in json.dumps(result)
    assert 'PROVIDER_USE_NOT_ACKNOWLEDGED' not in result['blockers']
    assert {'PROVIDER_ENTITLEMENT_UNVERIFIED', 'SECURITY_REVIEW_UNVERIFIED',
            'GREEK_OBSERVATION_TIME_UNDOCUMENTED'} <= set(result['blockers'])
    assert not result['forward_ready']


def test_past_verified_tariff_does_not_authorize_forward_session(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    result = preflight.report(market_day='2026-11-06', settings=Settings(), now=NOW)
    assert result['fee_policy'] is not None
    assert 'PAST_FORWARD_DATE' in result['blockers'] and not result['forward_ready']


def test_cli_hides_invalid_configuration_and_private_file_details(tmp_path, monkeypatch, capsys):
    isolate(tmp_path, monkeypatch)
    settings = Settings(broker_credentials_file=str(tmp_path / 'private-sensitive-path'))
    monkeypatch.setattr(preflight, 'get_settings', lambda: settings)
    assert preflight.main(['--market-day', '2026-11-09']) == 2
    output = capsys.readouterr().out
    assert 'private-sensitive-path' not in output
    assert 'LOCAL_CREDENTIAL_CONFIGURATION_INVALID' in output
    assert preflight.main(['--market-day', 'bad-date']) == 2
    assert 'INVALID_LOCAL_CONFIGURATION_OR_DATE' in capsys.readouterr().out
