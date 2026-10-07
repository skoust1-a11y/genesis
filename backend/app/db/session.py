from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, inspect
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


def _sqlite_engine(url: str, immediate: bool) -> Engine:
    parsed = make_url(url)
    if parsed.database and parsed.database != ":memory:":
        Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _record) -> None:
        # Let SQLAlchemy issue BEGIN itself so reads and writes share one transaction.
        dbapi_conn.isolation_level = None
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

    @event.listens_for(engine, "begin")
    def _on_begin(conn) -> None:
        # IMMEDIATE serializes writers so the audit hash chain cannot fork.
        conn.exec_driver_sql("BEGIN IMMEDIATE" if immediate else "BEGIN")

    return engine


@lru_cache
def engine_for(url: str, immediate: bool = True) -> Engine:
    """`immediate=False` suits read-mostly databases without a hash chain (market data)."""
    if make_url(url).get_backend_name() == "sqlite":
        return _sqlite_engine(url, immediate)
    return create_engine(url, pool_pre_ping=True)


def get_engine() -> Engine:
    return engine_for(get_settings().database_url)


@lru_cache
def _sessionmaker_for(url: str) -> sessionmaker[Session]:
    return sessionmaker(bind=engine_for(url), expire_on_commit=False)


def get_sessionmaker() -> sessionmaker[Session]:
    return _sessionmaker_for(get_settings().database_url)


def get_db() -> Iterator[Session]:
    session = get_sessionmaker()()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def _scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def session_scope():
    return _scope(get_sessionmaker())


# Live-trading database (LIVE_DATABASE_URL)


def get_live_engine() -> Engine:
    return engine_for(get_settings().live_database_url)


def get_live_sessionmaker() -> sessionmaker[Session]:
    return _sessionmaker_for(get_settings().live_database_url)


def get_live_db() -> Iterator[Session]:
    session = get_live_sessionmaker()()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def live_session_scope():
    return _scope(get_live_sessionmaker())


def live_schema_ready(session: Session) -> bool:
    return inspect(session.get_bind()).has_table("live_audit_events")
