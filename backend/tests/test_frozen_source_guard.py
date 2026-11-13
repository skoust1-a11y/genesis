"""Exercise the admission guard in disposable Git repositories only."""

import hashlib
import json
import importlib.util
import marshal
import os
import py_compile
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from tests import test_admission as admission

BASELINE = "8042f59f47db7b4989293d8c6eb6aa19155d1ab4"
APPROVED = [f"backend/app/strategies/{name}.py" for name in (
    "capabilities", "configuration", "configuration_store", "research",
)]
FROZEN = [
    "backend/app/liquidity_signals.py",
    "backend/app/strategies/clarify.py",
    "backend/app/strategies/store.py",
    *[f"backend/app/trading/crew/{name}.py" for name in (
        "__init__", "entry_agent", "exit_agent", "holding_agent", "journal_agent",
        "manager", "order_flow_agent", "risk_agent", "sniper_agent", "trend_agent",
    )],
    *[f"backend/app/trading/scanner_crew/{name}.py" for name in (
        "__init__", "consolidation_agent", "detector_agent", "feed_agent",
    )],
    "backend/scripts/backtest_pdc_15ema.py",
]


@pytest.fixture
def guarded_repo(tmp_path, monkeypatch):
    real_run = subprocess.run

    def git(*args):
        return real_run(["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True)

    git("init")
    for name in FROZEN:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# frozen {name}\n")
    git("add", ".")
    git("-c", "user.name=Guard Test", "-c", "user.email=guard@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "-m", "isolated baseline")
    baseline = git("rev-parse", "HEAD").stdout.strip()
    manifest = json.loads((Path(__file__).resolve().parents[2] / "frozen-source-manifest.json").read_text())
    for row in manifest["files"]:
        row["sha256"] = hashlib.sha256((tmp_path / row["path"]).read_bytes()).hexdigest()
    raw = (json.dumps(manifest, indent=2) + "\n").encode()
    (tmp_path / "frozen-source-manifest.json").write_bytes(raw)
    monkeypatch.setattr(admission, "FROZEN_MANIFEST_DIGEST", hashlib.sha256(raw).hexdigest())
    monkeypatch.setattr(admission, "MODULE", tmp_path / "backend/app/trading/admission.py")

    def isolated_run(command, **kwargs):
        assert Path(kwargs["cwd"]) == tmp_path
        assert command[:2] in (["git", "diff"], ["git", "ls-files"], ["git", "ls-tree"], ["git", "cat-file"])
        return real_run([baseline if arg == BASELINE else arg for arg in command], **kwargs)

    monkeypatch.setattr(admission.subprocess, "run", isolated_run)
    admission.test_20_frozen_v0_strategy_sources_are_untouched()
    return tmp_path, git


@pytest.mark.parametrize("name", APPROVED)
@pytest.mark.parametrize("state", ["staged", "untracked", "ignored"])
def test_approved_addition_is_permitted(guarded_repo, name, state):
    repo, git = guarded_repo
    (repo / name).write_text("# approved addition\n")
    if state == "staged":
        git("add", name)
    elif state == "ignored":
        (repo / ".git/info/exclude").write_text(f"/{name}\n")
    admission.test_20_frozen_v0_strategy_sources_are_untouched()


@pytest.mark.parametrize("name", FROZEN)
@pytest.mark.parametrize("operation", ["modify", "delete", "rename"])
def test_every_frozen_file_rejects_changes(guarded_repo, name, operation):
    repo, git = guarded_repo
    path = repo / name
    if operation == "modify":
        path.write_text("# unauthorized modification\n")
    elif operation == "delete":
        git("rm", name)
    else:
        # Even a move into an approved exclusion must expose the frozen deletion.
        git("mv", name, APPROVED[0])
    with pytest.raises(AssertionError):
        admission.test_20_frozen_v0_strategy_sources_are_untouched()


@pytest.mark.parametrize("name", [
    "backend/app/trading/crew.py",
    "backend/app/trading/crew.abi3.so",
    "backend/app/trading/crew.pyd",
    "backend/app/trading/scanner_crew.py",
    "backend/app/trading/scanner_crew.abi3.so",
    "backend/app/trading/scanner_crew.pyd",
    "backend/app/strategies.py",
    "backend/app/strategies.abi3.so",
    "backend/app/strategies.pyd",
    "backend/app/strategies/unauthorized.py",
    "backend/app/strategies/configuration.py.extra",
    "backend/app/strategies/nested/research.py",
    "backend/app/trading/crew/unauthorized.py",
    "backend/app/trading/scanner_crew/unauthorized.py",
    "backend/app/strategies/clarify/__init__.py",
    "backend/app/strategies/configuration/__init__.py",
    "backend/app/strategies/clarify.abi3.so",
    "backend/app/strategies/clarify.pyd",
    "backend/app/strategies/__pycache__/payload.py",
    "backend/app/strategies/__pycache__/clarify.so",
    "backend/app/strategies/__pycache__/orphan.cpython-312.pyc",
    "backend/app/strategies/clarify.pyc",
    "backend/app/__pycache__/liquidity_signals.py",
    "backend/app/__pycache__/liquidity_signals.abi3.so",
    "backend/scripts/__pycache__/backtest_pdc_15ema.pyc",
    "backend/app/trading/__pycache__/crew.cpython-312.pyc",
    "backend/app/trading/__pycache__/scanner_crew.cpython-312.pyc",
    "backend/app/__pycache__/strategies.cpython-312.pyc",
    "backend/app/liquidity_signals/__init__.py",
    "backend/app/liquidity_signals.abi3.so",
    "backend/scripts/backtest_pdc_15ema/__init__.py",
    "backend/scripts/backtest_pdc_15ema.pyd",
])
@pytest.mark.parametrize("state", ["staged", "untracked", "ignored"])
def test_unauthorized_additions_are_rejected(guarded_repo, name, state):
    repo, git = guarded_repo
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# unauthorized addition\n")
    if state == "staged":
        git("add", name)
    elif state == "ignored":
        (repo / ".git/info/exclude").write_text(f"/{name}\n")
    with pytest.raises(AssertionError):
        admission.test_20_frozen_v0_strategy_sources_are_untouched()


@pytest.mark.parametrize("ignored", [False, True])
@pytest.mark.parametrize("suffix", ["cpython-312.pyc", "cpython-313.opt-1.pyc", "cpython-313.opt-2.pyc"])
def test_source_backed_cache_is_rejected(guarded_repo, ignored, suffix):
    repo, _ = guarded_repo
    path = repo / f"backend/app/strategies/__pycache__/clarify.{suffix}"
    path.parent.mkdir()
    path.write_bytes(b"generated cache")
    if ignored:
        (repo / ".git/info/exclude").write_text("__pycache__/\n")
    with pytest.raises(AssertionError):
        admission.test_20_frozen_v0_strategy_sources_are_untouched()


@pytest.mark.parametrize("flag", ["h", "S", "s", "M", "?"])
@pytest.mark.parametrize("name", FROZEN)
def test_unexpected_index_flags_fail_closed(guarded_repo, monkeypatch, flag, name):
    repo, _ = guarded_repo
    original = admission.subprocess.run

    def flagged(command, **kwargs):
        result = original(command, **kwargs)
        if command[:3] == ["git", "ls-files", "-v"]:
            result.stdout = result.stdout.replace(f"H {name}\0", f"{flag} {name}\0")
        return result

    monkeypatch.setattr(admission.subprocess, "run", flagged)
    (repo / name).write_text("# hidden change\n")
    with pytest.raises(AssertionError, match="index flags"):
        admission.test_20_frozen_v0_strategy_sources_are_untouched()


@pytest.mark.parametrize("name", [*FROZEN, *APPROVED])
@pytest.mark.parametrize("optimize", [0, 1, 2])
@pytest.mark.parametrize("state", ["staged", "untracked", "ignored"])
def test_verified_generated_cache_is_permitted(guarded_repo, name, optimize, state):
    repo, git = guarded_repo
    source = repo / name
    source.write_text(source.read_text() if source.exists() else "VALUE = 1\n")
    cache = Path(py_compile.compile(str(source), optimize=optimize, doraise=True))
    if state == "staged":
        git("add", str(cache.relative_to(repo)))
    elif state == "ignored":
        (repo / ".git/info/exclude").write_text("__pycache__/\n")
    admission.test_20_frozen_v0_strategy_sources_are_untouched()


@pytest.mark.parametrize("name", FROZEN)
@pytest.mark.parametrize("mode", list(py_compile.PycInvalidationMode))
@pytest.mark.parametrize("state", ["staged", "untracked", "ignored"])
def test_forged_cache_executes_and_is_rejected(guarded_repo, name, mode, state):
    repo, git = guarded_repo
    source = repo / name
    cache = Path(py_compile.compile(str(source), invalidation_mode=mode, doraise=True))
    # Preserve a legitimate header (including source hash/mtime), replace executable code.
    cache.write_bytes(cache.read_bytes()[:16] + marshal.dumps(compile("FORGED = True", str(source), "exec")))
    spec = importlib.util.spec_from_file_location("guard_probe", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.FORGED is True
    assert module.__cached__ == str(cache)
    if state == "staged":
        git("add", str(cache.relative_to(repo)))
    elif state == "ignored":
        (repo / ".git/info/exclude").write_text("__pycache__/\n")
    with pytest.raises(AssertionError, match="Unverified executable cache"):
        admission.test_20_frozen_v0_strategy_sources_are_untouched()


@pytest.mark.parametrize("optimize", [1, 2])
def test_forged_optimized_cache_is_rejected(guarded_repo, optimize):
    repo, _ = guarded_repo
    source = repo / FROZEN[0]
    cache = Path(py_compile.compile(str(source), optimize=optimize, doraise=True))
    cache.write_bytes(cache.read_bytes()[:16] + marshal.dumps(compile("FORGED = True", str(source), "exec")))
    with pytest.raises(AssertionError, match="Unverified executable cache"):
        admission.test_20_frozen_v0_strategy_sources_are_untouched()


def test_all_30_ordinary_caches_without_future_annotations_are_permitted(guarded_repo):
    repo, _ = guarded_repo
    excluded = [
        "backend/app/trading/crew/alert_agent.py",
        *[f"backend/app/trading/scanner_crew/{name}.py" for name in (
            "news_agent", "scanner_manager", "levels_agent",
        )],
        *[f"backend/app/strategies/{name}.py" for name in (
            "rules", "schema", "normalizer", "analyzer",
        )],
        *APPROVED,
    ]
    sources = FROZEN + excluded
    assert len(sources) == 30
    for name in sources:
        source = repo / name
        if not source.exists():
            source.write_text("VALUE = 1\n")
        py_compile.compile(str(source), doraise=True)
    (repo / ".git/info/exclude").write_text("__pycache__/\n")
    admission.test_20_frozen_v0_strategy_sources_are_untouched()


@pytest.mark.parametrize("name", [FROZEN[0], FROZEN[-1], FROZEN[1]])
@pytest.mark.parametrize("ancestor", [False, True])
def test_symlinked_cache_payload_executes_and_is_rejected(guarded_repo, tmp_path, name, ancestor):
    repo, _ = guarded_repo
    source = repo / name
    cache = Path(py_compile.compile(str(source), doraise=True))
    cache.write_bytes(cache.read_bytes()[:16] + marshal.dumps(compile("FORGED = True", str(source), "exec")))
    directory = cache.parent.parent if ancestor else cache.parent
    external = tmp_path.parent / f"{tmp_path.name}-external"
    shutil.move(str(directory), external)
    directory.symlink_to(external, target_is_directory=True)
    spec = importlib.util.spec_from_file_location("guard_probe", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.FORGED is True
    with pytest.raises(AssertionError, match="[Ss]ymlink"):
        admission.test_20_frozen_v0_strategy_sources_are_untouched()


@pytest.mark.parametrize("concealment", ["stat", "fsmonitor"])
def test_git_configuration_cannot_hide_source_changes(guarded_repo, concealment):
    repo, git = guarded_repo
    source = repo / FROZEN[0]
    if concealment == "stat":
        git("config", "core.trustctime", "false")
        git("config", "core.checkStat", "minimal")
    else:
        hook = repo / ".git/dishonest-fsmonitor"
        hook.write_text("#!/bin/sh\nprintf 'dishonest-token\\0'\n")
        hook.chmod(0o755)
        git("config", "core.fsmonitor", str(hook))
    # Avoid Git's racy-timestamp fallback: the unchanged file must predate the index.
    stamp = time.time() - 10
    os.utime(source, (stamp, stamp))
    git("update-index", "--refresh")
    git("status", "--porcelain")
    before = source.stat()
    original = source.read_bytes()
    source.write_bytes(original.replace(b"frozen", b"FORGED"))
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    # Prove the chosen configuration actually conceals the equal-size modification.
    assert git("diff", "--name-only").stdout == ""
    with pytest.raises(AssertionError, match="differs from baseline"):
        admission.test_20_frozen_v0_strategy_sources_are_untouched()
