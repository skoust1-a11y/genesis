"""Local watchdog checks never control a real service or touch live state."""

import importlib.util
import json
import sqlite3
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from app.trading.forward_comparison import (
    candidate_admission_blocker,
    shutdown_safe_view,
)
from app.trading.research_runtime import MarketView

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def module(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


wd = module("paper_watchdog")
installer = module("install_paper_host_supervision")
NOW = datetime(2026, 11, 17, 8, 0, tzinfo=UTC)


def journals(tmp_path, *, candidate=0, paper=0, model=0):
    run = tmp_path / "runs/r1"
    run.mkdir(parents=True)
    (tmp_path / "active-run.json").write_text(
        json.dumps({"run_id": "r1", "directory": str(run)})
    )
    for name, tables in [
        ("candidate.db", {"trades": candidate}),
        ("baseline.db", {"crew_paper_trades": paper, "crew_trades": model}),
    ]:
        with sqlite3.connect(run / name) as db:
            for table, count in tables.items():
                db.execute("CREATE TABLE " + table + " (state TEXT NOT NULL)")
                db.executemany(
                    "INSERT INTO " + table + " VALUES (?)", [("OPEN",)] * count
                )
    return run


@pytest.mark.parametrize("state", ["SCANNING", "DEGRADED", "WAITING_FOR_MARKET"])
def test_fresh_heartbeat_is_not_restarted_for_prior_gaps(state):
    assert not wd.heartbeat({"updated_at": NOW.isoformat(), "state": state}, NOW)[
        "stale"
    ]


def test_stale_stopped_and_future_clocks_are_distinct():
    assert wd.heartbeat(
        {"updated_at": (NOW - timedelta(seconds=121)).isoformat()}, NOW
    )["stale"]
    assert wd.heartbeat(
        {"updated_at": (NOW - timedelta(seconds=31)).isoformat(), "state": "STOPPED"},
        NOW,
    )["stale"]
    assert (
        wd.heartbeat({"updated_at": (NOW + timedelta(minutes=5)).isoformat()}, NOW)[
            "state"
        ]
        == "CLOCK_SKEW"
    )
    assert wd.heartbeat({"updated_at": "broken"}, NOW)["stale"]


@pytest.mark.parametrize("kwargs", [{"candidate": 1}, {"paper": 1}, {"model": 1}])
def test_any_open_or_unresolved_model_or_paper_exposure_blocks_restart(
    tmp_path, kwargs
):
    journals(tmp_path, **kwargs)
    positions = wd.exposure(tmp_path)
    assert not positions["flat"]
    assert not wd.restart_allowed({}, positions, NOW)[0]


def test_full_journal_check_is_read_only_and_rejects_external_paths(tmp_path):
    journals(tmp_path)
    assert wd.exposure(tmp_path)["flat"]
    (tmp_path / "active-run.json").write_text(
        json.dumps({"run_id": "bad", "directory": str(tmp_path.parent)})
    )
    with pytest.raises(ValueError):
        wd.exposure(tmp_path)


def test_cooldown_and_hourly_budget_prevent_restart_loops():
    flat = {"verified": True, "flat": True}
    assert (
        wd.restart_allowed(
            {"restart_attempts": [(NOW - timedelta(seconds=30)).isoformat()]}, flat, NOW
        )[1]
        == "RESTART_COOLDOWN"
    )
    assert (
        wd.restart_allowed(
            {
                "restart_attempts": [
                    (NOW - timedelta(minutes=n)).isoformat() for n in (5, 10, 15)
                ]
            },
            flat,
            NOW,
        )[1]
        == "RESTART_LIMIT_REACHED"
    )
    assert not wd.restart_allowed({}, {"verified": False}, NOW)[0]


class Controller:
    def __init__(self):
        self.calls = 0

    def pid(self):
        return 42

    def restart_flat(self, root):
        self.calls += 1
        return {"result": "RESTART_REQUESTED"}


def test_restart_requires_persistent_staleness_and_flat_ledger(tmp_path, monkeypatch):
    journals(tmp_path)
    (tmp_path / "status.json").write_text(
        json.dumps(
            {
                "updated_at": (NOW - timedelta(minutes=10)).isoformat(),
                "state": "SCANNING",
            }
        )
    )
    monkeypatch.setattr(
        wd, "power_conditions", lambda: {"ac_power": True, "lid_open": True}
    )
    controller = Controller()
    first = wd.check(tmp_path, now=NOW, act=True, controller=controller)
    assert first["restart_gate"] == "PROCESS_STARTUP_GRACE" and not controller.calls
    second = wd.check(
        tmp_path, now=NOW + timedelta(seconds=46), act=True, controller=controller
    )
    assert controller.calls == 1 and second["restart"]["result"] == "RESTART_REQUESTED"
    wd.check(tmp_path, now=NOW + timedelta(seconds=60), act=True, controller=controller)
    assert controller.calls == 1


def test_unreadable_or_missing_journal_cannot_trigger_restart(tmp_path, monkeypatch):
    journals(tmp_path)
    (tmp_path / "runs/r1/baseline.db").unlink()
    monkeypatch.setattr(wd, "power_conditions", dict)
    controller = Controller()
    result = wd.check(tmp_path, now=NOW, act=True, controller=controller)
    assert not result["exposure"]["verified"] and controller.calls == 0


def test_check_only_cannot_control_service(tmp_path, monkeypatch):
    journals(tmp_path)
    monkeypatch.setattr(wd, "power_conditions", dict)
    controller = Controller()
    wd.check(tmp_path, now=NOW, act=False, controller=controller)
    assert controller.calls == 0


def test_shutdown_removes_entry_setups_without_mutating_exit_inputs():
    stop = threading.Event()
    bars = pd.DataFrame([{"close": 25000}])
    chain = {"contracts": []}
    view = MarketView(bars, [object()], chain, [25050])
    assert shutdown_safe_view(view, stop) is view
    stop.set()
    safe = shutdown_safe_view(view, stop)
    assert not safe.setups and len(view.setups) == 1
    assert safe.df_5m is bars and safe.chain is chain and safe.key_levels == [25050]
    assert (
        candidate_admission_blocker(stop, SimpleNamespace(profit_watch_error=True))
        == "SUPERVISOR_STOPPING"
    )


def test_wake_guard_and_watchdog_are_separate_launch_agents(tmp_path):
    configs = installer.definitions(tmp_path, tmp_path / "python")
    assert configs[installer.AWAKE]["ProgramArguments"] == [
        "/usr/bin/caffeinate",
        "-dimsu",
    ]
    assert configs[installer.AWAKE]["KeepAlive"]
    assert configs[installer.WATCHDOG]["StartInterval"] == 30
    assert "--act" in configs[installer.WATCHDOG]["ProgramArguments"]
    assert (
        "com.genesis.paper-forward" not in configs
    )  # installing guards does not replace the existing trading job
