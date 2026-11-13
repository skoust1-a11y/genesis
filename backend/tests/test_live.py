from __future__ import annotations

from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from app.brokers.base import BrokerAdapter, ChargeReport, FillReport, OrderAck, OrderRequest, OrderUpdate
from app.config import get_settings
from app.db.models import TradeReview
from app.live.store import LiveStore
from app.main import app


def test_research_trade_reviews_only_accept_sim_trades(session, live):
    session.add(TradeReview(trade_kind="sim", reviewer_type="human", reviewer="x", mistake_tags=[]))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()
    session.add(TradeReview(trade_kind="live", sim_trade_id=1, reviewer_type="human", reviewer="x", mistake_tags=[]))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_live_tables_exist_only_in_the_live_database(engine, live_engine):
    research = set(inspect(engine).get_table_names())
    live_tables = set(inspect(live_engine).get_table_names())
    assert {"live_trades", "deployments", "orders"} <= live_tables
    assert not research & {"live_trades", "deployments", "orders", "deployed_strategies"}
    assert "strategies" not in live_tables


def test_split_migration_aborts_when_research_live_tables_have_rows(tmp_path):
    from alembic import command
    from sqlalchemy import text

    from app.db.session import engine_for
    from tests.conftest import alembic_config

    url = f"sqlite:///{tmp_path / 'old.db'}"
    cfg = alembic_config(url)
    command.upgrade(cfg, "0003_journal")
    engine = engine_for(url)
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO broker_accounts (id, broker, account_ref_masked, mode, created_at) "
                 "VALUES ('acct-1', 'zerodha', 'XX1234', 'paper', '2026-01-05 09:00:00')")
        )  # fmt: skip
    with pytest.raises(RuntimeError, match="broker_accounts"):
        command.upgrade(cfg, "head")
    assert "broker_accounts" in inspect(engine).get_table_names()
    engine.dispose()


def test_client_order_id_is_unique(live_session, live):
    with pytest.raises(IntegrityError):
        LiveStore(live_session).place_order(
            trade_intent_id=live["open_intent"].id, client_order_id="cli-1", exchange="NFO",
            tradingsymbol="X", side="BUY", quantity=75, order_type="MARKET", product="NRML",
        )  # fmt: skip
    live_session.rollback()


def test_order_status_comes_from_latest_event(live_session, live):
    store = LiveStore(live_session)
    assert store.order_status(live["order"].id) == "COMPLETE"
    detail = store.order_detail(live["order"].id)
    assert [e["status"] for e in detail["events"]] == ["OPEN", "COMPLETE"]
    assert len(detail["fills"]) == 1
    live_session.rollback()


def test_rejected_order_keeps_intent_and_reason(client, live):
    detail = client.get(f"/api/live/orders/{live['rejected_order'].id}").json()
    assert detail["status"] == "REJECTED"
    assert detail["intent"]["reason_text"] == "IV rank 51 >= 40 with 10 DTE"
    intents = client.get(f"/api/live/deployments/{live['deployment'].id}/intents").json()["intents"]
    assert {i["id"] for i in intents} == {live["open_intent"].id, live["rejected_intent"].id}


def test_deployment_must_match_its_snapshot(live_session, live):
    store = LiveStore(live_session)
    with pytest.raises(ValueError, match="strategy_id"):
        store.create_deployment(
            deployed_strategy_id=live["snapshot"].id,
            strategy_id="not-the-strategy",
            broker_account_id=live["account"].id,
            mode="paper",
        )
    with pytest.raises(ValueError, match="deployed_strategy_id"):
        store.create_deployment(deployed_strategy_id="nope", broker_account_id=live["account"].id, mode="paper")
    live_session.rollback()


def test_intents_and_trades_point_at_the_snapshot(live):
    snapshot = live["snapshot"]
    for row in (live["deployment"], live["open_intent"], live["live_trade"]):
        assert row.deployed_strategy_id == snapshot.id
        assert row.strategy_version_id == snapshot.strategy_version_id


def test_live_endpoints_and_review(client, live):
    deployments = client.get("/api/live/deployments").json()["deployments"]
    assert deployments[0]["sebi_algo_id"] == "ALGO-TEST-1"
    assert deployments[0]["status"] == "started"
    assert [e["event"] for e in deployments[0]["events"]] == ["started"]
    trades = client.get("/api/live/live-trades").json()["live_trades"]
    assert trades[0]["exit_reason"] == "target_hit"
    r = client.post(f"/api/trades/live/{live['live_trade'].id}/reviews", json={"lesson": "held to target"})
    assert r.status_code == 200 and r.json()["trade_kind"] == "live"
    reviews = client.get(f"/api/trades/live/{live['live_trade'].id}/reviews").json()["reviews"]
    assert [x["lesson"] for x in reviews] == ["seeded review", "held to target"]

    live_actions = [e["action"] for e in client.get("/api/live/audit", params={"limit": 1000}).json()["events"]]
    for expected in (
        "deployed_strategy.created", "broker_account.created", "deployment.created", "deployment.started",
        "trade_intent.created", "order.placed", "order.event", "fill.recorded", "live_trade.closed",
        "broker_charge.recorded", "position.snapshot", "trade.reviewed",
    ):  # fmt: skip
        assert expected in live_actions
    research_actions = {e["action"] for e in client.get("/api/audit", params={"limit": 1000}).json()["events"]}
    assert not research_actions & set(live_actions) - {"trade.reviewed"}
    assert client.get("/api/live/audit/verify").json()["ok"] is True
    assert client.get("/api/audit/verify").json()["ok"] is True


def test_live_endpoints_report_unmigrated_database(db_url, tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_DATABASE_URL", f"sqlite:///{tmp_path / 'empty_live.db'}")
    get_settings.cache_clear()
    client = TestClient(app)
    r = client.get("/api/live/deployments")
    assert r.status_code == 503 and "alembic -n live" in r.json()["detail"]
    assert client.get("/api/journal", params={"kind": "live"}).json()["trades"] == []


class FakeBroker:
    name = "fake"
    mode = "paper"

    def place_order(self, order: OrderRequest) -> OrderAck:
        return OrderAck(order.client_order_id, "b-1", True)

    def cancel_order(self, broker_order_id: str) -> OrderAck:
        return OrderAck("", broker_order_id, True)

    def order_updates(self, since: datetime | None = None) -> list[OrderUpdate]:
        return []

    def fills(self, since: datetime | None = None) -> list[FillReport]:
        return []

    def positions(self) -> list[dict]:
        return []

    def charges(self, trade_date: date) -> list[ChargeReport]:
        return []


def test_broker_adapter_protocol():
    assert isinstance(FakeBroker(), BrokerAdapter)
