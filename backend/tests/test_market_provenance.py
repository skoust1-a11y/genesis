"""Contract-lot provenance, delta provenance and the signal-only supervised mode, on the shipped paper policy.

Quotes, deltas and the clock are fixtures. Nothing here observes a market session.
"""

from __future__ import annotations

import json
import socket
import subprocess
from datetime import timedelta
from decimal import Decimal

import pytest

from app.config import get_settings
from app.db import paper_ledger as pl
from app.trading import admission as adm
from app.trading import paper_registry as reg
from app.trading import research_runtime as rr
from app.trading.paper_risk import DeltaEvidence
from tests.test_research_runtime import DAY, Clock, bars, rows, setup

STRATEGY = "Breakout & Retest (OR-30) [5M]"
SYMBOL = "NIFTY26N1024100CE"
NO_STAMP = object()


def _settings(**kw):
    return get_settings().model_copy(update={"paper_capital_inr": Decimal("50000"), "live_trading_enabled": False,
                                             "sandbox_allow_live": False, **kw})


def _feed(*, lot=65, token="GC-24100CE", quote_age=0, delta=True, source="synthetic", delta_age=0):
    def feed(now):
        start = (now.replace(second=0, microsecond=0) - timedelta(minutes=now.minute % 5 + 5)).strftime("%H:%M")
        live = {"ltp": 10.2, "bid": 10.0, "ask": 10.4, "quoteTime": (now - timedelta(seconds=quote_age)).isoformat()}
        leg = {"liveData": live, "marketLot": lot, "tradingSymbol": SYMBOL, "growwContractId": token}
        if token is None:
            leg.pop("growwContractId")  # absent identity reaches the ledger; explicit null is malformed
        observed = None if delta_age is NO_STAMP else now - timedelta(seconds=delta_age)
        evidence = {SYMBOL: DeltaEvidence(Decimal("0.5"), observed, SYMBOL, source)} if delta else {}
        return rr.MarketView(df_5m=bars([24000.0], start=start),
                             setups=[setup(strategy=STRATEGY, stop=23995, ts=f"{DAY} 10:15")],
                             chain={"contracts": [{"underlying": "NIFTY", "strike": 24100, "ce": leg}], "expiry": "2026-11-10",
                                    "fetched_at": now},
                             key_levels=[], delta_evidence=evidence)
    return feed


def _tick(path, feed, *, trusted=frozenset({"synthetic"}), settings=None):
    rt = rr.ResearchRuntime(path, feed, settings=settings or _settings(), clock=Clock("10:19"),
                            trusted_delta_sources=trusted)
    rt.start()
    try:
        rt.tick()
    finally:
        rt.stop()
    return [(d["outcome"], json.loads(d["reasons"])) for d in rows(path, "SELECT * FROM crew_paper_decisions")]


@pytest.fixture
def path(tmp_path):
    return tmp_path / "crew_research.db"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def blocked(self, addr):
        raise AssertionError(f"network connect to {addr}")

    monkeypatch.setattr(socket.socket, "connect", blocked)


@pytest.fixture(autouse=True)
def _no_broker_order(monkeypatch):
    def refuse(self, order):
        raise AssertionError("broker place_order reached")

    for target in ("app.brokers.paper.PaperBroker.place_order", "app.brokers.sandbox.SandboxBroker.place_order",
                   "app.trading.execution_crew.broker_bridge_agent.BrokerBridgeAgent.place_order"):
        monkeypatch.setattr(target, refuse)


@pytest.fixture
def dated_lot_table(tmp_path, monkeypatch):
    table = tmp_path / "synthetic-lots.yaml"
    table.write_text("# SYNTHETIC transition scenario; no exchange authority\nchanges:\n"
        "  - {underlying: NIFTY, from: 2020-01-01, lot_size: 75}\n"
        "  - {underlying: NIFTY, from: 2021-06-25, lot_size: 50}\n"
        "  - {underlying: NIFTY, from: 2024-04-26, lot_size: 25}\n"
        "  - {underlying: NIFTY, from: 2024-11-20, lot_size: 75}\n"
        "  - {underlying: NIFTY, from: 2026-01-01, lot_size: 65}\n")
    monkeypatch.setattr(pl, "LOT_TABLE", table)


