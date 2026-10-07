from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from app.audit import audit_context
from app.config import get_settings
from app.db.models import StrategyVersion
from app.db.session import engine_for, get_live_sessionmaker, get_sessionmaker
from app.live.store import LiveStore
from app.main import app
from app.strategies.rules import rules_sha256
from app.strategies.schema import parse_strategy_markdown

BACKEND = Path(__file__).resolve().parents[1]
VERSION_FIELDS = ("id", "strategy_id", "version", "markdown", "content_sha256")
ACTOR = {"X-Actor": "alice"}

APPROVABLE_MARKDOWN = """---
name: approvable-condor
underlying: NIFTY
max_lots: 2
rules:
  instrument: {underlying: NIFTY, structure: iron_condor, short_distance: 1000, wing_width: 200}
  entry:
  - {field: iv_rank, op: '>=', value: 40}
  - {field: dte, op: '>=', value: 7}
  - {field: dte, op: '<=', value: 21}
  exit:
  - {kind: profit_target_pct_of_credit, value: 50}
  - {kind: stop_loss_pct_of_credit, value: 100}
  risk: {max_lots: 2, max_daily_loss_pct: 2, min_risk_reward: 0.4}
---
# Thesis
Sell an iron condor on NIFTY when implied volatility is rich.

## Entry
- IV rank at least 40 with 7 to 21 days to expiry

## Exit
- Take profit at 50% of credit, stop at 100% of credit

## Risk
- At most 2 lots, stop for the day after a 2% loss

## Gaps
"""


def alembic_config(url: str, live: bool = False) -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"), ini_section="live" if live else "alembic")
    cfg.set_main_option("script_location", str(BACKEND / ("alembic_live" if live else "alembic")))
    cfg.attributes["database_url"] = url
    cfg.attributes["configure_logger"] = False
    return cfg


@pytest.fixture
def market_db_url(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'test_market.db'}"
    monkeypatch.setenv("MARKET_DATABASE_URL", url)
    get_settings.cache_clear()
    yield url
    engine_for(url, immediate=False).dispose()
    get_settings.cache_clear()


@pytest.fixture
def live_db_url(tmp_path, monkeypatch, market_db_url):
    url = f"sqlite:///{tmp_path / 'test_live.db'}"
    monkeypatch.setenv("LIVE_DATABASE_URL", url)
    get_settings.cache_clear()
    command.upgrade(alembic_config(url, live=True), "head")
    yield url
    engine_for(url).dispose()


@pytest.fixture
def db_url(tmp_path, monkeypatch, live_db_url):
    url = f"sqlite:///{tmp_path / 'test.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("OPENAI_API_KEY", "")
    get_settings.cache_clear()
    command.upgrade(alembic_config(url), "head")
    yield url
    engine_for(url).dispose()
    get_settings.cache_clear()


@pytest.fixture
def engine(db_url):
    return engine_for(db_url)


@pytest.fixture
def live_engine(db_url, live_db_url):
    return engine_for(live_db_url)


@pytest.fixture
def session(db_url):
    s = get_sessionmaker()()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def live_session(db_url):
    s = get_live_sessionmaker()()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def client(db_url):
    return TestClient(app)


@pytest.fixture
def demo(client):
    response = client.post("/api/demo/run", headers={"X-Actor": "tester"})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def approvable(client):
    """A strategy with valid rules, no Gaps and a completed report: ready to approve."""
    ingest = client.post("/api/ingest/extract", json={"source_type": "markdown", "text": APPROVABLE_MARKDOWN})
    assert ingest.status_code == 200, ingest.text
    strategy_id = ingest.json()["strategy_id"]
    review = client.post("/api/review/run", json={"strategy_id": strategy_id})
    assert review.status_code == 200, review.text
    preview = client.get("/api/live/approvals/preview", params={"strategy_id": strategy_id, "version": 1}).json()
    assert preview["blockers"] == [], preview
    return {"strategy_id": strategy_id, "preview": preview, "report_id": review.json()["report_id"]}


