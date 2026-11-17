"""Protective paper exits: actual bid, unchanged entry limits, retained unpriceable exposure."""
import json
import sqlite3
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from app.db import crew_journal as cj
from app.trading import paper_exec as px
from app.trading import research_runtime as rr
from app.trading.forward_candidate_account import CandidateAccount
from app.trading.paper_exit_policy import FRESH_BID, LEGACY, bind
from app.trading.paper_risk import DeltaEvidence
from tests.test_forward_comparison import (
    NOW,
    config,  # noqa: F401 - imported pytest fixture
    quote,
    raw,
    signal,
    tick,
)
from tests.test_paper_trading import PaperFeed
from tests.test_research_runtime import DAY, Clock, bars, rows, setup, walk


def contract(now=NOW):
    chain, _ = quote(now)
    return px.contract_quote(chain, strike=25100, option_type="CE", expiry="2026-11-17")


def test_wide_book_remains_forbidden_for_entry_but_can_liquidate(config):
    settings = config.model_copy(update={"paper_exit_quote_policy": FRESH_BID})
    q = replace(contract(), bid=Decimal(".05"), ask=Decimal(".10"))
    assert px.executable(q, NOW, settings) == (None, px.SPREAD_WIDE)
    assert px.exit_executable(q, NOW, config) == (None, px.SPREAD_WIDE)
    accepted, reason = px.exit_executable(q, NOW, settings)
    assert reason is None and accepted.bid == Decimal(".05")
    assert accepted.source.endswith("paper-exit:fresh-bid-v1")
    assert settings.max_spread_pct == config.max_spread_pct


@pytest.mark.parametrize("change", [
    {"quote_time": NOW-timedelta(seconds=11)},
    {"fetched_at": NOW-timedelta(seconds=11)},
    {"quote_time": NOW+timedelta(seconds=1)},
    {"quote_time": None}, {"bid": None}, {"ask": None},
    {"bid": Decimal(0)}, {"bid": Decimal(".01")}, {"bid": Decimal("NaN")},
    {"bid": Decimal("Infinity")}, {"ask": Decimal(".01")},
    {"provider_instrument_id": None}, {"tradingsymbol": None}, {"lot_size": 0},
    {"expiry": "2026-11-13"},
])
def test_exit_policy_never_invents_a_quote(config, change):
    settings = config.model_copy(update={"paper_exit_quote_policy": FRESH_BID})
    q = replace(contract(), bid=Decimal(".05"), ask=Decimal(".10"), **change) if not {"bid", "ask"} & change.keys() else replace(contract(), **change)
    assert px.exit_executable(q, NOW, settings)[0] is None


def test_no_fills_after_market_close_or_under_live_flags(config):
    settings = config.model_copy(update={"paper_exit_quote_policy": FRESH_BID})
    late = NOW.replace(hour=15, minute=31)
    assert px.exit_executable(contract(late), late, settings)[1] == "MARKET_CLOSED"
    assert px.exit_executable(contract(), NOW.replace(tzinfo=None), settings)[0] is None
    assert px.exit_executable(contract(), NOW, settings.model_copy(update={"live_trading_enabled": True}))[0] is None


@pytest.mark.parametrize("policy,closed", [(LEGACY, 0), (FRESH_BID, 1)])
@pytest.mark.parametrize("bid", ["21", "20"])
def test_candidate_wide_exit_closes_at_bid_and_releases_exposure(config, tmp_path, monkeypatch, policy, closed, bid):
    settings = config.model_copy(update={"paper_exit_quote_policy": policy})
    account = CandidateAccount(tmp_path / "candidate.db", settings, run_id="exit-test")
    try:
        tick(account, settings, monkeypatch)
        later = NOW+timedelta(minutes=1)
        chain, evidence = quote(later)
        chain["contracts"][0]["ce"]["liveData"].update(bid=bid, ask="40")
        monkeypatch.setattr("app.trading.forward_zone_candidate.advance", lambda rows, state: (state, []))
        from app.trading.forward_fees import policy as fee_policy

        account.tick(now=later, raw_1m=raw(later, 25035), chain=chain, evidence=evidence,
                     news_safe=False, fee_policy=fee_policy("2026-11-16").policy_id)
        stats = account.stats()
        assert stats["closed"] == closed
        assert stats["unresolved"] == 1-closed
        with account.connect() as c:
            sells = [json.loads(r[0]) for r in c.execute("SELECT body FROM fills WHERE side='SELL'")]
        if closed:
            assert Decimal(sells[0]["price"]) == Decimal(bid)-Decimal(".05")
            assert sells[0]["charges_inr"] != "0"
            from app.trading import paper_risk

            original_size = paper_risk.size
            budgets = []

            def audited_size(**kwargs):
                budget = kwargs["stop_budget"]
                assert budget % Decimal(".01") == 0
                assert budget <= min(Decimal(1000), kwargs["available_cash"] * Decimal(".02"))
                budgets.append(budget)
                return original_size(**kwargs)

            monkeypatch.setattr(paper_risk, "size", audited_size)
            tick(account, settings, monkeypatch, now=NOW+timedelta(minutes=2), sig=signal(NOW+timedelta(minutes=2), id="s2"))
            assert budgets
            assert account.stats()["entries"] == 2
        else:
            assert sells == []
    finally:
        account.close()


