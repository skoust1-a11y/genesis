"""Which scanner-v0 setups are paper-traded, in which virtual account, under which risk policy.

The scanner's display names are already part of every signal id, so they are mapped to a family here and never
renamed. Each family has its own independent account; every registered signal also competes for ``SHARED``.
The registry and the risk policy are hashed onto every paper decision, so a rule change cannot hide in old rows.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

# scanner-v0 (registry f61fe382...) fed several sessions to the detector; its journals and accounts stay frozen.
STRATEGY_VERSION = "scanner-v0.1"
STRATEGY_PARAMETERS = {"otm_steps": 2, "strategy_filter": "auto", "timeframes": ["1m", "5m", "15m"]}
PARAMETER_HASH = hashlib.sha256(json.dumps(STRATEGY_PARAMETERS, sort_keys=True,
                                         separators=(",", ":")).encode()).hexdigest()
SHARED = "SHARED"
# Order is the shared-account priority when two signals share a candle.
FAMILIES = {
    "LIQ_SWEEP_PDHL": r"Liquidity Sweep \[\w+\]",
    "RETEST_PD": r"Breakout & Retest \(PD\) \[\w+\]",
    "RETEST_OR30": r"Breakout & Retest \(OR-30\) \[\w+\]",
    "TRENDLINE_CHOCH": r"Trendline Reversal \(\w+\)",
    "EQHL_SWEEP_INLINE": r"Equal (?:Highs|Lows) Sweep \[\w+\]",
}
# 1m and 15m are closed-bar filtered since v0.1 but their signal stamps are not (sweep: start+5, EQH/EQL: start).
PAPER_TIMEFRAMES = ("5m",)
DISABLED = {"PDC_15EMA_V0": "no card-faithful function exists and the frozen card text is not in the repo"}
RISK_POLICY = {
    "risk_policy_id": "paper-risk-v2",
    "max_loss_per_trade_inr": "5000",
    "daily_loss_limit_inr": "5000",
    "stop_risk_fraction": "0.02",
    "stop_risk_initial_cap_inr": "1000",
    "delta_max_age_seconds": 10,
    "max_lots": 3,
    "max_positions": {SHARED: 2, "INDEPENDENT": 1},
    "slippage_ticks": 1,
}
_BACKEND = Path(__file__).resolve().parents[2]
RULE_SOURCE_HASHES = {path: hashlib.sha256((_BACKEND / path).read_bytes()).hexdigest() for path in (
    "app/trading/scanner_crew/detector_agent.py", "app/liquidity_signals.py",
    "app/trading/crew/exit_agent.py", "app/trading/crew/manager.py", "scripts/backtest_pdc_15ema.py",
    # The input contract: which bars and levels the detector sees.
    "app/trading/market_validation.py", "app/trading/scanner_crew/scanner_manager.py",
    "app/trading/scanner_crew/levels_agent.py",
)}
REGISTRY_HASH = hashlib.sha256(json.dumps(
    {"version": STRATEGY_VERSION, "families": FAMILIES, "timeframes": PAPER_TIMEFRAMES, "disabled": DISABLED,
     "risk": RISK_POLICY, "rule_sources": RULE_SOURCE_HASHES,
     "parameters": STRATEGY_PARAMETERS}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


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


def strategy_max_lots(family: str) -> int | None:
    """The family spec's lot ceiling; None (fail closed) when its risk and sizing ceilings disagree or are invalid."""
    from app.strategies.normalizer import normalize_scanner_v0_family

    rules = normalize_scanner_v0_family(family).rules_v2
    caps = {rules.risk.max_lots, rules.position_sizing.max_lots}
    cap = caps.pop() if len(caps) == 1 else None
    return cap if type(cap) is int and cap >= 1 else None


def order_key(setup) -> tuple:
    """Deterministic processing order inside one tick: candle, family priority, then identity."""
    family = family_of(str(setup.strategy))
    rank = list(FAMILIES).index(family) if family else len(FAMILIES)
    return (str(setup.timestamp), rank, str(setup.strategy), str(setup.direction), float(setup.trigger_price))


