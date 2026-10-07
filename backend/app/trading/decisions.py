"""Decisions shared by the Trade Crew and the simulator.

Pure functions over a market snapshot and the strategy's rules, so a paper or live run and a
simulated run of the same rules on the same data take the same trades.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from app.agents.trade_crew.legs import Economics, Leg, build_legs, entry_economics, signed, tradingsymbol
from app.config import Settings
from app.market.feed import MarketSnapshot
from app.strategies.rules import (
    AccountState,
    Check,
    PositionState,
    Proposal,
    RiskLimits,
    StrategyRules,
    check_exit,
    check_risk,
)


class MissingMarketConfig(ValueError):
    pass


def describe_check(c: Check) -> str:
    actual = c.actual if c.actual is None else f"{c.actual:g}"
    threshold = c.threshold if c.threshold is None else f"{c.threshold:g}"
    return f"{c.rule}: {c.field} {c.op} {threshold} (actual {actual})"


def entry_rule_text(checks: list[Check]) -> str:
    return " and ".join(f"{c.field} {c.op} {c.threshold:g}" for c in checks if c.threshold is not None)


def proposal_lots(rules: StrategyRules, limit_lots: int | None) -> int:
    return min(rules.risk.max_lots, limit_lots) if limit_lots else rules.risk.max_lots


def lot_size_for(rules: StrategyRules, market: MarketSnapshot, settings: Settings) -> int | None:
    return rules.instrument.lot_size or market.lot_size or settings.lot_size(rules.instrument.underlying)


def build_entry_proposal(rules: StrategyRules, market: MarketSnapshot, settings: Settings, lots: int) -> dict[str, Any]:
    """Legs, quotes and economics for opening `lots` lots now. Raises MissingMarketConfig without a lot size or strike step."""
    instrument = rules.instrument
    lot_size = lot_size_for(rules, market, settings)
    step = settings.strike_step(instrument.underlying)
    if not lot_size or not step:
        raise MissingMarketConfig(f"no lot size or strike step configured for {instrument.underlying}")
    legs = build_legs(instrument, market.spot, step)
    quotes = [market.quote(leg.strike, leg.option_type) for leg in legs]
    illiquid = [(leg.strike, leg.option_type, market.expiry) in market.stale for leg in legs]
    return _proposal(rules, market.expiry, legs, quotes, lots, lot_size, illiquid)


def _proposal(
    rules: StrategyRules, expiry: str, legs, quotes, lots: int, lot_size: int, illiquid: list[bool]
) -> dict[str, Any]:
    quantity = lots * lot_size
    leg_dicts = [
        {
            **leg.as_dict(),
            "expiry": expiry,
            "tradingsymbol": tradingsymbol(rules.instrument.underlying, expiry, leg.strike, leg.option_type),
            "quantity": quantity,
            "quote": q,
            "illiquid": thin,  # a wide bid/ask live, or no trade that day in end-of-day data
        }
        for leg, q, thin in zip(legs, quotes, illiquid, strict=True)
    ]
    economics = entry_economics(rules, legs, quotes, quantity) if quantity and None not in quotes else None
    return {"lots": lots, "lot_size": lot_size, "quantity": quantity, "legs": leg_dicts, "economics": economics}


def resize_proposal(rules: StrategyRules, proposal: dict[str, Any], lots: int) -> dict[str, Any]:
    """The same legs and quotes at a different number of lots."""
    legs = [Leg(leg["side"], leg["option_type"], leg["strike"]) for leg in proposal["legs"]]
    quotes = [leg["quote"] for leg in proposal["legs"]]
    expiry = proposal["legs"][0]["expiry"] if proposal["legs"] else ""
    illiquid = [leg.get("illiquid", False) for leg in proposal["legs"]]
    return _proposal(rules, expiry, legs, quotes, lots, proposal["lot_size"], illiquid)


def entry_risk_checks(
    rules: StrategyRules, limits: RiskLimits, account: AccountState, proposal: dict[str, Any]
) -> list[Check]:
    economics: Economics | None = proposal["economics"]
    missing = sum(1 for leg in proposal["legs"] if leg["quote"] is None)
    credit = economics.credit if economics else 0.0
    premium = Check("risk.positive_credit", "credit", ">", 0, credit, credit > 0)
    if rules.instrument.is_debit:
        premium = Check("risk.positive_debit", "debit", ">", 0, -credit, -credit > 0)
    illiquid = sum(1 for leg in proposal["legs"] if leg.get("illiquid"))
    checks = [
        Check("market.quotes", "missing_quotes", "==", 0, missing, missing == 0),
        Check("market.liquidity", "illiquid_legs", "==", 0, illiquid, illiquid == 0),
        premium,
    ]
    return checks + check_risk(
        rules,
        limits,
        account,
        Proposal(
            lots=proposal["lots"],
            risk_reward=economics.risk_reward if economics else 0.0,
            max_loss=economics.max_loss if economics else 0.0,
        ),
    )


@dataclass
class PositionMark:
    quotes: list[float]
    credit: float  # premium received at entry, in INR
    pnl: float  # unrealized, at current quotes
    dte: int  # days to the nearest leg expiry


def mark_position(legs: list[dict[str, Any]], market: MarketSnapshot) -> PositionMark | None:
    """Mark filled legs (each with `price` and `quantity`) to market; None if any leg has no quote."""
    quotes = [market.quote(leg["strike"], leg["option_type"], leg["expiry"]) for leg in legs]
    if any(q is None for q in quotes):
        return None
    credit = sum(signed(leg["side"]) * leg["price"] * leg["quantity"] for leg in legs)
    pnl = sum(signed(leg["side"]) * (leg["price"] - q) * leg["quantity"] for leg, q in zip(legs, quotes, strict=True))
    dte = min(market.days_to(leg["expiry"]) for leg in legs)
    return PositionMark(quotes, credit, pnl, dte)


def exit_checks(rules: StrategyRules, mark: PositionMark) -> list[Check]:
    """Expiry, then the strategy's exit rules. Expired legs settle at intrinsic value, so expiry
    takes precedence over a stop or target that fires on the same day."""
    expiry = Check("exit.expiry", "dte", "<=", 0, mark.dte, mark.dte <= 0)
    return [expiry, *check_exit(rules, PositionState(credit=mark.credit, pnl=mark.pnl), SimpleNamespace(dte=mark.dte))]


def fired_exit(checks: list[Check]) -> Check | None:
    return next((c for c in checks if c.passed), None)
