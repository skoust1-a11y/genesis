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
BUDGET = Decimal("1000")
FUNDS = Decimal("50000")


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


def _size(ask: str, lot: int = 65, ceiling: int = 3, budget: Decimal = BUDGET, funds: Decimal | None = FUNDS):
    quote, decision = _book(ask, lot=lot)
    return long_premium_risk(
        quote, decision, _profile(), TRADE_DATE,
        lots_ceiling=ceiling, risk_ceiling=budget, funds=funds,
    )


def _buy_charges(ask: str, quantity: int) -> tuple[Decimal, Decimal, Decimal]:
    """Buy-side statutory charges from the code-default tariff. Sell-side STT is not included."""
    turnover = Decimal(ask) * quantity
    brokerage = Decimal("20")
    exchange = turnover * Decimal("0.0003503")
    sebi = turnover * Decimal("10") / Decimal("10000000")
    stamp = turnover * Decimal("0.00003")
    gst = (brokerage + exchange + sebi) * Decimal("0.18")
    charges = brokerage + exchange + sebi + stamp + gst
    return turnover, charges, turnover + charges


def _holds(risk, ask: Decimal, budget: Decimal, funds: Decimal) -> None:
    assert risk.lots > 0 and risk.quantity > 0
    assert risk.quantity == risk.lots * risk.lot_size
    assert risk.exposure == ask * risk.quantity
    assert risk.max_loss == risk.exposure + risk.entry_charges
    assert risk.entry_charges == Decimal(str(risk.charge_breakdown.total))
    assert risk.max_loss <= budget
    assert risk.exposure + risk.entry_charges <= funds
    assert risk.charge_breakdown.stt == 0 and risk.charge_breakdown.slippage == 0
    assert risk.charge_breakdown.stamp_duty > 0


def test_full_premium_risk():
    quote, decision = _book("100")
    assert decision.status == "executable" and decision.buy == Decimal("100")
    risk = long_premium_risk(
        quote, decision, _profile(), TRADE_DATE,
        lots_ceiling=1, risk_ceiling=Decimal("100000"), funds=FUNDS,
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
        quote, decision, _profile(), TRADE_DATE,
        lots_ceiling=3, risk_ceiling=BUDGET, funds=FUNDS,
    )
    assert risk.lots == 1 and risk.quantity == 65 and risk.exposure == Decimal("650")
    assert risk.max_loss <= BUDGET
    too_dear, dear_decision = _book("100")
    blocked = long_premium_risk(
        too_dear, dear_decision, _profile(), TRADE_DATE,
        lots_ceiling=3, risk_ceiling=BUDGET, funds=FUNDS,
    )
    assert blocked.lots == 0 and blocked.quantity == 0 and blocked.max_loss == 0
    short_funds = long_premium_risk(
        quote, decision, _profile(), TRADE_DATE,
        lots_ceiling=3, risk_ceiling=BUDGET, funds=Decimal("600"),
    )
    assert short_funds.lots == 0 and short_funds.quantity == 0


def test_one_lot_of_a_ten_rupee_ask_fits_the_budget():
    risk = _size("10")
    exposure, charges, max_loss = _buy_charges("10", 65)
    assert exposure == Decimal("650") and charges == Decimal("23.888947100")
    assert max_loss == Decimal("673.888947100")
    _holds(risk, Decimal("10"), BUDGET, FUNDS)
    assert risk.lots == 1 and risk.quantity == 65
    assert abs(risk.entry_charges - charges) < Decimal("0.000000001")
    assert _size("10", ceiling=3).lots == 1
    assert _buy_charges("10", 130)[2] > BUDGET


def test_fifteen_rupee_ask_includes_entry_charges():
    risk = _size("15", ceiling=1)
    exposure, charges, max_loss = _buy_charges("15", 65)
    assert exposure == Decimal("975")
    assert charges == Decimal("24.033420650")
    assert max_loss == Decimal("999.033420650")
    assert risk.entry_charges == charges and risk.max_loss == max_loss
    _holds(risk, Decimal("15"), BUDGET, FUNDS)
    assert risk.charge_breakdown.stt == 0
    assert _size("15", ceiling=2).lots == 1
    assert _buy_charges("15", 130)[0] == Decimal("1950")
    assert _buy_charges("15", 130)[2] > BUDGET


def test_hundred_rupee_ask_is_zero_lots():
    risk = _size("100")
    exposure, charges, max_loss = _buy_charges("100", 65)
    assert exposure == Decimal("6500") and max_loss == exposure + charges
    assert max_loss > BUDGET and max_loss < FUNDS
    assert risk.lots == 0 and risk.quantity == 0 and risk.max_loss == 0


def test_premium_on_the_ceiling_and_just_above_it():
    under = _size("15.01", ceiling=1)
    exposure, charges, max_loss = _buy_charges("15.01", 65)
    assert exposure == Decimal("975.65") and charges == Decimal("24.03370959710")
    assert max_loss == Decimal("999.68370959710")
    assert under.lots == 1 and under.max_loss == max_loss and under.max_loss <= BUDGET
    exact = _size("15.01", ceiling=1, budget=max_loss)
    assert exact.lots == 1 and exact.max_loss == max_loss
    just_over_budget = _size("15.01", ceiling=1, budget=max_loss - Decimal("0.01"))
    assert just_over_budget.lots == 0 and just_over_budget.quantity == 0
    over = _size("15.02", ceiling=1)
    over_exposure, over_charges, over_loss = _buy_charges("15.02", 65)
    assert over_exposure == Decimal("976.30") and over_loss == over_exposure + over_charges
    assert over_loss > BUDGET
    assert over.lots == 0 and over.quantity == 0


