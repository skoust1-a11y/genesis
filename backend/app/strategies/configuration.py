"""Immutable research configurations; no paper, broker or delivery side effects."""

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

import yaml

from app.config import Settings, get_settings
from app.db.hashing import canonical_json, sha256_hex
from app.strategies.capabilities import EOD_ADAPTER_VERSION, eod_rejection_reasons
from app.strategies.normalizer import normalize_markdown

CONFIGURATION_VERSION = "strategy-configuration-v1"
RESEARCH_MODES = frozenset({"historical_replay", "historical_synthetic"})


@dataclass(frozen=True)
class StrategyConfiguration:
    """Canonical text avoids mutable nested models and returns detached copies."""

    canonical: str

    @property
    def sha256(self) -> str:
        return sha256_hex(self.canonical)

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self.canonical)


def create_configuration(
    markdown: str, *, original_source: str, source_type: str, source_ref: str | None,
    strategy_id: str, strategy_version: str, execution_mode: str,
    settings: Settings | None = None, parent_sha256: str | None = None,
) -> StrategyConfiguration:
    if execution_mode not in RESEARCH_MODES:
        raise ValueError("Only explicit historical research modes are supported")
    if not original_source.strip() or not source_type.strip():
        raise ValueError("Original source text and source type are required")
    from app.engine.sim import ENGINE_VERSION
    from app.strategies.schema import parse_strategy_markdown
    from app.trading.walk_forward import (
        WALK_FORWARD_ENGINE_VERSION,
        RuleBasedCausalSignalGenerator,
        UnsupportedStrategyRuleError,
    )

    doc = parse_strategy_markdown(markdown)
    active_settings = settings or get_settings()
    spec = normalize_markdown(
        markdown, strategy_id=strategy_id, strategy_version=strategy_version,
        source_sha256=sha256_hex(original_source),
    )
    reasons = [{"code": "NORMALIZATION_REJECTED", "detail": r} for r in spec.quarantine_reasons]
    if source_type not in {"markdown", "deterministic_variant"}:
        reasons.append({"code": "UNVERIFIED_SOURCE_RULE_COVERAGE",
                        "detail": "Source extraction requires independent semantic fidelity verification"})
    rules = spec.rules_v2
    adapter = None
    engine = None
    if rules is not None and not reasons:
        if rules.market != "nse_options":
            reasons.append({"code": "UNSUPPORTED_MARKET", "detail": rules.market})
        elif rules.timeframes.primary == "1d":
            adapter, engine = EOD_ADAPTER_VERSION, ENGINE_VERSION
            reasons.extend(eod_rejection_reasons(spec))
            if rules.position_sizing.capital_inr != active_settings.default_capital_inr:
                reasons.append({"code": "EXECUTION_ASSUMPTION_MISMATCH", "detail": "EOD capital differs from declared capital"})
        elif rules.timeframes.primary == "5m":
            adapter, engine = "causal-rules-adapter-v1", WALK_FORWARD_ENGINE_VERSION
            parameters = {key: item.value for key, item in rules.parameters.items()}
            try:
                RuleBasedCausalSignalGenerator().generate_signals(
                    spec, parameters, [], allowed_dates=[], settings=active_settings,
                )
            except UnsupportedStrategyRuleError as exc:
                reasons.append({"code": "UNSUPPORTED_EXECUTION_RULE", "detail": str(exc)})
        else:
            reasons.append({"code": "UNSUPPORTED_TIMEFRAME", "detail": rules.timeframes.primary})
    from app.engine.costs import resolve_cost_profile
    payload = {
        "configuration_version": CONFIGURATION_VERSION,
        "strategy_id": strategy_id, "strategy_version": strategy_version,
        "parent_configuration_sha256": parent_sha256,
        "source": {"text": original_source, "type": source_type, "ref": source_ref,
                   "sha256": sha256_hex(original_source)},
        "submitted_markdown": markdown,
        "extracted_rules": {key: doc.frontmatter[key] for key in ("rules", "rules_v2") if key in doc.frontmatter},
        "normalized_specification": spec.to_dict(),
        "adapter_version": adapter, "engine_version": engine,
        "execution_mode": execution_mode,
        "execution_assumptions": {
            "default_capital_inr": active_settings.default_capital_inr,
            "strike_steps": active_settings.strike_steps,
            "eod_cost_profile": asdict(resolve_cost_profile(active_settings, doc)),
            "causal_cost_policy": "paper-fees-v2; historical policy and assumptions bound in replay report",
        },
        "execution_status": "NOT_EXECUTABLE_EXACTLY" if reasons else "SUPPORTED_STRUCTURED_RULES",
        "rejection_reasons": reasons,
        "paper_approved": False, "live_enabled": False,
    }
    return StrategyConfiguration(canonical_json(payload))


