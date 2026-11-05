"""One option contract quote. A chain row is not an order, and last price is not a bid or an ask."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from app.config import Settings
from app.engine.costs import CostBreakdown, CostProfile, leg_costs

# Same window as GrowwQuotes.max_age. The broker module is not imported: this file places no quotes.
MAX_QUOTE_AGE_SECONDS = 10
IST = timezone(timedelta(hours=5, minutes=30))
_MISSING = object()


@dataclass(frozen=True)
class ContractQuote:
    underlying: str
    expiry: str
    strike: float
    option_type: str
    provider_instrument_id: str | None
    tradingsymbol: str | None
    lot_size: int
    ltp: Decimal | None
    bid: Decimal | None
    ask: Decimal | None
    quote_time: datetime | None
    fetched_at: datetime
    source: str
    delta: float | None


@dataclass(frozen=True)
class QuoteDecision:
    status: str
    reason: str | None
    buy: Decimal | None
    sell: Decimal | None
    age_seconds: float | None


@dataclass(frozen=True)
class PremiumRisk:
    lots: int
    lot_size: int
    quantity: int
    exposure: Decimal
    entry_charges: Decimal
    max_loss: Decimal
    charge_breakdown: CostBreakdown


def parse_chain_leg(row: dict, side: str, expiry: str | None, fetched_at: datetime) -> ContractQuote | None:
    """The chain expiry, strike, and lot. None when the row is not that contract."""
    chain_expiry = _iso_date(expiry)
    fetched = _aware(fetched_at)
    if chain_expiry is None or fetched is None or not isinstance(row, dict):
        return None
    if row.get("expiry") not in (None, ""):
        if _iso_date(row.get("expiry")) != chain_expiry:
            return None
    if fetched.astimezone(IST).date() > date.fromisoformat(chain_expiry):
        return None
    option_type = {"CE": "CE", "CALL": "CE", "PE": "PE", "PUT": "PE"}.get(str(side).upper())
    if option_type is None:
        return None
    leg = row.get(option_type.lower())
    if not isinstance(leg, dict):
        return None
    strike = _strike(row)
    lot_size = _lot_size(leg, row)
    if strike is None or lot_size is None:
        return None
    live = leg.get("liveData") if isinstance(leg.get("liveData"), dict) else {}
    quote_time = _quote_time(live)
    if quote_time is _MISSING:
        return None
    ltp = _money(live.get("ltp"))
    return ContractQuote(
        underlying=_underlying(row),
        expiry=chain_expiry,
        strike=strike,
        option_type=option_type,
        provider_instrument_id=_text(leg.get("growwContractId")) or _text(leg.get("contract_id")),
        tradingsymbol=_text(leg.get("tradingSymbol")) or _text(leg.get("tradingsymbol")) or _text(leg.get("exchangeTradingSymbol")),
        lot_size=lot_size,
        ltp=ltp if ltp is not None and ltp > 0 else None,
        bid=_money(live.get("bid")),
        ask=_money(live.get("ask")),
        quote_time=quote_time,
        fetched_at=fetched,
        source="chain",
        delta=_delta(leg),
    )


def quote_decision(quote: ContractQuote, now: datetime, settings: Settings) -> QuoteDecision:
    """Buy is the ask and sell is the bid, and only when the book is fresh. Last price is never either."""
    clock = _aware(now)
    fetched = _aware(quote.fetched_at)
    if clock is None or fetched is None:
        return _reject("timestamp is not timezone-aware")
    if quote.quote_time is not None and _aware(quote.quote_time) is None:
        return _reject("timestamp is not timezone-aware")
    if fetched > clock or (quote.quote_time is not None and quote.quote_time > clock):
        return _reject("timestamp is in the future", _age(clock, fetched))
    fetch_age = _age(clock, fetched)
    if fetch_age is None or fetch_age > MAX_QUOTE_AGE_SECONDS:
        return _reject("quote is stale", fetch_age)
    if quote.quote_time is not None and _age(clock, quote.quote_time) > MAX_QUOTE_AGE_SECONDS:
        return _reject("quote is stale", _age(clock, quote.quote_time))

    bid, ask = quote.bid, quote.ask
    if bid is not None and ask is not None:
        if bid <= 0 or ask <= 0:
            return _reject("bid or ask is not positive", fetch_age)
        if ask < bid:
            return _reject("spread is inverted", fetch_age)
        mid = (ask + bid) / 2
        spread_pct = (ask - bid) / mid * Decimal(100)
        if spread_pct > Decimal(str(settings.max_spread_pct)):
            return _reject("spread is wide", fetch_age)
        if quote.quote_time is None:
            if quote.ltp is not None and quote.ltp > 0:
                return QuoteDecision("reference", "last price is not an executable quote", None, None, fetch_age)
            return _reject("quote time is missing", fetch_age)
        if not quote.tradingsymbol:
            return _reject("exchange trading symbol is missing", fetch_age)
        return QuoteDecision("executable", None, ask, bid, _age(clock, quote.quote_time))

    if quote.ltp is not None and quote.ltp > 0:
        return QuoteDecision("reference", "last price is not an executable quote", None, None, fetch_age)
    return _reject("bid or ask is missing", fetch_age)


def long_premium_risk(
    quote: ContractQuote,
    decision: QuoteDecision,
    profile: CostProfile,
    trade_date: str,
    *,
    lots_ceiling: int,
    risk_ceiling: Decimal,
    funds: Decimal | None = None,
) -> PremiumRisk:
    """Full premium plus buy-side charges. The caller passes 3 lots, or 4 for a super trend."""
    lot_size = quote.lot_size
    if isinstance(lot_size, bool) or not isinstance(lot_size, int) or lot_size < 1:
        lot_size = 0
    blank = PremiumRisk(0, lot_size, 0, Decimal(0), Decimal(0), Decimal(0), CostBreakdown.zero())
    buy = decision.buy
    if (
        decision.status != "executable"
        or lot_size < 1
        or not isinstance(buy, Decimal)
        or buy <= 0
        or not isinstance(quote.ask, Decimal)
        or quote.ask <= 0
        or isinstance(lots_ceiling, bool)
        or not isinstance(lots_ceiling, int)
        or lots_ceiling < 1
        or not isinstance(risk_ceiling, Decimal)
        or not risk_ceiling.is_finite()
        or risk_ceiling <= 0
        or not isinstance(funds, Decimal)
        or not funds.is_finite()
        or funds <= 0
    ):
        return blank
    priced = replace(profile, slippage_bps=0, slippage_min_ticks=0)
    for lots in range(lots_ceiling, 0, -1):
        quantity = lots * lot_size
        exposure = buy * quantity
        try:
            breakdown = leg_costs([("BUY", float(buy), quantity)], priced, trade_date)
        except Exception:  # noqa: BLE001  ponytail: unknown charges are an unknown loss; size nothing until costs raise a typed error
            return blank
        charges = Decimal(str(breakdown.total))
        if not charges.is_finite() or charges < 0:
            return blank
        max_loss = exposure + charges
        if max_loss <= risk_ceiling and max_loss <= funds:
            return PremiumRisk(lots, lot_size, quantity, exposure, charges, max_loss, breakdown)
    return blank


def _reject(reason: str, age: float | None = None) -> QuoteDecision:
    return QuoteDecision("rejected", reason, None, None, age)


def _iso_date(value: object) -> str | None:
    if not isinstance(value, str) or len(value.strip()) != 10:
        return None
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        return None


def _aware(value: object) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value


def _age(now: datetime, stamp: datetime) -> float | None:
    if _aware(stamp) is None:
        return None
    return (now - stamp).total_seconds()


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _money(value: object) -> Decimal | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _strike(row: dict) -> float | None:
    raw = row.get("strike")
    if raw in (None, ""):
        raw = row.get("strikePrice")
    try:
        strike = float(raw)
    except (TypeError, ValueError):
        return None
    if strike > 100_000:
        strike /= 100.0
    return strike if strike > 0 else None


def _whole(value: object) -> int | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.lstrip("-").isdigit():
            return int(text)
        return None
    if isinstance(value, Decimal) and value == value.to_integral_value():
        return int(value)
    return None


def _lot_size(leg: dict, row: dict) -> int | None:
    # The chain's marketLot is the contract. A page lot only confirms it. Never a stand-in, and never 65.
    if "marketLot" not in leg:
        return None
    market = _whole(leg.get("marketLot"))
    if market is None or market < 1:
        return None
    for key in ("lotSize", "lot_size"):
        if key not in row:
            continue
        page = _whole(row.get(key))
        if page is None or page < 1 or page != market:
            return None
    return market


def _underlying(row: dict) -> str:
    name = row.get("underlying")
    if isinstance(name, str) and name.strip():
        return name.strip().upper()
    return "NIFTY"  # ponytail: chain rows omit the index; the crew is NIFTY until a caller passes it


def _quote_time(live: dict):
    key = "quote_time" if "quote_time" in live else "quoteTime" if "quoteTime" in live else None
    if key is None:
        return None
    raw = live.get(key)
    if raw in (None, ""):
        return None
    if isinstance(raw, datetime):
        return raw if _aware(raw) is not None else _MISSING
    if not isinstance(raw, str):
        return _MISSING
    text = raw.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return _MISSING
    return parsed if _aware(parsed) is not None else _MISSING


def _delta(leg: dict) -> float | None:
    raw = (leg.get("greeks") or {}).get("delta") if isinstance(leg.get("greeks"), dict) else None
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None
