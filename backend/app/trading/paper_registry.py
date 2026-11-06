"""Which scanner-v0 setups are paper-traded, in which virtual account, under which risk policy.

The scanner's display names are already part of every signal id, so they are mapped to a family here and never
renamed. Each family has its own independent account; every registered signal also competes for ``SHARED``.
The registry and the risk policy are hashed onto every paper decision, so a rule change cannot hide in old rows.
"""

from __future__ import annotations

import hashlib
import json
import re

STRATEGY_VERSION = "scanner-v0"
SHARED = "SHARED"
# Order is the shared-account priority when two signals share a candle.
FAMILIES = {
    "LIQ_SWEEP_PDHL": r"Liquidity Sweep \[\w+\]",
    "RETEST_PD": r"Breakout & Retest \(PD\) \[\w+\]",
    "RETEST_OR30": r"Breakout & Retest \(OR-30\) \[\w+\]",
    "TRENDLINE_CHOCH": r"Trendline Reversal \(\w+\)",
    "EQHL_SWEEP_INLINE": r"Equal (?:Highs|Lows) Sweep \[\w+\]",
}
# ScannerManager fetches 1m and 15m frames raw, so their last bar may still be forming.
PAPER_TIMEFRAMES = ("5m",)
DISABLED = {"PDC_15EMA_V0": "no card-faithful function exists and the frozen card text is not in the repo"}
RISK_POLICY = {
    "risk_policy_id": "paper-risk-v1",
    "max_loss_per_trade_inr": "10000",
    "daily_loss_limit_inr": "5000",
    "max_lots": 3,
    "max_positions": {SHARED: 2, "INDEPENDENT": 1},
    "slippage_ticks": 1,
}
REGISTRY_HASH = hashlib.sha256(json.dumps(
    {"version": STRATEGY_VERSION, "families": FAMILIES, "timeframes": PAPER_TIMEFRAMES, "disabled": DISABLED,
     "risk": RISK_POLICY}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def family_of(strategy_id: str, strategy_version: str = STRATEGY_VERSION) -> str | None:
    if strategy_version != STRATEGY_VERSION:
        return None
    return next((f for f, pattern in FAMILIES.items() if re.fullmatch(pattern, strategy_id)), None)


def account_ids() -> list[str]:
    return [f"IND:{f}" for f in FAMILIES] + [SHARED]


def accounts_for(strategy_id: str, strategy_version: str = STRATEGY_VERSION) -> list[str]:
    """The independent account first, then SHARED. An unregistered signal is only recorded against SHARED."""
    family = family_of(strategy_id, strategy_version)
    return ([f"IND:{family}"] if family else []) + [SHARED]


def max_positions(account_id: str) -> int:
    return RISK_POLICY["max_positions"][SHARED if account_id == SHARED else "INDEPENDENT"]


def order_key(setup) -> tuple:
    """Deterministic processing order inside one tick: candle, family priority, then identity."""
    family = family_of(str(setup.strategy))
    rank = list(FAMILIES).index(family) if family else len(FAMILIES)
    return (str(setup.timestamp), rank, str(setup.strategy), str(setup.direction), float(setup.trigger_price))
