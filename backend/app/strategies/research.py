"""Bounded deterministic research. Final holdout rows never reach candidate selection."""

from datetime import date

from app.db.hashing import canonical_json, sha256_hex
from app.strategies.configuration import StrategyConfiguration, run_configuration, validate_configuration_execution
from app.trading.candle_integrity import (
    CandleIntegrityError, declaration_value, requirements_for, validate_candle_dataset,
    derive_development_snapshot, require_issued_snapshot,
)
from app.trading.candle_calendar import resolve_approved_calendar


# v2 replaces the raw-row development digest with the canonical UTC candle digest.
# Evidence remains outside candidate performance reports and their existing hashes.
RESEARCH_ARTIFACT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "research_version": {"const": "development-research-v2"},
        "plan": {"type": "object"}, "plan_sha256": {"type": "string"},
        "results": {"type": "array"}, "coordinator_full_integrity": {"type": "object"},
        "development_integrity": {"type": "object"},
        "status": {"const": "RESEARCH_ONLY_HOLDOUT_RESERVED"},
        "final_holdout_evaluated": {"const": False}, "paper_approved": {"const": False},
        "live_enabled": {"const": False}, "promotion_blockers": {"type": "array"},
    },
    "additionalProperties": False,
    "required": ("research_version", "plan", "plan_sha256", "results", "coordinator_full_integrity",
                 "development_integrity", "status", "final_holdout_evaluated", "paper_approved",
                 "live_enabled", "promotion_blockers"),
}


def run_development_research(
    configurations: tuple[StrategyConfiguration, ...], *, candles, trading_dates,
    holdout_start: str, config, settings=None, option_quote_provider=None, dataset_declaration=None,
) -> dict:
    if not configurations:
        raise ValueError("An explicit baseline configuration is required")
    boundary = date.fromisoformat(holdout_start).isoformat()
    days = tuple(date.fromisoformat(day).isoformat() for day in trading_dates)
    if days != tuple(sorted(set(days))):
        raise ValueError("Research calendar must be unique and chronological")
    development = tuple(day for day in days if day < boundary)
    holdout = tuple(day for day in days if day >= boundary)
    if not development or not holdout:
        raise ValueError("Both development and untouched holdout dates are required")
    baseline = configurations[0]
    identity = baseline.to_dict()["strategy_id"]
    versions = set()
    for index, candidate in enumerate(configurations):
        payload = candidate.to_dict()
        if payload["strategy_id"] != identity:
            raise ValueError("Research candidates must belong to the same independent strategy")
        if index and payload["parent_configuration_sha256"] != baseline.sha256:
            raise ValueError("Research variants must bind directly to the immutable baseline")
        if payload["strategy_version"] in versions:
            raise ValueError("Research candidate versions must be unique")
        versions.add(payload["strategy_version"])
    specs = [validate_configuration_execution(candidate, config=config, settings=settings)
             for candidate in configurations]
    # Preserve fold-definition error precedence without executing any candidate.
    from app.trading.walk_forward import generate_walk_forward_folds, WalkForwardError
    folds = generate_walk_forward_folds(development, config)
    if folds and config.step_trading_days < config.oos_trading_days:
        raise WalkForwardError("OVERLAPPING_OOS_ACCOUNT_WINDOWS_UNSUPPORTED")
    declaration = declaration_value(dataset_declaration)
    if declaration.scope != "full":
        raise CandleIntegrityError("CI_DECLARATION_MISMATCH")
    calendar = resolve_approved_calendar(declaration.calendar_id)
    full = validate_candle_dataset(candles, declaration=declaration, calendar=calendar,
                                  requirements=requirements_for(specs[0]), trading_dates=days)
    for spec in specs:
        require_issued_snapshot(full, requirements=requirements_for(spec), expected_dates=days,
                                permitted_scopes=("full",))
    development_snapshot = derive_development_snapshot(full, development_dates=development)
    results = []
    for candidate in configurations:
        result = run_configuration(
            candidate, candles=development_snapshot, trading_dates=development,
            config=config, settings=settings, option_quote_provider=option_quote_provider,
        )
        results.append(result)
    plan = {"baseline_configuration_sha256": baseline.sha256,
            "candidate_configuration_sha256": [candidate.sha256 for candidate in configurations],
            "walk_forward_configuration": config.to_dict(),
            "development_dates": list(development), "holdout_dates": list(holdout),
            "development_data_sha256": development_snapshot.receipt["candles_sha256"],
            "holdout_start": boundary}
    return {
        "research_version": "development-research-v2", "plan": plan,
        "coordinator_full_integrity": full.receipt,
        "development_integrity": development_snapshot.receipt,
        "plan_sha256": sha256_hex(canonical_json(plan)), "results": results,
        "status": "RESEARCH_ONLY_HOLDOUT_RESERVED",
        "final_holdout_evaluated": False, "paper_approved": False, "live_enabled": False,
        "promotion_blockers": ["FINAL_HOLDOUT_NOT_INDEPENDENTLY_EVALUATED",
                               "SUPERVISED_PAPER_REQUIRES_EXISTING_APPROVAL_DOSSIER"],
    }
