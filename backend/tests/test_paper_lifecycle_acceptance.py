"""One isolated paper lifecycle for a registered scanner-v0 family that is not the liquidity sweep.

The option book, delta and clock are fixtures. They are not a market session and not a performance claim.
"""

from __future__ import annotations

import json
import socket
from datetime import timedelta
from decimal import Decimal

import pytest

from app.brokers import registry as brokers
from app.config import Settings, get_settings
from app.db import crew_journal as cj
from app.db import paper_ledger as pl
from app.live.gates import live_blockers
from app.strategies.normalizer import normalize_scanner_v0_family
from app.telegram_transport import MOCK_CHAT_PREFIX, delivery_authorized
from app.trading import paper_registry as reg
from app.trading import paper_stats as ps
from app.trading import research_runtime as rr
from app.trading.paper_risk import DeltaEvidence
from app.trading.option_quote import parse_chain_leg
from tests.test_research_runtime import DAY, Clock, bars, rows, setup, walk

STRATEGY = "Breakout & Retest (OR-30) [5M]"
FAMILY = "RETEST_OR30"
CAPITAL = Decimal("50000")
IND = f"IND:{FAMILY}"


def _book(now):
    live = {"ltp": 10.2, "bid": 10.0, "ask": 10.4, "quoteTime": now.isoformat()}
    leg = {"liveData": live, "marketLot": 65, "tradingSymbol": "NIFTY26N1024100CE",
           "growwContractId": "GC-24100CE"}
    return {"contracts": [{"underlying": "NIFTY", "strike": 24100, "ce": leg}], "expiry": "2026-11-10", "fetched_at": now}


def _view(now, px):
    start = (now.replace(second=0, microsecond=0) - timedelta(minutes=now.minute % 5 + 5)).strftime("%H:%M")
    chain = _book(now)
    quote = parse_chain_leg(chain["contracts"][0], "CE", chain["expiry"], now)
    return rr.MarketView(
        df_5m=bars([px], start=start),
        setups=[setup(strategy=STRATEGY, stop=23995, ts=f"{DAY} 10:15")],
        chain=chain,
        key_levels=[],
        delta_evidence={quote.tradingsymbol: DeltaEvidence(Decimal("0.5"), now, quote.tradingsymbol, "synthetic")},
    )


def _settings():
    return get_settings().model_copy(update={
        "paper_capital_inr": CAPITAL,
        "live_trading_enabled": False,
        "sandbox_allow_live": False,
        "telegram_delivery_enabled": True,
        "signals_telegram_chat_id": "-100123",
        "alert_telegram_bot_token": "paper-test-token",
    })


@pytest.fixture
def path(tmp_path):
    return tmp_path / "crew_research.db"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def blocked(self, addr):
        raise OSError("network blocked in paper lifecycle acceptance")

    monkeypatch.setattr(socket.socket, "connect", blocked)


def test_an_ambiguous_strategy_lot_ceiling_rejects_admission_without_a_trade(path, monkeypatch):
    monkeypatch.setattr(reg, "strategy_max_lots", lambda family: None)
    rt = rr.ResearchRuntime(path, lambda now: _view(now, 24000.0), settings=_settings(), clock=Clock("10:19"),
                            trusted_delta_sources=frozenset({"synthetic"}))
    rt.start()
    try:
        rt.tick()
    finally:
        rt.stop()
    assert rows(path, "SELECT * FROM crew_paper_trades") == []
    decisions = rows(path, "SELECT * FROM crew_paper_decisions")
    assert len(decisions) == 2
    assert all(d["outcome"] == "REJECTED" and json.loads(d["reasons"]) == ["STRATEGY_LOT_LIMIT_AMBIGUOUS"]
               for d in decisions)


