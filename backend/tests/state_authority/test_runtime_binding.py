"""Opt-in binding uses synthetic paths only; never starts a broker or delivery."""
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from app.config import Settings
from app.state_authority import AuthorityError, Environment, StateAuthorityContract
from app.trading.research_runtime import ResearchRuntime, _check_state_binding
from test_contract import deployment


def settings(paper=False, **kwargs):
    return Settings(_env_file=None, paper_capital_inr=Decimal('50000') if paper else None,
                    **kwargs)


def contract(tmp_path):
    return StateAuthorityContract(tuple(deployment(tmp_path / e.value, e)
                                        for e in (Environment.RESEARCH, Environment.PAPER)))


def journal(d):
    return next(a.location for a in d.authorities if a.resource.value == 'crew_research_journal')


def test_separation_cwd_and_no_creation(tmp_path, monkeypatch):
    c = contract(tmp_path)
    paths = []
    for cwd in (tmp_path, Path('/tmp')):
        monkeypatch.chdir(cwd)
        for d in c.deployments:
            runtime = ResearchRuntime(journal(d), lambda now: None,
                                      settings=settings(d.environment == Environment.PAPER),
                                      state_contract=c, deployment_id=d.deployment_id)
            paths.append(runtime.path)
            assert not runtime.path.exists()
    assert paths[0] == paths[2] and paths[1] == paths[3] and paths[0] != paths[1]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('case', ['missing', 'incomplete', 'sharing', 'mode', 'path', 'live', 'delivery', 'enabled', 'repository'])
def test_fail_closed_before_path_resolution(tmp_path, monkeypatch, case):
    c = contract(tmp_path)
    d = c.deployments[0]
    path, identity, config = journal(d), d.deployment_id, settings()
    if case == 'missing': identity = None
    if case == 'incomplete': c = StateAuthorityContract((replace(d, authorities=d.authorities[:-1]),))
    if case == 'sharing': c = StateAuthorityContract((d, replace(c.deployments[1], permitted_root=d.permitted_root)))
    if case == 'mode': config = settings(True)
    if case == 'path': path = 'relative.db'
    if case == 'live': c = StateAuthorityContract((replace(d, environment=Environment.LIVE),))
    if case == 'delivery': config = settings(telegram_delivery_enabled=True)
    if case == 'enabled': config = settings(live_trading_enabled=True)
    if case == 'repository':
        from app.trading.research_runtime import _REPO
        d = deployment(_REPO / 'synthetic-state')
        c, path = StateAuthorityContract((d,)), journal(d)
    def forbidden(*args, **kwargs):
        raise AssertionError('Path resolution occurred before rejection')
    monkeypatch.setattr(Path, 'resolve', forbidden)
    with pytest.raises(AuthorityError):
        ResearchRuntime(path, lambda now: None, settings=config,
                        state_contract=c, deployment_id=identity)


def test_no_contract_preserves_explicit_path_behavior(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runtime = ResearchRuntime('old-explicit.db', lambda now: None, settings=settings())
    assert runtime.path == tmp_path / 'old-explicit.db'
    with pytest.raises(AuthorityError):
        _check_state_binding(None, 'research', 'old-explicit.db', settings())


def test_start_rechecks_mode_before_io(tmp_path, monkeypatch):
    c = contract(tmp_path)
    d = c.deployments[0]
    runtime = ResearchRuntime(journal(d), lambda now: None, settings=settings(),
                              state_contract=c, deployment_id=d.deployment_id)
    runtime.settings = settings(True)
    with pytest.raises(AuthorityError):
        runtime.start()
    assert list(tmp_path.iterdir()) == []


def test_binding_preflight_performs_no_io(tmp_path, monkeypatch):
    import builtins
    import sqlite3
    c = contract(tmp_path)
    d = c.deployments[0]
    config = settings()
    def forbidden(*args, **kwargs):
        raise AssertionError('Binding attempted resource I/O')
    for name in ('open', 'mkdir', 'stat', 'resolve'):
        monkeypatch.setattr(Path, name, forbidden)
    monkeypatch.setattr(builtins, 'open', forbidden)
    monkeypatch.setattr(sqlite3, 'connect', forbidden)
    _check_state_binding(c, d.deployment_id, journal(d), config)


def test_bound_journals_do_not_share_writes(tmp_path):
    from app.db import crew_journal as cj
    c = contract(tmp_path)
    runtimes = [ResearchRuntime(journal(d), lambda now: None,
                               settings=settings(d.environment == Environment.PAPER),
                               state_contract=c, deployment_id=d.deployment_id)
                for d in c.deployments]
    for i, runtime in enumerate(runtimes):
        runtime.path.parent.mkdir()  # Explicit synthetic provisioning, after preflight.
        cj.migrate(runtime.path)
        with cj.connect(runtime.path) as conn:
            conn.execute('CREATE TABLE crew_synthetic_marker (value INTEGER)')
            conn.execute('INSERT INTO crew_synthetic_marker VALUES (?)', (i,))
    for i, runtime in enumerate(runtimes):
        with cj.connect_readonly(runtime.path) as conn:
            assert [r[0] for r in conn.execute('SELECT value FROM crew_synthetic_marker')] == [i]
