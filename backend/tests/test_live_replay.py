"""Live-strategy replay: feed parity with the live FeedAgent, a deterministic golden fixture, no look-ahead,
lane separation and paper-risk-v2 invariants. Temp journals only; no network.

Regenerate the golden file after an approved rule change: ``REGEN_LIVE_REPLAY=1 pytest tests/test_live_replay.py``.
"""

from __future__ import annotations

import dataclasses
import json
import os
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.config import get_settings
from app.market.expiry import resolve_expiry
from app.market.hours import holidays
from app.trading import live_replay as lr
from app.trading import paper_registry as reg
from app.trading import research_runtime as rr
from app.trading.market_validation import closed_bars
from app.trading.option_quote import IST
from app.trading.scanner_crew.feed_agent import FeedAgent
from app.trading.scanner_crew.news_agent import NewsAgent
from app.trading.scanner_crew.scanner_manager import ScannerManager

FIXTURES = Path(__file__).parent / "fixtures" / "live_replay"
DAY = "2026-11-04"
VIX = {"2026-11-03": 16.0}  # test input: the EOD VIX table stops at 2026-10-13


def _days() -> dict[str, list[dict]]:
    days: dict[str, list[dict]] = {}
    for b in json.loads((FIXTURES / "nifty_5m_2026-11-03_04.json").read_text())["bars"]:
        days.setdefault(b["date"][:10], []).append(b)
    return days


def _expiry(day: str) -> str:
    return resolve_expiry(date.fromisoformat(day), holidays=holidays(get_settings())).isoformat()


def _replay(lane: str, journal: Path, days=None, **kw):
    settings = lr.replay_settings(get_settings(), paper=lane == "SYNTHETIC")
    # DAY predates the verified live fee policy (2026-11-06), so fees come from the historical schedule
    with lr.historical_fee_assumption():
        return lr.replay(days or _days(), journal, settings=settings, lane=lane, start=DAY, end=DAY, vix=VIX,
                         expiry_for=_expiry, lot_for=lambda d: 65, **kw)


def _summary(results, feed, journal: Path) -> dict:
    rows = lambda sql: lr.journal_rows(journal, sql)  # noqa: E731
    strip = lambda p: {k: v for k, v in p.items() if "session" not in k}  # noqa: E731
    paper = "crew_paper_decisions" in {r["name"] for r in rows("SELECT name FROM sqlite_master")}
    return {
        "days": [r.__dict__ for r in results],
        "news_gated_ticks": feed.scanner.news_gated_ticks,
        "candidates": [dict(v, identity=list(k)) for k, v in sorted(feed.scanner.candidates.items())],
        "admissions": [[a["class"], json.loads(a["reasons"]), a["decided_at"],
                        *(json.loads(a["inputs"])[k] for k in ("strategy_id", "signal_candle", "direction", "setup_id"))]
                       for a in rows("SELECT * FROM crew_admissions ORDER BY decided_at, admission_id")],
        "events": [[e["trade_id"], e["event_type"], strip(json.loads(e["payload"]))]
                   for e in rows("SELECT * FROM crew_events ORDER BY trade_id, seq")],
        "paper_decisions": [[d["account_id"], d["outcome"], json.loads(d["reasons"]), d["decided_at"]]
                            for d in rows("SELECT * FROM crew_paper_decisions ORDER BY decided_at, account_id")]
        if paper else [],
    }


@pytest.fixture(scope="module", autouse=True)
def identified_synthetic_contracts():
    monkeypatch = pytest.MonkeyPatch()
    from scripts.generate_synthetic_fixtures import identified_chain

    original = lr.synthetic_chain
    monkeypatch.setattr(lr, "synthetic_chain", lambda *a, **kw: identified_chain(original(*a, **kw)))
    yield
    monkeypatch.undo()


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    out = {}
    for lane in lr.LANES:
        journal = tmp_path_factory.mktemp(lane) / "journal.db"
        results, feed = _replay(lane, journal)
        out[lane] = {"journal": journal, "feed": feed, "summary": _summary(results, feed, journal)}
    return out


# --- Phase 3: parity ---------------------------------------------------------------------------------------