def test_retest_or30_paper_lifecycle_reconciles_without_a_broker_order(path, monkeypatch):
    spec = normalize_scanner_v0_family(FAMILY)
    again = normalize_scanner_v0_family(FAMILY)
    eligible, reasons = reg.check_paper_admission_eligibility(STRATEGY, spec=spec)
    assert spec.strategy_id == FAMILY and spec.strategy_version == reg.STRATEGY_VERSION
    assert spec.paper_eligible and not spec.quarantined
    assert spec.paper_approved is False and not spec.promotable  # no signed PAPER_APPROVED gate exists for it
    assert spec.spec_sha256 == again.spec_sha256
    assert {"nse_5m_ohlcv", "nse_1d_levels", "nse_option_chain_intraday"} <= set(spec.rules_v2.data_dependencies)
    assert eligible and reasons == []
    assert Settings.model_fields["live_trading_enabled"].default is False
    assert Settings.model_fields["telegram_delivery_enabled"].default is False
    modes = _settings()
    assert brokers.get_spec("paper").modes(modes) == ("paper",)
    assert brokers.get_spec("groww").modes(modes) == ("paper",)
    assert brokers.get_spec("upstox").modes(modes) == ("paper",)
    assert "live" not in brokers.get_spec("sandbox").modes(modes)
    assert "LIVE_TRADING_ENABLED is not true" in live_blockers(None, None, "long_call", modes)
    assert delivery_authorized(f"{MOCK_CHAT_PREFIX}paper") is False

    orders = []

    def refuse_order(self, order):
        orders.append(type(self).__name__)
        raise AssertionError("broker place_order is outside this paper lifecycle")

    monkeypatch.setattr("app.brokers.paper.PaperBroker.place_order", refuse_order)
    monkeypatch.setattr("app.brokers.sandbox.SandboxBroker.place_order", refuse_order)
    monkeypatch.setattr("app.trading.execution_crew.broker_bridge_agent.BrokerBridgeAgent.place_order", refuse_order)

    from app import telegram_transport as tt

    sent = []

    class StubTransport:
        def __init__(self, token):
            assert token == "paper-test-token"

        def send(self, chat_id, text):
            assert chat_id == "-100123"
            sent.append(text)
            return tt.DeliveryOutcome(tt.DELIVERED, 200, True, len(sent))

    monkeypatch.setattr(tt, "TelegramTransport", StubTransport)
    monkeypatch.setattr(tt, "delivery_authorized", lambda chat_id: chat_id == "-100123")

    spot = {"px": 24000.0}
    clock = Clock("10:19")

    def feed(now):
        return _view(now, spot["px"])

    def start():
        runtime = rr.ResearchRuntime(path, feed, settings=modes, clock=clock,
                                     trusted_delta_sources=frozenset({"synthetic"}))
        runtime.start()
        return runtime

    rt = start()
    try:
        opened = rt.tick()
        assert opened["admissions"][0]["class"] == "RESEARCH_ONLY"
        # The 1-lot family spec caps the sizer even though 3 lots fit inside the rupee gates.
        assert [(t["lots_initial"], t["lots_open"]) for t in rows(path, "SELECT * FROM crew_paper_trades")] == [(1, 1)] * 2
        held = rt.tick()
        assert held["exits"][0]["action"] == "HOLD"
        trades = rows(path, "SELECT * FROM crew_paper_trades")
        assert len(trades) == 2
        assert {t["account_id"] for t in trades} == {IND, reg.SHARED}
        assert all(t["strategy_id"] == STRATEGY and t["family"] == FAMILY for t in trades)
        assert all(t["strategy_version"] == reg.STRATEGY_VERSION and t["registry_hash"] == reg.REGISTRY_HASH for t in trades)
        assert all(t["state"] == "OPEN" for t in trades)
        decisions = rows(path, "SELECT * FROM crew_paper_decisions")
        assert all(d["outcome"] == "OPENED" and d["strategy_id"] == STRATEGY and d["family"] == FAMILY for d in decisions)
        assert all(d["registry_hash"] == reg.REGISTRY_HASH for d in decisions)
        risk = [json.loads(r["evidence"]) for r in rows(path, "SELECT evidence FROM crew_paper_risk_decisions")]
        assert all(r["outcome"] == "OPENED" and r["registry_hash"] == reg.REGISTRY_HASH for r in risk)
        assert all(r["parameter_hash"] == reg.PARAMETER_HASH and r["delta"]["source"] == "synthetic" for r in risk)
        assert all(r["limits"]["strategy_max_lots"] == 1 and r["sizing"]["lots"] == 1 for r in risk)

        spot["px"] = 23990.0
        walk(rt, clock, "10:24")
        stopped = rt.tick()
        assert stopped["exits"][0]["action"] == "STOP_LOSS_HIT"
        closed = rows(path, "SELECT * FROM crew_paper_trades")
        assert all(t["state"] == "CLOSED" and t["strategy_id"] == STRATEGY and t["family"] == FAMILY for t in closed)
        events = rows(path, "SELECT event_type, reason FROM crew_paper_events ORDER BY event_id")
        assert [e["event_type"] for e in events] == ["ENTRY", "ENTRY", "EXIT", "EXIT"]
        assert all(e["reason"] == "STOP_LOSS_HIT" for e in events if e["event_type"] == "EXIT")
        assert pl.check_consistency(path) == []
        conn = cj.connect_readonly(path)
        try:
            for account in (IND, reg.SHARED):
                accounting = ps.account_stats(conn, account)["accounting"]
                assert accounting["charge_reconciliation"] == "EXACT"
                assert Decimal(accounting["reserved_worst_case_inr"]) == 0
                assert Decimal(accounting["committed_capital_inr"]) == 0
                assert Decimal(accounting["cash_inr"]) == CAPITAL + Decimal(accounting["realized_pnl_inr"])
        finally:
            conn.close()
        replay = rt.tick()
        assert replay["exits"] == []
        assert rows(path, "SELECT * FROM crew_paper_trades") == closed
        rt.stop()
        rt = start()
        rt.tick()
        assert rows(path, "SELECT * FROM crew_paper_trades") == closed
        assert all((t["lots_initial"], t["lots_open"]) == (1, 0) for t in closed)
        assert pl.check_consistency(path) == []
    finally:
        rt.stop()

    assert orders == []
    assert sent and all("Mode: PAPER ONLY" in text and "No real broker order placed." in text for text in sent)
    assert all(STRATEGY in text or "DAILY STRATEGY SUMMARY" in text for text in sent)
    assert any("PAPER TRADE" in text and FAMILY in text and "SYNTHETIC ENGINEERING FIXTURE" in text for text in sent)
    assert any("PAPER STOP LOSS" in text and FAMILY in text for text in sent)
    assert len(sent) == len({text for text in sent})
