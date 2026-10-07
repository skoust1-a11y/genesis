"""Sweep entry fields and the odds gate: pure functions, policy, simulator, Trade Crew and API."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app.agents.trade_crew.graph import run_tick
from app.agents.trade_crew.runner import build_context
from app.brokers.sandbox import SandboxBroker, SandboxMarket
from app.config import get_settings
from app.engine.costs import resolve_cost_profile
from app.engine.sim import Management, Scenario, run_window
from app.live import odds as live_odds
from app.live.store import LiveStore
from app.market.feed import BrokerFeed, MarketSnapshot, ReplayFeed
from app.market.source import HistoricalSource
from app.strategies.rules import ENTRY_FIELDS, StrategyRules, check_entry
from app.strategies.schema import parse_strategy_markdown
from app.trading import odds
from app.trading.odds import HistTrade
from app.trading.policy import OddsGate, TradePolicy
from tests.conftest import APPROVABLE_MARKDOWN, approve_and_start
from tests.test_risk_rules import RAW, simulate_md
from tests.test_simulator import ENTRY as ENTRY_CHAIN
from tests.test_simulator import seed, store  # noqa: F401
from tests.test_trade_crew import ENTRY, Clock, deployment_id  # noqa: F401

GATE = OddsGate(vix_bands=[13, 16], min_trades=5)


def snap(**kw) -> MarketSnapshot:
    base = {"underlying": "NIFTY", "timestamp": datetime(2026, 1, 5, tzinfo=UTC), "as_of": "2026-01-05",
            "spot": 22_000, "iv_rank": 50, "dte": 14, "expiry": "2026-01-19"}  # fmt: skip
    return MarketSnapshot(**(base | kw))


def hist(*rows: tuple[str, str, float, float | None]) -> list[HistTrade]:
    return [HistTrade(entry, exit_, net, vix) for entry, exit_, net, vix in rows]


# Sweep fields


def test_sweep_fields():
    s = snap(spot=22_050, day_high=22_220, day_low=21_900, prev_high=22_100, prev_low=21_950)
    assert s.pct_high_from_prev_high == round(120 / 22_100 * 100, 4)  # swept the previous high
    assert s.pct_from_prev_high < 0  # and closed back below it
    assert s.pct_low_from_prev_low == round(-50 / 21_950 * 100, 4)
    assert snap(prev_high=22_100).pct_high_from_prev_high is None  # no day high: the check fails safe
    assert snap(day_low=21_900).pct_low_from_prev_low is None
    assert {"pct_high_from_prev_high", "pct_low_from_prev_low"} <= set(ENTRY_FIELDS)

    rules = StrategyRules.model_validate(RAW | {"entry": [
        {"field": "pct_high_from_prev_high", "op": ">", "value": 0},
        {"field": "pct_from_prev_high", "op": "<", "value": 0},
    ]})  # fmt: skip
    assert all(c.passed for c in check_entry(rules, s))
    assert not all(c.passed for c in check_entry(rules, replace(s, spot=22_150)))  # closed above: a breakout
    assert not all(c.passed for c in check_entry(rules, replace(s, day_high=None)))


def test_broker_feed_passes_the_day_range_from_the_index_quote():
    now = datetime(2026, 1, 5, 6, 0, tzinfo=UTC)
    broker = SandboxBroker(market=SandboxMarket(spot=22_000, day_high=22_150, day_low=21_800), clock=lambda: now)
    feed = BrokerFeed(broker, get_settings(), clock=lambda: now, iv_rank=lambda u, v, d: 50.0,
                      prev_range=lambda u, d: (22_100.0, 21_900.0))  # fmt: skip
    s = feed.snapshot("NIFTY")
    assert (s.day_high, s.day_low) == (22_150, 21_800)
    assert s.pct_high_from_prev_high > 0 and s.pct_low_from_prev_low < 0
    plain = SandboxBroker(market=SandboxMarket(), clock=lambda: now)
    s2 = BrokerFeed(plain, get_settings(), clock=lambda: now, iv_rank=lambda u, v, d: 50.0,
                    prev_range=lambda u, d: (22_100.0, 21_900.0)).snapshot("NIFTY")  # fmt: skip
    assert s2.day_high is None and s2.pct_high_from_prev_high is None


# Pure functions


def test_band_edges():
    edges = [13, 16]
    assert [odds.band_of(v, edges) for v in (12.99, 13, 15.9, 16, 30)] == [0, 1, 1, 2, 2]
    assert odds.band_of(None, edges) is None
    assert [odds.band_label(i, edges) for i in range(3)] == ["VIX < 13", "VIX 13-16", "VIX >= 16"]


def test_odds_never_use_a_trade_that_closes_on_or_after_the_day():
    h = hist(("2026-01-01", "2026-01-02", 100, 14), ("2026-01-02", "2026-01-05", 100, 14), ("2026-01-05", "2026-01-07", -5000, 14))
    o = odds.odds_for(h, "2026-01-05", 14.5, GATE)
    assert o.trades == 1 and o.expectancy == 100
    assert odds.odds_for(h, "2026-01-06", 14.5, GATE).trades == 2
    assert odds.odds_for(h, "2026-01-08", 14.5, GATE).trades == 3


def test_break_even_maths_and_bands():
    h = hist(
        ("2025-01-01", "2025-01-02", 3000, 12), ("2025-01-03", "2025-01-04", 3000, 12),
        ("2025-01-05", "2025-01-06", -1000, 12), ("2025-01-07", "2025-01-08", -1000, 12),
        ("2025-01-09", "2025-01-10", -1000, 12), ("2025-01-11", "2025-01-12", 500, 20),
    )  # fmt: skip
    o = odds.odds_for(h, "2026-01-01", 12.5, GATE)
    assert (o.band, o.trades, o.wins) == ("VIX < 13", 5, 2)
    assert o.win_rate == 40.0 and o.avg_win == 3000 and o.avg_loss == 1000
    assert o.breakeven_win_rate == 25.0  # 1000 / (3000 + 1000)
    assert o.expectancy == 600
    assert all(c.passed for c in odds.odds_checks(o, GATE))
    table = odds.odds_table(h, "2026-01-01", GATE)
    assert [(b.band, b.trades) for b in table] == [("VIX < 13", 5), ("VIX 13-16", 0), ("VIX >= 16", 1)]
    assert odds.odds_text(o) == "VIX < 13: 5 past trades, win rate 40% vs 25% needed, average +600 INR per lot"


def test_lookback_years_drops_old_trades():
    h = hist(("2020-01-01", "2020-01-02", 100, 14), ("2025-06-01", "2025-06-02", 100, 14))
    gate = GATE.model_copy(update={"lookback_years": 2})
    assert odds.odds_for(h, "2026-01-05", 14, gate).trades == 1
    assert odds.odds_for(h, "2026-01-05", 14, GATE).trades == 2


def test_gate_fails_safe_without_vix_or_enough_trades():
    h = hist(*[(f"2025-01-0{i}", f"2025-01-0{i + 1}", 500, 14) for i in range(1, 6)])
    no_vix = odds.odds_checks(odds.odds_for(h, "2026-01-01", None, GATE), GATE)
    assert [c.passed for c in no_vix] == [False, False]
    few = odds.odds_checks(odds.odds_for(h[:4], "2026-01-01", 14, GATE), GATE)
    assert {c.rule: c.passed for c in few} == {"odds.sample": False, "odds.expectancy": True}
    losing = hist(*[(f"2025-01-0{i}", f"2025-01-0{i + 1}", -500, 14) for i in range(1, 6)])
    bad = odds.odds_checks(odds.odds_for(losing, "2026-01-01", 14, GATE), GATE)
    assert {c.rule: c.passed for c in bad} == {"odds.sample": True, "odds.expectancy": False}
    high_bar = GATE.model_copy(update={"min_expectancy_inr": 600})
    assert not odds.odds_checks(odds.odds_for(h, "2026-01-01", 14, high_bar), high_bar)[1].passed


@pytest.mark.parametrize(
    "raw, message",
    [
        ({"vix_bands": [16, 13]}, "must increase"),
        ({"vix_bands": []}, "at least 1"),
        ({"vix_bands": [10, 13, 16, 20, 25]}, "at most 4"),
        ({"min_trades": 4}, "greater than or equal to 5"),
        ({"lookback_years": 0}, "greater than or equal to 1"),
    ],
)
def test_policy_validation(raw, message):
    with pytest.raises(ValueError, match=message):
        TradePolicy.model_validate({"odds_gate": raw})
    assert TradePolicy.model_validate({"odds_gate": {}}).odds_gate.vix_bands == [13, 16]


# Simulator

WINNERS = hist(*[(f"2025-12-0{i}", f"2025-12-0{i + 1}", 800, 15) for i in range(1, 6)])
LOSERS = hist(*[(f"2025-12-0{i}", f"2025-12-0{i + 1}", -800, 15) for i in range(1, 6)])


def gated(history: list[HistTrade]) -> Management:
    return Management(policy=TradePolicy(odds_gate=GATE), odds_history=history)


def test_sim_odds_gate_blocks_and_counts(store):  # noqa: F811
    seed(store, [("2026-01-05", 22000, 60, ENTRY_CHAIN), ("2026-01-06", 22000, 60, ENTRY_CHAIN)])  # VIX 15
    allowed = simulate_md(store, APPROVABLE_MARKDOWN, gated(WINNERS))
    assert [t.date for t in allowed.trades] == ["2026-01-05"]
    assert allowed.management["odds_blocked"] == 0
    bands = {b["band"]: b for b in allowed.management["odds_bands"]}
    assert bands["VIX 13-16"]["trades"] == 5 and bands["VIX 13-16"]["expectancy"] == 800

    blocked = simulate_md(store, APPROVABLE_MARKDOWN, gated(LOSERS))
    assert blocked.trades == [] and blocked.skipped["odds.expectancy"] == 2
    assert blocked.management["odds_blocked"] == 2

    later = hist(*[(f"2026-01-0{i}", "2026-01-05", 800, 15) for i in range(1, 6)])  # close on the entry day
    early = simulate_md(store, APPROVABLE_MARKDOWN, gated(later))
    assert early.skipped["odds.sample"] == 1 and [t.date for t in early.trades] == ["2026-01-06"]
    assert simulate_md(store, APPROVABLE_MARKDOWN, Management()).management.get("odds_bands") is None


def test_run_window_builds_the_history_from_the_strategy_itself(store):  # noqa: F811
    seed(store, [("2026-01-05", 22000, 60, ENTRY_CHAIN), ("2026-01-06", 22000, 60, ENTRY_CHAIN)])
    source = HistoricalSource(store, "NIFTY")
    settings = get_settings()
    doc = parse_strategy_markdown(APPROVABLE_MARKDOWN)
    scenario = Scenario("base", doc.frontmatter["rules"], lots=2)
    profile = resolve_cost_profile(settings, doc)
    r = run_window(scenario, source, source.dates(), profile, settings, "backtest",
                   Management(policy=TradePolicy(odds_gate=GATE)))  # fmt: skip
    rules_only = odds.strategy_history(doc.rules, source, profile, settings)
    assert r.management["odds_history_trades"] == len(rules_only) == 1
    assert r.trades == [] and r.management["odds_blocked"] == 2  # one past trade is below min_trades


# Trade Crew and API


class VixFeed(ReplayFeed):
    def __init__(self, *args, vix: float | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.vix = vix

    def snapshot(self, underlying, contracts=()):
        s = super().snapshot(underlying, contracts)
        return replace(s, vix=self.vix) if s is not None else None


@pytest.fixture
def odds_deployment(client, approvable, paper_account, monkeypatch):
    live_odds.clear_cache()
    loaded: list[str] = []

    def use(history: list[HistTrade]) -> str:
        def load(rules, profile, settings, as_of):
            loaded.append(as_of)
            return history

        monkeypatch.setattr(live_odds, "_load", load)
        policy = {"odds_gate": {"vix_bands": [13, 16], "min_trades": 5}}
        return approve_and_start(client, approvable, paper_account, risk_limits={"policy": policy})

    use.loaded = loaded
    return use


def crew(live_session, dep_id, vix):
    clock = Clock()
    feed = VixFeed("NIFTY", bars=[ENTRY], strike_step=50, clock=clock, vix=vix)
    return build_context(live_session, dep_id, get_settings(), feed=feed, clock=clock)


def test_crew_enters_when_the_odds_favour_it(live_session, odds_deployment):
    dep_id = odds_deployment(WINNERS)
    assert run_tick(crew(live_session, dep_id, 15.0))["outcome"] == "entered"
    [intent] = LiveStore(live_session).intents(dep_id)
    assert "Odds: VIX 13-16: 5 past trades, win rate 100%, average +800 INR per lot." in intent.reason_text
    assert odds_deployment.loaded == ["2026-01-05"]


def test_crew_blocks_when_the_odds_are_against_it(live_session, odds_deployment):
    dep_id = odds_deployment(LOSERS)
    out = run_tick(crew(live_session, dep_id, 15.0))
    assert out["outcome"] == "blocked" and out["detail"] == "odds.expectancy"
    [intent] = LiveStore(live_session).intents(dep_id)
    assert "odds.expectancy: avg_net_per_lot_inr > 0 (actual -800)" in intent.reason_text


def test_crew_blocks_without_a_vix_reading(live_session, odds_deployment):
    dep_id = odds_deployment(WINNERS)
    out = run_tick(crew(live_session, dep_id, None))
    assert out["outcome"] == "blocked" and set(out["detail"].split(", ")) == {"odds.sample", "odds.expectancy"}


def test_odds_endpoint(client, live_session, odds_deployment, monkeypatch):
    dep_id = odds_deployment(WINNERS)
    monkeypatch.setattr(live_odds, "_latest_vix", lambda settings, underlying: ("2026-01-02", 12.0))
    live_session.rollback()
    body = client.get(f"/api/live/deployments/{dep_id}/odds").json()["odds"]
    assert body["entries_open"] is False and body["today"]["band"] == "VIX < 13"  # no trades in that band
    assert [b["trades"] for b in body["bands"]] == [0, 5, 0]
    assert body["gate"]["min_trades"] == 5 and body["history_trades"] == 5


def test_odds_endpoint_without_a_gate(client, deployment_id):  # noqa: F811
    assert client.get(f"/api/live/deployments/{deployment_id}/odds").json() == {"odds": None}
    assert client.get("/api/live/deployments/nope/odds").status_code == 404
