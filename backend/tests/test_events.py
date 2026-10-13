from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from app.config import get_settings
from app.engine.costs import resolve_cost_profile
from app.engine.sim import Scenario, dte_window, simulate
from app.market.calendar import event_lookup, load_event_yaml, parse_board_meetings
from app.market.historical import HistoricalFeed
from app.strategies.rules import EntryBlackout, check_blackout, check_entry, parse_rules
from app.strategies.schema import parse_strategy_markdown
from scripts.import_events import import_events
from tests.conftest import APPROVABLE_MARKDOWN
from tests.test_simulator import ENTRY, chain, seed

SEED_DIR = Path(__file__).resolve().parents[1] / "data" / "events"
BOARD_MEETINGS = """\ufeff"SYMBOL","COMPANY NAME","PURPOSE","BOARD MEETING DATE","DETAILS"
"RELIANCE","Reliance Industries Limited","Financial Results","19-Jan-2024","To consider results"
"HDFCBANK","HDFC Bank Limited","Fund Raising","16-Jan-2024","Bonds"
"INFY","Infosys Limited","Financial Results/Dividend","11-Jan-2024","Q3 results"
"""
BLACKOUT = {"kinds": ["rbi_policy"], "days_before": 1, "days_after": 0}


@pytest.fixture
def store(market_db_url):
    from app.market.store import open_market_store

    s = open_market_store(get_settings())
    yield s
    s.close()


def test_seed_calendar_loads_and_is_marked_for_verification():
    paths = sorted(SEED_DIR.glob("*.yaml"))
    events = [e for path in paths for e in load_event_yaml(path)]
    assert {e["kind"] for e in events} == {"budget", "election", "rbi_policy", "fomc"}
    assert all(e["underlying"] is None and e["source"] == "manual_macro_calendar" for e in events)
    assert all("VERIFY" in path.read_text() for path in paths)


def test_unknown_event_kind_is_rejected(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({"events": [{"date": "2024-01-01", "kind": "eclipse", "title": "x"}]}))
    with pytest.raises(ValueError, match="kind must be one of"):
        load_event_yaml(bad)


def test_board_meetings_become_per_stock_events():
    events = parse_board_meetings(BOARD_MEETINGS)
    assert [(e["date"], e["kind"], e["underlying"]) for e in events] == [
        ("2024-01-19", "results", "RELIANCE"),
        ("2024-01-16", "board_meeting", "HDFCBANK"),
        ("2024-01-11", "results", "INFY"),
    ]


def test_import_is_idempotent_and_lookup_is_scoped(market_db_url, tmp_path):
    csv_path = tmp_path / "board_meetings.csv"
    csv_path.write_text(BOARD_MEETINGS)
    first = import_events(SEED_DIR, [csv_path])
    assert first["added"] == first["read"] > 30
    assert import_events(SEED_DIR, [csv_path])["added"] == 0

    lookup = event_lookup(get_settings())
    near_policy = lookup("NIFTY", "2024-02-07")
    assert [(e["kind"], e["days_from"]) for e in near_policy if e["kind"] == "rbi_policy"] == [("rbi_policy", 1)]
    assert all(e["underlying"] is None for e in near_policy)
    assert [e["underlying"] for e in lookup("RELIANCE", "2024-01-18") if e["kind"] == "results"] == ["RELIANCE"]


def test_no_calendar_means_no_lookup(market_db_url):
    assert event_lookup(get_settings()) is None


def test_blackout_check_fails_safe_without_a_calendar():
    blackout = EntryBlackout(**BLACKOUT)
    assert check_blackout(blackout, None).passed is False
    assert check_blackout(blackout, []).passed is True
    policy = {"kind": "rbi_policy", "days_from": 1}
    assert check_blackout(blackout, [policy]).passed is False
    assert check_blackout(blackout, [policy | {"days_from": 2}]).passed is True
    assert check_blackout(blackout, [policy | {"days_from": -1}]).passed is True
    assert check_blackout(blackout, [{"kind": "fomc", "days_from": 0}]).passed is True


def test_blackout_rule_parses_and_joins_the_entry_checks():
    raw = parse_strategy_markdown(APPROVABLE_MARKDOWN).frontmatter["rules"] | {"entry_blackout": BLACKOUT}
    rules, errors = parse_rules({"rules": raw})
    assert errors == []
    market = SimpleNamespace(iv_rank=60, dte=10, spot=22000, events=[{"kind": "rbi_policy", "days_from": 0}])
    checks = check_entry(rules, market)
    assert [c.rule for c in checks][-1] == "entry.blackout" and not checks[-1].passed
    _, errors = parse_rules({"rules": raw | {"entry_blackout": {"kinds": ["eclipse"]}}})
    assert errors and errors[0].startswith("rules.entry_blackout.kinds")


def test_simulator_skips_entries_inside_a_blackout(store):
    doc = parse_strategy_markdown(APPROVABLE_MARKDOWN)
    raw = doc.frontmatter["rules"] | {"entry_blackout": BLACKOUT}
    rules, _ = parse_rules({"rules": raw})
    seed(store, [("2026-01-05", 22000, 60, ENTRY), ("2026-01-06", 22000, 60, ENTRY), ("2026-01-07", 22000, 10, chain(20, 8, 15, 6))])
    store.add_events([{"date": "2026-01-06", "kind": "rbi_policy", "title": "RBI MPC decision"}])
    settings = get_settings()
    feed = HistoricalFeed(store, "NIFTY", dte_window=dte_window(rules), events=event_lookup(settings))
    result = simulate(
        rules, feed, 3, resolve_cost_profile(settings, doc), settings,
        scenario=Scenario("base", raw, lots=2), mode="backtest",
    )  # fmt: skip
    assert result.trades == []  # day before and day of the policy are blocked; day 3 has no signal
