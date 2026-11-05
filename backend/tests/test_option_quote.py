"""Synthetic chain rows for the option quote contract. No broker, scanner, or database."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.config import get_settings
from app.engine.costs import CostProfile, leg_costs
from app.trading.crew.alert_agent import AlertAgent
from app.trading.option_quote import (
    long_premium_risk,
    parse_chain_leg,
    quote_decision,
)
from app.trading.post_market_crew.briefing_agent import BriefingAgent

IST = timezone(timedelta(hours=5, minutes=30))
WHEN = datetime(2026, 11, 5, 10, 15, tzinfo=IST)
EXPIRY = "2026-11-10"
TRADE_DATE = "2026-11-05"


def _row(leg: dict, **extra) -> dict:
    return {"strikePrice": 2_610_000, "ce": leg, **extra}


def _leg(**live) -> dict:
    body = {
        "marketLot": 75,
        "growwContractId": "provider-1",
        "tradingSymbol": "NIFTY25N1026100CE",
        "greeks": {"delta": 0.42},
        "liveData": live,
    }
    return body


def _quote(leg: dict, expiry: str = EXPIRY, fetched_at: datetime = WHEN, **extra):
    return parse_chain_leg(_row(leg, **extra), "CE", expiry, fetched_at)


def _decision(quote, now: datetime = WHEN):
    return quote_decision(quote, now, get_settings().model_copy(update={"max_spread_pct": 5}))


def _profile() -> CostProfile:
    settings = get_settings()
    return CostProfile(
        brokerage_per_order=settings.brokerage_per_order,
        stt_pct=settings.stt_pct,
        exchange_txn_pct=settings.exchange_txn_pct,
        gst_pct=settings.gst_pct,
        sebi_per_crore=settings.sebi_per_crore,
        stamp_duty_pct=settings.stamp_duty_pct,
        slippage_bps=settings.slippage_bps,
        stt_schedule=settings.schedule(settings.stt_schedule),
        exchange_txn_schedule=settings.schedule(settings.exchange_txn_schedule),
        exercise_stt_pct=settings.exercise_stt_pct,
        slippage_min_ticks=settings.slippage_min_ticks,
    )


def _book(ask: str, lot: int = 65, quote_time: datetime = WHEN):
    bid = str(Decimal(ask) - Decimal("0.20"))
    leg = _leg(ltp="50", bid=bid, ask=ask, quote_time=quote_time.isoformat())
    leg["marketLot"] = lot
    quote = _quote(leg)
    decision = _decision(quote)
    return quote, decision


def test_missing_and_malformed_expiry():
    leg = _leg(ltp="10", bid="9.80", ask="10.20", quote_time=WHEN.isoformat())
    assert parse_chain_leg(_row(leg), "CE", None, WHEN) is None
    assert parse_chain_leg(_row(leg), "CE", "2026/11/10", WHEN) is None
    assert parse_chain_leg(_row(leg), "CE", "10-11-2026", WHEN) is None
    assert parse_chain_leg(_row(leg, expiry="2026-11-12"), "CE", "2026-11-10", WHEN) is None
    saturday = _quote(leg, expiry="2026-11-07")
    assert saturday is not None and saturday.expiry == "2026-11-07"
    assert parse_chain_leg(_row(leg), "CE", "2026-11-04", WHEN) is None


def test_strike_normalization():
    quote = _quote(_leg(ltp="10"))
    assert quote is not None and quote.strike == 26100
    already = parse_chain_leg({"strike": 26100, "ce": _leg(ltp="10")}, "CE", EXPIRY, WHEN)
    assert already is not None and already.strike == 26100


def test_missing_and_conflicting_lot_sizes():
    bare = _leg(ltp="10")
    bare.pop("marketLot")
    assert _quote(bare) is None
    page_only = _leg(ltp="10")
    page_only.pop("marketLot")
    assert _quote(page_only, lotSize=75) is None
    assert _quote(_leg(ltp="10"), lotSize=65) is None
    kept = _quote(_leg(ltp="10"), lotSize=75)
    assert kept is not None and kept.lot_size == 75


def test_missing_bid_or_ask_is_not_executable():
    quote = _quote(_leg(ltp="11.5", bid="9.80"))
    assert quote is not None and quote.ltp == Decimal("11.5") and quote.ask is None
    decision = _decision(quote)
    assert decision.status == "reference"
    assert decision.buy is None and decision.sell is None
    assert decision.buy != quote.ltp
    risk = long_premium_risk(
        quote, decision, _profile(), TRADE_DATE, lots_ceiling=3, risk_ceiling=Decimal("1000"),
    )
    assert risk.lots == 0 and risk.quantity == 0 and risk.max_loss == 0


def test_invalid_and_stale_timestamps():
    leg = _leg(ltp="10", bid="9.80", ask="10.20", quote_time="2026-11-05T10:15:00")
    assert _quote(leg) is None
    assert parse_chain_leg(_row(_leg(ltp="10")), "CE", EXPIRY, WHEN.replace(tzinfo=None)) is None
    stale_fetch = WHEN - timedelta(seconds=11)
    quote = _quote(_leg(ltp="10", bid="9.80", ask="10.20", quote_time=stale_fetch.isoformat()), fetched_at=stale_fetch)
    assert quote is not None
    assert _decision(quote).status == "rejected"
    assert _decision(quote).reason == "quote is stale"
    fresh_fetch = _quote(
        _leg(ltp="10", bid="9.80", ask="10.20", quote_time=(WHEN - timedelta(seconds=11)).isoformat()),
    )
    assert _decision(fresh_fetch).status == "rejected"


def test_future_timestamps():
    ahead = WHEN + timedelta(seconds=5)
    fetched_ahead = _quote(_leg(ltp="10", bid="9.80", ask="10.20", quote_time=WHEN.isoformat()), fetched_at=ahead)
    assert fetched_ahead is not None
    assert _decision(fetched_ahead).status == "rejected"
    assert _decision(fetched_ahead).reason == "timestamp is in the future"
    quote_ahead = _quote(_leg(ltp="10", bid="9.80", ask="10.20", quote_time=ahead.isoformat()))
    decision = _decision(quote_ahead)
    assert decision.status == "rejected" and decision.buy is None and decision.reason == "timestamp is in the future"


def test_wide_and_inverted_spreads():
    wide = _decision(_quote(_leg(ltp="10", bid="9.70", ask="10.30", quote_time=WHEN.isoformat())))
    assert wide.status == "rejected" and wide.reason == "spread is wide" and wide.buy is None
    inverted = _decision(_quote(_leg(ltp="10", bid="10.30", ask="9.70", quote_time=WHEN.isoformat())))
    assert inverted.status == "rejected" and inverted.reason == "spread is inverted" and inverted.sell is None


def test_valid_executable_quote():
    quote, decision = _book("10.20", lot=75)
    assert quote.provider_instrument_id == "provider-1"
    assert quote.tradingsymbol == "NIFTY25N1026100CE"
    assert quote.provider_instrument_id != quote.tradingsymbol
    assert quote.expiry == EXPIRY
    assert decision.status == "executable"
    assert decision.buy == Decimal("10.20") and decision.sell == Decimal("10.00")
    assert decision.buy != quote.ltp and decision.sell != quote.ltp
    unnamed = _leg(ltp="10", bid="9.80", ask="10.20", quote_time=WHEN.isoformat())
    unnamed.pop("tradingSymbol")
    blocked = _decision(_quote(unnamed))
    assert blocked.status == "rejected" and blocked.reason == "exchange trading symbol is missing"
    assert blocked.buy is None


def test_full_premium_risk():
    quote, decision = _book("100")
    assert decision.status == "executable" and decision.buy == Decimal("100")
    risk = long_premium_risk(
        quote, decision, _profile(), TRADE_DATE, lots_ceiling=1, risk_ceiling=Decimal("100000"),
    )
    priced = replace(_profile(), slippage_bps=0, slippage_min_ticks=0)
    charges = leg_costs([("BUY", float(decision.buy), risk.quantity)], priced, TRADE_DATE)
    assert risk.lots == 1 and risk.quantity == 65
    assert risk.exposure == Decimal("6500")
    assert risk.charge_breakdown == charges
    assert risk.entry_charges == Decimal(str(charges.total))
    assert risk.max_loss == risk.exposure + risk.entry_charges
    assert risk.max_loss != risk.exposure * Decimal("0.5")


def test_quantity_step_down_under_1000():
    quote, decision = _book("10")
    risk = long_premium_risk(
        quote, decision, _profile(), TRADE_DATE, lots_ceiling=3, risk_ceiling=Decimal("1000"),
    )
    assert risk.lots == 1 and risk.quantity == 65 and risk.exposure == Decimal("650")
    assert risk.max_loss <= Decimal("1000")
    too_dear, dear_decision = _book("100")
    blocked = long_premium_risk(
        too_dear, dear_decision, _profile(), TRADE_DATE, lots_ceiling=3, risk_ceiling=Decimal("1000"),
    )
    assert blocked.lots == 0 and blocked.quantity == 0 and blocked.max_loss == 0
    short_funds = long_premium_risk(
        quote, decision, _profile(), TRADE_DATE,
        lots_ceiling=3, risk_ceiling=Decimal("1000"), funds=Decimal("600"),
    )
    assert short_funds.lots == 0


def test_missing_credentials(monkeypatch, tmp_path):
    def _refuse(*_args, **_kwargs):
        raise AssertionError("telegram url was built")

    monkeypatch.setattr("app.trading.crew.alert_agent.requests.post", _refuse)
    monkeypatch.setattr("app.trading.post_market_crew.briefing_agent.requests.post", _refuse)
    alert = AlertAgent()
    briefing = BriefingAgent(reports_dir=tmp_path)
    assert alert.bot_token is None and alert.chat_id is None
    assert briefing.bot_token is None and briefing.chat_id is None
    assert alert._send("ticket") is False
    assert briefing._send_telegram("digest") is False
    partial = AlertAgent(bot_token="configured-for-test")
    assert partial.chat_id is None and partial._send("ticket") is False