def test_charge_calculation_failure_sizes_zero(monkeypatch):
    def _boom(*_args, **_kwargs):
        raise RuntimeError("tariff unavailable")

    monkeypatch.setattr("app.trading.option_quote.leg_costs", _boom)
    risk = _size("10")
    assert risk.lots == 0 and risk.quantity == 0 and risk.max_loss == 0


def test_zero_and_negative_ask_lot_capital_and_fraction():
    quote, decision = _book("10")
    for buy in (Decimal("0"), Decimal("-10")):
        blocked = long_premium_risk(
            quote, replace(decision, buy=buy), _profile(), TRADE_DATE,
            lots_ceiling=3, risk_ceiling=BUDGET, funds=FUNDS,
        )
        assert blocked.lots == 0 and blocked.quantity == 0
    for ask in (Decimal("0"), Decimal("-10")):
        blocked = long_premium_risk(
            replace(quote, ask=ask), decision, _profile(), TRADE_DATE,
            lots_ceiling=3, risk_ceiling=BUDGET, funds=FUNDS,
        )
        assert blocked.lots == 0 and blocked.quantity == 0
    for lot in (0, -65, 65.5, True):
        blocked = long_premium_risk(
            replace(quote, lot_size=lot), decision, _profile(), TRADE_DATE,
            lots_ceiling=3, risk_ceiling=BUDGET, funds=FUNDS,
        )
        assert blocked.lots == 0 and blocked.quantity == 0 and blocked.max_loss == 0
        assert blocked.quantity >= 0
    for capital, fraction in (
        (Decimal("0"), Decimal("0.02")),
        (Decimal("-50000"), Decimal("0.02")),
        (Decimal("50000"), Decimal("0")),
        (Decimal("50000"), Decimal("-0.02")),
    ):
        blocked = long_premium_risk(
            quote, decision, _profile(), TRADE_DATE,
            lots_ceiling=3, risk_ceiling=capital * fraction, funds=FUNDS,
        )
        assert blocked.lots == 0 and blocked.quantity == 0 and blocked.max_loss == 0


def test_missing_and_insufficient_funds_size_nothing():
    quote, decision = _book("10")
    missing = long_premium_risk(
        quote, decision, _profile(), TRADE_DATE, lots_ceiling=3, risk_ceiling=BUDGET,
    )
    assert missing.lots == 0 and missing.quantity == 0
    for funds in (None, Decimal("0"), Decimal("-1"), Decimal("673.88")):
        blocked = long_premium_risk(
            quote, decision, _profile(), TRADE_DATE,
            lots_ceiling=3, risk_ceiling=BUDGET, funds=funds,
        )
        assert blocked.lots == 0 and blocked.quantity == 0
    _, _, max_loss = _buy_charges("15", 65)
    assert _size("15", ceiling=1, funds=max_loss).lots == 1
    short = _size("15", ceiling=1, funds=max_loss - Decimal("0.01"))
    assert short.lots == 0 and short.quantity == 0


def test_fractional_ceiling_and_maximum_lot_ceiling():
    quote, decision = _book("10")
    for ceiling in (1.5, Decimal("2"), True, 0, -3):
        blocked = long_premium_risk(
            quote, decision, _profile(), TRADE_DATE,
            lots_ceiling=ceiling, risk_ceiling=BUDGET, funds=FUNDS,
        )
        assert blocked.lots == 0 and blocked.quantity == 0
    room = Decimal("100000")
    four = _size("10", ceiling=4, budget=room, funds=room)
    assert four.lots == 4 and four.quantity == 260
    _holds(four, Decimal("10"), room, room)
    three = _size("10", ceiling=3, budget=room, funds=room)
    assert three.lots == 3 and three.quantity == 195
    capped = _size("10", ceiling=4)
    assert capped.lots == 1 and capped.lots <= 4


def test_repeated_calculation_matches():
    first = _size("15", ceiling=4)
    second = _size("15", ceiling=4)
    assert first == second
    assert first.lots == 1 and first.quantity == 65


def test_decimal_dust_stays_under_one_paisa():
    risk = _size("10", ceiling=1)
    _, charges, max_loss = _buy_charges("10", 65)
    assert abs(risk.entry_charges - charges) < Decimal("0.000000001")
    assert abs(risk.max_loss - max_loss) < Decimal("0.000000001")
    assert risk.max_loss <= BUDGET
    dear = _size("100", ceiling=1, budget=Decimal("100000"), funds=Decimal("100000"))
    _, dear_charges, _ = _buy_charges("100", 65)
    assert abs(dear.entry_charges - dear_charges) < Decimal("0.000000001")
    assert dear.max_loss == dear.exposure + dear.entry_charges


def test_failed_budget_or_funds_never_returns_a_positive_quantity():
    for ask in ("10", "15", "15.02", "100"):
        for budget, funds in (
            (BUDGET, Decimal("1")),
            (Decimal("1"), FUNDS),
            (Decimal("0"), FUNDS),
            (Decimal("-1000"), FUNDS),
            (BUDGET, Decimal("0")),
            (BUDGET, Decimal("-5")),
            (BUDGET, None),
        ):
            risk = _size(ask, budget=budget, funds=funds)
            assert risk.lots == 0 and risk.quantity == 0 and risk.quantity >= 0


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
