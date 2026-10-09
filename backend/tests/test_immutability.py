from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.db.base import Base, LiveBase
from app.db.guards import AppendOnlyViolation
from app.db.models import APPEND_ONLY_TABLES, LIVE_APPEND_ONLY_TABLES, Strategy
from app.journal.store import JournalStore


@pytest.fixture
def populated(session, client, demo, live):
    sim_trade_id = client.get(f"/api/reports/{demo['report_id']}/trades", params={"limit": 1}).json()["trades"][0]["id"]
    journal = JournalStore(session)
    journal.add_review("sim", sim_trade_id, {"lesson": "fine", "mistake_tags": []})
    improvement = journal.propose(
        strategy_id=demo["strategy_id"], title="t", rationale="r", proposed_change={"section": "Risk"}
    )
    journal.decide(improvement.id, "rejected", note="no")
    session.commit()
    order = {"underlying": "NIFTY", "instrument": "FUT", "side": "BUY", "lots": 1, "price": 24000}
    assert client.post("/api/paper/orders", json=order).status_code == 200
    return demo


def _append_only_models(base=Base):
    return [m.class_ for m in base.registry.mappers if getattr(m.class_, "__append_only__", False)]


def test_every_append_only_table_is_covered():
    assert {m.__tablename__ for m in _append_only_models()} == set(APPEND_ONLY_TABLES)
    assert {m.__tablename__ for m in _append_only_models(LiveBase)} == set(LIVE_APPEND_ONLY_TABLES)


TABLES = [("research", t) for t in APPEND_ONLY_TABLES] + [("live", t) for t in LIVE_APPEND_ONLY_TABLES]
MODELS = [("research", m) for m in _append_only_models()] + [("live", m) for m in _append_only_models(LiveBase)]


@pytest.mark.parametrize(("db", "table"), TABLES, ids=lambda v: v if isinstance(v, str) else None)
def test_raw_sql_update_and_delete_are_blocked(engine, live_engine, populated, db, table):
    with (live_engine if db == "live" else engine).connect() as conn:
        assert conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() > 0
        conn.rollback()
        with pytest.raises(DBAPIError, match="append-only"):
            conn.execute(text(f"UPDATE {table} SET id = id"))
        conn.rollback()
        with pytest.raises(DBAPIError, match="append-only"):
            conn.execute(text(f"DELETE FROM {table}"))
        conn.rollback()


@pytest.mark.parametrize(
    ("db", "model"), MODELS, ids=lambda v: v if isinstance(v, str) else v.__tablename__
)
def test_orm_update_and_delete_are_blocked(session, live_session, populated, db, model):
    session = live_session if db == "live" else session
    row = session.scalars(select(model).limit(1)).one()
    pk_name = model.__mapper__.primary_key[0].key
    column = next(c.key for c in model.__mapper__.column_attrs if c.key != pk_name)
    current = getattr(row, column)
    setattr(row, column, "changed" if not isinstance(current, (int, float)) or isinstance(current, bool) else current + 1)
    with pytest.raises(AppendOnlyViolation):
        session.flush()
    session.rollback()

    row = session.scalars(select(model).limit(1)).one()
    session.delete(row)
    with pytest.raises(AppendOnlyViolation):
        session.flush()
    session.rollback()


def test_strategies_can_be_updated_but_not_deleted(engine, session, populated):
    with engine.connect() as conn:
        conn.execute(text("UPDATE strategies SET name = name"))
        conn.rollback()
        with pytest.raises(DBAPIError, match="cannot be deleted"):
            conn.execute(text("DELETE FROM strategies"))
        conn.rollback()
    strategy = session.scalars(select(Strategy)).first()
    session.delete(strategy)
    with pytest.raises(AppendOnlyViolation):
        session.flush()
    session.rollback()
