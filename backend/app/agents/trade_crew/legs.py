"""Turn a strategy's `instrument` rules into option legs and the numbers the risk check needs."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date

from app.strategies.rules import DEFINED_RISK, Instrument, StrategyRules


@dataclass
class Leg:
    side: str  # "SELL" | "BUY"
    option_type: str  # "CE" | "PE"
    strike: float

    def as_dict(self) -> dict:
        return asdict(self)


def _snap(value: float, step: float) -> float:
    return float(round(value / step) * step)


def build_legs(instrument: Instrument, spot: float, strike_step: float) -> list[Leg]:
    atm = _snap(spot, strike_step)
    if instrument.structure == "short_straddle":
        return [Leg("SELL", "CE", atm), Leg("SELL", "PE", atm)]
    call = _snap(atm + instrument.short_distance, strike_step)
    put = _snap(atm - instrument.short_distance, strike_step)
    legs = [Leg("SELL", "CE", call), Leg("SELL", "PE", put)]
    if instrument.structure == "iron_condor":
        legs += [
            Leg("BUY", "CE", _snap(call + instrument.wing_width, strike_step)),
            Leg("BUY", "PE", _snap(put - instrument.wing_width, strike_step)),
        ]
    return legs


def tradingsymbol(underlying: str, expiry: str, strike: float, option_type: str) -> str:
    return f"{underlying}{date.fromisoformat(expiry).strftime('%y%b%d').upper()}{strike:g}{option_type}"


def signed(side: str) -> int:
    """+1 for premium received (SELL), -1 for premium paid (BUY)."""
    return 1 if side == "SELL" else -1


@dataclass
class Economics:
    credit: float  # INR received for the whole position
    target_pnl: float
    stop_loss_pnl: float  # negative
    max_loss: float  # loss the stop allows, capped by the structure's own max loss
    risk_reward: float


def entry_economics(rules: StrategyRules, legs: list[Leg], prices: list[float], quantity: int) -> Economics:
    credit = sum(signed(leg.side) * price for leg, price in zip(legs, prices, strict=True)) * quantity
    target = credit * rules.exit_value("profit_target_pct_of_credit") / 100
    stop = credit * rules.exit_value("stop_loss_pct_of_credit") / 100
    max_loss = stop
    if rules.instrument.structure in DEFINED_RISK:
        width = max(
            abs(s.strike - b.strike)
            for s in legs if s.side == "SELL"
            for b in legs if b.side == "BUY" and b.option_type == s.option_type
        )  # fmt: skip
        max_loss = min(stop, width * quantity - credit)
    risk_reward = target / max_loss if max_loss > 0 else 0.0
    return Economics(credit, target, -stop, max_loss, risk_reward)
