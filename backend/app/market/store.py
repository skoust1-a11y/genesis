"""Read and write the market-data database (MARKET_DATABASE_URL)."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, NamedTuple

from sqlalchemy import Engine, func, inspect, select, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.db.session import engine_for
from app.market.models import ImportFile, IndexBar, MarketBase, MarketEvent, NewsItem, OptionEod
from app.market.nse import (
    LotChange,
    iv_rank_series,
    load_lot_sizes,
    lot_size_on,
    parse_daily_bars,
    parse_daily_closes,
    parse_fo_bhavcopy,
    read_text,
)

BATCH = 2000  # rows per multi-row INSERT; keeps bound parameters under SQLite's limit


@lru_cache
def _ready_engine(url: str) -> Engine:
    engine = engine_for(url, immediate=False)
    MarketBase.metadata.create_all(engine)
    _add_missing_columns(engine)
    return engine


def _add_missing_columns(engine: Engine) -> None:
    """Nullable columns added to a model after its table was created. The market database has no
    migrations: it is rebuilt from source files, and new columns fill on the next import."""
    existing = inspect(engine)
    with engine.begin() as conn:
        for table in MarketBase.metadata.sorted_tables:
            have = {c["name"] for c in existing.get_columns(table.name)}
            for column in table.columns:
                if column.name not in have and column.nullable:
                    kind = column.type.compile(dialect=engine.dialect)
                    conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {kind}'))


def get_market_sessionmaker(settings: Settings | None = None) -> sessionmaker[Session]:
    url = (settings or get_settings()).market_database_url
    return sessionmaker(bind=_ready_engine(url), expire_on_commit=False)


def open_market_store(settings: Settings | None = None) -> MarketStore:
    return MarketStore(get_market_sessionmaker(settings)())


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class OptionQuote(NamedTuple):
    close: float | None
    settle: float | None
    volume: int
    lot_size: int | None


class MarketStore:
    def __init__(self, session: Session) -> None:
        self.session = session
        self._cache: dict[tuple, Any] = {}

    def close(self) -> None:
        self.session.close()

    # Imports

    def _already_imported(self, sha: str) -> bool:
        return self.session.get(ImportFile, sha) is not None

    def _record_file(self, path: Path, sha: str, kind: str, rows: int, dates: list[str]) -> None:
        if self._already_imported(sha):
            return
        self.session.add(
            ImportFile(
                sha256=sha, name=path.name, kind=kind, rows=rows,
                first_date=min(dates) if dates else None, last_date=max(dates) if dates else None,
            )
        )  # fmt: skip

    def _upsert(self, model, rows: list[dict], update: tuple[str, ...]) -> None:
        for i in range(0, len(rows), BATCH):
            stmt = sqlite_insert(model).values(rows[i : i + BATCH])
            if update:
                stmt = stmt.on_conflict_do_update(
                    index_elements=[c.name for c in model.__table__.primary_key.columns],
                    set_={c: getattr(stmt.excluded, c) for c in update},
                )
            else:
                stmt = stmt.on_conflict_do_nothing()
            self.session.execute(stmt)

    def _lot_changes(self) -> list[LotChange]:
        if "lot_changes" not in self._cache:
            self._cache["lot_changes"] = load_lot_sizes(Path(get_settings().data_dir) / "lot_sizes" / "nse.yaml")
        return self._cache["lot_changes"]

    def import_bhavcopy(self, path: Path, underlyings: Iterable[str] | None = None, *, force: bool = False) -> int:
        """Index option rows from one bhavcopy. Returns rows imported; 0 if the file was seen before,
        unless `force` (to add underlyings a first import left out; rows are upserted)."""
        sha = _sha256(path)
        if self._already_imported(sha) and not force:
            return 0
        rows = parse_fo_bhavcopy(read_text(path), underlyings)
        for r in rows:
            if r.lot_size is None:
                r.lot_size = lot_size_on(self._lot_changes(), r.underlying, r.date)
        option_rows = [r.__dict__ for r in rows]
        self._upsert(
            OptionEod, option_rows,
            ("close", "settle", "volume", "open_interest", "lot_size", "underlying_price"),
        )  # fmt: skip
        spots = {(r.underlying, r.date): r.underlying_price for r in rows if r.underlying_price}
        if spots:
            # The bhavcopy's underlying price fills a missing index close; an imported index file wins.
            stmt = sqlite_insert(IndexBar).values(
                [{"underlying": u, "date": d, "close": p} for (u, d), p in spots.items()]
            )
            self.session.execute(
                stmt.on_conflict_do_update(
                    index_elements=["underlying", "date"],
                    set_={"close": func.coalesce(IndexBar.close, stmt.excluded.close)},
                )
            )
        self._record_file(path, sha, "bhavcopy", len(rows), [r.date for r in rows])
        self.session.commit()
        return len(rows)

    def import_index_closes(self, path: Path, underlying: str) -> int:
        """Daily index bars. The close always replaces what is stored; open/high/low only when the file has them."""
        sha = _sha256(path)
        if self._already_imported(sha):
            return 0
        bars = parse_daily_bars(read_text(path))
        rows = [
            {"underlying": underlying.upper(), "date": b.date, "close": b.close, "open": b.open, "high": b.high,
             "low": b.low}
            for b in bars
        ]  # fmt: skip
        for i in range(0, len(rows), BATCH):
            stmt = sqlite_insert(IndexBar).values(rows[i : i + BATCH])
            keep = {c: func.coalesce(getattr(stmt.excluded, c), getattr(IndexBar, c)) for c in ("open", "high", "low")}
            self.session.execute(
                stmt.on_conflict_do_update(
                    index_elements=["underlying", "date"], set_={"close": stmt.excluded.close, **keep}
                )
            )
        self._record_file(path, sha, f"index:{underlying.upper()}", len(rows), [b.date for b in bars])
        self.session.commit()
        return len(rows)

    def import_vix(
        self, path: Path, underlying: str = "NIFTY", settings: Settings | None = None, *, force: bool = False
    ) -> int:
        """India VIX closes as the volatility series behind `underlying`'s IV rank."""
        sha = _sha256(path)
        if self._already_imported(sha) and not force:
            return 0
        closes = parse_daily_closes(read_text(path))
        rows = [{"underlying": underlying.upper(), "date": d, "vix": c} for d, c in closes]
        self._upsert(IndexBar, rows, ("vix",))
        self._record_file(path, sha, f"vix:{underlying.upper()}", len(rows), [d for d, _ in closes])
        self.recompute_iv_rank(underlying, settings)
        self.session.commit()
        return len(rows)

    def recompute_iv_rank(self, underlying: str, settings: Settings | None = None) -> None:
        settings = settings or get_settings()
        bars = list(
            self.session.scalars(
                select(IndexBar).where(IndexBar.underlying == underlying.upper()).order_by(IndexBar.date)
            )
        )
        ranks = iv_rank_series([b.vix for b in bars], settings.iv_rank_lookback_days, settings.iv_rank_min_history)
        for bar, rank in zip(bars, ranks, strict=True):
            bar.iv_rank = rank
        self.session.flush()

    # Reads used by HistoricalFeed

    def has_data(self, underlying: str) -> bool:
        return bool(self.trading_dates(underlying))

    def trading_dates(self, underlying: str) -> list[str]:
        """Dates with an index close, an IV rank and at least one option row."""
        option_dates = select(OptionEod.date).where(OptionEod.underlying == underlying.upper()).distinct()
        stmt = (
            select(IndexBar.date)
            .where(
                IndexBar.underlying == underlying.upper(),
                IndexBar.close.is_not(None),
                IndexBar.iv_rank.is_not(None),
                IndexBar.date.in_(option_dates),
            )
            .order_by(IndexBar.date)
        )
        return list(self.session.scalars(stmt))

    def index_bar(self, underlying: str, day: str) -> IndexBar | None:
        return self.session.get(IndexBar, (underlying.upper(), day))

    def previous_range(self, underlying: str, day: str) -> tuple[float, float] | None:
        """(high, low) of the last trading day before `day` that has both."""
        stmt = (
            select(IndexBar.high, IndexBar.low)
            .where(
                IndexBar.underlying == underlying.upper(), IndexBar.date < day,
                IndexBar.high.is_not(None), IndexBar.low.is_not(None),
            )  # fmt: skip
            .order_by(IndexBar.date.desc())
            .limit(1)
        )
        row = self._memo(("previous_range", underlying.upper(), day), lambda: self.session.execute(stmt).first())
        return (row[0], row[1]) if row else None

    def _memo(self, key: tuple, load):
        if key not in self._cache:
            self._cache[key] = load()
        return self._cache[key]

    def expiries(self, underlying: str, day: str) -> list[str]:
        stmt = (
            select(OptionEod.expiry)
            .where(OptionEod.underlying == underlying.upper(), OptionEod.date == day)
            .distinct()
            .order_by(OptionEod.expiry)
        )
        return self._memo(("expiries", underlying.upper(), day), lambda: list(self.session.scalars(stmt)))

    def chain(self, underlying: str, day: str, expiry: str) -> dict[tuple[float, str], OptionQuote]:
        """{(strike, option_type): OptionQuote} for one expiry on one day. Cached: the data is read-only here."""
        stmt = select(
            OptionEod.strike, OptionEod.option_type, OptionEod.close, OptionEod.settle,
            OptionEod.volume, OptionEod.lot_size,
        ).where(OptionEod.underlying == underlying.upper(), OptionEod.date == day, OptionEod.expiry == expiry)  # fmt: skip

        def load():
            return {(float(s), t): OptionQuote(c, st, v or 0, lot) for s, t, c, st, v, lot in self.session.execute(stmt)}

        return self._memo(("chain", underlying.upper(), day, expiry), load)

    def option(self, underlying: str, day: str, expiry: str, strike: float, option_type: str) -> OptionQuote | None:
        return self.chain(underlying, day, expiry).get((float(strike), option_type))

    def fingerprint(self, underlying: str) -> dict[str, Any]:
        dates = self.trading_dates(underlying)
        files = sorted(self.session.scalars(select(ImportFile.sha256)))
        option_rows = self.session.scalar(
            select(func.count()).select_from(OptionEod).where(OptionEod.underlying == underlying.upper())
        )
        return {
            "source": "nse_eod",
            "underlying": underlying.upper(),
            "first_date": dates[0] if dates else None,
            "last_date": dates[-1] if dates else None,
            "trading_days": len(dates),
            "option_rows": option_rows,
            "files": len(files),
            "files_sha256": hashlib.sha256("".join(files).encode()).hexdigest(),
        }

    # Events and news

    def add_events(self, events: Iterable[dict[str, Any]]) -> int:
        rows = [
            {
                "date": e["date"], "kind": e["kind"], "title": str(e["title"])[:300],
                "underlying": e.get("underlying"), "source": e.get("source", "manual"),
            }
            for e in events
        ]  # fmt: skip
        if not rows:
            return 0
        before = self.session.scalar(select(func.count()).select_from(MarketEvent))
        self._upsert(MarketEvent, rows, ())
        self.session.commit()
        return self.session.scalar(select(func.count()).select_from(MarketEvent)) - before

    def events_between(self, start: str, end: str, underlying: str | None = None) -> list[MarketEvent]:
        stmt = select(MarketEvent).where(MarketEvent.date >= start, MarketEvent.date <= end)
        if underlying:
            stmt = stmt.where((MarketEvent.underlying.is_(None)) | (MarketEvent.underlying == underlying.upper()))
        return list(self.session.scalars(stmt.order_by(MarketEvent.date, MarketEvent.kind)))

    def has_events(self) -> bool:
        return bool(self.session.scalar(select(func.count()).select_from(MarketEvent)))

    def add_news(self, items: Iterable[dict[str, Any]]) -> int:
        rows = [
            {
                "source": i["source"], "title": i["title"], "url": i["url"],
                "published_at": i["published_at"], "symbol": i.get("symbol"), "fetched_at": i["fetched_at"],
            }
            for i in items
        ]  # fmt: skip
        if not rows:
            return 0
        before = self.session.scalar(select(func.count()).select_from(NewsItem))
        for i in range(0, len(rows), BATCH):
            self.session.execute(sqlite_insert(NewsItem).values(rows[i : i + BATCH]).on_conflict_do_nothing())
        self.session.commit()
        return self.session.scalar(select(func.count()).select_from(NewsItem)) - before

    def news_between(
        self, start: datetime, end: datetime, limit: int, symbols: Iterable[str] | None = None
    ) -> list[NewsItem]:
        """Headlines published in (start, end], newest first: general ones plus any for `symbols`."""
        stmt = select(NewsItem).where(NewsItem.published_at > start, NewsItem.published_at <= end)
        if symbols is not None:
            stmt = stmt.where(NewsItem.symbol.is_(None) | NewsItem.symbol.in_([s.upper() for s in symbols]))
        return list(self.session.scalars(stmt.order_by(NewsItem.published_at.desc()).limit(limit)))
