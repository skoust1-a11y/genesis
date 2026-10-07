from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class LiveBase(DeclarativeBase):
    """Tables in the separate live-trading database (LIVE_DATABASE_URL)."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class AppendOnly:
    """Rows can be inserted but never updated or deleted."""

    __append_only__ = True


class NoDelete:
    """Rows can be updated (with an audit event) but never deleted."""

    __no_delete__ = True
