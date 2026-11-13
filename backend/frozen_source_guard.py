"""Portable, owner-pinned source evidence; no application or state imports."""
import hashlib
import importlib.util
import json
import marshal
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

MANIFEST_DIGEST = '386e7d671c85226baca50575555ee92175a6f2a5a7b746ef4a854e12cbd4a73d'


def verify(root, expected_digest=MANIFEST_DIGEST):
    root = Path(root).absolute()
    def no_links(path):
        for ancestor in (path, *path.parents):
            assert not ancestor.is_symlink(), f'Symlinked frozen/cache path: {ancestor}'
            if ancestor == root:
                break
    manifest = root / 'frozen-source-manifest.json'
    no_links(manifest)
    raw = manifest.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == expected_digest, 'Frozen manifest digest mismatch'
    evidence = json.loads(raw)
    assert evidence['schema'] == 1 and len(evidence['files']) == 18
    records = {row['path']: row for row in evidence['files']}
    assert len(records) == 18
    approved = set(evidence['permitted_additions'])
    assert len(approved) == 12 and not approved.intersection(records)
    roots = evidence['guarded_roots']
    def guarded(name):
        for prefix in roots:
            stem = prefix.removesuffix('.py')
            if name == prefix or name.startswith(prefix + '/') or name.startswith(stem + '.') or name.startswith(stem + '/'):
                return True
            p = Path(stem)
            if name.startswith(str(p.parent / '__pycache__' / (p.name + '.'))):
                return True
        return False
    # Inspect the index as an extra surface; filesystem content is always checked directly.
    indexed = set()
    indexed_caches = {}
    if (root / '.git').exists():
        flags = subprocess.run(['git', 'ls-files', '-v', '-z'], cwd=root, check=True, capture_output=True, text=True)
        unexpected = [e for e in flags.stdout.split('\0') if e and guarded(e[2:]) and not e.startswith('H ')]
        assert not unexpected, f'Unexpected frozen-source index flags: {unexpected}'
        entries = subprocess.run(['git', 'ls-files', '--stage', '-z'], cwd=root, check=True, capture_output=True, text=True)
        for entry in entries.stdout.split('\0'):
            if not entry:
                continue
            metadata, name = entry.split('\t', 1)
            if not guarded(name):
                continue
            mode, oid, stage = metadata.split()
            assert stage == '0' and mode in ('100644', '100755'), f'Invalid index entry: {name}'
            indexed.add(name)
            if name.endswith('.pyc'):
                indexed_caches[name] = subprocess.run(['git', 'cat-file', 'blob', oid], cwd=root, check=True, capture_output=True).stdout
            if name in records:
                assert mode == records[name]['mode'], f'Frozen index mode: {name}'
                blob = subprocess.run(['git', 'cat-file', 'blob', oid], cwd=root, check=True, capture_output=True).stdout
                assert hashlib.sha256(blob).hexdigest() == records[name]['sha256'], f'Frozen index differs from baseline: {name}'
    # Standalone modules also load caches from a sibling directory; reject links
    # before walking, since os.walk intentionally never follows linked directories.
    for prefix in roots:
        no_links(root / Path(prefix).parent / '__pycache__')
    for name, row in records.items():
        p = root / name
        no_links(p)
        assert p.is_file(), f'Missing frozen source: {name}'
        assert stat.S_IMODE(p.stat().st_mode) == int(row['mode'], 8) & 0o777, f'Frozen mode: {name}'
        assert hashlib.sha256(p.read_bytes()).hexdigest() == row['sha256'], f'Frozen source differs from baseline: {name}'
    found = set(indexed)
    # Walk without following links; examine relevant directory entries as well as files.
    for directory, dirs, files in os.walk(root, followlinks=False):
        skipped = {root / '.git', root / 'frontend/node_modules', root / 'backend/.venv'}
        dirs[:] = [d for d in dirs if Path(directory) / d not in skipped]
        for item in dirs + files:
            p = Path(directory) / item
            name = p.relative_to(root).as_posix()
            if guarded(name):
                no_links(p)
                if p.is_dir():
                    assert any(n.startswith(name + '/') for n in records.keys() | approved) or p.name == '__pycache__', f'Unauthorized directory: {name}'
                else:
                    found.add(name)
    for name in sorted(found):
        p = root / name
        no_links(p)
        if name in records or name in approved:
            assert p.is_file(), f'Missing source: {name}'
            continue
        match = re.fullmatch(rf'([A-Za-z_]\w*)\.{re.escape(sys.implementation.cache_tag)}(?:\.opt-([12]))?\.pyc', p.name)
        assert p.parent.name == '__pycache__' and match, f'Unauthorized frozen-source addition: {name}'
        source = p.parent.parent / (match[1] + '.py')
        assert source.relative_to(root).as_posix() in records.keys() | approved, f'Unauthorized cache source: {name}'
        no_links(source)
        try:
            data = p.read_bytes()
            assert len(data) > 16 and data[:4] == importlib.util.MAGIC_NUMBER
            expected = compile(source.read_bytes(), str(source), 'exec', dont_inherit=True, optimize=int(match[2] or 0))
            assert marshal.loads(data[16:]) == expected
            if name in indexed_caches:
                staged = indexed_caches[name]
                assert len(staged) > 16 and staged[:4] == importlib.util.MAGIC_NUMBER
                assert marshal.loads(staged[16:]) == expected
        except (OSError, ValueError, EOFError, TypeError, SyntaxError, AssertionError) as exc:
            raise AssertionError(f'Unverified executable cache: {name}') from exc
    return len(records)


if __name__ == '__main__':
    print(f'{verify(Path(__file__).resolve().parents[1])}/18 frozen sources verified')
