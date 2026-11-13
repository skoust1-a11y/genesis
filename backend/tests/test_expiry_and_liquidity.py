"""The live feed trades the expiry a backtest would, and entries refuse illiquid legs in both."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.brokers.base import Instrument, Quote
from app.config import get_settings
from app.engine.costs import resolve_cost_profile
from app.engine.sim import Scenario, dte_window, simulate
from app.market.feed import IST, BrokerFeed
from app.market.historical import HistoricalFeed
from app.market.models import IndexBar, OptionEod
from app.strategies.rules import AccountState, RiskLimits
from app.strategies.schema import parse_strategy_markdown
from app.trading.decisions import build_entry_proposal, entry_risk_checks
from tests.test_option_buying import LONG_CALL
from tests.test_simulator import store  # noqa: F401

NOW = datetime(2026, 1, 5, 10, 0, tzinfo=IST)
WEEKLY, MONTHLY = "2026-01-08", "2026-01-15"  # 3 and 10 days out
RULES = parse_strategy_markdown(LONG_CALL).rules  # dte 7 to 21


class TwoExpiries:
    """A broker with this week's and next week's 22,000 options; next week's call is quoted wide."""

    def __init__(self, wide_spread: bool = True) -> None:
        self.wide_spread = wide_spread

    def instruments(self, underlying: str) -> list[Instrument]:
        return [
            Instrument(f"NIFTY{e}{t}", "NIFTY", e, 22_000.0, t, 65)
            for e in (WEEKLY, MONTHLY)
            for t in ("CE", "PE")
        ]  # fmt: skip

    def quotes(self, tradingsymbols: list[str]) -> dict[str, Quote]:
        def quote(symbol: str) -> Quote:
            wide = self.wide_spread and symbol == f"NIFTY{MONTHLY}CE"
            bid, ask = (Decimal(90), Decimal(110)) if wide else (Decimal(99), Decimal(101))
            return Quote(symbol, None, bid, ask, NOW)

        return {s: quote(s) for s in tradingsymbols}

    def underlying_quote(self, underlying: str) -> Quote:
        return Quote(underlying, Decimal(22_010), None, None, NOW)

    def vix_quote(self) -> None:
        return None


def feed(window: tuple[int, int] | None, wide_spread: bool = True) -> BrokerFeed:
    return BrokerFeed(
        TwoExpiries(wide_spread), get_settings(), clock=lambda: NOW, iv_rank=lambda u, v, d: 50.0,
        prev_range=lambda u, d: (21_950.0, 21_800.0), dte_window=window,
    )  # fmt: skip


def test_broker_feed_trades_the_expiry_inside_the_strategy_window():
    assert dte_window(RULES) == (7, 21)
    windowed = feed(dte_window(RULES)).snapshot("NIFTY")
    assert (windowed.expiry, windowed.dte) == (MONTHLY, 10)
    assert (22_000.0, "CE", MONTHLY) in windowed.quotes
    nearest = feed(None).snapshot("NIFTY")
    assert (nearest.expiry, nearest.dte) == (WEEKLY, 3)


def risk(snapshot) -> dict[str, bool]:
    proposal = build_entry_proposal(RULES, snapshot, get_settings(), lots=1)
    checks = entry_risk_checks(RULES, RiskLimits(capital_inr=500_000), AccountState(0, 0, 0, 0), proposal)
    return {c.rule: c.passed for c in checks}


def test_a_wide_bid_ask_blocks_the_entry():
    wide = feed((7, 21)).snapshot("NIFTY")
    assert wide.stale == {(22_000.0, "CE", MONTHLY)}  # 20% of the mid, over MAX_SPREAD_PCT=5
    assert risk(wide)["market.liquidity"] is False
    assert all(risk(feed((7, 21), wide_spread=False).snapshot("NIFTY")).values())


def test_sim_skips_a_leg_that_did_not_trade(store):  # noqa: F811
    for day, close, high, low, volume in (
        ("2026-01-05", 22_000, 22_100, 21_900, 100),
        ("2026-01-06", 22_200, 22_250, 22_050, 0),  # a breakout, but the call only has a settlement price
    ):
        store.session.add(
            IndexBar(underlying="NIFTY", date=day, close=close, high=high, low=low, vix=15.0, iv_rank=50)
        )
        store.session.add(
            OptionEod(
                underlying="NIFTY", date=day, expiry=MONTHLY, strike=22_200.0, option_type="CE", close=100.0,
                settle=100.0, volume=volume, open_interest=1000, lot_size=65, underlying_price=None,
            )  # fmt: skip
        )
    store.session.commit()

    settings = get_settings()
    doc = parse_strategy_markdown(LONG_CALL)
    result = simulate(
        doc.rules, HistoricalFeed(store, "NIFTY", dte_window=dte_window(doc.rules)), 2,
        resolve_cost_profile(settings, doc), settings,
        scenario=Scenario("base", doc.frontmatter["rules"], lots=1), mode="backtest",
    )  # fmt: skip
    assert result.trades == []
    assert result.skipped == {"market.liquidity": 1}
