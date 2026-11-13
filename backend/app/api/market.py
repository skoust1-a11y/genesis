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

from collections import defaultdict

from app.audit import current_actor, record_event
from app.brokers.groww import GrowwQuotes
from app.config import get_settings
from app.db.models import PaperOrder, Strategy
from app.db.serialize import row_to_dict
from app.db.session import get_db
from app.db.types import utcnow
from app.market.chart_analytics import calculate_robust_vwap, enrich_chart_payload
from app.market.feed import BrokerFeed
from app.paper import net_positions, order_charges
from app.trading.option_pnl import black76, plan_pnl

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
# Longest history Yahoo serves per interval (1m: 7 days, 5m/15m: 60 days, 60m: 730 days), so the chart can scroll back.
RANGE = {"1m": "7d", "5m": "60d", "15m": "60d", "60m": "2y", "1d": "30y"}  # "max" downgrades 1d to monthly bars
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
def market_candles(
    symbol: str,
    interval: Literal["1m", "5m", "15m", "60m", "1d"] = "5m",
    sensitivity: int = Query(default=2, ge=1, le=10),
):
    segment, ticker = resolve(symbol)
    try:
        payload = _chart(ticker, interval, RANGE[interval])
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"market data source unavailable: {exc}") from exc
    bars = parse_yahoo_chart(payload)
    if not bars:
        return {"symbol": symbol.upper(), "segment": segment, "interval": interval, "bars": [], "levels": [], "zones": []}
    session = bars[-1]["date"][:10]
    if interval == "1d":
        bars = [b | {"date": b["date"][:10]} for b in bars]
        daily = bars
    else:
        try:
            daily = parse_yahoo_chart(_chart(ticker, "1d", "3mo"))
        except httpx.HTTPError:
            daily = []  # levels are a bonus; the chart still loads without them
        # Yahoo's daily list sometimes lacks the current session, so rebuild it from the intraday bars.
        day = [b for b in bars if b["date"][:10] == session]
        if day:
            first_open = day[0].get("open")
            if first_open is None:
                first_open = day[0]["close"]
            today = {
                "date": session,
                "open": first_open,
                "high": max(b.get("high") or b["close"] for b in day),
                "low": min(b.get("low") or b["close"] for b in day),
                "close": day[-1]["close"],
                "volume": sum(b.get("volume") or 0 for b in day),
            }
            daily = [d for d in daily if d["date"][:10] < session] + [today]

    enriched = enrich_chart_payload(symbol, segment, interval, bars, daily, sensitivity=sensitivity)
    zones = trading_zones(enriched["levels"], bars[-1]["close"], interval=interval)
    return enriched | {"zones": zones}


# Option-chain providers by name: any `QuoteSource` (a broker adapter for Kite, Dhan, ... plugs in here too).
QUOTE_PROVIDERS = {"groww": GrowwQuotes}


@router.get("/market/option-pnl")
def option_pnl(
    entry: float = Query(gt=0, description="Index level at which the option is bought"),
    stop: float = Query(gt=0),
    target1: float = Query(gt=0),
    target2: float | None = Query(default=None, gt=0),
    option_type: Literal["CE", "PE"] = "CE",
    strike: float | None = Query(default=None, gt=0, description="Defaults to the at-the-money strike now"),
    underlying: str = Query(default="NIFTY", max_length=20),
    provider: str = "groww",
    lots: int = Query(default=1, ge=1, le=100),
    entry_after_days: float = Query(default=0, ge=0, le=30),
    hold_days: float = Query(default=0.15, ge=0, le=30),
):
    """Net P&L per exit for buying an option when the index reaches `entry`, priced from a provider's chain."""
    factory = QUOTE_PROVIDERS.get(provider)
    if factory is None:
        raise HTTPException(status_code=400, detail=f"unknown provider {provider!r} (available: {', '.join(QUOTE_PROVIDERS)})")
    settings = get_settings()
    feed = BrokerFeed(factory(), settings, iv_rank=lambda *_: None, prev_range=lambda *_: None)
    try:
        snapshot = feed.snapshot(underlying)
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=502, detail=f"option chain unavailable from {provider}: {exc}") from exc
    if snapshot is None:
        raise HTTPException(status_code=502, detail=f"{provider} returned no index quote or expiry for {underlying}")
    step = settings.strike_step(underlying) or 50
    exits = {"stop": stop, "target1": target1} | ({"target2": target2} if target2 else {})
    try:
        plan = plan_pnl(
            snapshot, settings, option_type, strike or round(snapshot.spot / step) * step, entry, exits,
            lots=lots, entry_after_days=entry_after_days, hold_days=hold_days,
        )  # fmt: skip
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return plan | {"provider": provider}