@pytest.mark.parametrize("day,lot", [("2026-11-10", 65), ("2025-06-02", 75), ("2022-06-01", 50),
                                     ("2024-06-03", 25), ("2024-12-02", 25)])  # last: an old-lot expiry still trading
def test_broker_lot_agrees_with_the_dated_reference_table(day, lot, dated_lot_table):
    assert pl.lot_verified("NIFTY", day, lot)


@pytest.mark.parametrize("day,lot", [("2026-11-10", 75), ("2026-11-10", 50), ("2022-06-01", 75), ("2018-06-01", 75),
                                     ("2026-11-10", 0)])
def test_broker_lot_that_contradicts_or_predates_the_table_is_unverified(day, lot, dated_lot_table):
    assert not pl.lot_verified("NIFTY", day, lot)


def test_a_stale_broker_lot_rejects_priced_paper_admission(path):
    assert _tick(path, _feed(lot=75)) == [("REJECTED", [pl.CONTRACT_LOT_UNVERIFIED])] * 2
    assert rows(path, "SELECT * FROM crew_paper_trades") == []


def test_a_missing_instrument_token_rejects_priced_paper_admission(path):
    assert _tick(path, _feed(token=None)) == [("REJECTED", ["CONTRACT_IDENTITY_MISSING"])] * 2
    assert rows(path, "SELECT * FROM crew_paper_trades") == []


def test_an_explicit_null_instrument_token_is_rejected_before_paper_admission(path):
    base = _feed(token=None)
    def malformed(now):
        view = base(now)
        view.chain["contracts"][0]["ce"]["growwContractId"] = None
        return view
    assert _tick(path, malformed) == []
    assert [json.loads(a["reasons"]) for a in rows(path, "SELECT reasons FROM crew_admissions")] == [[adm.QUOTE_MISSING]]
    assert rows(path, "SELECT * FROM crew_paper_trades") == []
    assert rows(path, "SELECT * FROM crew_paper_events") == []


def test_a_stale_quote_is_rejected_at_admission_and_never_reaches_the_ledger(path):
    assert _tick(path, _feed(quote_age=11)) == []
    assert [json.loads(a["reasons"]) for a in rows(path, "SELECT reasons FROM crew_admissions")] == [[adm.QUOTE_STALE]]
    assert rows(path, "SELECT * FROM crew_paper_trades") == []


@pytest.mark.parametrize("feed_kw,trusted", [
    ({"delta": False}, frozenset({"synthetic"})),  # no delta at all
    ({"delta_age": NO_STAMP}, frozenset({"synthetic"})),  # a delta without its own observation time
    ({"delta_age": 11}, frozenset({"synthetic"})),  # a stale delta
    ({"source": "groww"}, frozenset()),  # a broker chain delta under main()'s empty trust set
])
def test_unverifiable_delta_fails_closed_without_a_paper_fill(path, feed_kw, trusted):
    assert _tick(path, _feed(**feed_kw), trusted=trusted) == [("REJECTED", ["STOP_RISK_UNVERIFIABLE"])] * 2
    assert rows(path, "SELECT * FROM crew_paper_trades") == []
    assert rows(path, "SELECT * FROM crew_paper_events") == []


def test_a_verified_contract_opens_exactly_one_lot_of_the_reference_size(path):
    assert reg.RISK_POLICY["risk_policy_id"] == "paper-risk-v2"
    assert _tick(path, _feed()) == [("OPENED", [])] * 2
    assert [t["lots_initial"] for t in rows(path, "SELECT * FROM crew_paper_trades")] == [1, 1]
    risk = [json.loads(r["evidence"]) for r in rows(path, "SELECT evidence FROM crew_paper_risk_decisions")]
    assert all(r["limits"]["strategy_max_lots"] == 1 and r["sizing"]["lots"] == 1 for r in risk)
    assert {e["qty"] for e in rows(path, "SELECT qty FROM crew_paper_events")} == {65}


def test_signal_only_refuses_live_flags_armed_telegram_and_repo_journals(tmp_path):
    from app import telegram_transport as tt

    assert rr.ARM_ENV == tt.ARM_ENV
    base = _settings(telegram_delivery_enabled=True)
    journal = tmp_path / "observe.db"
    assert rr.signal_only_settings(base, journal, environ={}).telegram_delivery_enabled is False
    for settings, env, where in [(_settings(live_trading_enabled=True), {}, journal),
                                 (_settings(sandbox_allow_live=True), {}, journal),
                                 (base, {rr.ARM_ENV: "1"}, journal),
                                 (base, {}, rr._REPO / "observe.db")]:
        with pytest.raises(rr.StartupRefused):
            rr.signal_only_settings(settings, where, environ=env)
    assert rr.main(["--signal-only", "--journal", str(rr._REPO / "observe.db")]) == 2
    assert not (rr._REPO / "observe.db").exists()