def create_variant(
    baseline: StrategyConfiguration, *, strategy_version: str,
    parameters: Mapping[str, Any], settings: Settings | None = None,
) -> StrategyConfiguration:
    payload = baseline.to_dict()
    if strategy_version == payload["strategy_version"] or not strategy_version.strip():
        raise ValueError("A variant requires a distinct immutable version")
    if payload["execution_status"] == "NOT_EXECUTABLE_EXACTLY":
        raise ValueError("Cannot derive an executable variant from a rejected baseline")
    rules = payload["normalized_specification"]["rules_v2"]
    from app.strategies.rules import ParameterDef

    for name, value in parameters.items():
        if name not in rules["parameters"]:
            raise ValueError(f"Undeclared variant parameter: {name}")
        if not rules["parameters"][name]["mutable_in_research"]:
            raise ValueError(f"Frozen research parameter: {name}")
        definition = {**rules["parameters"][name], "value": value}
        rules["parameters"][name] = ParameterDef.model_validate(definition).model_dump(mode="json")
        if name in ("stop_loss_points", "target_points", "target_r_multiple"):
            field, mode = {"stop_loss_points": ("stop_loss", "index_points"),
                           "target_points": ("target", "index_points"),
                           "target_r_multiple": ("target", "r_multiple")}[name]
            if rules[field]["mode"] != mode:
                raise ValueError(f"Parameter {name} does not match declared {field} mode")
            rules[field]["value"] = value
        elif name in ("ema_period", "body_ratio_min", "sweep_buffer_points"):
            matching = [p for p in rules["intraday_entry"] if name in p["params"]]
            if len(matching) != 1:
                raise ValueError(f"Ambiguous predicate parameter binding: {name}")
            matching[0]["params"][name] = value
        elif name not in ("iv", "max_lots", "strike_offset", "swing_lookback_bars", "swing_buffer_points"):
            raise ValueError(f"Unsupported variant parameter binding: {name}")
        if name == "max_lots":
            rules["risk"]["max_lots"] = value
            rules["position_sizing"]["max_lots"] = value
        elif name == "strike_offset":
            rules["instrument"]["strike_offset"] = value
    rules["strategy_version"] = strategy_version
    markdown = "---\n" + yaml.safe_dump({"name": payload["strategy_id"], "rules_v2": rules}, sort_keys=False) + "---\n"
    return create_configuration(
        markdown, original_source=canonical_json({"parent_sha256": baseline.sha256,
                                                  "parameters": dict(parameters), "rules_v2": rules}),
        source_type="deterministic_variant", source_ref=baseline.sha256,
        strategy_id=payload["strategy_id"], strategy_version=strategy_version,
        execution_mode=payload["execution_mode"], settings=settings, parent_sha256=baseline.sha256,
    )


def run_configuration(
    configuration: StrategyConfiguration, *, candles, trading_dates, config,
    settings: Settings | None = None, parameter_selector=None, option_quote_provider=None,
    dataset_declaration=None,
) -> dict[str, Any]:
    """Revalidate the snapshot, then delegate exclusively to causal historical replay."""
    from app.trading.walk_forward import run_causal_walk_forward_validation

    if parameter_selector is not None:
        raise ValueError("UNBOUND_PARAMETER_SELECTOR: create a content-addressed configuration variant")
    spec = validate_configuration_execution(configuration, config=config, settings=settings)
    payload = configuration.to_dict()
    report = run_causal_walk_forward_validation(
        spec, candles=candles, trading_dates=trading_dates, config=config, settings=settings,
        dataset_declaration=dataset_declaration,
        parameter_selector=parameter_selector, option_quote_provider=option_quote_provider,
    )
    evidence = report.to_dict()
    integrity = evidence.pop("dataset_integrity", None)
    evidence["engine_report_hash"] = evidence.pop("report_hash")
    evidence["validation_scope"] = "DEVELOPMENT_ONLY"
    evidence["paper_promotion_eligible"] = False
    evidence["data_fidelity_and_provenance"]["paper_promotion_eligible"] = False
    evidence["failed_validation_criteria"].append("FINAL_HOLDOUT_NOT_INDEPENDENTLY_EVALUATED")
    if evidence["strategy_state"] == "INDEPENDENTLY_VALIDATED":
        evidence["strategy_state"] = "BACKTEST_ELIGIBLE"
    evidence["report_hash"] = sha256_hex(canonical_json(evidence))
    return {**({"development_integrity": integrity} if integrity is not None else {}),
            "configuration_sha256": configuration.sha256, "execution_mode": payload["execution_mode"],
            "report": evidence, "paper_approved": False, "live_enabled": False}


def validate_configuration_execution(configuration, *, config, settings=None):
    """Shared preflight; performs no candle inspection or persistent writes."""
    payload = configuration.to_dict()
    fresh = create_configuration(
        payload["submitted_markdown"], original_source=payload["source"]["text"],
        source_type=payload["source"]["type"], source_ref=payload["source"]["ref"],
        strategy_id=payload["strategy_id"], strategy_version=payload["strategy_version"],
        execution_mode=payload["execution_mode"], settings=settings,
        parent_sha256=payload["parent_configuration_sha256"],
    )
    if fresh.sha256 != configuration.sha256:
        raise ValueError("CONFIGURATION_BINDING_MISMATCH")
    if payload["execution_status"] == "NOT_EXECUTABLE_EXACTLY":
        raise ValueError("NOT_EXECUTABLE_EXACTLY")
    required_tier = {"historical_replay": "TIER_1_VERIFIED",
                     "historical_synthetic": "TIER_2_SYNTHETIC"}[payload["execution_mode"]]
    if config.fidelity_tier != required_tier:
        raise ValueError("EXECUTION_MODE_FIDELITY_MISMATCH")
    spec = normalize_markdown(
        payload["submitted_markdown"], strategy_id=payload["strategy_id"],
        strategy_version=payload["strategy_version"], source_sha256=payload["source"]["sha256"],
    )
    if spec.rules_v2.timeframes.primary != "5m":
        raise ValueError("UNSUPPORTED_CAUSAL_ADAPTER: use the gated EOD review for daily rules")
    return spec
