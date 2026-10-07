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
    if instrument.is_debit:
        # Calls go out of the money upward, puts downward; spreads sell `wing_width` further out.
        option_type, direction = ("CE", 1) if instrument.structure in ("long_call", "bull_call_spread") else ("PE", -1)
        bought = _snap(atm + direction * (instrument.strike_offset or 0), strike_step)
        legs = [Leg("BUY", option_type, bought)]
        if instrument.wing_width is not None:
            legs.append(Leg("SELL", option_type, _snap(bought + direction * instrument.wing_width, strike_step)))
        return legs
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
    credit: float  # net INR for the whole position: received is positive, paid (a debit) negative
    target_pnl: float
    stop_loss_pnl: float  # negative
    max_loss: float  # loss the stop allows, capped by the structure's own max loss
    risk_reward: float


def _width(legs: list[Leg]) -> float:
    return max(
        abs(s.strike - b.strike)
        for s in legs if s.side == "SELL"
        for b in legs if b.side == "BUY" and b.option_type == s.option_type
    )  # fmt: skip


def entry_economics(rules: StrategyRules, legs: list[Leg], prices: list[float], quantity: int) -> Economics:
    credit = sum(signed(leg.side) * price for leg, price in zip(legs, prices, strict=True)) * quantity
    if rules.instrument.is_debit:
        debit = -credit
        target = debit * rules.profit_target_pct / 100
        if rules.instrument.wing_width is not None:
            target = min(target, _width(legs) * quantity - debit)  # a spread earns at most its width
        max_loss = min(debit * rules.stop_loss_pct / 100, debit)
        risk_reward = target / max_loss if max_loss > 0 else 0.0
        return Economics(credit, target, -max_loss, max_loss, risk_reward)
    target = credit * rules.profit_target_pct / 100
    stop = credit * rules.stop_loss_pct / 100
    max_loss = stop
    if rules.instrument.structure in DEFINED_RISK:
        max_loss = min(stop, _width(legs) * quantity - credit)
    risk_reward = target / max_loss if max_loss > 0 else 0.0
    return Economics(credit, target, -stop, max_loss, risk_reward)