def test_signal_only_session_records_vetoes_and_local_messages_without_delivery(path, monkeypatch):
    from app import telegram_transport as tt

    transports = []
    monkeypatch.setattr(tt, "TelegramTransport", lambda *a, **k: transports.append(a) or pytest.fail("transport"))
    settings = rr.signal_only_settings(_settings(telegram_delivery_enabled=True, signals_telegram_chat_id="-100123",
                                                 alert_telegram_bot_token="t"), path, environ={})
    assert _tick(path, _feed(source="groww"), trusted=frozenset(), settings=settings) == \
        [("REJECTED", ["STOP_RISK_UNVERIFIABLE"])] * 2
    risk = [json.loads(r["evidence"]) for r in rows(path, "SELECT evidence FROM crew_paper_risk_decisions")]
    assert all(r["quote"]["source"] == "chain" and r["delta"]["source"] == "groww"
               and r["trusted_delta_sources"] == [] for r in risk)
    messages = rows(path, "SELECT * FROM crew_outbox")
    assert messages and all(m["chat_id"].startswith(tt.MOCK_CHAT_PREFIX) for m in messages)
    assert transports == []
    assert rows(path, "SELECT * FROM crew_paper_trades") == []


@pytest.mark.parametrize("day,trading", [("2026-11-10", False), ("2026-11-07", False), ("2026-11-08", False),
                                         (DAY, True)])
def test_daily_summary_only_after_a_trading_session(tmp_path, day, trading):
    path = tmp_path / f"{day}.db"
    rt = rr.ResearchRuntime(path, _feed(), settings=_settings(), clock=Clock("22:14", day=day))
    rt.start()
    rt.stop()
    purposes = [m["purpose"] for m in rows(path, "SELECT purpose FROM crew_outbox")]
    accounts = len(rows(path, "SELECT * FROM crew_paper_accounts"))
    assert accounts and sum("DAILY_STRATEGY_SUMMARY" in p for p in purposes) == (accounts if trading else 0)


def test_session_records_code_revision_strategy_and_data_sources(path):
    feed = rr.scanner_feed()
    rt = rr.ResearchRuntime(path, feed, settings=_settings(), clock=Clock("10:19"))
    rt.start()
    rt.stop()
    (session,) = rows(path, "SELECT code_rev FROM crew_sessions")
    assert session["code_rev"] == rr.code_rev() and len(session["code_rev"].split("+")[0]) == 40
    (event,) = rows(path, "SELECT detail FROM crew_session_events WHERE event_type='ACQUIRED'")
    detail = json.loads(event["detail"])
    assert detail["strategy_version"] == rr.STRATEGY_VERSION and detail["registry_hash"] == reg.REGISTRY_HASH
    assert detail["data_sources"]["option_chain"].startswith("groww_web") and detail["data_sources"]["delta"] is None


def test_journal_and_observation_store_agree_on_a_frontend_only_change(tmp_path):
    from app.market import option_quote_v1 as oq

    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                       check=True, capture_output=True)
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "a.tsx").write_text("1")
    git("init", "-q")
    git("add", ".")
    git("commit", "-qm", "c")
    assert rr.code_rev(tmp_path) == oq.code_revision(tmp_path) and "+" not in rr.code_rev(tmp_path)
    (tmp_path / "frontend" / "a.tsx").write_text("2")
    assert rr.code_rev(tmp_path) == oq.code_revision(tmp_path) and rr.code_rev(tmp_path).endswith("+dirty")


def test_released_synthetic_lot_table_is_flat_and_rejects_other_sizes():
    """Transition scenarios above must not conceal the shipped synthetic contract."""
    for day in ("2021-01-04", "2022-06-01", "2024-06-03", "2025-06-02", "2026-11-10"):
        assert pl.lot_verified("NIFTY", day, 65)
        assert not pl.lot_verified("NIFTY", day, 75)
        assert not pl.lot_verified("NIFTY", day, 25)
