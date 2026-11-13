"""Synthetic declarations only; no configured runtime state is accessed."""

import builtins
import os
import socket
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from app.state_authority import (
    Authority,
    AuthorityError,
    Deployment,
    Environment,
    Resource,
    StateAuthorityContract,
    preflight,
)


def deployment(root, environment=Environment.RESEARCH):
    return Deployment(
        environment.value,
        environment.value + "-account",
        environment,
        str(root),
        tuple(Authority(r, str(root / r.value)) for r in Resource),
    )


def check(d):
    preflight(StateAuthorityContract((d,)))


def location(d, value, index=0):
    entries = list(d.authorities)
    entries[index] = replace(entries[index], location=value)
    return replace(d, authorities=tuple(entries))


def test_valid_all_environments_and_sqlite(tmp_path):
    deployments = []
    for env in Environment:
        d = deployment(tmp_path / env.value, env)
        index = list(Resource).index(Resource.RESEARCH_DATABASE)
        deployments.append(
            location(d, "sqlite:///" + d.authorities[index].location, index)
        )
    preflight(StateAuthorityContract(tuple(deployments)))


@pytest.mark.parametrize(
    "value",
    [
        "relative",
        "/",
        "//host/path",
        "/a/../b",
        "/a/./b",
        "/a//b",
        "/a/",
        "/a\x00b",
        "/a\\b",
        "/a%2fb",
        "/a?x",
        "/a#x",
        "/a/$HOME",
        "/a/ b",
    ],
)
def test_path_syntax(tmp_path, value):
    with pytest.raises(AuthorityError):
        check(location(deployment(tmp_path / "state"), value))


def test_missing_and_duplicate_authorities(tmp_path):
    d = deployment(tmp_path / "state")
    for authorities in (d.authorities[:-1], d.authorities + d.authorities[:1], ()):
        with pytest.raises(AuthorityError):
            check(replace(d, authorities=authorities))
    with pytest.raises(AuthorityError):
        preflight(StateAuthorityContract(()))


@pytest.mark.parametrize("suffix", ["", "/child"])
def test_duplicate_or_overlapping_paths(tmp_path, suffix):
    d = deployment(tmp_path / "state")
    with pytest.raises(AuthorityError):
        check(location(d, d.authorities[1].location + suffix))


def test_boundary_escape(tmp_path):
    d = deployment(tmp_path / "state")
    for value in (str(tmp_path / "state-other" / "risk"), d.permitted_root):
        with pytest.raises(AuthorityError):
            check(location(d, value))


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///relative.db",
        "sqlite:///:memory:",
        "sqlite://host/a.db",
        "sqlite:////a.db?mode=ro",
        "sqlite:////a%2f.db",
        "sqlite://///a.db",
        "sqlite+pysqlite:////a.db",
        "postgresql://host/db",
        "file:/a.db",
    ],
)
def test_invalid_sqlite(tmp_path, url):
    d = deployment(tmp_path / "state")
    with pytest.raises(AuthorityError):
        check(location(d, url, list(Resource).index(Resource.PAPER_DATABASE)))


@pytest.mark.parametrize("conflict", ["deployment", "account", "boundary", "shared"])
def test_conflicting_environments(tmp_path, conflict):
    a = deployment(tmp_path / "research")
    b = deployment(tmp_path / "paper", Environment.PAPER)
    if conflict == "deployment":
        b = replace(b, deployment_id=a.deployment_id)
    elif conflict == "account":
        b = replace(b, account_id=a.account_id)
    elif conflict == "boundary":
        b = replace(b, permitted_root=a.permitted_root + "/nested")
    else:
        b = location(b, a.authorities[0].location)
    with pytest.raises(AuthorityError):
        preflight(StateAuthorityContract((a, b)))


def test_no_io_or_network_and_cwd_invariance(tmp_path, monkeypatch):
    d = deployment(tmp_path / "never-created")

    def forbidden(*args, **kwargs):
        pytest.fail("Preflight attempted filesystem, database or network access")

    for cwd in (tmp_path, tmp_path.parent):
        monkeypatch.chdir(cwd)
        with monkeypatch.context() as guard:
            for obj, names in (
                (builtins, ("open",)),
                (
                    Path,
                    (
                        "open",
                        "stat",
                        "resolve",
                        "mkdir",
                        "exists",
                        "read_text",
                        "write_text",
                    ),
                ),
                (
                    os,
                    ("open", "mkdir", "stat", "lstat", "listdir", "scandir", "getcwd"),
                ),
                (sqlite3, ("connect",)),
                (socket, ("socket", "create_connection", "getaddrinfo")),
            ):
                for name in names:
                    guard.setattr(obj, name, forbidden)
            guard.setattr(builtins, "__import__", forbidden)
            check(d)
    assert not (tmp_path / "never-created").exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"environment": "live"},
        {"account_id": ""},
        {"deployment_id": "bad identity"},
        {"permitted_root": "relative"},
    ],
)
def test_invalid_identity_or_boundary(tmp_path, changes):
    with pytest.raises(AuthorityError):
        check(replace(deployment(tmp_path / "state"), **changes))
