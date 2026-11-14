"""Scan indexed source only; reject protected paths before reading any contents."""
from pathlib import Path, PurePosixPath
import re
import subprocess


SECRET_PATTERNS = (
    re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    re.compile(rb'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    re.compile(rb'\bgh[pousr]_[A-Za-z0-9]{36,255}\b'),
    re.compile(rb'\bgithub_pat_[A-Za-z0-9_]{60,255}\b'),
    re.compile(rb'\b[0-9]{8,12}:[A-Za-z0-9_-]{35}\b'),
)


def protected(name):
    p = PurePosixPath(name)
    return (
        any(part in {'.aws', '.ssh', 'trades', 'node_modules', '.venv', '.venv-dev'}
            or part.startswith('.containment_evidence') for part in p.parts)
        or p.name.startswith('.env')
        or p.name in {'broker.credentials.properties', 'daily_risk_config.json', 'parameter_proposals.jsonl'}
        or p.name.startswith(('.active_trades_', 'active_trades_'))
        or bool(re.search(r'\.(?:db|sqlite3?)(?:$|[-.])', p.name))
        or p.suffix in {'.pem', '.key', '.log', '.pid', '.pyc'}
        or any(name.startswith(prefix) for prefix in (
            'logs/', 'backend/data/strategies/', 'backend/data/reports/', 'backend/data/sandbox/',
            'backend/data/logs/', 'backend/data/intraday/', 'backend/data/nse/raw/',
            'backend/research/out/', 'backend/research/profiles/',
            'DocumentsAndDiagrams/scanner_v01_phase4_replay/runs/'))
    )


def check(root):
    paths = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root).decode().split('\0')
    errors = []
    for name in filter(None, paths):
        if protected(name):
            errors.append(f'Protected tracked path: {name}')
            continue
        path = root / name
        if path.is_symlink():
            errors.append(f'Symlinked release path: {name}')
            continue
        data = path.read_bytes()
        if any(pattern.search(data) for pattern in SECRET_PATTERNS):
            errors.append(f'Possible secret in source: {name}')  # Never print matching values.
    if errors:
        raise SystemExit('\n'.join(errors))
    print(f'Secret and protected-path checks passed ({len(list(filter(None, paths)))} tracked files)')


if __name__ == '__main__':
    check(Path(__file__).resolve().parents[1])
