"""Release scanner rejects unsafe paths before opening their contents."""
import importlib.util
from pathlib import Path
import subprocess

import pytest

spec = importlib.util.spec_from_file_location(
    'release_security', Path(__file__).resolve().parents[2] / 'scripts/check_release_security.py')
security = importlib.util.module_from_spec(spec)
spec.loader.exec_module(security)


@pytest.mark.parametrize('name', [
    'backend/.env', 'trades/risk.json', 'backend/data/state.db-wal',
    'broker.credentials.properties', '.aws/credentials', 'logs/session.txt',
    'daily_risk_config.json', '.active_trades_example.json', 'parameter_proposals.jsonl',
])
def test_rejects_protected_paths_without_reading(tmp_path, monkeypatch, name):
    monkeypatch.setattr(subprocess, 'check_output', lambda *a, **k: (name + '\0').encode())
    # No such path exists: reading it would fail instead of reporting the boundary.
    with pytest.raises(SystemExit, match='Protected tracked path'):
        security.check(tmp_path)


def test_secret_detection_redacts_value(tmp_path, monkeypatch):
    token = 'ghp_' + 'A' * 36
    (tmp_path / 'source.py').write_text('TOKEN = ' + repr(token))
    monkeypatch.setattr(subprocess, 'check_output', lambda *a, **k: b'source.py\0')
    with pytest.raises(SystemExit, match='Possible secret') as exc:
        security.check(tmp_path)
    assert token not in str(exc.value)


def test_allows_synthetic_source_and_lockfiles(tmp_path, monkeypatch, capsys):
    names = ['uv.lock', 'synthetic.yaml']
    for name in names:
        (tmp_path / name).write_text('synthetic-token')
    monkeypatch.setattr(subprocess, 'check_output', lambda *a, **k: '\0'.join(names).encode())
    security.check(tmp_path)
    assert '2 tracked files' in capsys.readouterr().out
