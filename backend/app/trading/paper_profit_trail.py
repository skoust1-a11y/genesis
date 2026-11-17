"""Causal peak-net-profit giveback rule. Quotes/fills remain the caller's responsibility."""
import hashlib
import json
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

VERSION = "peak-net-profit-giveback-v1"


def profile(percent):
    if percent is None:
        return {"version": "fixed-3r-v1"}
    value = Decimal(str(percent))
    if not value.is_finite() or not 10 <= value <= 20:
        raise ValueError("Profit giveback must be between 10 and 20 percent")
    return {"version": VERSION, "giveback_pct": str(value.quantize(Decimal(".01"))),
            "activation_r": "1.00", "basis": "net liquidation profit after all estimated charges",
            "fixed_target": False, "initial_stop": True, "square_off": "15:10"}


def identity(percent):
    rules = profile(percent)
    source = hashlib.sha256(Path(__file__).read_bytes()).hexdigest() if percent is not None else None
    return hashlib.sha256(json.dumps({"rules": rules, "source": source}, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class Mark:
    peak: Decimal
    armed: bool
    threshold: Decimal | None
    triggered: bool


def evaluate(*, previous_peak, current_net, initial_risk, percent):
    profile(percent)
    if percent is None:
        raise ValueError("A giveback percentage is required")
    values = [Decimal(str(v)) for v in (previous_peak, current_net, initial_risk)]
    if any(not v.is_finite() for v in values) or values[0] < 0 or values[2] <= 0:
        raise ValueError("Unverifiable profit trail inputs")
    peak = max(values[0], values[1], Decimal(0))
    armed = peak >= values[2]
    threshold = ((peak * (1-Decimal(str(percent))/100)).quantize(Decimal(".01"), rounding=ROUND_CEILING)
                 if armed else None)
    return Mark(peak, armed, threshold, bool(armed and values[1] <= threshold))