def _yahoo(bars: list[dict]) -> dict:
    stamps = [int(datetime.fromisoformat(b["date"] if " " in b["date"] else f"{b['date']} 09:15")
                  .replace(tzinfo=IST).timestamp()) for b in bars]
    quote = {k: [b[k] for b in bars] for k in ("open", "high", "low", "close", "volume")}
    return {"chart": {"result": [{"timestamp": stamps, "indicators": {"quote": [quote]}}]} if bars else {}}


def test_replay_feed_matches_the_live_feed_agent_on_the_same_yahoo_payload(monkeypatch):
    """The live FeedAgent path, served the payload Yahoo would return at each instant (the bar in progress included,
    5 sessions of 5m, 1 month of daily rows with today's partial row), gives the replay's exact scanner output."""
    days, clock = _days(), lr.Clock()
    history = lr.HistoricalFeedAgent(days, clock)

    def chart(symbol, interval, range_):
        assert symbol == "^NSEI"
        if interval not in ("5m", "1d"):
            return {}
        return _yahoo(history.fetch_timeframe_candles(interval, limit_bars=10_000).to_dict(orient="records"))

    monkeypatch.setattr("app.api.market._chart", chart)
    live_agent = FeedAgent()
    live = rr.scanner_feed(scanner=ScannerManager(feed_agent=live_agent), chain_source=lambda: {}, clock=clock)
    replay = lr.replay_feed(days, clock, lane="LIVE")
    compared = 0
    for minutes in range(0, 376, 5):
        for offset in (0, 2):  # at a bar close and mid-bar
            clock.t = datetime.fromisoformat(f"{DAY}T09:15:05+05:30") + timedelta(minutes=minutes + offset)
            live_agent.candle_cache.clear()  # its 3 s cache is wall-clock based
            a, b = live(clock.t), replay(clock.t)
            assert a.df_5m.to_dict(orient="records") == b.df_5m.to_dict(orient="records")
            assert a.key_levels == b.key_levels
            assert [dataclasses.asdict(s) for s in a.setups] == [dataclasses.asdict(s) for s in b.setups]
            compared += bool(a.setups)
    assert compared  # the day produced actionable setups, so the comparison is not vacuous


# One golden per strategy version; superseded versions are frozen byte-for-byte and never regenerated.
GOLDEN = {"scanner-v0": f"golden_{DAY}.json", "scanner-v0.1": f"golden_{DAY}_scanner-v0.1.json"}
FROZEN_GOLDEN = {"scanner-v0": ("f61fe382657771b62be7c02bb549b2759d045a5821821985eaae9f529577059c",
                                "97a834c1c42cbadfbcff0fe349e0803d1e5d030ca32a5017074dd5f812244729")}


@pytest.mark.skipif(not (FIXTURES / GOLDEN[rr.STRATEGY_VERSION]).is_file(),
                    reason="Historical replay golden excluded from source-only V3; independent synthetic outcomes and repeatability run in test_synthetic_release")
def test_golden_replay_is_deterministic(runs):
    golden_path = FIXTURES / GOLDEN[rr.STRATEGY_VERSION]
    current = {"strategy_version": rr.STRATEGY_VERSION, "rule_source_hashes": reg.RULE_SOURCE_HASHES,
               "registry_hash": reg.REGISTRY_HASH, "lanes": {k: v["summary"] for k, v in runs.items()}}
    current = json.loads(json.dumps(current, default=str))
    if os.environ.get("REGEN_LIVE_REPLAY"):
        assert rr.STRATEGY_VERSION not in FROZEN_GOLDEN, "superseded goldens are frozen"
        golden_path.write_text(json.dumps(current, indent=1, sort_keys=True) + "\n")
        pytest.skip("golden regenerated")
    golden = json.loads(golden_path.read_text())
    assert (golden["strategy_version"], golden["registry_hash"]) == (rr.STRATEGY_VERSION, reg.REGISTRY_HASH), \
        "golden recorded under another registry: re-baseline"
    assert current["rule_source_hashes"] == golden["rule_source_hashes"], "rule sources changed: re-baseline"
    assert current == golden


