"""Versioned experiment selection never discards an active held position."""
import importlib.util
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType

import pytest

from app.trading import paper_profit_trail
from app.trading.forward_comparison import execution_settings
from app.trading.option_quote import IST
from app.trading.paper_exit_policy import FRESH_BID, LEGACY, identity
from tests.test_forward_comparison import config  # noqa: F401 - imported pytest fixture


def test_manifest_pins_policy_source_without_changing_entry_settings(config):
    selected, policy, digest = execution_settings(config, {"exit_quote_policy": FRESH_BID,
                                                           "exit_quote_policy_identity": identity(FRESH_BID)})
    assert policy == FRESH_BID and digest == identity(FRESH_BID)
    assert selected.max_spread_pct == config.max_spread_pct
    assert config.paper_exit_quote_policy == LEGACY
    assert execution_settings(config, {})[1] == LEGACY
    for value in (None, "wrong"):
        with pytest.raises(ValueError, match="new experiment"):
            execution_settings(config, {"exit_quote_policy": FRESH_BID, "exit_quote_policy_identity": value})


def test_manifest_binds_profit_trail_and_does_not_accept_an_unpinned_change(config):
    manifest = {"exit_quote_policy": FRESH_BID, "exit_quote_policy_identity": identity(FRESH_BID),
                "candidate_profit_giveback_pct": "20",
                "candidate_profit_policy_identity": paper_profit_trail.identity(20)}
    selected, _, _ = execution_settings(config, manifest)
    assert selected.paper_candidate_profit_giveback_pct == 20
    assert selected.max_spread_pct == config.max_spread_pct
    manifest["candidate_profit_giveback_pct"] = "10"
    with pytest.raises(ValueError, match="new experiment"):
        execution_settings(config, manifest)


@pytest.mark.parametrize("held,hour", [(True, 16), (False, 16), (False, 10)])
def test_replacement_preserves_prior_journals_and_refuses_held_exposure(config, tmp_path, monkeypatch, held, hour):
    state = tmp_path / "state"
    prior = state / "runs" / "prior"
    prior.mkdir(parents=True)
    for name, table in (("baseline", "crew_paper_trades"), ("candidate", "trades")):
        with sqlite3.connect(prior / (name + ".db")) as c:
            c.execute(f"CREATE TABLE {table}(state TEXT)")
            if held and name == "baseline":
                c.execute(f"INSERT INTO {table} VALUES ('OPEN')")
    previous = {"directory": str(prior), "run_id": "prior"}
    (state / "active-run.json").write_text(json.dumps(previous))
    fake = ModuleType("paper_forward")
    fake.STATE = state
    fake.settings = lambda: config
    monkeypatch.setitem(sys.modules, "paper_forward", fake)
    file = Path(__file__).resolve().parents[2] / "scripts/start_paper_comparison.py"
    spec = importlib.util.spec_from_file_location("test_start_paper_exit", file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Clock:
        @classmethod
        def now(cls, tz):
            return datetime(2026, 11, 16, hour, 0, tzinfo=IST)

    module.datetime = Clock
    monkeypatch.setattr(sys, "argv", [str(file), "--activate", "--replace-active", "--exit-quote-policy", FRESH_BID,
                                      "--candidate-profit-giveback-pct", "20"])
    if held or hour == 10:
        with pytest.raises(SystemExit, match="held/unresolved" if held else "supervision hours"):
            module.main()
        assert json.loads((state / "active-run.json").read_text()) == previous
    else:
        module.main()
        active = json.loads((state / "active-run.json").read_text())
        assert active["exit_quote_policy"] == FRESH_BID
        assert active["exit_quote_policy_identity"] == identity(FRESH_BID)
        assert active["candidate_profit_giveback_pct"] == "20"
        assert active["candidate_profit_profile"]["fixed_target"] is False
        root = Path(active["directory"])
        assert (root / "previous-baseline.db").exists()
        assert (root / "previous-candidate.db").exists()
        assert json.loads((root / "previous-active-run.json").read_text()) == previous
        assert (prior / "baseline.db").exists() and (prior / "candidate.db").exists()
