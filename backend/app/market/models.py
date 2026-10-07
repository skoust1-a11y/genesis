"""Tables in the market-data database (MARKET_DATABASE_URL).

Imported reference data: NSE end-of-day prices, the event calendar and news headlines. It can be
rebuilt from source files at any time, so it is not append-only or audited; reports record a
fingerprint of what was imported instead.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Float, Integer, MetaData, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.db.base import NAMING_CONVENTION
from app.db.types import UTCDateTime, utcnow


class MarketBase(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class IndexBar(MarketBase):
    __tablename__ = "index_bars"

    underlying: Mapped[str] = mapped_column(String(32), primary_key=True)
    date: Mapped[str] = mapped_column(String(10), primary_key=True)
    close: Mapped[float | None] = mapped_column(Float, nullable=True)
    vix: Mapped[float | None] = mapped_column(Float, nullable=True)
    iv_rank: Mapped[float | None] = mapped_column(Float, nullable=True)


class OptionEod(MarketBase):
    __tablename__ = "option_eod"

    underlying: Mapped[str] = mapped_column(String(32), primary_key=True)
    date: Mapped[str] = mapped_column(String(10), primary_key=True)
    expiry: Mapped[str] = mapped_column(String(10), primary_key=True)
    strike: Mapped[float] = mapped_column(Float, primary_key=True)
    option_type: Mapped[str] = mapped_column(String(2), primary_key=True)
    close: Mapped[float | None] = mapped_column(Float, nullable=True)
    settle: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[int] = mapped_column(Integer, default=0)
    open_interest: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lot_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    underlying_price: Mapped[float | None] = mapped_column(Float, nullable=True)


class ImportFile(MarketBase):
    __tablename__ = "import_files"

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    kind: Mapped[str] = mapped_column(String(32))
    rows: Mapped[int] = mapped_column(Integer)
    first_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    last_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    imported_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class MarketEvent(MarketBase):
    __tablename__ = "market_events"
    __table_args__ = (UniqueConstraint("date", "kind", "title"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[str] = mapped_column(String(10), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(300))
    underlying: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source: Mapped[str] = mapped_column(String(64))


class NewsItem(MarketBase):
    __tablename__ = "news_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(String(1000), unique=True)
    published_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