@pytest.mark.skipif(any(not (FIXTURES / GOLDEN[v]).is_file() for v in FROZEN_GOLDEN),
                    reason="Superseded historical goldens intentionally excluded from source-only V3")
def test_superseded_goldens_are_preserved_byte_for_byte():
    import hashlib

    for version, (registry, digest) in FROZEN_GOLDEN.items():
        raw = (FIXTURES / GOLDEN[version]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == digest
        golden = json.loads(raw)
        assert (golden["strategy_version"], golden["registry_hash"]) == (version, registry) != (
            rr.STRATEGY_VERSION, reg.REGISTRY_HASH)


def test_lanes_differ_only_after_the_scanner(runs):
    live, syn = runs["LIVE"]["summary"], runs["SYNTHETIC"]["summary"]
    assert live["candidates"] == syn["candidates"] and live["candidates"]
    assert live["news_gated_ticks"] == syn["news_gated_ticks"] == 0
    assert live["admissions"] and all(a[0] == "REJECTED" and a[1] == ["QUOTE_MISSING"] for a in live["admissions"])
    assert live["events"] == [] and live["paper_decisions"] == []
    # every live rejection was admitted on the synthetic chain at the same instant
    assert [a[2:] for a in live["admissions"]] == [a[2:] for a in syn["admissions"]]


# --- no look-ahead --------------------------------------------------------------------------------------


def test_future_bars_cannot_change_any_earlier_decision(tmp_path):
    cut = "14:30"
    days = _days()
    shocked = {d: [b | {k: b[k] + 400 for k in ("open", "high", "low", "close")} if b["date"] > f"{DAY} {cut}" else b
                   for b in bars] for d, bars in days.items()}
    a = _summary(*_replay("SYNTHETIC", tmp_path / "a.db", days=days, last=cut), tmp_path / "a.db")
    b = _summary(*_replay("SYNTHETIC", tmp_path / "b.db", days=shocked, last=cut), tmp_path / "b.db")
    assert a == b and a["admissions"]


def test_feed_never_serves_a_bar_after_the_clock():
    days, clock = _days(), lr.Clock()
    agent = lr.HistoricalFeedAgent(days, clock)
    for minutes in range(0, 376, 7):
        clock.t = datetime.fromisoformat(f"{DAY}T09:15:05+05:30") + timedelta(minutes=minutes)
        raw = agent.fetch_timeframe_candles("5m")
        assert raw["date"].max() <= clock.t.strftime("%Y-%m-%d %H:%M")
        done = closed_bars(raw, clock.t)
        if not done.empty:
            last = datetime.fromisoformat(done["date"].iloc[-1]).replace(tzinfo=IST) + timedelta(minutes=5)
            assert last <= clock.t
        daily = agent.fetch_timeframe_candles("1d")
        assert daily.iloc[-1]["date"] == DAY and daily.iloc[-2]["date"] == "2026-11-03"
    assert agent.fetch_timeframe_candles("1m").empty and agent.fetch_timeframe_candles("15m").empty


def test_point_in_time_inputs_never_use_the_same_day():
    series = {"2026-11-03": 1.0, "2026-11-04": 2.0}
    assert lr.prior_session(series, "2026-11-04") == 1.0
    assert lr.prior_session(series, "2026-11-03") is None
    assert lr.prior_session(series, "2026-11-20") is None  # too stale to stand in


def test_synthetic_chain_is_empty_without_point_in_time_vix():
    days, clock = _days(), lr.Clock(datetime.fromisoformat(f"{DAY}T10:00:05+05:30"))
    agent = lr.HistoricalFeedAgent(days, clock)
    kw = dict(expiry_for=_expiry, lot_for=lambda d: 65, spread_pct=1.0)
    assert lr.synthetic_chain(agent, clock, vix={}, **kw)() == {}
    source = lr.synthetic_chain(agent, clock, vix=VIX, **kw)
    rows = source()["optionChain"]["optionContracts"]
    assert all(r["underlying"] == "NIFTY" for r in rows)
    assert all("oi" not in r[s]["liveData"] for r in rows for s in ("ce", "pe"))  # no invented OI walls
    assert {e.source for e in source.deltas.values()} == {lr.SYNTHETIC_SOURCE}


# --- risk policy and capital feasibility --------------------------------------------------------------------


def test_synthetic_paper_decisions_obey_paper_risk_v2(runs):
    check_paper_risk(runs["SYNTHETIC"]["journal"])


def check_paper_risk(journal: Path) -> None:
    """paper-risk-v2 invariants over any replay journal (also run against the full-history journals)."""
    decisions = lr.journal_rows(journal, "SELECT * FROM crew_paper_decisions")
    risk = {(r["account_id"], r["admission_id"]): json.loads(r["evidence"])
            for r in lr.journal_rows(journal, "SELECT * FROM crew_paper_risk_decisions")}
    assert decisions and len(risk) == len(decisions)
    for d in decisions:
        ev = risk[(d["account_id"], d["admission_id"])]
        if ev["reasons"] == ["FULL_PREMIUM_LIMIT"]:  # one whole lot already exceeds the max-loss cap
            q = ev["quote"]
            # full_premium_loss adds entry and exit charges, well under ₹150 a lot
            one_lot = (Decimal(q["ask"]) + Decimal("0.05")) * q["lot_size"]
            assert one_lot + Decimal("150") > Decimal(ev["limits"]["full_premium"])
    for d in [d for d in decisions if d["outcome"] == "OPENED"]:
        ev = risk[(d["account_id"], d["admission_id"])]
        sizing, before = ev["sizing"], ev["account_before"]
        assert 1 <= sizing["lots"] <= reg.RISK_POLICY["max_lots"]
        assert Decimal(sizing["full_premium_loss"]) <= Decimal(reg.RISK_POLICY["max_loss_per_trade_inr"])
        assert Decimal(sizing["estimated_stop_risk"]) <= Decimal(before["stop_budget"]) <= Decimal("1000.00")
        assert ev["registry_hash"] == reg.REGISTRY_HASH and ev["cost_policy_id"]
    trades = lr.journal_rows(journal, "SELECT * FROM crew_paper_trades")
    for account in {t["account_id"] for t in trades}:
        spans = [(t["opened_at"], t["closed_at"] or "9") for t in trades if t["account_id"] == account]
        peak = max(sum(o <= at < c for o, c in spans) for at, _ in spans)
        assert peak <= reg.max_positions(account)


def test_live_lane_trusts_no_delta_and_never_reaches_the_ledger(runs):
    tables = {r["name"] for r in lr.journal_rows(runs["LIVE"]["journal"], "SELECT name FROM sqlite_master")}
    assert "crew_paper_decisions" not in tables or not lr.journal_rows(
        runs["LIVE"]["journal"], "SELECT * FROM crew_paper_decisions")


def test_historical_fee_assumption_is_scoped_to_the_context():
    original = rr.pl.enter
    with lr.historical_fee_assumption():
        assert rr.pl.enter.keywords == {"allow_historical_brokerage_assumption": True}
    assert rr.pl.enter is original


def test_outputs_must_live_outside_the_repository(tmp_path):
    with pytest.raises(rr.StartupRefused):
        lr.check_output(rr._REPO / "reports" / "replay")
    assert lr.check_output(tmp_path) == tmp_path.resolve()


def test_years_without_a_holiday_calendar_are_not_replayable(tmp_path):
    days = {"2016-03-01": _days()[DAY]}
    results, _ = lr.replay(days, tmp_path / "j.db", settings=lr.replay_settings(get_settings(), paper=False))
    assert [(r.status, r.detail.split(":")[0]) for r in results] == [("NOT_REPLAYABLE", "CALENDAR_MISSING")]


# --- Phase 6 contracts pinned as-is (fixes proposed, not applied) -----------------------------------------


def test_live_news_guard_is_inert_without_headlines():
    """The runtime's ScannerManager builds NewsAgent() with no headlines, so the event guard never fires and the
    historical replay reproduces it exactly. Wiring headlines would need a point-in-time archive first."""
    agent = ScannerManager().news_agent
    assert agent.cached_headlines == []
    for minutes in range(0, 376):
        t = (datetime(2026, 1, 1, 9, 15) + timedelta(minutes=minutes)).strftime("%H:%M")
        assert not NewsAgent().evaluate_news_sentiment(current_time_str=t).high_impact_event_active


def test_regime_v2_ema_spans_the_prior_session_while_vwap_resets():
    import pandas as pd

    from app.trading import regime

    days, clock = _days(), lr.Clock(datetime.fromisoformat(f"{DAY}T10:30:05+05:30"))
    df = closed_bars(lr.HistoricalFeedAgent(days, clock).fetch_timeframe_candles("5m"), clock.t)
    vec = regime.classify_regime(df, now=clock.t)
    session = df[df["date"].str.startswith(DAY)]
    assert len(session) < len(df)
    assert vec.ema_15 == round(regime.compute_ema(df["close"]), 2) != round(regime.compute_ema(session["close"]), 2)
    assert vec.session_vwap == round(regime.compute_session_vwap(pd.DataFrame(session)), 2)


@pytest.mark.parametrize('trusted,age,expected', [
    (True, 0, 'OPENED'),
    (False, 0, 'STOP_RISK_UNVERIFIABLE'),
    (True, 11, 'STOP_RISK_UNVERIFIABLE'),
])
def test_source_only_synthetic_risk_replay_golden(tmp_path, trusted, age, expected):
    """Independent invented input and expected outcomes; shipped policy and rule hashes stay intact."""
    from app.trading.paper_risk import DeltaEvidence
    from tests.test_paper_trading import PaperFeed, quote
    from tests.test_research_runtime import Clock, setup

    expected_accounts = ['IND:LIQ_SWEEP_PDHL', 'SHARED']
    summaries = []
    for repeat in range(2):
        clock = Clock('10:19')
        base = PaperFeed([setup(stop=23980)])

        def feed(now):
            contract = quote(now).tradingsymbol
            evidence = DeltaEvidence(Decimal('0.5'), now - timedelta(seconds=age), contract, 'synthetic-spec')
            return dataclasses.replace(base(now), delta_evidence={contract: evidence})

        journal = tmp_path / f'replay-{repeat}.db'
        runtime = rr.ResearchRuntime(
            journal, feed, settings=lr.replay_settings(get_settings(), paper=True), clock=clock,
            trusted_delta_sources=frozenset({'synthetic-spec'}) if trusted else frozenset())
        runtime.start()
        try:
            runtime.tick()
            decisions = lr.journal_rows(journal, 'SELECT * FROM crew_paper_decisions ORDER BY account_id')
            assert [d['account_id'] for d in decisions] == expected_accounts
            if expected == 'OPENED':
                assert [d['outcome'] for d in decisions] == ['OPENED', 'OPENED']
                trades = lr.journal_rows(journal, 'SELECT * FROM crew_paper_trades ORDER BY account_id')
                assert len(trades) == 2 and all(t['lots_initial'] == 1 for t in trades)
            else:
                assert all(d['outcome'] == 'REJECTED' and json.loads(d['reasons']) == [expected] for d in decisions)
                assert lr.journal_rows(journal, 'SELECT * FROM crew_paper_trades') == []
            check_paper_risk(journal)
            summaries.append([(d['account_id'], d['outcome'], json.loads(d['reasons'])) for d in decisions])
        finally:
            runtime.stop('SYNTHETIC_SPEC_END')
    assert summaries[0] == summaries[1]


def test_source_only_catalog_replacements_fail_closed():
    from app.strategies.analyzer import RESEARCH_CATALOG, load_markdown_content
    from app.strategies.normalizer import normalize_markdown

    assert len(RESEARCH_CATALOG) == 7
    for item in RESEARCH_CATALOG:
        markdown = load_markdown_content(item['file_name'])
        assert 'SYNTHETIC' in markdown
        if markdown.startswith('---'):
            spec = normalize_markdown(markdown)
            assert spec.quarantined and not spec.simulatable and not spec.paper_eligible and not spec.promotable
        else:
            assert 'no executable rules' in markdown
