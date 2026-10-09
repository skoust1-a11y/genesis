from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import current_actor, record_event
from app.config import get_settings
from app.db.models import PaperOrder, Strategy
from app.db.serialize import row_to_dict
from app.db.session import get_db
from app.db.types import utcnow
from app.paper import net_positions, order_charges

router = APIRouter(prefix="/api")

# ponytail: Yahoo's unofficial chart API (free, a few minutes delayed, can change without notice). It has no
# MCX data, so commodities are global futures in USD. Switch to the Groww/Kite APIs once a broker key is set.
INDICES = {
    "NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK", "FINNIFTY": "NIFTY_FIN_SERVICE.NS", "MIDCAP50": "^NSEMDCP50",
    "NIFTYIT": "^CNXIT", "SENSEX": "^BSESN", "INDIAVIX": "^INDIAVIX",
}  # fmt: skip
COMMODITIES = {
    "GOLD": "GC=F", "SILVER": "SI=F", "CRUDEOIL": "CL=F", "NATURALGAS": "NG=F", "COPPER": "HG=F", "USDINR": "INR=X",
}  # fmt: skip
NSE_SYMBOL = re.compile(r"^[A-Z0-9&-]{1,20}$")
# Longest history Yahoo serves per interval (1m: 7 days, 5m/15m: 60 days), so the chart can scroll back.
RANGE = {"1m": "5d", "5m": "1mo", "15m": "60d", "60m": "1y", "1d": "5y"}
IST = timezone(timedelta(hours=5, minutes=30))
UA = {"User-Agent": "Mozilla/5.0"}


def resolve(symbol: str) -> tuple[str, str]:
    """(segment, Yahoo ticker) for an index or commodity name, else an NSE stock symbol."""
    s = symbol.strip().upper()
    if s in INDICES:
        return "index", INDICES[s]
    if s in COMMODITIES:
        return "commodity", COMMODITIES[s]
    if NSE_SYMBOL.match(s):
        return "stock", f"{s}.NS"
    raise HTTPException(status_code=400, detail=f"unknown symbol: {symbol}")


def _chart(ticker: str, interval: str, range_: str) -> dict:
    r = httpx.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(ticker, safe='')}",
        params={"interval": interval, "range": range_}, headers=UA, timeout=15,
    )  # fmt: skip
    r.raise_for_status()
    return r.json()


def parse_yahoo_chart(payload: dict) -> list[dict]:
    """OHLCV bars with IST timestamps; minutes without a close are dropped. Indices report volume 0 or none."""
    result = (payload.get("chart") or {}).get("result") or []
    if not result:
        return []
    q = result[0]["indicators"]["quote"][0]
    volume = q.get("volume") or []
    bars = []
    for i, ts in enumerate(result[0].get("timestamp") or []):
        if q["close"][i] is None:
            continue
        bars.append(
            {"date": datetime.fromtimestamp(ts, IST).strftime("%Y-%m-%d %H:%M")}
            | {k: round(q[k][i], 2) if q[k][i] is not None else None for k in ("open", "high", "low", "close")}
            | {"volume": volume[i] if i < len(volume) else None}
        )
    return bars


def parse_quote(symbol: str, segment: str, payload: dict) -> dict | None:
    result = (payload.get("chart") or {}).get("result") or []
    if not result:
        return None
    m = result[0]["meta"]
    price, prev = m.get("regularMarketPrice"), m.get("chartPreviousClose")
    if price is None:
        return None
    change = price - prev if prev else None
    return {
        "symbol": symbol, "segment": segment, "name": m.get("shortName") or symbol, "currency": m.get("currency"),
        "price": round(price, 2), "prev_close": prev, "change": round(change, 2) if change is not None else None,
        "change_pct": round(change / prev * 100, 2) if change is not None else None,
        "time": datetime.fromtimestamp(m["regularMarketTime"], IST).strftime("%Y-%m-%d %H:%M") if m.get("regularMarketTime") else None,
    }  # fmt: skip


def fetch_quotes(symbols: list[str]) -> dict[str, dict | None]:
    """Latest quote per symbol; None where the source has nothing or is unreachable."""

    def one(symbol: str) -> tuple[str, dict | None]:
        segment, ticker = resolve(symbol)
        try:
            return symbol, parse_quote(symbol, segment, _chart(ticker, "1d", "1d"))
        except (httpx.HTTPError, ValueError, KeyError):
            return symbol, None

    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(pool.map(one, symbols))


@router.get("/market/quotes")
def market_quotes(symbols: str = Query(max_length=1000)):
    wanted = list(dict.fromkeys(s.strip().upper() for s in symbols.split(",") if s.strip()))[:40]
    for s in wanted:
        resolve(s)
    settings = get_settings()
    quotes = fetch_quotes(wanted)
    return {
        "quotes": [
            q | {"lot_size": settings.lot_size(s) if q["segment"] == "index" else None}
            for s in wanted if (q := quotes[s]) is not None
        ],
        "missing": [s for s in wanted if quotes[s] is None],
        "source": "Yahoo Finance (delayed); commodities are global futures in USD",
    }


