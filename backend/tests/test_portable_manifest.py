"""Manifest trust and mode checks, independent of historical Git objects."""
import hashlib
import json
from pathlib import Path
import shutil
import pytest
from frozen_source_guard import verify, MANIFEST_DIGEST

ROOT = Path(__file__).resolve().parents[2]

@pytest.fixture
def portable_copy(tmp_path):
    raw = (ROOT / 'frozen-source-manifest.json').read_bytes()
    (tmp_path / 'frozen-source-manifest.json').write_bytes(raw)
    for row in json.loads(raw)['files']:
        p = tmp_path / row['path']
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / row['path'], p)
        p.chmod(0o644)
    return tmp_path


def test_owner_digest_matches_exact_bytes(portable_copy):
    assert hashlib.sha256((portable_copy / 'frozen-source-manifest.json').read_bytes()).hexdigest() == MANIFEST_DIGEST
    assert verify(portable_copy) == 18

@pytest.mark.parametrize('operation', ['delete', 'edit', 'symlink'])
def test_manifest_cannot_be_replaced(portable_copy, operation):
    p = portable_copy / 'frozen-source-manifest.json'
    if operation == 'delete':
        p.unlink()
    elif operation == 'edit':
        p.write_text('{}')
    else:
        other = portable_copy / 'other.json'
        p.rename(other)
        p.symlink_to(other)
    with pytest.raises((AssertionError, OSError)):
        verify(portable_copy)


def test_executable_mode_is_rejected(portable_copy):
    (portable_copy / 'backend/app/liquidity_signals.py').chmod(0o755)
    with pytest.raises(AssertionError, match='mode'):
        verify(portable_copy)


def test_staged_cache_cannot_hide_behind_good_worktree(portable_copy):
    import marshal
    import py_compile
    import subprocess
    source = portable_copy / 'backend/app/liquidity_signals.py'
    cache = Path(py_compile.compile(str(source), doraise=True))
    good = cache.read_bytes()
    cache.write_bytes(good[:16] + marshal.dumps(compile('FORGED = True', str(source), 'exec')))
    subprocess.run(['git', 'init', '-q'], cwd=portable_copy, check=True)
    subprocess.run(['git', 'add', str(cache.relative_to(portable_copy))], cwd=portable_copy, check=True)
    cache.write_bytes(good)
    with pytest.raises(AssertionError, match='Unverified executable cache'):
        verify(portable_copy)


@pytest.mark.parametrize('directory', ['.venv', 'node_modules', '.git'])
def test_runtime_directory_names_do_not_exempt_guarded_additions(portable_copy, directory):
    payload = portable_copy / 'backend/app/trading/crew' / directory / 'payload.py'
    payload.parent.mkdir()
    payload.write_text('FORGED = True\n')
    with pytest.raises(AssertionError, match='Unauthorized'):
        verify(portable_copy)