@router.get("/market/option-chain")
def option_chain(
    underlying: str = Query(default="NIFTY", max_length=20),
    expiry: str | None = Query(default=None, max_length=15),
):
    """Live option chain for NIFTY / BANKNIFTY / FINNIFTY with Greeks, OI, and live prices from Groww."""
    g = GrowwQuotes()
    try:
        page = g._page(underlying, expiry)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Option chain unavailable for {underlying}: {exc}") from exc

    agg = page.get("optionChain", {}).get("aggregatedDetails", {})
    contracts = page.get("optionChain", {}).get("optionContracts", [])
    company = page.get("company", {})
    spot = float(company.get("liveData", {}).get("ltp") or 0.0)
    current_expiry = agg.get("currentExpiry") or expiry
    expiries = agg.get("expiryDates", [])
    lot_size = agg.get("lotSize") or get_settings().lot_size(underlying) or 65

    step = get_settings().strike_step(underlying) or 50
    atm_strike = round(spot / step) * step if spot > 0 else 0

    strikes = []
    for r in contracts:
        strike = r["strikePrice"] / 100
        # Filter window of strikes around ATM (+/- 15 strikes)
        if atm_strike == 0 or abs(strike - atm_strike) <= (15 * step):
            ce = r.get("ce")
            pe = r.get("pe")
            ce_live = ce.get("liveData", {}) if ce else {}
            pe_live = pe.get("liveData", {}) if pe else {}
            ce_greeks = ce.get("greeks", {}) if ce else {}
            pe_greeks = pe.get("greeks", {}) if pe else {}

            strikes.append({
                "strike": strike,
                "is_atm": strike == atm_strike,
                "ce": {
                    "contract_id": ce.get("growwContractId"),
                    "ltp": ce_live.get("ltp"),
                    "close": ce_live.get("close"),
                    "change": ce_live.get("dayChange"),
                    "change_pct": ce_live.get("dayChangePerc"),
                    "oi": ce_live.get("oi"),
                    "iv": ce_greeks.get("iv"),
                    "delta": ce_greeks.get("delta"),
                    "theta": ce_greeks.get("theta"),
                    "gamma": ce_greeks.get("gamma"),
                    "vega": ce_greeks.get("vega"),
                } if ce else None,
                "pe": {
                    "contract_id": pe.get("growwContractId"),
                    "ltp": pe_live.get("ltp"),
                    "close": pe_live.get("close"),
                    "change": pe_live.get("dayChange"),
                    "change_pct": pe_live.get("dayChangePerc"),
                    "oi": pe_live.get("oi"),
                    "iv": pe_greeks.get("iv"),
                    "delta": pe_greeks.get("delta"),
                    "theta": pe_greeks.get("theta"),
                    "gamma": pe_greeks.get("gamma"),
                    "vega": pe_greeks.get("vega"),
                } if pe else None,
            })

    return {
        "underlying": underlying.upper(),
        "spot": spot,
        "lot_size": lot_size,
        "current_expiry": current_expiry,
        "expiries": expiries,
        "atm_strike": atm_strike,
        "strike_step": step,
        "strikes": strikes,
    }