@router.get("/market/candles")
def market_candles(symbol: str, interval: Literal["1m", "5m", "15m", "60m", "1d"] = "5m"):
    segment, ticker = resolve(symbol)
    try:
        payload = _chart(ticker, interval, RANGE[interval])
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"market data source unavailable: {exc}") from exc
    bars = parse_yahoo_chart(payload)
    if interval == "1d":
        bars = [b | {"date": b["date"][:10]} for b in bars]
        daily = bars
    else:
        try:
            daily = parse_yahoo_chart(_chart(ticker, "1d", "5d"))
        except httpx.HTTPError:
            daily = []  # levels are a bonus; the chart still loads without them
    return {
        "symbol": symbol.upper(), "segment": segment, "interval": interval, "bars": bars,
        "levels": trading_levels(daily),
    }  # fmt: skip


def trading_levels(daily: list[dict]) -> list[dict]:
    """Previous session's high/low/close and classic floor pivots for the latest session in `daily`."""
    if len(daily) < 2:
        return []
    prev = daily[-2]
    c = prev["close"]
    h, low = prev["high"] or c, prev["low"] or c
    p = (h + low + c) / 3
    levels = {
        "PDH": h, "PDL": low, "PDC": c,
        "R2": p + (h - low), "R1": 2 * p - low, "P": p, "S1": 2 * p - h, "S2": p - (h - low),
    }  # fmt: skip
    return [{"label": k, "price": round(v, 2)} for k, v in levels.items()]


class PaperOrderIn(BaseModel):
    underlying: str = Field(max_length=32)
    instrument: Literal["EQ", "FUT", "CE", "PE"]
    strike: float | None = Field(default=None, gt=0)
    expiry: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    side: Literal["BUY", "SELL"]
    lots: int = Field(ge=1, le=10_000)  # shares for EQ, lots otherwise
    price: float = Field(gt=0)
    strategy_id: str | None = None
    note: str | None = Field(default=None, max_length=2000)

    @field_validator("underlying")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()

    @model_validator(mode="after")
    def _option_needs_strike(self):
        if self.instrument in ("CE", "PE") and self.strike is None:
            raise ValueError("options need a strike")
        return self


def _lot_size(segment: str, body: PaperOrderIn) -> int:
    if segment == "commodity":
        raise HTTPException(status_code=400, detail="commodities are view-only until an MCX price feed is connected")
    if segment == "stock":
        if body.instrument != "EQ":
            raise HTTPException(status_code=400, detail="stock futures and options are not supported yet; trade shares (EQ)")
        return 1
    if body.instrument == "EQ":
        raise HTTPException(status_code=400, detail="an index cannot be bought as shares; use FUT, CE or PE")
    lot_size = get_settings().lot_size(body.underlying)
    if not lot_size:
        raise HTTPException(status_code=400, detail=f"no lot size configured for {body.underlying} (set LOT_SIZES)")
    return lot_size


@router.post("/paper/orders")
def place_paper_order(body: PaperOrderIn, db: Session = Depends(get_db)):
    segment, _ = resolve(body.underlying)
    lot_size = _lot_size(segment, body)
    if body.strategy_id and db.get(Strategy, body.strategy_id) is None:
        raise HTTPException(status_code=404, detail=f"not found: {body.strategy_id}")
    now = utcnow()
    charges = order_charges(
        get_settings(), body.instrument, body.side, body.price, body.lots * lot_size, now.date().isoformat()
    )
    fields = body.model_dump() | {"strike": body.strike if body.instrument in ("CE", "PE") else None}
    order = PaperOrder(**fields, lot_size=lot_size, charges=charges, actor=current_actor(), created_at=now)
    db.add(order)
    db.flush()
    record_event(db, "paper.order_placed", "paper_order", order.id, fields | {"lot_size": lot_size, "charges": charges})
    db.commit()
    return row_to_dict(order)


def _orders(db: Session) -> list[PaperOrder]:
    return list(db.scalars(select(PaperOrder).order_by(PaperOrder.created_at.desc())))


@router.get("/paper/orders")
def list_paper_orders(db: Session = Depends(get_db)):
    return {"orders": [row_to_dict(o) for o in _orders(db)]}


@router.get("/paper/positions")
def list_paper_positions(db: Session = Depends(get_db)):
    """Net positions; shares and futures are marked at the latest quote (index spot as the futures proxy)."""
    positions = net_positions(_orders(db))
    marked = {p["underlying"] for p in positions if p["quantity"] and p["instrument"] in ("EQ", "FUT")}
    quotes = fetch_quotes(sorted(marked)) if marked else {}
    for p in positions:
        q = quotes.get(p["underlying"]) if p["instrument"] in ("EQ", "FUT") else None
        p["mark_price"] = q["price"] if q else None
        p["mark_time"] = q["time"] if q else None
        p["unrealized_pnl"] = (
            round(p["quantity"] * (p["mark_price"] - p["avg_price"]), 2)
            if p["quantity"] and p["mark_price"] is not None else 0.0
        )  # fmt: skip
        p["net_pnl"] = round(p["realized_pnl"] + p["unrealized_pnl"] - p["charges"], 2)
    return {
        "positions": positions,
        "net_pnl": round(sum(p["net_pnl"] for p in positions), 2),
        "charges": round(sum(p["charges"] for p in positions), 2),
    }
