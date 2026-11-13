"""Same-location offline restore rehearsal; all state is disposable."""
from dataclasses import replace
from datetime import datetime
import sqlite3

import pytest

from app.state_authority import AuthorityError, StateAuthorityContract
from app.synthetic_research_state import binding, validate
from test_synthetic_startup import setup, snapshot
from test_runtime_binding import contract, journal, settings


def records(path):
    conn = sqlite3.connect(f'{path.as_uri()}?mode=ro&immutable=1', uri=True)
    try:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {n: conn.execute(f'SELECT * FROM "{n}" ORDER BY rowid').fetchall() for n in names}
    finally:
        conn.close()


@pytest.fixture
def checkpoint(setup):
    path, make, c = setup
    runtime = make(authorize_synthetic_init=True)
    runtime.start()
    now = datetime.fromisoformat('2026-11-13T10:00:00+05:30')
    from app.db import crew_journal as cj
    try:
        cj.record_admission(path, lease=runtime.lease, admission_class='RESEARCH_ONLY',
                            reasons=['QUOTE_NOT_EXECUTABLE'], market_day='2026-11-13',
                            instrument='NIFTY', strategy_id='restore-test', strategy_version='v1',
                            signal_candle=now.isoformat(), direction='CALL', setup_id='restore', now=now)
    finally:
        runtime.stop()
    backup = path.with_name('checkpoint.sqlite')
    source = sqlite3.connect(f'{path.as_uri()}?mode=ro&immutable=1', uri=True)
    target = sqlite3.connect(backup)
    try:
        source.backup(target)
        target.execute('PRAGMA journal_mode=DELETE')
    finally:
        target.close(); source.close()
    backup.chmod(0o600)
    return path, make, c, backup


def test_same_location_restore_restart_and_records(checkpoint):
    path, make, _, backup = checkpoint
    original = backup.read_bytes()
    expected = records(backup)
    assert expected['crew_admissions'] and expected['crew_session_events']
    path.unlink()  # Simulated loss, only after all writers have stopped.
    path.write_bytes(original); path.chmod(0o600)
    assert records(path) == expected
    for _ in range(2):
        runtime = make(); runtime.start(); runtime.stop()
    actual = records(path)
    for name, rows in expected.items():
        if name not in ('crew_lease', 'sqlite_sequence'):
            assert all(row in actual[name] for row in rows), name
    assert actual['crew_admissions'] == expected['crew_admissions']
    assert backup.read_bytes() == original


@pytest.mark.parametrize('case', ['missing', 'corrupt', 'account', 'deployment', 'relocation'])
def test_restore_rejections_do_not_mutate_destination(checkpoint, tmp_path, case):
    path, make, c, backup = checkpoint
    d = c.deployments[0]
    identity, root = binding(c, d.deployment_id, d.account_id, path, settings())
    if case == 'missing':
        backup.unlink()
        before = snapshot(path)
        with pytest.raises(FileNotFoundError): backup.read_bytes()
        assert snapshot(path) == before
        return
    if case == 'corrupt':
        backup.write_bytes(b'not a SQLite checkpoint')
        before = snapshot(path)
        with pytest.raises(AuthorityError): validate(backup, root, identity, False)
        assert snapshot(path) == before
        return
    if case in ('account', 'deployment'):
        field = 'account_id' if case == 'account' else 'deployment_id'
        d = replace(d, **{field: 'other'})
        c = StateAuthorityContract((d, c.deployments[1]))
    if case == 'relocation':
        c = contract(tmp_path / 'relocated')
        d = c.deployments[0]
        root = type(path)(d.permitted_root); root.mkdir(parents=True, mode=0o700)
        path = type(path)(journal(d))
        path.write_bytes(backup.read_bytes()); path.chmod(0o600)
    identity, root = binding(c, d.deployment_id, d.account_id, path, settings())
    before = snapshot(path)
    # Both initialization settings must refuse existing mismatched/corrupt state.
    for authorized in (False, True):
        with pytest.raises(AuthorityError): validate(path, root, identity, authorized)
    assert snapshot(path) == before