@router.get("/market/option-candles")
def option_candles(
    symbol: str = Query(default="NIFTY", max_length=20),
    strike: float | None = Query(default=None, gt=0),
    option_type: Literal["CE", "PE"] = "CE",
    expiry: str | None = Query(default=None, max_length=15),
    interval: Literal["1m", "5m", "15m", "60m", "1d"] = "5m",
    sensitivity: int = Query(default=2, ge=1, le=10),
):
    """Synthetic Black-76 + Live Greeks candlestick chart for individual option strikes."""
    segment, ticker = resolve(symbol)
    if segment != "index":
        raise HTTPException(status_code=400, detail="Option charts currently supported for indices (NIFTY, BANKNIFTY, FINNIFTY)")

    try:
        payload = _chart(ticker, interval, RANGE[interval])
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Market data source unavailable: {exc}") from exc
    bars = parse_yahoo_chart(payload)

    step = 100 if symbol == "BANKNIFTY" else 50
    if strike is None:
        if bars:
            strike = float(round(bars[-1]["close"] / step) * step)
        else:
            strike = 22600.0

    if not bars:
        return {"symbol": f"{symbol} {int(strike)} {option_type}", "segment": "option", "interval": interval, "bars": [], "levels": [], "zones": []}

    g = GrowwQuotes()
    contract_info = None
    iv_pct = 14.0
    expiry_str = expiry
    lot_size = get_settings().lot_size(symbol) or 65

    try:
        page = g._page(symbol, expiry)
        agg = page.get("optionChain", {}).get("aggregatedDetails", {})
        if not expiry_str:
            expiry_str = agg.get("currentExpiry")
        lot_size = agg.get("lotSize") or lot_size

        target_strike_int = int(round(strike * 100))
        for r in page.get("optionChain", {}).get("optionContracts", []):
            if r["strikePrice"] == target_strike_int:
                c = r.get(option_type.lower())
                if c:
                    contract_info = c
                    greeks = c.get("greeks") or {}
                    if greeks.get("iv") and greeks["iv"] > 0:
                        iv_pct = float(greeks["iv"])
                break
    except Exception:
        pass

    if not expiry_str:
        last_dt = datetime.strptime(bars[-1]["date"][:10], "%Y-%m-%d").date()
        exp_date = last_dt + timedelta(days=(1 - last_dt.weekday()) % 7)
        expiry_str = exp_date.isoformat()
    else:
        exp_date = datetime.strptime(expiry_str, "%Y-%m-%d").date()

    expires = datetime.combine(exp_date, datetime.strptime("15:30", "%H:%M").time(), IST)
    iv = iv_pct / 100.0

    opt_bars = []
    by_day = defaultdict(list)
    live_ltp = contract_info.get("liveData", {}).get("ltp") if contract_info else None
    contract_oi = contract_info.get("liveData", {}).get("oi") if contract_info else 1000

    for idx, b in enumerate(bars):
        dt = datetime.strptime(b["date"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
        years = max((expires - dt).total_seconds(), 0) / 86400 / 365
        p = {k: round(black76(b[k], strike, years, iv, option_type), 2) for k in ("open", "high", "low", "close")}

        if idx == len(bars) - 1 and live_ltp is not None:
            p["close"] = round(float(live_ltp), 2)
            p["high"] = max(p["high"], p["close"])
            p["low"] = min(p["low"], p["close"])

        bar = {
            "date": b["date"],
            "open": p["open"],
            "close": p["close"],
            "high": max(p["high"], p["low"]),
            "low": min(p["high"], p["low"]),
            "volume": b.get("volume") or contract_oi,
        }
        opt_bars.append(bar)
        by_day[b["date"][:10]].append(bar)

    daily = []
    for d, dbars in sorted(by_day.items()):
        daily.append({
            "date": d,
            "open": dbars[0]["open"],
            "high": max(x["high"] for x in dbars),
            "low": min(x["low"] for x in dbars),
            "close": dbars[-1]["close"],
            "volume": sum(x["volume"] for x in dbars),
        })

    opt_symbol = f"{symbol} {int(strike)} {option_type}"
    enriched = enrich_chart_payload(opt_symbol, "index", interval, opt_bars, daily, sensitivity=sensitivity)
    zones = trading_zones(enriched["levels"], opt_bars[-1]["close"], interval=interval)

    contract_meta = {
        "underlying": symbol,
        "strike": strike,
        "option_type": option_type,
        "expiry": expiry_str,
        "ltp": live_ltp if live_ltp is not None else opt_bars[-1]["close"],
        "close": contract_info.get("liveData", {}).get("close") if contract_info else None,
        "change": contract_info.get("liveData", {}).get("dayChange") if contract_info else None,
        "change_pct": contract_info.get("liveData", {}).get("dayChangePerc") if contract_info else None,
        "oi": contract_oi,
        "iv_pct": round(iv_pct, 2),
        "delta": contract_info.get("greeks", {}).get("delta") if contract_info else None,
        "theta": contract_info.get("greeks", {}).get("theta") if contract_info else None,
        "gamma": contract_info.get("greeks", {}).get("gamma") if contract_info else None,
        "vega": contract_info.get("greeks", {}).get("vega") if contract_info else None,
        "lot_size": lot_size,
        "spot": bars[-1]["close"],
    }

    return enriched | {"zones": zones, "contract": contract_meta}


def trading_levels(daily: list[dict], session_over: bool = False) -> list[dict]:
    """Previous session's high/low/close and classic floor pivots for the latest session in `daily`, or for the
    next session once the latest one is `session_over`."""
    if len(daily) < (1 if session_over else 2):
        return []
    prev = daily[-1] if session_over else daily[-2]
    c = prev["close"]
    h, low = prev["high"] or c, prev["low"] or c
    p = (h + low + c) / 3
    levels = {
        "PDH": h, "PDL": low, "PDC": c,
        "R2": p + (h - low), "R1": 2 * p - low, "P": p, "S1": 2 * p - h, "S2": p - (h - low),
    }  # fmt: skip
    return [{"label": k, "price": round(v, 2)} for k, v in levels.items()]


def intraday_levels(bars: list[dict], or_bars: int, swing: int = 2, keep: int = 3) -> list[dict]:
    """Levels from the latest session in intraday `bars`: its high/low so far (DH/DL), the opening range over the
    first `or_bars` bars (ORH/ORL, skipped when 0), and the `keep` latest swing highs/lows price has not traded
    through since (BSL/SSL: buy-/sell-side liquidity, the stops resting above/below them; 1 is the most recent)."""
    day = [b for b in bars if b["date"][:10] == bars[-1]["date"][:10] and None not in (b["high"], b["low"])]
    if not day:
        return []
    hi, lo = [b["high"] for b in day], [b["low"] for b in day]
    levels = [("DH", max(hi)), ("DL", min(lo))]
    if or_bars:
        levels += [("ORH", max(hi[:or_bars])), ("ORL", min(lo[:or_bars]))]
    # A swing needs `swing` bars either side; beating every later bar means it is both a swing and still untouched.
    rng = range(swing, len(day) - swing)
    bsl = [hi[i] for i in rng if hi[i] >= max(hi[i - swing : i]) and hi[i] > max(hi[i + 1 :]) and hi[i] != max(hi)]
    ssl = [lo[i] for i in rng if lo[i] <= min(lo[i - swing : i]) and lo[i] < min(lo[i + 1 :]) and lo[i] != min(lo)]
    levels += [(f"BSL{n}", p) for n, p in enumerate(reversed(bsl[-keep:]), 1)]
    levels += [(f"SSL{n}", p) for n, p in enumerate(reversed(ssl[-keep:]), 1)]
    return [{"label": k, "price": round(v, 2)} for k, v in levels]


def trading_zones(
    levels: list[dict],
    last: float,
    width: float | None = None,
    interval: str = "5m",
) -> list[dict]:
    """Bands where 2+ distinct levels cluster or major benchmark levels sit;
    returns all structural zones covering the market trading range.

    Timeframe-Scaled Zone Dimensions:
    - 1m: sharp, pinpoint zones (~3-5 pts) to match 1m micro candle volatility.
    - 5m: standard execution zones (~6-10 pts).
    - 15m: setup confirmation zones (~15-20 pts).
    - 60m / 1d: broad macro institutional bands (~30-50 pts).
    """
    interval_params = {
        "1m": {"width": 0.00010, "buffer_pct": 0.000055, "min_buf": 1.15},
        "5m": {"width": 0.00022, "buffer_pct": 0.00009, "min_buf": 1.8},
        "15m": {"width": 0.00050, "buffer_pct": 0.00020, "min_buf": 4.0},
        "60m": {"width": 0.0015, "buffer_pct": 0.00050, "min_buf": 10.0},
        "1d": {"width": 0.0030, "buffer_pct": 0.0010, "min_buf": 20.0},
    }
    cfg = interval_params.get(interval, interval_params["5m"])
    eff_width = width if width is not None else cfg["width"]
    buffer = max(cfg["min_buf"], round(last * cfg["buffer_pct"], 2))

    groups: list[list[dict]] = []
    for lv in sorted(levels, key=lambda lv: lv["price"]):
        if groups and lv["price"] - groups[-1][0]["price"] <= eff_width * last:
            groups[-1].append(lv)
        else:
            groups.append([lv])
    raw_zones = []
    major_keys = (
        "DH", "DL", "DO", "PDH", "PDL", "PWH", "PWL", "PMH", "PML",
        "52WH", "52WL", "ATH", "P", "R1", "R2", "R3", "S1", "S2", "S3",
        "ORH", "ORL", "EQH", "EQL", "Res", "Sup", "BSL", "SSL",
        "5M-H", "5M-L", "15M-H", "15M-L", "5M-", "15M-", "30M-", "1H-", "Daily-",
    )
    cluster_pad = 0.15 if interval == "1m" else (0.20 if interval == "5m" else 0.25)
    for g in groups:
        low, high = g[0]["price"], g[-1]["price"]
        labels = [lv["label"] for lv in g]
        is_confluent = len(g) >= 2
        is_major = any(any(lbl.startswith(k) for k in major_keys) for lbl in labels)
        if not (is_confluent or is_major):
            continue
        if low == high:
            low = round(low - buffer, 2)
            high = round(high + buffer, 2)
        else:
            low = round(low - buffer * cluster_pad, 2)
            high = round(high + buffer * cluster_pad, 2)

        # 1m specific calibration: keep liquidity zone span strictly within 2.0 to 3.0 points range
        if interval == "1m":
            span = high - low
            if span > 3.0:
                mid = (high + low) / 2.0
                low = round(mid - 1.5, 2)
                high = round(mid + 1.5, 2)
            elif span < 2.0:
                mid = (high + low) / 2.0
                low = round(mid - 1.15, 2)
                high = round(mid + 1.15, 2)

        side = "resistance" if low >= last else "support" if high <= last else ("resistance" if (last - low) < (high - last) else "support")
        raw_zones.append({"low": low, "high": high, "labels": labels, "side": side})

    return sorted(raw_zones, key=lambda z: z["low"])


def add_vwap(bars: list[dict]) -> list[dict]:
    """Sets each intraday bar's session VWAP (reset daily); None until the source reports volume (indices never do)."""
    day, pv, vol = None, 0.0, 0
    for b in bars:
        if b["date"][:10] != day:
            day, pv, vol = b["date"][:10], 0.0, 0
        if b["volume"] and None not in (b["high"], b["low"]):
            pv += (b["high"] + b["low"] + b["close"]) / 3 * b["volume"]
            vol += b["volume"]
        b["vwap"] = round(pv / vol, 2) if vol else None
    return bars


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
