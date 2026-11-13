"""Deterministic capability checks at the EOD review boundary."""

from app.strategies.normalizer import NormalizedStrategySpec
from app.strategies.rules import _derive_stop_and_target_from_exits

EOD_ADAPTER_VERSION = "eod-exact-adapter-v1"


def eod_rejection_reasons(spec: NormalizedStrategySpec) -> list[dict[str, str]]:
    reasons = [{"code": "NORMALIZATION_REJECTED", "path": "spec", "detail": reason}
               for reason in spec.quarantine_reasons]
    rules = spec.rules_v2
    legacy = spec.legacy_rules
    if rules is None:
        reasons.append({"code": "MISSING_EXECUTABLE_SPEC", "path": "rules_v2",
                        "detail": "No normalized executable specification"})
        return reasons

    def reject(path: str, detail: str) -> None:
        reasons.append({"code": "UNSUPPORTED_EOD_RULE", "path": path, "detail": detail})

    if rules.timeframes.primary != "1d":
        reject("timeframes.primary", "EOD review cannot execute intraday rules")
    if rules.timeframes.structure or rules.timeframes.reference:
        reject("timeframes", "EOD review cannot execute additional timeframe dependencies")
    for field in ("intraday_entry", "partial_exits", "session_window", "entry_composition"):
        if getattr(rules, field):
            reject(field, "Field has no EOD execution implementation")
    for index, indicator in enumerate(rules.indicators):
        if (indicator.name not in {"iv_rank", "vix", "pdh_pdl_pdc"}
                or indicator.timeframe != "1d" or indicator.period is not None or indicator.params):
            reject(f"indicators.{index}", "Indicator dependency has no exact EOD implementation")
    for dependency in rules.data_dependencies:
        if dependency not in {"nse_option_chain_eod", "india_vix_eod", "nse_event_calendar"}:
            reject("data_dependencies", f"Unsupported data requirement: {dependency}")
    if rules.position_sizing.lot_step_mode != "fixed_lots":
        reject("position_sizing.lot_step_mode", "EOD review only supports declared fixed lots")
    if rules.position_sizing.max_lots != rules.risk.max_lots:
        reject("position_sizing.max_lots", "Sizing and executed risk lot counts differ")
    if legacy is None:
        reject("entry/exit", "No lossless legacy execution representation")
    else:
        stop, target = _derive_stop_and_target_from_exits(legacy.exit)
        if rules.stop_loss != stop:
            reject("stop_loss", "Explicit stop differs from executed exit rules")
        if rules.target != target:
            reject("target", "Explicit target differs from executed exit rules")
        for field in ("instrument", "entry", "exit", "risk", "entry_blackout"):
            if getattr(rules, field) != getattr(legacy, field):
                reject(field, "Normalized and executed rules differ")
    return reasons
