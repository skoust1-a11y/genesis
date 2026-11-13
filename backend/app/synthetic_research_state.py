"""Fail-closed, test-only research journal origin gate. No state discovery or adoption."""
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile

from app.db import crew_journal as cj
from app.state_authority import AuthorityError, Environment, Resource, _path, preflight


IDENTITY_KEY = 'synthetic_research_identity_v1'


def binding(contract, deployment_id, account_id, path, settings):
    preflight(contract)
    selected = [d for d in contract.deployments if d.deployment_id == deployment_id]
    if len(selected) != 1:
        raise AuthorityError('Synthetic mode requires an explicit deployment')
    d = selected[0]
    if d.environment != Environment.RESEARCH or d.account_id != account_id:
        raise AuthorityError('Synthetic research deployment/account/environment mismatch')
    if (settings.paper_capital_inr is not None or settings.live_trading_enabled
            or settings.sandbox_allow_live or settings.telegram_delivery_enabled
            or os.environ.get('GENESIS_TELEGRAM_ARMED')):
        raise AuthorityError('Synthetic research requires research-only, unarmed settings')
    location = next(a.location for a in d.authorities if a.resource == Resource.CREW_RESEARCH_JOURNAL)
    if str(path) != location:
        raise AuthorityError('Synthetic journal must exactly match its authority')
    _path(str(path))
    return dict(version=1, origin='authorized-synthetic-research', deployment_id=d.deployment_id,
                account_id=d.account_id, environment=d.environment.value,
                permitted_root=d.permitted_root, journal=location), Path(d.permitted_root)


def physical(path, root):
    """Inspect metadata only, walking every component without following links."""
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parent.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            if info.st_uid not in (0, os.getuid()):
                raise AuthorityError('Untrusted directory owner')
            if info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX):
                raise AuthorityError('Unsafe writable directory')
        # Canonical spelling detects case and platform aliases after the no-follow walk.
        if path.parent.resolve(strict=True) != path.parent:
            raise AuthorityError('Noncanonical journal parent')
        for directory in (root, *[p for p in path.parent.parents if p.is_relative_to(root)], path.parent):
            info = directory.lstat()
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise AuthorityError('Synthetic directories require invoking UID and mode 0700')
            if info.st_dev != os.fstat(fd).st_dev:
                raise AuthorityError('Cross-device journal containment is unsupported')
        for suffix in ('-wal', '-shm', '-journal'):
            if os.path.lexists(str(path) + suffix):
                raise AuthorityError('Synthetic startup requires a cleanly closed journal without sidecars')
        try:
            info = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_dev != os.fstat(fd).st_dev):
            raise AuthorityError('Unsafe journal type, aliases, ownership or permissions')
        if path.resolve(strict=True) != path:
            raise AuthorityError('Noncanonical journal spelling')
        return True
    finally:
        os.close(fd)


def validate(path, root, identity, authorized):
    try:
        exists = physical(path, root)
        if not exists:
            if not authorized:
                raise AuthorityError('Explicit synthetic initialization authorization required')
            return True
        # Immutable reads cannot create WAL/SHM or recover a hot journal.
        conn = sqlite3.connect(f'{path.as_uri()}?mode=ro&immutable=1', uri=True)
        conn.row_factory = sqlite3.Row
        try:
            if conn.execute('PRAGMA integrity_check').fetchall()[0][0] != 'ok':
                raise AuthorityError('Corrupt synthetic journal')
            rows = dict(conn.execute('SELECT key, value FROM crew_meta'))
            if json.loads(rows.get(IDENTITY_KEY, 'null')) != identity:
                raise AuthorityError('Missing, corrupt or mismatched synthetic identity')
            if rows.get('schema_version') != str(cj.SCHEMA_VERSION):
                raise AuthorityError('Synthetic startup does not migrate existing state')
            held = conn.execute('SELECT released_at FROM crew_lease WHERE id=1').fetchone()
            if held is not None and held['released_at'] is None:
                raise AuthorityError('Synthetic startup does not adopt an unreleased session')
            names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            if any(not n.startswith(('crew_', 'sqlite_')) for n in names):
                raise AuthorityError('Unapproved journal tables')
            if cj._consistency_findings(conn):
                raise AuthorityError('Inconsistent synthetic journal')
        finally:
            conn.close()
        return False
    except (OSError, sqlite3.Error, ValueError, TypeError) as error:
        raise AuthorityError(f'Synthetic startup refused: {error}') from error


def initialize(path, root, identity):
    """Build off-path; publish only a complete identity-bearing journal, without overwrite."""
    if not validate(path, root, identity, True):
        raise AuthorityError('Journal appeared during initialization')
    fd, name = tempfile.mkstemp(prefix='.synthetic-init-', dir=path.parent)
    os.close(fd)
    staging = Path(name)
    try:
        cj.migrate(staging)
        with sqlite3.connect(staging) as conn:
            conn.execute('INSERT INTO crew_meta(key,value,set_at) VALUES (?,?,?)',
                         (IDENTITY_KEY, json.dumps(identity, sort_keys=True), 'synthetic-initialization'))
        conn.close()
        with sqlite3.connect(staging) as conn:
            conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            conn.execute('PRAGMA journal_mode=DELETE')
        conn.close()
        with staging.open("rb") as complete:
            os.fsync(complete.fileno())
        os.link(staging, path, follow_symlinks=False)
    finally:
        for suffix in ('', '-wal', '-shm', '-journal'):
            Path(str(staging) + suffix).unlink(missing_ok=True)