def check_paper_admission_eligibility(
    strategy_id: str,
    strategy_version: str = STRATEGY_VERSION,
    *,
    spec: object | None = None,
) -> tuple[bool, list[str]]:
    """Boundary check for paper-trading admission: quarantined, disabled, synthetic-only, or unregistered strategies fail closed."""
    from collections.abc import Mapping

    sid = str(strategy_id or "").strip()
    if sid in DISABLED:
        return False, [f"STRATEGY_DISABLED:{DISABLED[sid]}"]
    if sid.upper().startswith("PDC_15EMA_V0"):
        return False, [f"STRATEGY_DISABLED:{DISABLED['PDC_15EMA_V0']}"]

    if spec is not None:
        from pydantic import ValidationError
        from app.strategies.normalizer import (
            NormalizedStrategySpec,
            normalize_markdown,
            normalize_strategy_document,
        )
        from app.strategies.rules import StrategyRulesV2
        from app.strategies.schema import StrategyDocument

        norm: NormalizedStrategySpec | None = None
        if isinstance(spec, NormalizedStrategySpec):
            norm = spec
        elif isinstance(spec, StrategyDocument):
            norm = normalize_strategy_document(
                spec,
                strategy_id=sid,
                strategy_version=strategy_version,
                strict_gaps=True,
            )
        elif isinstance(spec, str):
            norm = normalize_markdown(
                spec,
                strategy_id=sid,
                strategy_version=strategy_version,
                strict_gaps=True,
            )
        elif isinstance(spec, StrategyRulesV2):
            if spec.unresolved_gaps:
                return False, ["STRATEGY_QUARANTINED", *[f"OPEN_GAP: {g}" for g in spec.unresolved_gaps]]
        elif isinstance(spec, Mapping):
            fidelity = str(spec.get("fidelity_tier") or spec.get("validation_fidelity_tier") or "").strip().upper()
            if fidelity == "TIER_2_SYNTHETIC" or spec.get("paper_promotion_eligible") is False:
                return False, ["STRATEGY_QUARANTINED", "SYNTHETIC_TIER2_NOT_ELIGIBLE_FOR_PAPER"]
            if spec.get("quarantined") is True or spec.get("paper_eligible") is False:
                extra = [str(r) for r in (spec.get("quarantine_reasons") or []) if str(r)]
                return False, ["STRATEGY_QUARANTINED", *(extra or ["QUARANTINED_SPEC_PAYLOAD"])]
            if spec.get("unresolved_gaps"):
                return False, ["STRATEGY_QUARANTINED", *[f"OPEN_GAP: {g}" for g in spec["unresolved_gaps"]]]
            raw_v2 = spec.get("rules_v2") if "rules_v2" in spec else (spec if "instrument" in spec else None)
            if raw_v2 is not None:
                try:
                    v2 = StrategyRulesV2.model_validate(raw_v2)
                    if v2.unresolved_gaps:
                        return False, ["STRATEGY_QUARANTINED", *[f"OPEN_GAP: {g}" for g in v2.unresolved_gaps]]
                except ValidationError as exc:
                    return False, ["STRATEGY_QUARANTINED", *[str(e["msg"]) for e in exc.errors()]]
            elif "markdown" in spec and isinstance(spec["markdown"], str):
                norm = normalize_markdown(
                    spec["markdown"],
                    strategy_id=sid,
                    strategy_version=strategy_version,
                    strict_gaps=True,
                )
            else:
                return False, ["STRATEGY_QUARANTINED", "INCOMPLETE_SPEC_MAPPING"]
        else:
            return False, ["STRATEGY_QUARANTINED", f"UNSUPPORTED_SPEC_TYPE:{type(spec).__name__}"]

        if norm is not None:
            if norm.validation_fidelity_tier == "TIER_2_SYNTHETIC":
                return False, ["STRATEGY_QUARANTINED", "SYNTHETIC_TIER2_NOT_ELIGIBLE_FOR_PAPER"]
            if norm.quarantined or not norm.paper_eligible or not norm.structurally_complete:
                return False, ["STRATEGY_QUARANTINED", *norm.quarantine_reasons]

    family = family_of(sid, strategy_version)
    if family is None:
        return False, ["STRATEGY_NOT_REGISTERED"]

    from app.strategies.normalizer import normalize_scanner_v0_family

    family_spec = normalize_scanner_v0_family(family)
    if family_spec.quarantined or not family_spec.paper_eligible:
        return False, ["STRATEGY_QUARANTINED", *family_spec.quarantine_reasons]
    return True, []
