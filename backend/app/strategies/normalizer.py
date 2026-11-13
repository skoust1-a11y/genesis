"""Deterministic Strategy Normalizer (`strategy-normalizer-v2`).

Transforms existing strategy representations (`StrategyDocument`, markdown/frontmatter,
registered `scanner-v0` families, and frozen benchmarks) into `StrategyRulesV2` without
calling an LLM or inferring missing financial, entry, exit, stop-loss, target, instrument,
timeframe, or execution rules.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import ValidationError

from app.db.hashing import canonical_json, normalize_json, sha256_hex
from app.strategies.rules import (
    INTRADAY_TIMEFRAMES,
    STRATEGY_RULES_V2_VERSION,
    SUPPORTED_TIMEFRAMES,
    EntryBlackout,
    EntryCondition,
    ExitRule,
    IndicatorDependency,
    Instrument,
    IntradayEntryPredicate,
    ParameterDef,
    PartialExitRule,
    PositionSizingSpec,
    ProvenanceSpec,
    RiskRules,
    SessionWindowSpec,
    StopLossSpec,
    StrategyRules,
    StrategyRulesV2,
    TargetSpec,
    TimeframeSpec,
    _derive_stop_and_target_from_exits,
    _format_error,
    rules_sha256,
)
from app.strategies.schema import StrategyDocument, parse_strategy_markdown

NORMALIZER_VERSION = "strategy-normalizer-v3"
FROZEN_NORMALIZER_VERSION = "strategy-normalizer-v2"

StrategyCategoryClass = Literal[
    "IMPLEMENTED",
    "EXTRACTED_CANDIDATE",
    "RESEARCH_HYPOTHESIS",
    "INCOMPLETE_SPECIFICATION",
    "FROZEN_BENCHMARK",
    "VALIDATED",
]

PromotionState = Literal[
    "EXTRACTED",
    "QUARANTINED",
    "NORMALIZED",
    "RESEARCH_CANDIDATE",
    "FROZEN_BENCHMARK",
    "VALIDATED",
]

StrategyLifecycleState = Literal[
    "QUARANTINED",
    "FROZEN_BENCHMARK",
    "STRUCTURALLY_COMPLETE",
    "BACKTEST_ELIGIBLE",
    "INDEPENDENTLY_VALIDATED",
    "PAPER_APPROVED",
]

ValidationFidelityTier = Literal["TIER_1_VERIFIED", "TIER_2_SYNTHETIC"]

_ASSUMED_RULE_MARKERS = (
    "assumed, not in the source",
    "assumed:",
    "assumed ",
    "not in the source",
    "not in source",
    "llm assumption",
    "inferred",
    "fabricated",
)

_MANDATORY_GAP_MARKERS = (
    "missing",
    "unspecified",
    "not specified",
    "not quantified",
    "needs specification",
    "ambiguous",
    "unknown",
    "undefined",
    "unresolved",
    "mandatory",
    "tbd",
    "todo",
    "which timeframe",
    "which index",
    "which underlying",
    "what stop",
    "what target",
    "what entry",
    "what exit",
    "stop loss",
    "stop-loss",
    "profit target",
    "entry condition",
    "entry trigger",
    "exit rule",
    "exit condition",
    "timeframe",
    "instrument",
    "position size",
    "lot size",
)


def is_mandatory_or_assumed_gap(gap_text: str) -> tuple[bool, str]:
    """Classify a gap line as an assumed/ambiguous extracted rule or unresolved mandatory rule."""
    low = gap_text.strip().lower()
    if not low:
        return False, "EMPTY"
    if any(m in low for m in _ASSUMED_RULE_MARKERS):
        return True, f"AMBIGUOUS_EXTRACTED_RULE: {gap_text.strip()}"
    if any(m in low for m in _MANDATORY_GAP_MARKERS) or low.endswith("?"):
        return True, f"UNRESOLVED_MANDATORY_GAP: {gap_text.strip()}"
    return False, f"OPEN_GAP: {gap_text.strip()}"


@dataclass(frozen=True)
class NormalizedStrategySpec:
    normalizer_version: str
    strategy_id: str
    strategy_version: str
    name: str
    category_class: StrategyCategoryClass
    promotion_state: PromotionState
    rules_v2: StrategyRulesV2 | None
    legacy_rules: StrategyRules | None
    quarantined: bool
    quarantine_reasons: tuple[str, ...]
    unresolved_gaps: tuple[str, ...]
    validation_errors: tuple[str, ...]
    simulatable: bool
    paper_eligible: bool
    promotable: bool
    source_references: tuple[str, ...]
    provenance: dict[str, Any]
    content_sha256: str
    rules_sha256: str | None
    rules_v2_sha256: str | None
    parameter_hash: str | None
    spec_sha256: str
    provenance_sha256: str = ""
    structurally_complete: bool = False
    backtest_eligible: bool = False
    independently_validated: bool = False
    paper_approved: bool = False
    strategy_state: StrategyLifecycleState = "QUARANTINED"
    validation_fidelity_tier: str | None = None

    def validation_binding(self) -> dict[str, Any]:
        """Independent cryptographic binding of provenance, normalizer version, rules hash, and parameter hash."""
        binding_payload = {
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "normalizer_version": self.normalizer_version,
            "source_provenance": dict(self.provenance),
            "source_references": list(self.source_references),
            "provenance_sha256": self.provenance_sha256,
            "content_sha256": self.content_sha256,
            "rules_sha256": self.rules_sha256,
            "rules_v2_sha256": self.rules_v2_sha256,
            "parameter_hash": self.parameter_hash,
            "spec_sha256": self.spec_sha256,
        }
        binding_hash = sha256_hex(canonical_json(normalize_json(binding_payload)))
        return {**binding_payload, "binding_sha256": binding_hash}

    def to_dict(self) -> dict[str, Any]:
        return {
            "normalizer_version": self.normalizer_version,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "name": self.name,
            "category_class": self.category_class,
            "promotion_state": self.promotion_state,
            "strategy_state": self.strategy_state,
            "quarantined": self.quarantined,
            "quarantine_reasons": list(self.quarantine_reasons),
            "unresolved_gaps": list(self.unresolved_gaps),
            "validation_errors": list(self.validation_errors),
            "structurally_complete": self.structurally_complete,
            "backtest_eligible": self.backtest_eligible,
            "independently_validated": self.independently_validated,
            "paper_approved": self.paper_approved,
            "validation_fidelity_tier": self.validation_fidelity_tier,
            "simulatable": self.simulatable,
            "paper_eligible": self.paper_eligible,
            "promotable": self.promotable,
            "source_references": list(self.source_references),
            "provenance": dict(self.provenance),
            "provenance_sha256": self.provenance_sha256,
            "content_sha256": self.content_sha256,
            "rules_sha256": self.rules_sha256,
            "rules_v2_sha256": self.rules_v2_sha256,
            "parameter_hash": self.parameter_hash,
            "spec_sha256": self.spec_sha256,
            "validation_binding": self.validation_binding(),
            "rules_v2": self.rules_v2.model_dump(mode="json") if self.rules_v2 is not None else None,
        }


def _extract_parameters(
    frontmatter: Mapping[str, Any],
    raw_rules: Mapping[str, Any] | None,
) -> tuple[dict[str, ParameterDef], list[str]]:
    raw_params = None
    if isinstance(raw_rules, Mapping) and "parameters" in raw_rules:
        raw_params = raw_rules.get("parameters")
    elif "parameters" in frontmatter:
        raw_params = frontmatter.get("parameters")

    if raw_params is None:
        return {}, []
    if not isinstance(raw_params, Mapping):
        return {}, ["INVALID_PARAMETER_DEFINITIONS: parameters must be a mapping"]

    parsed: dict[str, ParameterDef] = {}
    errors: list[str] = []
    for key, val in raw_params.items():
        if not isinstance(key, str) or not key.strip():
            errors.append("INVALID_PARAMETER_DEFINITIONS: parameter key must be a non-empty string")
            continue
        if not isinstance(val, Mapping):
            errors.append(f"INVALID_PARAMETER_DEFINITIONS: parameter {key!r} must be a mapping")
            continue
        payload = dict(val)
        payload.setdefault("name", key)
        try:
            parsed[key] = ParameterDef.model_validate(payload)
        except ValidationError as exc:
            for err in exc.errors():
                msg = err["msg"].removeprefix("Value error, ")
                errors.append(f"INVALID_PARAMETER_DEFINITIONS: parameter {key!r}: {msg}")
    return parsed, errors


def _synthesize_default_parameters(instrument: Instrument, risk: RiskRules) -> dict[str, ParameterDef]:
    params: dict[str, ParameterDef] = {
        "max_lots": ParameterDef(
            name="max_lots",
            type="int",
            value=int(risk.max_lots),
            min_value=1.0,
            description="Maximum lots per position",
        ),
        "max_daily_loss_pct": ParameterDef(
            name="max_daily_loss_pct",
            type="float",
            value=float(risk.max_daily_loss_pct),
            min_value=0.01,
            max_value=100.0,
            description="Daily loss cutoff (% of capital)",
        ),
    }
    if instrument.short_distance is not None:
        params["short_distance"] = ParameterDef(
            name="short_distance",
            type="float",
            value=float(instrument.short_distance),
            min_value=1.0,
            description="Short strike distance from spot",
        )
    if instrument.wing_width is not None:
        params["wing_width"] = ParameterDef(
            name="wing_width",
            type="float",
            value=float(instrument.wing_width),
            min_value=1.0,
            description="Spread / condor wing width",
        )
    if instrument.strike_offset is not None:
        params["strike_offset"] = ParameterDef(
            name="strike_offset",
            type="float",
            value=float(instrument.strike_offset),
            description="Debit option strike offset from ATM",
        )
    return params


def _resolve_timeframes(
    frontmatter: Mapping[str, Any],
    raw_rules: Mapping[str, Any] | None,
) -> tuple[TimeframeSpec | None, list[str]]:
    tf_raw = None
    if isinstance(raw_rules, Mapping) and "timeframes" in raw_rules:
        tf_raw = raw_rules.get("timeframes")
    elif isinstance(raw_rules, Mapping) and "timeframe" in raw_rules:
        tf_raw = raw_rules.get("timeframe")
    elif "timeframes" in frontmatter:
        tf_raw = frontmatter.get("timeframes")
    elif "timeframe" in frontmatter:
        tf_raw = frontmatter.get("timeframe")

    if tf_raw is None:
        return None, ["MISSING_TIMEFRAME: an explicit strategy timeframe is required"]

    if isinstance(tf_raw, str):
        tf_clean = tf_raw.strip()
        if not tf_clean:
            return None, ["MISSING_TIMEFRAME: timeframe cannot be blank"]
        if tf_clean not in SUPPORTED_TIMEFRAMES:
            return None, [f"INVALID_TIMEFRAME: unsupported timeframe {tf_clean!r}"]
        return TimeframeSpec(primary=tf_clean), []  # type: ignore[arg-type]

    if isinstance(tf_raw, Mapping):
        try:
            return TimeframeSpec.model_validate(tf_raw), []
        except ValidationError as exc:
            return None, [f"INVALID_TIMEFRAME: {_format_error(e)}" for e in exc.errors()]

    return None, [f"INVALID_TIMEFRAME: unsupported timeframe representation {type(tf_raw).__name__}"]


def _extract_provenance(
    doc: StrategyDocument,
    *,
    source_sha256: str | None = None,
    ingest_run_id: str | None = None,
) -> tuple[ProvenanceSpec, tuple[str, ...]]:
    fm = doc.frontmatter
    refs: list[str] = []
    for key in ("source_youtube", "source_url", "source_ref", "source_file"):
        val = fm.get(key)
        if isinstance(val, str) and val.strip():
            refs.append(val.strip())
    for c in doc.source_citations:
        if c not in refs:
            refs.append(c)

    has_assumed = any(is_mandatory_or_assumed_gap(g)[1].startswith("AMBIGUOUS_EXTRACTED_RULE") for g in doc.gaps)
    source_type = str(fm.get("source_type") or ("youtube" if fm.get("source_youtube") else "markdown")).strip()
    extraction_mode: Literal["deterministic", "llm_extracted", "manual", "frozen_benchmark"] = (
        "llm_extracted" if (has_assumed or doc.source_citations or fm.get("source_youtube")) else "deterministic"
    )
    prov = ProvenanceSpec(
        source_type=source_type or "markdown",
        source_ref=refs[0] if refs else None,
        source_sha256=source_sha256 or (str(fm["source_sha256"]) if fm.get("source_sha256") else None),
        ingest_run_id=ingest_run_id or (str(fm["ingest_run_id"]) if fm.get("ingest_run_id") else None),
        author_hints_applied=bool(fm.get("author_hints_applied", False)),
        extraction_mode=extraction_mode,
        citations=list(doc.source_citations),
    )
    return prov, tuple(refs)


def normalize_markdown(
    markdown_text: str,
    *,
    strategy_id: str | None = None,
    strategy_version: str = "v1",
    source_sha256: str | None = None,
    ingest_run_id: str | None = None,
    strict_gaps: bool = False,
    has_validated_report: bool = False,
    validation_fidelity_tier: ValidationFidelityTier | None = None,
    paper_approval_granted: bool = False,
) -> NormalizedStrategySpec:
    doc = parse_strategy_markdown(markdown_text)
    return normalize_strategy_document(
        doc,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        source_sha256=source_sha256,
        ingest_run_id=ingest_run_id,
        strict_gaps=strict_gaps,
        has_validated_report=has_validated_report,
        validation_fidelity_tier=validation_fidelity_tier,
        paper_approval_granted=paper_approval_granted,
    )


def normalize_strategy_document(
    doc: StrategyDocument,
    *,
    strategy_id: str | None = None,
    strategy_version: str = "v1",
    source_sha256: str | None = None,
    ingest_run_id: str | None = None,
    strict_gaps: bool = False,
    has_validated_report: bool = False,
    validation_fidelity_tier: ValidationFidelityTier | None = None,
    paper_approval_granted: bool = False,
) -> NormalizedStrategySpec:
    """Deterministically normalize a `StrategyDocument` into `NormalizedStrategySpec`.

    Never infers missing rules or defaults missing mandatory fields.
    """
    fm = doc.frontmatter
    resolved_id = str(strategy_id or fm.get("strategy_id") or fm.get("slug") or doc.name or "unnamed-strategy").strip()
    resolved_version = str(fm.get("strategy_version") or fm.get("version") or strategy_version or "v1").strip()
    content_hash = sha256_hex(doc.markdown)
    prov_spec, source_refs = _extract_provenance(doc, source_sha256=source_sha256, ingest_run_id=ingest_run_id)

    quarantine_reasons: list[str] = []
    validation_errors: list[str] = []
    if doc.source_verification_required or fm.get("source_verification_required"):
        quarantine_reasons.append("UNVERIFIED_SOURCE_RULE_COVERAGE: source extraction requires independent verification")
    if (doc.entry or doc.exit or doc.risk) and fm.get("rules_authority") != "structured":
        quarantine_reasons.append(
            "UNVERIFIED_PROSE_RULE_COVERAGE: submit authoritative structured rules or independently verify prose-to-rule coverage"
        )

    # 1. Inspect prose sections and open gaps
    unresolved_gaps: list[str] = [g.strip() for g in doc.gaps if g.strip()]
    fm_gaps = fm.get("unresolved_gaps")
    if isinstance(fm_gaps, list):
        for g in fm_gaps:
            if isinstance(g, str) and g.strip() and g.strip() not in unresolved_gaps:
                unresolved_gaps.append(g.strip())

    for gap in unresolved_gaps:
        is_mandatory, tag = is_mandatory_or_assumed_gap(gap)
        if is_mandatory or strict_gaps:
            quarantine_reasons.append(tag)

    # 2. Check if rules_v2 is directly supplied in frontmatter
    if "rules_v2" in fm:
        raw_v2 = fm.get("rules_v2")
        if not isinstance(raw_v2, Mapping):
            quarantine_reasons.append("RULES_VALIDATION_ERROR: rules_v2 must be a mapping")
            validation_errors.append("rules_v2: must be a mapping")
            v2_obj = None
        else:
            payload = dict(raw_v2)
            payload.setdefault("strategy_id", resolved_id)
            payload.setdefault("strategy_version", resolved_version)
            if unresolved_gaps and "unresolved_gaps" not in payload:
                payload["unresolved_gaps"] = list(unresolved_gaps)
            try:
                v2_obj = StrategyRulesV2.model_validate(payload)
            except ValidationError as exc:
                v2_obj = None
                for err in exc.errors():
                    formatted = _format_error(err).replace("rules.", "rules_v2.", 1)
                    validation_errors.append(formatted)
                    loc = ".".join(str(p) for p in err["loc"])
                    if "instrument" in loc:
                        quarantine_reasons.append(f"MISSING_INSTRUMENT: {formatted}")
                    elif "timeframe" in loc or "session_window" in loc:
                        quarantine_reasons.append(f"MISSING_TIMEFRAME: {formatted}")
                    elif "entry" in loc:
                        quarantine_reasons.append(f"MISSING_ENTRY_CONDITIONS: {formatted}")
                    elif "exit" in loc or "stop_loss" in loc or "target" in loc:
                        quarantine_reasons.append(f"MISSING_EXIT_OR_STOP_RULES: {formatted}")
                    elif "parameter" in loc:
                        quarantine_reasons.append(f"INVALID_PARAMETER_DEFINITIONS: {formatted}")
                    else:
                        quarantine_reasons.append(f"RULES_VALIDATION_ERROR: {formatted}")
        if v2_obj is not None and v2_obj.unresolved_gaps:
            for g in v2_obj.unresolved_gaps:
                if g not in unresolved_gaps:
                    unresolved_gaps.append(g)
                is_mand, tag = is_mandatory_or_assumed_gap(g)
                if (is_mand or strict_gaps) and tag not in quarantine_reasons:
                    quarantine_reasons.append(tag)
        legacy = v2_obj.to_legacy_rules() if v2_obj is not None else None
        return _finalize_normalized_spec(
            doc=doc,
            strategy_id=resolved_id,
            strategy_version=resolved_version,
            rules_v2=v2_obj,
            legacy_rules=legacy,
            raw_rules_for_hash=fm.get("rules") if "rules" in fm else raw_v2,
            quarantine_reasons=quarantine_reasons,
            unresolved_gaps=unresolved_gaps,
            validation_errors=validation_errors,
            source_refs=source_refs,
            prov_spec=prov_spec,
            content_hash=content_hash,
            has_validated_report=has_validated_report,
            validation_fidelity_tier=validation_fidelity_tier,
            paper_approval_granted=paper_approval_granted,
        )

    # 3. Validate legacy `rules:` block + prose sections without inventing missing rules
    raw_rules = fm.get("rules")
    if not doc.entry:
        quarantine_reasons.append("MISSING_ENTRY_CONDITIONS: ## Entry section is empty")
    if not doc.exit:
        quarantine_reasons.append("MISSING_EXIT_OR_STOP_RULES: ## Exit section is empty")
    if not doc.risk:
        quarantine_reasons.append("MISSING_RISK_RULES: ## Risk section is empty")

    if raw_rules is None:
        quarantine_reasons.append("MISSING_MACHINE_RULES: rules: block missing in frontmatter")
        validation_errors.append("rules: block missing")
        return _finalize_normalized_spec(
            doc=doc,
            strategy_id=resolved_id,
            strategy_version=resolved_version,
            rules_v2=None,
            legacy_rules=None,
            raw_rules_for_hash=None,
            quarantine_reasons=quarantine_reasons,
            unresolved_gaps=unresolved_gaps,
            validation_errors=validation_errors,
            source_refs=source_refs,
            prov_spec=prov_spec,
            content_hash=content_hash,
            has_validated_report=has_validated_report,
            validation_fidelity_tier=validation_fidelity_tier,
            paper_approval_granted=paper_approval_granted,
        )

    if not isinstance(raw_rules, Mapping) or not raw_rules:
        quarantine_reasons.append("MISSING_MACHINE_RULES: rules: block is empty or not a mapping")
        validation_errors.append("rules: block is empty or invalid")
        return _finalize_normalized_spec(
            doc=doc,
            strategy_id=resolved_id,
            strategy_version=resolved_version,
            rules_v2=None,
            legacy_rules=None,
            raw_rules_for_hash=raw_rules,
            quarantine_reasons=quarantine_reasons,
            unresolved_gaps=unresolved_gaps,
            validation_errors=validation_errors,
            source_refs=source_refs,
            prov_spec=prov_spec,
            content_hash=content_hash,
            has_validated_report=has_validated_report,
            validation_fidelity_tier=validation_fidelity_tier,
            paper_approval_granted=paper_approval_granted,
        )

    # Check explicit sub-blocks in raw_rules before Pydantic validation for precise quarantine codes
    raw_inst = raw_rules.get("instrument")
    if not isinstance(raw_inst, Mapping) or not raw_inst.get("underlying") or not raw_inst.get("structure"):
        quarantine_reasons.append("MISSING_INSTRUMENT: rules.instrument requires underlying and structure")
    raw_entry = raw_rules.get("entry")
    raw_intraday_entry = raw_rules.get("intraday_entry")
    if not raw_entry and not raw_intraday_entry:
        quarantine_reasons.append("MISSING_ENTRY_CONDITIONS: rules.entry is missing or empty")
    raw_exit = raw_rules.get("exit")
    if not raw_exit and not (raw_rules.get("stop_loss") and raw_rules.get("target")):
        quarantine_reasons.append("MISSING_EXIT_OR_STOP_RULES: rules.exit (stop-loss and target) is missing or empty")
    raw_risk = raw_rules.get("risk")
    if not isinstance(raw_risk, Mapping):
        quarantine_reasons.append("MISSING_RISK_RULES: rules.risk is missing")

    tf_spec, tf_errors = _resolve_timeframes(fm, raw_rules)
    for err in tf_errors:
        quarantine_reasons.append(err)
        validation_errors.append(err)

    explicit_params, param_errors = _extract_parameters(fm, raw_rules)
    for err in param_errors:
        quarantine_reasons.append(err)
        validation_errors.append(err)

    # Legacy conversion cannot represent execution extensions without losing rules.
    supported_legacy_keys = {"instrument", "entry", "exit", "risk", "entry_blackout",
                             "timeframe", "timeframes", "parameters"}
    for key in sorted(set(raw_rules) - supported_legacy_keys):
        reason = f"UNSUPPORTED_LEGACY_FIELD: rules.{key}; submit a complete rules_v2 specification"
        quarantine_reasons.append(reason)
        validation_errors.append(reason)

    legacy_candidate = {
        k: v
        for k, v in raw_rules.items()
        if k in ("instrument", "entry", "exit", "risk", "entry_blackout")
    }
    legacy_rules: StrategyRules | None = None
    try:
        legacy_rules = StrategyRules.model_validate(legacy_candidate)
    except ValidationError as exc:
        for err in exc.errors():
            formatted = _format_error(err)
            validation_errors.append(formatted)
            loc = ".".join(str(p) for p in err["loc"])
            if "instrument" in loc:
                quarantine_reasons.append(f"MISSING_INSTRUMENT: {formatted}")
            elif "entry" in loc:
                quarantine_reasons.append(f"MISSING_ENTRY_CONDITIONS: {formatted}")
            elif "exit" in loc or "stop_loss" in formatted or "profit_target" in formatted or "exit needs" in formatted:
                quarantine_reasons.append(f"MISSING_EXIT_OR_STOP_RULES: {formatted}")
            elif "risk" in loc:
                quarantine_reasons.append(f"MISSING_RISK_RULES: {formatted}")
            else:
                quarantine_reasons.append(f"RULES_VALIDATION_ERROR: {formatted}")

    rules_v2_obj: StrategyRulesV2 | None = None
    if legacy_rules is not None and tf_spec is not None and not param_errors:
        stop_spec, target_spec = _derive_stop_and_target_from_exits(legacy_rules.exit)
        params = explicit_params or _synthesize_default_parameters(legacy_rules.instrument, legacy_rules.risk)
        session_win: SessionWindowSpec | None = None
        raw_session = raw_rules.get("session_window") or fm.get("session_window")
        if raw_session is not None:
            try:
                session_win = SessionWindowSpec.model_validate(raw_session)
            except ValidationError as exc:
                for err in exc.errors():
                    msg = _format_error(err)
                    quarantine_reasons.append(f"INVALID_SESSION_WINDOW: {msg}")
                    validation_errors.append(msg)
        elif tf_spec.primary in INTRADAY_TIMEFRAMES:
            quarantine_reasons.append(
                f"MISSING_SESSION_WINDOW: session_window is required for intraday timeframe {tf_spec.primary}"
            )
            validation_errors.append("rules.session_window: required for intraday timeframe")

        indicators: list[IndicatorDependency] = []
        used_fields = {c.field for c in legacy_rules.entry}
        if "iv_rank" in used_fields:
            indicators.append(IndicatorDependency(name="iv_rank", timeframe=tf_spec.primary))
        if "vix" in used_fields:
            indicators.append(IndicatorDependency(name="vix", timeframe=tf_spec.primary))
        if used_fields & {"pct_from_prev_high", "pct_from_prev_low", "pct_high_from_prev_high", "pct_low_from_prev_low"}:
            indicators.append(IndicatorDependency(name="pdh_pdl_pdc", timeframe="1d"))

        data_deps = ["nse_option_chain_eod", "india_vix_eod"]
        if legacy_rules.entry_blackout is not None:
            data_deps.append("nse_event_calendar")

        if not validation_errors:
            try:
                rules_v2_obj = StrategyRulesV2(
                    strategy_id=resolved_id,
                    strategy_version=resolved_version,
                    market="nse_options",
                    timeframes=tf_spec,
                    instrument=legacy_rules.instrument,
                    entry=legacy_rules.entry,
                    exit=legacy_rules.exit,
                    stop_loss=stop_spec,
                    target=target_spec,
                    risk=legacy_rules.risk,
                    position_sizing=PositionSizingSpec(
                        risk_policy_id="paper-risk-v2",
                        capital_inr=500000.0,
                        max_lots=legacy_rules.risk.max_lots,
                        lot_step_mode="fixed_lots",
                    ),
                    session_window=session_win,
                    indicators=indicators,
                    data_dependencies=data_deps,
                    entry_blackout=legacy_rules.entry_blackout,
                    parameters=params,
                    provenance=prov_spec,
                    unresolved_gaps=list(unresolved_gaps),
                )
            except ValidationError as exc:
                for err in exc.errors():
                    msg = _format_error(err)
                    validation_errors.append(msg)
                    quarantine_reasons.append(f"RULES_VALIDATION_ERROR: {msg}")

    return _finalize_normalized_spec(
        doc=doc,
        strategy_id=resolved_id,
        strategy_version=resolved_version,
        rules_v2=rules_v2_obj,
        legacy_rules=legacy_rules,
        raw_rules_for_hash=raw_rules,
        quarantine_reasons=quarantine_reasons,
        unresolved_gaps=unresolved_gaps,
        validation_errors=validation_errors,
        source_refs=source_refs,
        prov_spec=prov_spec,
        content_hash=content_hash,
        has_validated_report=has_validated_report,
        validation_fidelity_tier=validation_fidelity_tier,
        paper_approval_granted=paper_approval_granted,
    )


def _finalize_normalized_spec(
    *,
    doc: StrategyDocument,
    strategy_id: str,
    strategy_version: str,
    rules_v2: StrategyRulesV2 | None,
    legacy_rules: StrategyRules | None,
    raw_rules_for_hash: Any,
    quarantine_reasons: list[str],
    unresolved_gaps: list[str],
    validation_errors: list[str],
    source_refs: tuple[str, ...],
    prov_spec: ProvenanceSpec,
    content_hash: str,
    has_validated_report: bool,
    validation_fidelity_tier: ValidationFidelityTier | None = None,
    paper_approval_granted: bool = False,
) -> NormalizedStrategySpec:
    dedup_reasons: list[str] = []
    for r in quarantine_reasons:
        if r not in dedup_reasons:
            dedup_reasons.append(r)

    quarantined = bool(dedup_reasons) or rules_v2 is None
    has_any_open_gap = bool(unresolved_gaps)

    # Synthetic Tier 2 evidence never qualifies a strategy as independently validated for paper promotion
    verified_validation = bool(has_validated_report and validation_fidelity_tier == "TIER_1_VERIFIED")

    # Classification into canonical Stage B2 catalog taxonomy
    fm_category = str(doc.frontmatter.get("catalog_category") or "").strip().upper()
    if fm_category == "FROZEN_BENCHMARK":
        category_class: StrategyCategoryClass = "FROZEN_BENCHMARK"
    elif fm_category == "RESEARCH_HYPOTHESIS":
        category_class = "RESEARCH_HYPOTHESIS"
    elif any(r.startswith("AMBIGUOUS_EXTRACTED_RULE") for r in dedup_reasons) or prov_spec.extraction_mode == "llm_extracted":
        if quarantined or has_any_open_gap:
            category_class = "EXTRACTED_CANDIDATE"
        else:
            category_class = "VALIDATED" if verified_validation else "IMPLEMENTED"
    elif rules_v2 is None or quarantined:
        category_class = "INCOMPLETE_SPECIFICATION"
    elif verified_validation and not has_any_open_gap:
        category_class = "VALIDATED"
    else:
        category_class = "IMPLEMENTED"

    if category_class == "FROZEN_BENCHMARK":
        promotion_state: PromotionState = "FROZEN_BENCHMARK"
    elif quarantined:
        promotion_state = "EXTRACTED" if category_class == "EXTRACTED_CANDIDATE" else "QUARANTINED"
    elif verified_validation and not has_any_open_gap:
        promotion_state = "VALIDATED"
    else:
        promotion_state = "NORMALIZED"

    structurally_complete = (not quarantined) and (not has_any_open_gap) and (rules_v2 is not None)
    simulatable = (not quarantined) and (legacy_rules is not None or rules_v2 is not None)
    backtest_eligible = simulatable
    independently_validated = structurally_complete and backtest_eligible and verified_validation
    # Paper admission and live/paper promotion require zero open gaps AND zero quarantine reasons;
    # Tier 2 synthetic simulation explicitly blocks paper promotion eligibility.
    paper_eligible = structurally_complete and (validation_fidelity_tier != "TIER_2_SYNTHETIC")
    promotable = paper_eligible and independently_validated
    paper_approved = promotable and bool(paper_approval_granted)

    if category_class == "FROZEN_BENCHMARK":
        strategy_state: StrategyLifecycleState = "FROZEN_BENCHMARK"
    elif quarantined or not structurally_complete:
        strategy_state = "QUARANTINED"
    elif paper_approved:
        strategy_state = "PAPER_APPROVED"
    elif independently_validated:
        strategy_state = "INDEPENDENTLY_VALIDATED"
    elif backtest_eligible:
        strategy_state = "BACKTEST_ELIGIBLE"
    else:
        strategy_state = "STRUCTURALLY_COMPLETE"

    legacy_hash = rules_sha256(raw_rules_for_hash) if raw_rules_for_hash is not None else None
    v2_hash = rules_v2.rules_v2_sha256() if rules_v2 is not None else None
    param_hash = rules_v2.parameter_hash() if rules_v2 is not None else None
    prov_dict = prov_spec.model_dump(mode="json")
    prov_hash = sha256_hex(canonical_json(normalize_json(prov_dict)))

    spec_payload = {
        "normalizer_version": NORMALIZER_VERSION,
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "category_class": category_class,
        "promotion_state": promotion_state,
        "quarantined": quarantined,
        "quarantine_reasons": dedup_reasons,
        "unresolved_gaps": unresolved_gaps,
        "content_sha256": content_hash,
        "rules_sha256": legacy_hash,
        "rules_v2_sha256": v2_hash,
        "parameter_hash": param_hash,
    }
    spec_hash = sha256_hex(canonical_json(normalize_json(spec_payload)))

    return NormalizedStrategySpec(
        normalizer_version=NORMALIZER_VERSION,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        name=doc.name,
        category_class=category_class,
        promotion_state=promotion_state,
        rules_v2=rules_v2,
        legacy_rules=legacy_rules,
        quarantined=quarantined,
        quarantine_reasons=tuple(dedup_reasons),
        unresolved_gaps=tuple(unresolved_gaps),
        validation_errors=tuple(validation_errors),
        simulatable=simulatable,
        paper_eligible=paper_eligible,
        promotable=promotable,
        source_references=source_refs,
        provenance=prov_dict,
        content_sha256=content_hash,
        rules_sha256=legacy_hash,
        rules_v2_sha256=v2_hash,
        parameter_hash=param_hash,
        spec_sha256=spec_hash,
        provenance_sha256=prov_hash,
        structurally_complete=structurally_complete,
        backtest_eligible=backtest_eligible,
        independently_validated=independently_validated,
        paper_approved=paper_approved,
        strategy_state=strategy_state,
        validation_fidelity_tier=validation_fidelity_tier,
    )


_SCANNER_V0_FAMILY_SPECS: dict[str, tuple[str, str, dict[str, float | int | str | bool]]] = {
    "LIQ_SWEEP_PDHL": (
        "Liquidity Sweep (PDH/PDL)",
        "pdh_pdl_sweep",
        {"zone_buffer_pct": 0.10, "ema_period": 15},
    ),
    "RETEST_PD": (
        "Breakout & Retest (Previous Day)",
        "pdh_pdl_sweep",
        {"retest_tolerance_pct": 0.10, "ema_period": 15},
    ),
    "RETEST_OR30": (
        "Breakout & Retest (OR-30)",
        "or30_breakout_retest",
        {"opening_range_minutes": 30, "ema_period": 15},
    ),
    "TRENDLINE_CHOCH": (
        "Trendline Reversal (CHoCH)",
        "trendline_breakout_3rd_touch",
        {"min_touches": 3, "min_r_squared": 0.85},
    ),
    "EQHL_SWEEP_INLINE": (
        "Equal Highs/Lows Sweep",
        "equal_hl_sweep",
        {"swing_lookback_bars": 5, "min_touches": 2},
    ),
    # Detector category aliases
    "PREVIOUS_DAY_LEVEL": (
        "Previous Day Level Liquidity",
        "pdh_pdl_sweep",
        {"zone_buffer_pct": 0.10, "ema_period": 15},
    ),
    "OPENING_RANGE_LIQUIDITY": (
        "Opening Range Liquidity (OR-30)",
        "or30_breakout_retest",
        {"opening_range_minutes": 30, "ema_period": 15},
    ),
    "INTRADAY_SWING_LIQUIDITY": (
        "Intraday Swing Liquidity",
        "equal_hl_sweep",
        {"swing_lookback_bars": 5, "min_touches": 2},
    ),
    "TRENDLINE_LIQUIDITY": (
        "Trendline Liquidity Breakout",
        "trendline_breakout_3rd_touch",
        {"min_touches": 3, "min_r_squared": 0.85},
    ),
    "CONSOLIDATION_LIQUIDITY": (
        "Consolidation Liquidity",
        "candle_body_ratio",
        {"min_bars": 8, "max_bars": 20},
    ),
    "TRAP_DETECTION": (
        "Trap Detection",
        "pdh_pdl_sweep",
        {"body_ratio_min": 0.65, "volume_spike_mult": 1.8},
    ),
}


def normalize_scanner_v0_family(family_id: str) -> NormalizedStrategySpec:
    """Deterministically normalize a registered `scanner-v0` intraday strategy family into `StrategyRulesV2`."""
    from app.trading.paper_registry import REGISTRY_HASH, RULE_SOURCE_HASHES, STRATEGY_VERSION

    fid = family_id.strip().upper()
    if fid not in _SCANNER_V0_FAMILY_SPECS:
        raise KeyError(f"Unknown scanner-v0 strategy family: {family_id!r}")
    label, pred_kind, pred_params = _SCANNER_V0_FAMILY_SPECS[fid]
    source_files = sorted(RULE_SOURCE_HASHES.keys())
    src_hash = REGISTRY_HASH
    params_defs = {
        k: ParameterDef(
            name=k,
            type="int" if isinstance(v, int) and not isinstance(v, bool) else "float",
            value=v,
            mutable_in_research=False,
        )
        for k, v in pred_params.items()
    }
    prov = ProvenanceSpec(
        source_type="code_pinned",
        source_ref=",".join(source_files),
        source_sha256=src_hash,
        extraction_mode="deterministic",
        citations=source_files,
    )
    rules_v2 = StrategyRulesV2(
        strategy_id=fid,
        strategy_version=STRATEGY_VERSION,
        market="nse_options",
        timeframes=TimeframeSpec(primary="5m", structure="15m", reference="1d"),
        instrument=Instrument(underlying="NIFTY", structure="long_call", strike_offset=0, lot_size=75),
        intraday_entry=[
            IntradayEntryPredicate(
                kind=pred_kind,  # type: ignore[arg-type]
                timeframe="5m",
                direction="BOTH",
                params=pred_params,
                requires_closed_bar=True,
            )
        ],
        stop_loss=StopLossSpec(mode="swing_extreme_buffer", value=2.0, min_points=10.0, max_points=30.0),
        target=TargetSpec(mode="r_multiple", value=2.0, allow_liquidity_hop=True),
        partial_exits=[PartialExitRule(at_r_multiple=1.0, book_fraction=0.5, move_stop_to="entry")],
        risk=RiskRules(
            max_lots=1,
            max_daily_loss_pct=3.0,
            max_open_positions=1,
            min_risk_reward=1.5,
            max_entries_per_day=3,
            max_losses_per_day=2,
        ),
        position_sizing=PositionSizingSpec(
            risk_policy_id="paper-risk-v2",
            capital_inr=50000.0,
            max_lots=1,
            lot_step_mode="risk_budget_lots",
        ),
        session_window=SessionWindowSpec(
            timezone="Asia/Kolkata",
            entry_start="09:30",
            entry_cutoff="14:45",
            square_off="15:15",
        ),
        indicators=[
            IndicatorDependency(name="ema", timeframe="5m", period=15),
            IndicatorDependency(name="vwap", timeframe="5m"),
            IndicatorDependency(name="pdh_pdl_pdc", timeframe="1d"),
            IndicatorDependency(name="or30", timeframe="5m"),
        ],
        data_dependencies=["nse_5m_ohlcv", "nse_1d_levels", "nse_option_chain_intraday"],
        parameters=params_defs,
        provenance=prov,
        unresolved_gaps=[],
    )
    v2_hash = rules_v2.rules_v2_sha256()
    p_hash = rules_v2.parameter_hash()
    spec_hash = sha256_hex(
        canonical_json(
            normalize_json(
                {
                    "normalizer_version": FROZEN_NORMALIZER_VERSION,
                    "strategy_id": fid,
                    "strategy_version": STRATEGY_VERSION,
                    "rules_v2_sha256": v2_hash,
                    "parameter_hash": p_hash,
                    "source_sha256": src_hash,
                }
            )
        )
    )
    prov_dict = prov.model_dump(mode="json")
    prov_hash = sha256_hex(canonical_json(normalize_json(prov_dict)))
    return NormalizedStrategySpec(
        normalizer_version=FROZEN_NORMALIZER_VERSION,
        strategy_id=fid,
        strategy_version=STRATEGY_VERSION,
        name=label,
        category_class="IMPLEMENTED",
        promotion_state="NORMALIZED",
        rules_v2=rules_v2,
        legacy_rules=None,
        quarantined=False,
        quarantine_reasons=(),
        unresolved_gaps=(),
        validation_errors=(),
        simulatable=True,
        paper_eligible=True,
        promotable=False,
        source_references=tuple(source_files),
        provenance=prov_dict,
        content_sha256=src_hash,
        rules_sha256=src_hash,
        rules_v2_sha256=v2_hash,
        parameter_hash=p_hash,
        spec_sha256=spec_hash,
        provenance_sha256=prov_hash,
        structurally_complete=True,
        backtest_eligible=True,
        independently_validated=False,
        paper_approved=False,
        strategy_state="BACKTEST_ELIGIBLE",
        validation_fidelity_tier=None,
    )


def normalize_pdc_15ema_v0_benchmark() -> NormalizedStrategySpec:
    """Normalize the frozen `PDC Zone + 15 EMA V0` benchmark.

    Preserves frozen V0 identity while explicitly quarantining it from live/paper promotion
    due to the documented Stage A card divergence (`DocumentsAndDiagrams/stage_a_strategy_compatibility.md`).
    """
    gap_reason = (
        "FROZEN_BENCHMARK_CARD_DIVERGENCE: scripts/backtest_pdc_15ema.py diverges from the frozen "
        "PDC Zone + 15 EMA V0 card on 7 rules and no card-faithful runtime function exists in the repo"
    )
    prov = ProvenanceSpec(
        source_type="frozen_benchmark",
        source_ref="backend/scripts/backtest_pdc_15ema.py",
        extraction_mode="frozen_benchmark",
        citations=[
            "backend/scripts/backtest_pdc_15ema.py",
            "DocumentsAndDiagrams/stage_a_strategy_compatibility.md",
        ],
    )
    rules_v2 = StrategyRulesV2(
        strategy_id="PDC_15EMA_V0",
        strategy_version="v0-frozen",
        market="nse_options",
        timeframes=TimeframeSpec(primary="5m", structure="15m", reference="1d"),
        instrument=Instrument(underlying="NIFTY", structure="long_call", strike_offset=0, lot_size=65),
        intraday_entry=[
            IntradayEntryPredicate(
                kind="pdc_zone_reaction",
                timeframe="5m",
                direction="BOTH",
                params={"zone_half_width_pct": 0.05, "ema_period": 15, "body_ratio_min": 0.55},
                requires_closed_bar=True,
            )
        ],
        stop_loss=StopLossSpec(mode="wick_touch", value=12.0, min_points=10.0, max_points=30.0),
        target=TargetSpec(mode="r_multiple", value=2.0, allow_liquidity_hop=False),
        risk=RiskRules(
            max_lots=1,
            max_daily_loss_pct=3.0,
            max_open_positions=1,
            min_risk_reward=2.0,
            max_entries_per_day=1,
        ),
        position_sizing=PositionSizingSpec(
            risk_policy_id="paper-risk-v2",
            capital_inr=50000.0,
            max_lots=1,
            lot_step_mode="fixed_lots",
        ),
        session_window=SessionWindowSpec(
            timezone="Asia/Kolkata",
            entry_start="09:15",
            entry_cutoff="10:30",
            square_off="15:15",
        ),
        indicators=[
            IndicatorDependency(name="ema", timeframe="5m", period=15),
            IndicatorDependency(name="pdh_pdl_pdc", timeframe="1d"),
        ],
        data_dependencies=["nse_5m_ohlcv", "nse_1d_levels"],
        parameters={
            "zone_half_width_pct": ParameterDef(
                name="zone_half_width_pct",
                type="float",
                value=0.05,
                min_value=0.01,
                max_value=0.50,
                mutable_in_research=False,
            ),
            "ema_period": ParameterDef(
                name="ema_period",
                type="int",
                value=15,
                min_value=5,
                max_value=50,
                mutable_in_research=False,
            ),
        },
        provenance=prov,
        unresolved_gaps=[gap_reason],
    )
    v2_hash = rules_v2.rules_v2_sha256()
    p_hash = rules_v2.parameter_hash()
    spec_hash = sha256_hex(
        canonical_json(
            normalize_json(
                {
                    "normalizer_version": FROZEN_NORMALIZER_VERSION,
                    "strategy_id": "PDC_15EMA_V0",
                    "strategy_version": "v0-frozen",
                    "rules_v2_sha256": v2_hash,
                    "parameter_hash": p_hash,
                }
            )
        )
    )
    prov_dict = prov.model_dump(mode="json")
    prov_hash = sha256_hex(canonical_json(normalize_json(prov_dict)))
    return NormalizedStrategySpec(
        normalizer_version=FROZEN_NORMALIZER_VERSION,
        strategy_id="PDC_15EMA_V0",
        strategy_version="v0-frozen",
        name="PDC Zone + 15 EMA (Frozen V0 Benchmark)",
        category_class="FROZEN_BENCHMARK",
        promotion_state="FROZEN_BENCHMARK",
        rules_v2=rules_v2,
        legacy_rules=None,
        quarantined=True,
        quarantine_reasons=(gap_reason,),
        unresolved_gaps=(gap_reason,),
        validation_errors=(),
        simulatable=False,
        paper_eligible=False,
        promotable=False,
        source_references=(
            "backend/scripts/backtest_pdc_15ema.py",
            "DocumentsAndDiagrams/stage_a_strategy_compatibility.md",
        ),
        provenance=prov_dict,
        content_sha256=v2_hash,
        rules_sha256=v2_hash,
        rules_v2_sha256=v2_hash,
        parameter_hash=p_hash,
        spec_sha256=spec_hash,
        provenance_sha256=prov_hash,
        structurally_complete=False,
        backtest_eligible=False,
        independently_validated=False,
        paper_approved=False,
        strategy_state="FROZEN_BENCHMARK",
        validation_fidelity_tier=None,
    )


def has_bound_oos_validation(payload: object, spec: NormalizedStrategySpec) -> bool:
    """Require explicit verified OOS evidence bound to this exact normalized version."""
    if not isinstance(payload, dict):
        return False
    wf = payload.get("walk_forward", payload)
    if not isinstance(wf, dict):
        return False
    provenance = wf.get("data_fidelity_and_provenance")
    binding = provenance.get("validation_binding") if isinstance(provenance, dict) else None
    return (
        wf.get("fidelity_tier") == "TIER_1_VERIFIED"
        and all(wf.get(key) is True for key in (
            "validation_passed", "paper_promotion_eligible", "verified_market_performance", "has_valid_oos_folds"))
        and isinstance(binding, dict)
        and binding == spec.validation_binding()
    )
