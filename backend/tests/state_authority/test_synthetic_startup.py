"""Disposable synthetic journals only; guarded runner blocks operational state and network."""
from dataclasses import replace
from datetime import datetime
import hashlib
import os
from pathlib import Path
import sqlite3

import pytest

from app.state_authority import AuthorityError, StateAuthorityContract
from app.synthetic_research_state import IDENTITY_KEY
from app.trading import research_runtime as rr
from test_runtime_binding import contract, journal, settings


@pytest.fixture
def setup(tmp_path, monkeypatch):
    c = contract(tmp_path)
    d = c.deployments[0]
    Path(d.permitted_root).mkdir(mode=0o700)
    monkeypatch.setattr(rr, '_check_calendar', lambda *a: None)
    monkeypatch.setattr(rr, 'code_rev', lambda: 'synthetic-test')
    def forbidden(*a, **k):
        pytest.fail('Unexpected feed/network/delivery activity')
    monkeypatch.setattr(rr.paper_delivery, 'deliver_pending', forbidden, raising=False)
    def make(**overrides):
        args = dict(settings=settings(), state_contract=c, deployment_id=d.deployment_id,
                    account_id=d.account_id, synthetic_research=True, heartbeat_seconds=0.01,
                    clock=lambda: datetime.fromisoformat('2026-11-13T10:00:00+05:30'))
        args.update(overrides)
        return rr.ResearchRuntime(journal(d), forbidden, **args)
    return Path(journal(d)), make, c


def snapshot(path):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in path.parent.iterdir() if p.is_file()}


def test_authorized_start_restart_cwd(setup, monkeypatch, tmp_path):
    path, make, _ = setup
    r = make(authorize_synthetic_init=True)
    r.start()
    r.stop()
    assert path.stat().st_mode & 0o777 == 0o600
    monkeypatch.chdir(tmp_path.parent)
    r = make()
    r.start()
    r.stop()
    assert not list(path.parent.glob('.synthetic-init-*'))


@pytest.mark.parametrize('case', ['unapproved', 'missing', 'corrupt_identity', 'mismatch',
                                  'corrupt_db', 'old_schema', 'symlink', 'hardlink',
                                  'permissions', 'sidecar', 'inconsistent'])
def test_rejection_unchanged_before_writers(setup, monkeypatch, case):
    path, make, c = setup
    if case != 'unapproved':
        r = make(authorize_synthetic_init=True)
        r.start(); r.stop()
    if case in ('missing', 'corrupt_identity', 'old_schema'):
        with sqlite3.connect(path) as conn:
            # Deliberate corruption of disposable metadata only.
            conn.execute('DROP TRIGGER IF EXISTS crew_meta_no_update')
            conn.execute('DROP TRIGGER IF EXISTS crew_meta_no_delete')
            if case == 'missing': conn.execute('DELETE FROM crew_meta WHERE key=?', (IDENTITY_KEY,))
            elif case == 'corrupt_identity': conn.execute('UPDATE crew_meta SET value=? WHERE key=?', ('{', IDENTITY_KEY))
            else: conn.execute("UPDATE crew_meta SET value='0' WHERE key='schema_version'")
    if case == 'corrupt_db': path.write_bytes(b'not sqlite')
    if case == 'permissions': path.chmod(0o644)
    if case == 'sidecar': Path(str(path) + '-wal').write_bytes(b'unsafe')
    if case == 'inconsistent':
        with sqlite3.connect(path) as conn: conn.execute('DROP TABLE crew_events')
    if case == 'symlink':
        target = path.with_name('target'); path.rename(target); path.symlink_to(target)
    if case == 'hardlink': os.link(path, path.with_name('alias'))
    if case == 'mismatch':
        c = StateAuthorityContract((replace(c.deployments[0], account_id='other'), c.deployments[1]))
    before = snapshot(path)
    def forbidden(*a, **k): pytest.fail('Writer/migration/recovery reached before rejection')
    monkeypatch.setattr(rr.cj, 'migrate', forbidden)
    monkeypatch.setattr(rr.cs, 'acquire', forbidden)
    monkeypatch.setattr(rr.cs, 'recover', forbidden)
    with pytest.raises(AuthorityError):
        make(state_contract=c, account_id=c.deployments[0].account_id).start()
    assert snapshot(path) == before


@pytest.mark.parametrize('case', ['no_contract', 'account', 'paper', 'live', 'armed', 'traversal', 'fallback'])
def test_binding_rejects_without_creation(setup, monkeypatch, case):
    path, make, c = setup
    args = {}
    if case == 'no_contract': args['state_contract'] = None
    if case == 'account': args['account_id'] = 'other'
    if case == 'paper': args['settings'] = settings(True)
    if case == 'live': args['settings'] = settings(sandbox_allow_live=True)
    if case == 'armed': monkeypatch.setenv(rr.ARM_ENV, '1')
    if case in ('traversal', 'fallback'):
        d = c.deployments[0]
        loc = str(path.parent / '..' / path.parent.name / path.name) if case == 'traversal' else 'data/crew.db'
        authorities = tuple(replace(a, location=loc) if a.resource.value == 'crew_research_journal' else a for a in d.authorities)
        args['state_contract'] = StateAuthorityContract((replace(d, authorities=authorities),))
    with pytest.raises(AuthorityError): make(**args).start()
    assert list(path.parent.iterdir()) == []


def test_parent_symlink_and_permissions(setup):
    path, make, _ = setup
    root = path.parent
    root.chmod(0o755)
    with pytest.raises(AuthorityError): make(authorize_synthetic_init=True).start()
    root.chmod(0o700)
    target = root.with_name('physical')
    root.rename(target); root.symlink_to(target, target_is_directory=True)
    with pytest.raises(AuthorityError): make(authorize_synthetic_init=True).start()
    assert list(target.iterdir()) == []


def test_calendar_failure_does_not_initialize(setup, monkeypatch):
    path, make, _ = setup
    def fail(*a): raise rr.StartupRefused('calendar')
    monkeypatch.setattr(rr, '_check_calendar', fail)
    with pytest.raises(rr.StartupRefused): make(authorize_synthetic_init=True).start()
    assert list(path.parent.iterdir()) == []


def test_failed_initialization_cleans_staging(setup, monkeypatch):
    path, make, _ = setup
    def fail(staging):
        staging.write_bytes(b'interrupted temporary initialization')
        raise OSError('injected initialization failure')
    monkeypatch.setattr(rr.cj, 'migrate', fail)
    with pytest.raises(OSError): make(authorize_synthetic_init=True).start()
    assert list(path.parent.iterdir()) == []


def test_manifest_cannot_approve_existing_journal(setup):
    path, make, _ = setup
    rr.cj.migrate(path)
    path.chmod(0o600)
    path.with_name('provisioning.json').write_text('{"status":"approved"}')
    before = snapshot(path)
    with pytest.raises(AuthorityError): make(authorize_synthetic_init=True).start()
    assert snapshot(path) == before


def test_unreleased_session_rejected_without_refusal_write(setup, monkeypatch):
    path, make, _ = setup
    r = make(authorize_synthetic_init=True)
    r.start()
    r._stop.set()
    r._beater.join()  # Simulate interrupted process, leaving its lease unreleased.
    before = snapshot(path)
    def forbidden(*a, **k): pytest.fail('Lease writer reached')
    monkeypatch.setattr(rr.cs, 'acquire', forbidden)
    with pytest.raises(AuthorityError): make().start()
    assert snapshot(path) == before