@pytest.mark.parametrize("mutation", ["provider", "symbol", "fees", "stale"])
def test_candidate_keeps_unknown_exit_and_reservation(config, tmp_path, monkeypatch, mutation):
    settings = config.model_copy(update={"paper_exit_quote_policy": FRESH_BID})
    account = CandidateAccount(tmp_path / "candidate.db", settings, run_id="exit-test")
    try:
        tick(account, settings, monkeypatch)
        cash = account.stats()["cash_inr"]
        later = NOW+timedelta(minutes=1)
        chain, evidence = quote(later)
        leg = chain["contracts"][0]["ce"]
        leg["liveData"].update(bid="21", ask="40")
        if mutation == "provider":
            leg["growwContractId"] = "DIFFERENT"
        elif mutation == "symbol":
            leg["tradingSymbol"] = "DIFFERENT"
        elif mutation == "stale":
            leg["liveData"]["quote_time"] = (later-timedelta(seconds=11)).isoformat()
        from app.trading.forward_fees import policy as fee_policy

        monkeypatch.setattr("app.trading.forward_zone_candidate.advance", lambda rows, state: (state, []))
        account.tick(now=later, raw_1m=raw(later, 25035), chain=chain, evidence=evidence, news_safe=True,
                     fee_policy=None if mutation == "fees" else fee_policy("2026-11-16").policy_id)
        assert account.stats()["unresolved"] == 1
        assert account.stats()["cash_inr"] == cash
    finally:
        account.close()


def test_policy_changes_cannot_relabel_an_existing_journal(config, tmp_path, monkeypatch):
    path = tmp_path / "candidate.db"
    account = CandidateAccount(path, config, run_id="exit-test")
    tick(account, config, monkeypatch)
    account.close()
    with pytest.raises(ValueError, match="changed"):
        CandidateAccount(path, config.model_copy(update={"paper_exit_quote_policy": FRESH_BID}), run_id="exit-test")
    with sqlite3.connect(":memory:") as c:
        c.execute("CREATE TABLE trades(id INTEGER)")
        c.execute("INSERT INTO trades VALUES (1)")
        with pytest.raises(ValueError, match="new journal"):
            bind(c, FRESH_BID, trades_table="trades")
        bind(c, LEGACY, trades_table="trades")
        with pytest.raises(ValueError, match="new journal"):
            bind(c, FRESH_BID, trades_table="trades")


@pytest.mark.parametrize("policy,expected", [(LEGACY, "EXIT_UNPRICED"), (FRESH_BID, "CLOSED")])
def test_baseline_liquidation_preserves_entry_rules_and_reconciles(config, tmp_path, policy, expected):
    clock = Clock("14:59")
    base = PaperFeed([setup(stop=23995, ts=f"{DAY} 14:55")], bid=10, ask=10.4)
    spot = [24000.0]

    def feed(now):
        view = base(now)
        start = (now-timedelta(minutes=now.minute%5+5)).strftime("%H:%M")
        symbol = view.chain["contracts"][0]["ce"]["tradingSymbol"]
        return replace(view, df_5m=bars(spot, start=start),
                       delta_evidence={symbol: DeltaEvidence(Decimal(".5"), now, symbol, "synthetic")})

    settings = config.model_copy(update={"paper_exit_quote_policy": policy, "paper_capital_inr": Decimal(50000)})
    path = tmp_path / "baseline.db"
    runtime = rr.ResearchRuntime(path, feed, settings=settings, clock=clock, trusted_delta_sources=frozenset({"synthetic"}))
    runtime.start()
    try:
        runtime.tick()
        assert len(rows(path, "SELECT * FROM crew_paper_trades")) == 2
        base.book_kw.update(bid=.05, ask=.1)
        spot[:] = [23990.0]
        walk(runtime, clock, "15:00")
        runtime.tick()
        assert all(r["state"] == expected for r in rows(path, "SELECT state FROM crew_paper_trades"))
        sold = rows(path, "SELECT * FROM crew_paper_events WHERE side='SELL'")
        if expected == "CLOSED":
            assert len(sold) == 2
            assert all(Decimal(r["price_inr"]) == Decimal(".05") for r in sold)
            assert all("fresh-bid-v1" in r["quote_source"] for r in sold)
        else:
            assert sold == []
        with pytest.raises(ValueError, match="new journal"):
            cj._fenced(path, runtime.lease, clock(), lambda c: bind(c, FRESH_BID if policy == LEGACY else LEGACY,
                                                                 trades_table="crew_paper_trades"))
    finally:
        runtime.stop()
