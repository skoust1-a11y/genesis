"""Private credential inputs never authorize a provider, order or notification."""
from types import SimpleNamespace

import pytest

from app.brokers import credentials as c
from app.brokers.registry import BrokerSpec, CredentialField
from app.config import Settings


def setup_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    c._dotenv.cache_clear()
    monkeypatch.delenv('BROKER_EXAMPLE_API_KEY', raising=False)
    path = tmp_path / 'broker.credentials.properties'
    path.write_text('BROKER_EXAMPLE_API_KEY=file-$literal\nBROKER_OTHER_API_KEY=other\n')
    path.chmod(0o600)
    return path, Settings(broker_credentials_file=str(path))


def test_private_file_resolution_redaction_and_precedence(tmp_path, monkeypatch):
    path, settings = setup_file(tmp_path, monkeypatch)
    spec = BrokerSpec('example', 'Example', False, lambda *a: None, lambda s: ('paper',),
                      (CredentialField('api_key', 'Key'),))
    store = SimpleNamespace(broker_credentials=lambda _: {})
    account = SimpleNamespace(id='test')
    assert c.resolve(store, account, spec, settings) == {'api_key': 'file-$literal'}
    status = c.status(store, account, spec, settings)[0].as_dict()
    assert status['source'] == 'file' and 'file-$literal' not in str(status)
    path.write_text('BROKER_EXAMPLE_API_KEY=rotated\n')
    assert c.resolve(store, account, spec, settings)['api_key'] == 'rotated'
    assert c._configured_value('missing', 'api_key', settings) == (None, None)
    monkeypatch.setenv('BROKER_EXAMPLE_API_KEY', 'environment')
    assert c.resolve(store, account, spec, settings)['api_key'] == 'environment'
    assert c.status(store, account, spec, settings)[0].source == 'env'
    monkeypatch.delenv('BROKER_EXAMPLE_API_KEY')
    (tmp_path / '.env').write_text('BROKER_EXAMPLE_API_KEY=dotenv\n')
    c._dotenv.cache_clear()
    assert c.resolve(store, account, spec, settings)['api_key'] == 'dotenv'
    c._dotenv.cache_clear()


def test_unreadable_public_and_symlink_files_fail_closed(tmp_path, monkeypatch):
    path, settings = setup_file(tmp_path, monkeypatch)
    path.chmod(0o644)
    with pytest.raises(ValueError, match='private regular file'):
        c._configured_value('example', 'api_key', settings)
    path.chmod(0o600)
    link = tmp_path / 'link.properties'
    link.symlink_to(path)
    with pytest.raises(ValueError, match='private regular file'):
        c._configured_value('example', 'api_key', Settings(broker_credentials_file=str(link)))
    path.unlink()
    with pytest.raises(ValueError, match='cannot be read'):
        c._configured_value('example', 'api_key', settings)


def test_fifo_is_rejected_without_waiting_for_a_writer(tmp_path, monkeypatch):
    import os
    if not hasattr(os, 'mkfifo'):
        pytest.skip('POSIX FIFO check')
    path, settings = setup_file(tmp_path, monkeypatch)
    path.unlink()
    os.mkfifo(path, 0o600)
    with pytest.raises(ValueError, match='private regular file'):
        c._configured_value('example', 'api_key', settings)