def create_account(client, mode: str = "paper", broker: str = "paper") -> dict:
    response = client.post(
        "/api/live/broker-accounts", json={"broker": broker, "account_ref_masked": "PAPER-1", "mode": mode}
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def paper_account(client):
    return create_account(client)


def approval_body(approvable: dict, account: dict, **overrides) -> dict:
    return {
        "strategy_id": approvable["strategy_id"],
        "version": 1,
        "mode": account["mode"],
        "broker_account_id": account["id"],
        "confirm_rules_sha256": approvable["preview"]["rules_sha256"],
        "acknowledge_verdict": True,
    } | overrides


def approve_and_start(client, approvable: dict, account: dict, **overrides) -> str:
    response = client.post("/api/live/approvals", json=approval_body(approvable, account, **overrides), headers=ACTOR)
    assert response.status_code == 200, response.text
    deployment_id = response.json()["deployment"]["id"]
    started = client.post(f"/api/live/deployments/{deployment_id}/start", json={"reason": "test"}, headers=ACTOR)
    assert started.status_code == 200, started.text
    return deployment_id


def seed_live(session, version: StrategyVersion | SimpleNamespace) -> dict:
    """Populate every live table: one closed trade, one rejected order and one review."""
    now = datetime(2026, 1, 5, 9, 20, tzinfo=UTC)
    store = LiveStore(session)
    strategy_id, strategy_version_id = version.strategy_id, version.id
    raw_rules = parse_strategy_markdown(version.markdown).frontmatter.get("rules") or {}
    with audit_context("live-seed"):
        snapshot = store.create_deployed_strategy(
            strategy_id=strategy_id,
            strategy_version_id=strategy_version_id,
            version=version.version,
            name="seeded",
            markdown=version.markdown,
            content_sha256=version.content_sha256,
            rules=raw_rules,
            rules_sha256=rules_sha256(raw_rules),
            approved_by="tester",
        )
        account = store.create_broker_account(broker="zerodha", account_ref_masked="XX1234", mode="paper")
        deployment = store.create_deployment(
            deployed_strategy_id=snapshot.id,
            broker_account_id=account.id,
            mode="paper",
            sebi_algo_id="ALGO-TEST-1",
            risk_limits={"max_lots": 2},
            approved_by="tester",
        )
        store.add_deployment_event(deployment.id, "started", "paper run")
        open_intent = store.create_trade_intent(
            deployment_id=deployment.id,
            action="open",
            legs=[{"side": "SELL", "option_type": "CE", "strike": 22500}],
            entry_rule="iv_rank >= 40 and 7 <= dte <= 21",
            entry_conditions=[{"field": "iv_rank", "op": ">=", "threshold": 40, "actual": 47, "passed": True}],
            reason_text="IV rank 47 >= 40 with 12 DTE",
            market_snapshot={"spot": 22410, "iv_rank": 47, "dte": 12, "vix": 14.2},
            thesis_ref="strategy_version:thesis",
            target_pnl=Decimal("1500"),
            stop_loss_pnl=Decimal("-3000"),
            max_loss=Decimal("3000"),
            risk_reward=0.5,
            confidence=0.7,
            risk_checks={"max_lots_ok": True},
            decided_by="agent",
            model=None,
            approval="auto",
        )
        order = store.place_order(
            trade_intent_id=open_intent.id,
            client_order_id="cli-1",
            broker_order_id="b-1",
            sebi_algo_id="ALGO-TEST-1",
            exchange="NFO",
            tradingsymbol="NIFTY26JAN22500CE",
            expiry="2026-01-29",
            strike=Decimal("22500"),
            option_type="CE",
            side="SELL",
            quantity=75,
            order_type="LIMIT",
            product="NRML",
            limit_price=Decimal("120.5"),
            placed_at=now,
        )
        store.add_order_event(order_id=order.id, status="OPEN", filled_qty=0, pending_qty=75, event_at=now)
        store.add_order_event(
            order_id=order.id, status="COMPLETE", filled_qty=75, pending_qty=0,
            avg_price=Decimal("120.5"), event_at=now + timedelta(seconds=2),
        )  # fmt: skip
        store.record_fill(
            order_id=order.id, broker_trade_id="t-1", quantity=75, price=Decimal("120.5"),
            filled_at=now + timedelta(seconds=2),
        )  # fmt: skip
        live_trade = store.close_live_trade(
            deployment_id=deployment.id,
            open_intent_id=open_intent.id,
            legs=[{"side": "SELL", "option_type": "CE", "strike": 22500}],
            opened_at=now,
            closed_at=now + timedelta(days=2),
            holding_minutes=2 * 24 * 60,
            exit_rule="Close all when profit >= 50% of credit",
            exit_reason="target_hit",
            gross=Decimal("1650"),
            est_costs=Decimal("95.5"),
            actual_costs=Decimal("97.25"),
            net=Decimal("1552.75"),
            slippage_vs_expected=Decimal("-12.5"),
            pnl_vs_target=Decimal("52.75"),
            outcome_label="win",
        )
        store.record_broker_charge(
            broker_account_id=account.id, trade_date="2026-01-05", order_id=order.id,
            source="contract_note", breakdown={"brokerage": "40"}, total=Decimal("97.25"),
        )  # fmt: skip
        store.snapshot_positions(
            broker_account_id=account.id, deployment_id=deployment.id, positions=[], mtm=Decimal("0")
        )
        store.add_capital_event(deployment_id=deployment.id, kind="deposit", amount_inr=Decimal("50000"))
        store.record_drawdown_event(
            deployment_id=deployment.id, level=1, action="warn", at_pct=3.0, drawdown_pct=3.2,
            drawdown_inr=Decimal("16000"), equity=Decimal("484000"), peak=Decimal("500000"),
        )  # fmt: skip

        rejected_intent = store.create_trade_intent(
            deployment_id=deployment.id,
            action="open",
            legs=[{"side": "SELL", "option_type": "PE", "strike": 22000}],
            reason_text="IV rank 51 >= 40 with 10 DTE",
            decided_by="agent",
        )
        rejected_order = store.place_order(
            trade_intent_id=rejected_intent.id, client_order_id="cli-2", exchange="NFO",
            tradingsymbol="NIFTY26JAN22000PE", side="SELL", quantity=75, order_type="MARKET", product="NRML",
        )  # fmt: skip
        store.add_order_event(
            order_id=rejected_order.id, status="REJECTED", message="margin shortfall", event_at=now
        )
        review = store.add_review(live_trade.id, {"lesson": "seeded review", "mistake_tags": ["seeded"]})
        opinion = store.add_trade_opinion(
            deployment_id=deployment.id, trade_intent_id=rejected_intent.id, mode="veto", status="ok",
            decision="skip", lots=0, rules_lots=1, final_lots=0, applied=True, reasons=["seeded"],
            confidence=0.6, model="test-model", prompt_sha256="0" * 64,
        )  # fmt: skip
        phantom = store.record_phantom_trade(
            deployment_id=deployment.id, trade_intent_id=rejected_intent.id, trade_opinion_id=opinion.id,
            kind="vetoed", lots=1, legs=[{"side": "SELL", "option_type": "PE", "strike": 22000}],
            opened_at=now, closed_at=now + timedelta(days=1), exit_reason="profit_target",
            gross=Decimal("500"), est_costs=Decimal("50"), net=Decimal("450"),
        )  # fmt: skip
    session.commit()
    return {
        "snapshot": snapshot,
        "review": review,
        "account": account,
        "deployment": deployment,
        "open_intent": open_intent,
        "order": order,
        "live_trade": live_trade,
        "rejected_intent": rejected_intent,
        "rejected_order": rejected_order,
        "opinion": opinion,
        "phantom": phantom,
    }


@pytest.fixture
def live(session, live_session, demo):
    from app.strategies.store import StrategyStore

    row = StrategyStore(session).latest_version(demo["strategy_id"])
    version = SimpleNamespace(**{k: getattr(row, k) for k in VERSION_FIELDS})
    session.rollback()
    return seed_live(live_session, version)
