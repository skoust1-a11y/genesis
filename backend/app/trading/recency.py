"""Shadow-Only Recency Decay Model & Four-Arm Regime/Recency Ablation Engine (Stage B Phase B3).

Implements:
1. Six-factor exponential decay model (`pa_5m`, `liq_sweep`, `struct_15m`, `breaking_news`,
   `macro_post`, `pdh_pdl_or30`) with causal timestamp validation and 15m-bucket / `cluster_id`
   deduplication.
2. Pure entry and open-position conviction/conflict assessment (`assess_entry`, `assess_position`)
   and fail-safe shadow JSONL logging (`orders_sent=False`, `shadow_only=True`).
3. Independent four-arm walk-forward ablation comparison over identical historical periods,
   folds, execution assumptions, transaction fee policies (`paper_fees.py`), and capital policies
   (`Rs 50,000`, `paper-risk-v2`):
   - Arm 0: Frozen baseline (`ARM_0_FROZEN_BASELINE`)
   - Arm 1: Regime-only (`ARM_1_REGIME_ONLY`)
   - Arm 2: Recency-only (`ARM_2_RECENCY_ONLY`)
   - Arm 3: Combined regime and recency (`ARM_3_COMBINED_REGIME_RECENCY`)
4. Skipped-winner audit and +/-25% half-life stability neighborhood verification.
5. Blueprint improvement thresholds evaluated strictly as research acceptance criteria,
   never as proof of profitability. Regime and recency remain shadow-only and never mutate
   active strategy entry, exit, lot sizing, or risk approval.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from app.config import Settings, get_settings
from app.db.hashing import canonical_json, normalize_json, sha256_hex
from app.strategies.normalizer import NormalizedStrategySpec, normalize_strategy_document
from app.strategies.schema import StrategyDocument
from app.trading.option_quote import IST
from app.trading.walk_forward import (
    DEFAULT_INITIAL_CAPITAL_INR,
    CandidateSignalEvent,
    FrozenFoldParameters,
    LookAheadLeakageError,
    ParameterFreezeViolationError,
    WalkForwardConfig,
    WalkForwardReport,
    _parse_ts,
    run_walk_forward_validation,
)

logger = logging.getLogger(__name__)

RECENCY_ENGINE_VERSION = "recency-v3.0"

FactorCategory = Literal[
    "pa_5m",
    "liq_sweep",
    "struct_15m",
    "breaking_news",
    "macro_post",
    "pdh_pdl_or30",
]

AblationArmId = Literal[
    "ARM_0_FROZEN_BASELINE",
    "ARM_1_REGIME_ONLY",
    "ARM_2_RECENCY_ONLY",
    "ARM_3_COMBINED_REGIME_RECENCY",
]

ABLA_ARMS: tuple[AblationArmId, ...] = (
    "ARM_0_FROZEN_BASELINE",
    "ARM_1_REGIME_ONLY",
    "ARM_2_RECENCY_ONLY",
    "ARM_3_COMBINED_REGIME_RECENCY",
)

DEFAULT_FACTOR_HALF_LIVES_MINUTES: dict[str, float] = {
    "pa_5m": 20.0,
    "liq_sweep": 45.0,
    "struct_15m": 180.0,
    "breaking_news": 60.0,
    "macro_post": 120.0,
    "pdh_pdl_or30": 375.0,
}

DEFAULT_FACTOR_FLOORS: dict[str, float] = {
    "pa_5m": 0.0,
    "liq_sweep": 0.0,
    "struct_15m": 0.0,
    "breaking_news": 0.0,
    "macro_post": 0.0,
    "pdh_pdl_or30": 0.50,
}

DEFAULT_ALLOWED_REGIMES: frozenset[str] = frozenset({"TREND", "RANGE_EXPANSION", "COMPRESSION"})
DEFAULT_BLOCKED_REGIMES: frozenset[str] = frozenset({"EVENT_PANIC", "DISORDERED_VOLATILITY"})


@dataclass(frozen=True)
class RecencyConfig:
    """Frozen configuration for recency decay and shadow gating thresholds."""

    half_lives_minutes: Mapping[str, float] = field(
        default_factory=lambda: dict(DEFAULT_FACTOR_HALF_LIVES_MINUTES)
    )
    floors: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_FACTOR_FLOORS))
    entry_full_conviction_min: float = 0.25
    entry_half_conviction_min: float = 0.10
    entry_max_conflict_ratio: float = 0.45
    macro_veto_severity_min: float = 0.80
    macro_veto_recency_min: float = 0.60
    dedup_bucket_minutes: int = 15
    strict_leakage_check: bool = True

    def scaled_half_lives(self, multiplier: float) -> RecencyConfig:
        if multiplier <= 0:
            raise ValueError("multiplier must be positive")
        return RecencyConfig(
            half_lives_minutes={k: round(float(v) * multiplier, 4) for k, v in self.half_lives_minutes.items()},
            floors=dict(self.floors),
            entry_full_conviction_min=self.entry_full_conviction_min,
            entry_half_conviction_min=self.entry_half_conviction_min,
            entry_max_conflict_ratio=self.entry_max_conflict_ratio,
            macro_veto_severity_min=self.macro_veto_severity_min,
            macro_veto_recency_min=self.macro_veto_recency_min,
            dedup_bucket_minutes=self.dedup_bucket_minutes,
            strict_leakage_check=self.strict_leakage_check,
        )

    def parameter_hash(self) -> str:
        return sha256_hex(canonical_json(normalize_json(self.to_dict())))

    def to_dict(self) -> dict[str, Any]:
        return {
            "half_lives_minutes": dict(sorted(self.half_lives_minutes.items())),
            "floors": dict(sorted(self.floors.items())),
            "entry_full_conviction_min": self.entry_full_conviction_min,
            "entry_half_conviction_min": self.entry_half_conviction_min,
            "entry_max_conflict_ratio": self.entry_max_conflict_ratio,
            "macro_veto_severity_min": self.macro_veto_severity_min,
            "macro_veto_recency_min": self.macro_veto_recency_min,
            "dedup_bucket_minutes": self.dedup_bucket_minutes,
            "strict_leakage_check": self.strict_leakage_check,
        }


@dataclass(frozen=True)
class EvidenceItem:
    """A single price-action or market-event evidence observation."""

    evidence_id: str
    factor_category: FactorCategory
    observed_at: str
    direction: Literal[-1, 0, 1]
    reliability: float = 0.75
    severity: float = 0.70
    regime_relevance: float = 1.0
    available_at: str | None = None
    active_window_end: str | None = None
    cluster_id: str | None = None
    source: str = "price_action"

    @property
    def effective_available_at(self) -> str:
        return self.available_at or self.observed_at


def compute_decay(
    elapsed_minutes: float,
    half_life_minutes: float,
    *,
    floor: float = 0.0,
    in_active_window: bool = False,
) -> float:
    """Pure exponential decay weight $w(\\Delta t) = \\max(w_{\\text{floor}}, 2^{-\\Delta t / \\tau_{1/2}})$.

    Returns `0.0` if `elapsed_minutes < 0` (future timestamp protection).
    Returns `1.0` if `in_active_window=True` (e.g. active scheduled macro event window).
    """
    if elapsed_minutes < 0:
        return 0.0
    if in_active_window:
        return 1.0
    if half_life_minutes <= 0:
        return max(float(floor), 0.0)
    raw = math.pow(2.0, -float(elapsed_minutes) / float(half_life_minutes))
    return round(min(1.0, max(float(floor), raw)), 6)


def score_evidence_item(
    item: EvidenceItem,
    *,
    decision_time: datetime | str,
    config: RecencyConfig | None = None,
) -> float:
    """Compute the decayed magnitude weight $w_i \\cdot r_i \\cdot s_i \\cdot g_i$ for one `EvidenceItem` at `decision_time`."""
    cfg = config or RecencyConfig()
    dec_ts = _parse_ts(decision_time)
    obs_ts = _parse_ts(item.observed_at)
    avail_ts = _parse_ts(item.effective_available_at)

    if avail_ts > dec_ts or obs_ts > dec_ts:
        if cfg.strict_leakage_check:
            raise LookAheadLeakageError(
                f"RECENCY_FUTURE_EVIDENCE_LEAKAGE: evidence {item.evidence_id!r} "
                f"(available_at={avail_ts.isoformat()}) > decision_time={dec_ts.isoformat()}"
            )
        return 0.0

    in_active = False
    if item.factor_category == "macro_post" and item.active_window_end:
        win_end = _parse_ts(item.active_window_end)
        if dec_ts <= win_end:
            in_active = True
            elapsed_min = 0.0
        else:
            elapsed_min = (dec_ts - win_end).total_seconds() / 60.0
    else:
        elapsed_min = (dec_ts - obs_ts).total_seconds() / 60.0

    half_life = float(cfg.half_lives_minutes.get(item.factor_category, 45.0))
    floor = float(cfg.floors.get(item.factor_category, 0.0))
    w_decay = compute_decay(elapsed_min, half_life, floor=floor, in_active_window=in_active)

    r_clamped = min(1.0, max(0.0, float(item.reliability)))
    s_clamped = min(1.0, max(0.0, float(item.severity)))
    g_clamped = min(1.5, max(0.0, float(item.regime_relevance)))
    return round(w_decay * r_clamped * s_clamped * g_clamped, 6)


def score_price_evidence(
    items: Sequence[EvidenceItem],
    *,
    decision_time: datetime | str,
    config: RecencyConfig | None = None,
) -> list[tuple[EvidenceItem, float]]:
    """Score price-action and structural evidence items (`pa_5m`, `liq_sweep`, `struct_15m`, `pdh_pdl_or30`)."""
    price_cats = {"pa_5m", "liq_sweep", "struct_15m", "pdh_pdl_or30"}
    return [
        (it, score_evidence_item(it, decision_time=decision_time, config=config))
        for it in items
        if it.factor_category in price_cats
    ]


def score_event_evidence(
    items: Sequence[EvidenceItem],
    *,
    decision_time: datetime | str,
    config: RecencyConfig | None = None,
) -> list[tuple[EvidenceItem, float]]:
    """Score market-event and news evidence items (`breaking_news`, `macro_post`)."""
    event_cats = {"breaking_news", "macro_post"}
    return [
        (it, score_evidence_item(it, decision_time=decision_time, config=config))
        for it in items
        if it.factor_category in event_cats
    ]


def deduplicate_and_cluster(
    items: Sequence[EvidenceItem],
    *,
    decision_time: datetime | str,
    config: RecencyConfig | None = None,
) -> list[tuple[EvidenceItem, float]]:
    """Collapse duplicate or syndicated evidence items within a `cluster_id` or 15-minute bucket.

    Keeps only the highest-scoring representative per cluster so syndicated headlines or repeated
    candle touches on the same bar cluster cannot artificially inflate conviction.
    """
    cfg = config or RecencyConfig()
    bucket_sec = max(int(cfg.dedup_bucket_minutes), 1) * 60
    best_by_key: dict[str, tuple[EvidenceItem, float]] = {}

    for item in items:
        score = score_evidence_item(item, decision_time=decision_time, config=cfg)
        if item.cluster_id and item.cluster_id.strip():
            key = f"CLUSTER:{item.cluster_id.strip()}"
        else:
            obs_epoch = int(_parse_ts(item.observed_at).timestamp())
            bucket = obs_epoch // bucket_sec
            key = f"BUCKET:{item.factor_category}:{item.direction}:{bucket}"

        existing = best_by_key.get(key)
        if existing is None or score > existing[1] or (score == existing[1] and item.evidence_id < existing[0].evidence_id):
            best_by_key[key] = (item, score)

    return [best_by_key[k] for k in sorted(best_by_key)]


@dataclass(frozen=True)
class ConvictionScore:
    """Aggregated recency-weighted support, opposition, net conviction, and conflict ratio."""

    trade_direction: Literal[-1, 1]
    support_score: float
    oppose_score: float
    neutral_Macro_hazard: float
    net_conviction: float
    conflict_ratio: float
    dominant_factor: str | None
    deduplicated_count: int
    raw_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_conviction(
    items: Sequence[EvidenceItem],
    *,
    trade_direction: Literal["CALL", "PUT", "LONG", "SHORT", 1, -1],
    decision_time: datetime | str,
    config: RecencyConfig | None = None,
) -> ConvictionScore:
    """Compute recency-weighted net conviction $C_{\\text{net}} \\in [-1, +1]$ and conflict ratio $\\rho_{\\text{conflict}} \\in [0, 1]$."""
    cfg = config or RecencyConfig()
    if trade_direction in ("CALL", "LONG", 1):
        d_trade: Literal[-1, 1] = 1
    elif trade_direction in ("PUT", "SHORT", -1):
        d_trade = -1
    else:
        raise ValueError(f"Unsupported trade_direction: {trade_direction!r}")

    deduped = deduplicate_and_cluster(items, decision_time=decision_time, config=cfg)
    support = 0.0
    oppose = 0.0
    macro_hazard = 0.0
    dominant_factor: str | None = None
    best_single = 0.0

    for item, weight in deduped:
        if weight <= 0:
            continue
        if weight > best_single:
            best_single = weight
            dominant_factor = item.factor_category

        if item.direction == 0:
            # Non-directional macro/event shock acts as opposing/hazard friction
            oppose += weight * 0.5
            if item.factor_category in ("macro_post", "breaking_news"):
                macro_hazard = max(macro_hazard, weight)
        elif item.direction == d_trade:
            support += weight
        else:
            oppose += weight
            if item.factor_category in ("macro_post", "breaking_news"):
                macro_hazard = max(macro_hazard, weight)

    denom = support + oppose + 1e-6
    net_conv = (support - oppose) / denom if (support + oppose) > 0 else 0.0
    conflict = oppose / denom if (support + oppose) > 0 else 0.0

    return ConvictionScore(
        trade_direction=d_trade,
        support_score=round(support, 6),
        oppose_score=round(oppose, 6),
        neutral_Macro_hazard=round(macro_hazard, 6),
        net_conviction=round(net_conv, 6),
        conflict_ratio=round(conflict, 6),
        dominant_factor=dominant_factor,
        deduplicated_count=len(deduped),
        raw_count=len(items),
    )


@dataclass(frozen=True)
class EntryAssessment:
    """Shadow entry assessment from recency-weighted evidence."""

    hypothetical_action: Literal[
        "ALLOW_FULL",
        "ALLOW_HALF_SIZE",
        "SKIP_CONFLICT",
        "SKIP_LOW_CONVICTION",
        "SKIP_MACRO_VETO",
    ]
    allow_entry: bool
    size_fraction: float
    conviction: ConvictionScore
    reason: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["conviction"] = self.conviction.to_dict()
        return payload


def assess_entry(
    items: Sequence[EvidenceItem],
    *,
    trade_direction: Literal["CALL", "PUT", "LONG", "SHORT", 1, -1],
    decision_time: datetime | str,
    config: RecencyConfig | None = None,
) -> EntryAssessment:
    """Evaluate candidate trade entry using recency-weighted conviction and conflict rules."""
    cfg = config or RecencyConfig()
    conv = compute_conviction(items, trade_direction=trade_direction, decision_time=decision_time, config=cfg)

    # Check high-severity recent macro/news veto first
    for item in items:
        if item.factor_category in ("macro_post", "breaking_news") and item.severity >= cfg.macro_veto_severity_min:
            w = score_evidence_item(item, decision_time=decision_time, config=cfg)
            if w >= cfg.macro_veto_recency_min and item.direction != conv.trade_direction:
                return EntryAssessment(
                    hypothetical_action="SKIP_MACRO_VETO",
                    allow_entry=False,
                    size_fraction=0.0,
                    conviction=conv,
                    reason=f"RECENT_HIGH_SEVERITY_EVENT:{item.factor_category}",
                )

    if conv.conflict_ratio > cfg.entry_max_conflict_ratio:
        return EntryAssessment(
            hypothetical_action="SKIP_CONFLICT",
            allow_entry=False,
            size_fraction=0.0,
            conviction=conv,
            reason=f"CONFLICT_RATIO_EXCEEDED:{conv.conflict_ratio:.3f}>{cfg.entry_max_conflict_ratio:.3f}",
        )

    if conv.net_conviction >= cfg.entry_full_conviction_min:
        return EntryAssessment(
            hypothetical_action="ALLOW_FULL",
            allow_entry=True,
            size_fraction=1.0,
            conviction=conv,
            reason="HIGH_RECENCY_CONVICTION",
        )

    if conv.net_conviction >= cfg.entry_half_conviction_min:
        return EntryAssessment(
            hypothetical_action="ALLOW_HALF_SIZE",
            allow_entry=True,
            size_fraction=0.5,
            conviction=conv,
            reason="MODERATE_RECENCY_CONVICTION",
        )

    return EntryAssessment(
        hypothetical_action="SKIP_LOW_CONVICTION",
        allow_entry=False,
        size_fraction=0.0,
        conviction=conv,
        reason=f"LOW_RECENCY_CONVICTION:{conv.net_conviction:.3f}<{cfg.entry_half_conviction_min:.3f}",
    )


@dataclass(frozen=True)
class PositionAssessment:
    """Shadow open-position assessment from recency-weighted evidence."""

    hypothetical_action: Literal[
        "HOLD",
        "TIGHTEN_SL_TO_BE",
        "PARTIAL_EXIT_50PCT",
        "FULL_EXIT_INVALIDATED",
    ]
    conviction: ConvictionScore
    bars_since_favorable_impulse: int
    unrealized_r: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["conviction"] = self.conviction.to_dict()
        return payload


def assess_position(
    items: Sequence[EvidenceItem],
    *,
    trade_direction: Literal["CALL", "PUT", "LONG", "SHORT", 1, -1],
    decision_time: datetime | str,
    unrealized_r: float = 0.0,
    bars_since_favorable_impulse: int = 0,
    config: RecencyConfig | None = None,
) -> PositionAssessment:
    """Evaluate an open trade using recency decay and opposing impulse appearance."""
    cfg = config or RecencyConfig()
    conv = compute_conviction(items, trade_direction=trade_direction, decision_time=decision_time, config=cfg)

    if conv.net_conviction <= -0.35 and conv.oppose_score >= 0.45:
        return PositionAssessment(
            hypothetical_action="FULL_EXIT_INVALIDATED",
            conviction=conv,
            bars_since_favorable_impulse=bars_since_favorable_impulse,
            unrealized_r=unrealized_r,
            reason="FRESH_OPPOSING_IMPULSE_INVALIDATION",
        )

    if conv.net_conviction < 0.0 and unrealized_r >= 0.8:
        return PositionAssessment(
            hypothetical_action="PARTIAL_EXIT_50PCT",
            conviction=conv,
            bars_since_favorable_impulse=bars_since_favorable_impulse,
            unrealized_r=unrealized_r,
            reason="NEGATIVE_CONVICTION_LOCK_PARTIAL",
        )

    if bars_since_favorable_impulse >= 6 and -0.20 <= UnrealizedRClamp(unrealized_r) <= 0.30 and conv.support_score < 0.25:
        return PositionAssessment(
            hypothetical_action="TIGHTEN_SL_TO_BE",
            conviction=conv,
            bars_since_favorable_impulse=bars_since_favorable_impulse,
            unrealized_r=unrealized_r,
            reason="STALE_THESIS_DECAY_TIGHTEN_STOP",
        )

    return PositionAssessment(
        hypothetical_action="HOLD",
        conviction=conv,
        bars_since_favorable_impulse=bars_since_favorable_impulse,
        unrealized_r=unrealized_r,
        reason="THESIS_INTACT",
    )


def UnrealizedRClamp(val: float) -> float:
    return float(val)


def build_shadow_record(
    *,
    decision_time: datetime | str,
    strategy_id: str,
    strategy_version: str,
    regime: str,
    entry_assessment: EntryAssessment | None = None,
    position_assessment: PositionAssessment | None = None,
) -> dict[str, Any]:
    """Construct an immutable shadow-only telemetry record. Explicitly attests zero active-path mutation."""
    return {
        "engine_version": RECENCY_ENGINE_VERSION,
        "timestamp": _parse_ts(decision_time).isoformat(),
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "regime": regime,
        "shadow_only": True,
        "orders_sent": False,
        "mutated_active_entry": False,
        "mutated_active_exit": False,
        "mutated_lot_sizing": False,
        "mutated_risk_approval": False,
        "entry_assessment": entry_assessment.to_dict() if entry_assessment is not None else None,
        "position_assessment": position_assessment.to_dict() if position_assessment is not None else None,
    }


def safe_shadow_hook(
    log_path: Path | str | None,
    *,
    decision_time: datetime | str,
    strategy_id: str,
    strategy_version: str,
    regime: str,
    items: Sequence[EvidenceItem],
    trade_direction: Literal["CALL", "PUT", "LONG", "SHORT", 1, -1],
    config: RecencyConfig | None = None,
) -> dict[str, Any] | None:
    """Fail-safe shadow telemetry hook. Never raises into caller and never modifies active strategy state."""
    try:
        cfg = config or RecencyConfig()
        assessment = assess_entry(
            items,
            trade_direction=trade_direction,
            decision_time=decision_time,
            config=cfg,
        )
        record = build_shadow_record(
            decision_time=decision_time,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            regime=regime,
            entry_assessment=assessment,
        )
        if log_path is not None:
            path = Path(log_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, sort_keys=True) + "\n")
        return record
    except Exception as exc:
        logger.warning("recency shadow hook suppressed error: %s", exc)
        return None


# --- Four-Arm Ablation Engine ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkippedWinnerAudit:
    """Audit of baseline (Arm 0) trades skipped by a filtered arm (Arm 1, Arm 2, or Arm 3)."""

    arm_id: AblationArmId
    baseline_oos_trades: int
    arm_oos_trades: int
    skipped_trades_total: int
    skipped_winners_count: int
    skipped_losers_count: int
    skipped_winners_gross_pnl_inr: float
    skipped_net_pnl_foregone_inr: float
    skipped_winner_rate: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HalflifeStabilityNeighborhood:
    """Sensitivity check across 0.75x (-25%), 1.00x, and 1.25x (+25%) half-life scalings."""

    net_expectancy_minus_25pct: float | None
    net_expectancy_base: float | None
    net_expectancy_plus_25pct: float | None
    net_pnl_minus_25pct: float
    net_pnl_base: float
    net_pnl_plus_25pct: float
    neighborhood_stable: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AblationArmResult:
    """Walk-forward validation report and skipped-winner audit for a single ablation arm."""

    arm_id: AblationArmId
    description: str
    walk_forward_report: WalkForwardReport
    skipped_winner_audit: SkippedWinnerAudit
    arm_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "arm_id": self.arm_id,
            "description": self.description,
            "walk_forward_report": self.walk_forward_report.to_dict(),
            "skipped_winner_audit": self.skipped_winner_audit.to_dict(),
            "arm_hash": self.arm_hash,
        }


@dataclass(frozen=True)
class AblationComparisonReport:
    """Independent four-arm ablation comparison report."""

    engine_version: str
    strategy_id: str
    strategy_version: str
    shadow_only: bool
    proof_of_profitability: bool
    identical_evaluation_conditions_verified: bool
    evaluation_conditions_sha256: str
    recency_config: RecencyConfig
    allowed_regimes: tuple[str, ...]
    arms: Mapping[AblationArmId, AblationArmResult]
    halflife_stability: HalflifeStabilityNeighborhood | None
    research_acceptance_criteria: Mapping[str, Any]
    research_acceptance_passed: bool
    recommended_arm: AblationArmId
    ablation_report_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine_version": self.engine_version,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "shadow_only": self.shadow_only,
            "proof_of_profitability": self.proof_of_profitability,
            "identical_evaluation_conditions_verified": self.identical_evaluation_conditions_verified,
            "evaluation_conditions_sha256": self.evaluation_conditions_sha256,
            "recency_config": self.recency_config.to_dict(),
            "allowed_regimes": list(self.allowed_regimes),
            "arms": {k: v.to_dict() for k, v in self.arms.items()},
            "halflife_stability": self.halflife_stability.to_dict() if self.halflife_stability is not None else None,
            "research_acceptance_criteria": dict(self.research_acceptance_criteria),
            "research_acceptance_passed": self.research_acceptance_passed,
            "recommended_arm": self.recommended_arm,
            "ablation_report_hash": self.ablation_report_hash,
        }


def _extract_event_evidence(
    event: CandidateSignalEvent,
    evidence_by_event_id: Mapping[str, Sequence[EvidenceItem]] | None,
) -> Sequence[EvidenceItem]:
    if evidence_by_event_id is not None and event.event_id in evidence_by_event_id:
        return evidence_by_event_id[event.event_id]
    raw = event.metadata.get("recency_evidence") if isinstance(event.metadata, Mapping) else None
    if not raw:
        return ()
    items: list[EvidenceItem] = []
    for entry in raw:
        if isinstance(entry, EvidenceItem):
            items.append(entry)
        elif isinstance(entry, Mapping):
            items.append(
                EvidenceItem(
                    evidence_id=str(entry.get("evidence_id") or f"{event.event_id}_ev_{len(items)}"),
                    factor_category=entry.get("factor_category", "pa_5m"),
                    observed_at=str(entry.get("observed_at") or event.decision_time),
                    available_at=str(entry["available_at"]) if entry.get("available_at") else None,
                    direction=int(entry.get("direction", 1)),  # type: ignore[arg-type]
                    reliability=float(entry.get("reliability", 0.75)),
                    severity=float(entry.get("severity", 0.70)),
                    regime_relevance=float(entry.get("regime_relevance", 1.0)),
                    active_window_end=str(entry["active_window_end"]) if entry.get("active_window_end") else None,
                    cluster_id=str(entry["cluster_id"]) if entry.get("cluster_id") else None,
                    source=str(entry.get("source") or "price_action"),
                )
            )
    return items


def _check_regime_causal_and_allowed(
    event: CandidateSignalEvent,
    *,
    allowed_regimes: frozenset[str],
    strict_leakage_check: bool,
) -> tuple[bool, str | None]:
    if isinstance(event.metadata, Mapping) and event.metadata.get("regime_available_at"):
        reg_avail = _parse_ts(str(event.metadata["regime_available_at"]))
        dec_ts = _parse_ts(event.decision_time)
        if reg_avail > dec_ts:
            if strict_leakage_check:
                raise LookAheadLeakageError(
                    f"REGIME_FUTURE_LEAKAGE: event {event.event_id} regime_available_at={reg_avail.isoformat()} > decision_time={dec_ts.isoformat()}"
                )
            return False, "REGIME_FUTURE_TIMESTAMP"

    regime_label = str(event.regime or "UNKNOWN").strip().upper()
    if regime_label in DEFAULT_BLOCKED_REGIMES or regime_label not in allowed_regimes:
        return False, f"REGIME_FILTERED:{regime_label}"
    return True, None


def _compute_skipped_winner_audit(
    arm_id: AblationArmId,
    baseline_report: WalkForwardReport,
    arm_report: WalkForwardReport,
) -> SkippedWinnerAudit:
    base_execs = {
        ex.event_id: ex
        for fold_res in baseline_report.folds
        for ex in fold_res.oos_executions
        if ex.priced
    }
    arm_exec_ids = {
        ex.event_id
        for fold_res in arm_report.folds
        for ex in fold_res.oos_executions
        if ex.priced
    }
    skipped_ids = sorted(set(base_execs) - arm_exec_ids)
    skipped_execs = [base_execs[eid] for eid in skipped_ids]
    skipped_winners = [ex for ex in skipped_execs if ex.net_pnl_inr > 0]
    skipped_losers = [ex for ex in skipped_execs if ex.net_pnl_inr < 0]
    base_winners_count = sum(1 for ex in base_execs.values() if ex.net_pnl_inr > 0)

    winners_gross = round(sum(float(ex.gross_pnl_inr) for ex in skipped_winners), 2)
    net_foregone = round(sum(float(ex.net_pnl_inr) for ex in skipped_execs), 2)
    skipped_winner_rate = (
        round(len(skipped_winners) / base_winners_count, 4)
        if base_winners_count > 0
        else 0.0
    )

    return SkippedWinnerAudit(
        arm_id=arm_id,
        baseline_oos_trades=len(base_execs),
        arm_oos_trades=len(arm_exec_ids),
        skipped_trades_total=len(skipped_execs),
        skipped_winners_count=len(skipped_winners),
        skipped_losers_count=len(skipped_losers),
        skipped_winners_gross_pnl_inr=winners_gross,
        skipped_net_pnl_foregone_inr=net_foregone,
        skipped_winner_rate=skipped_winner_rate,
    )


def run_four_arm_ablation(
    spec_or_doc: NormalizedStrategySpec | StrategyDocument,
    *,
    trading_dates: Sequence[date | str],
    events: Sequence[CandidateSignalEvent],
    walk_forward_config: WalkForwardConfig | None = None,
    recency_config: RecencyConfig | None = None,
    allowed_regimes: frozenset[str] = DEFAULT_ALLOWED_REGIMES,
    evidence_by_event_id: Mapping[str, Sequence[EvidenceItem]] | None = None,
    settings: Settings | None = None,
    selection_source_dates: Sequence[str] | None = None,
    min_expectancy_improvement_pct: float = 10.0,
    max_skipped_winner_rate: float = 0.25,
) -> AblationComparisonReport:
    """Run the independent four-arm regime and recency ablation study.

    Enforces:
    - Identical historical trading dates, walk-forward folds, execution tier, fee policy, and capital policy
      (`Rs 50,000`, `paper-risk-v2`) across all 4 arms.
    - No parameter selection using OOS outcomes (`selection_source_dates` checked against first OOS fold start).
    - Causal feature/evidence availability checks across all arms.
    - Skipped-winner audit and +/-25% half-life stability neighborhood check.
    - Blueprint thresholds used strictly as research acceptance criteria (`proof_of_profitability=False`,
      `shadow_only=True`).
    """
    wf_cfg = walk_forward_config or WalkForwardConfig(initial_capital_inr=DEFAULT_INITIAL_CAPITAL_INR)
    rec_cfg = recency_config or RecencyConfig(strict_leakage_check=wf_cfg.strict_leakage_check)
    active_settings = settings or get_settings()

    if isinstance(spec_or_doc, NormalizedStrategySpec):
        spec = spec_or_doc
    elif isinstance(spec_or_doc, StrategyDocument):
        spec = normalize_strategy_document(spec_or_doc, strict_gaps=True)
    else:
        raise TypeError(f"Expected NormalizedStrategySpec or StrategyDocument, got {type(spec_or_doc).__name__}")

    # Verify all events have causally valid regime and recency timestamps upfront so no arm can hide leakage
    for ev in events:
        _check_regime_causal_and_allowed(
            ev,
            allowed_regimes=allowed_regimes,
            strict_leakage_check=wf_cfg.strict_leakage_check,
        )
        ev_items = _extract_event_evidence(ev, evidence_by_event_id)
        if ev_items:
            deduplicate_and_cluster(ev_items, decision_time=ev.decision_time, config=rec_cfg)

    # Run Arm 0: Frozen Baseline
    arm0_report = run_walk_forward_validation(
        spec,
        trading_dates=trading_dates,
        events=events,
        config=wf_cfg,
        settings=active_settings,
        oos_event_filter=None,
    )

    # Guard against parameter selection using OOS outcomes
    if selection_source_dates is not None and arm0_report.folds:
        first_is_end = arm0_report.folds[0].fold.is_end_date
        for src_day in selection_source_dates:
            day_str = str(src_day)[:10]
            if day_str > first_is_end:
                raise ParameterFreezeViolationError(
                    f"ABLATION_OOS_PARAMETER_SELECTION_FORBIDDEN: selection_source_dates includes {day_str} > IS end {first_is_end}"
                )

    def _arm1_filter(ev: CandidateSignalEvent, _frozen: FrozenFoldParameters) -> tuple[bool, str | None]:
        return _check_regime_causal_and_allowed(
            ev,
            allowed_regimes=allowed_regimes,
            strict_leakage_check=wf_cfg.strict_leakage_check,
        )

    def _make_recency_filter(active_rec_cfg: RecencyConfig):
        def _filter(ev: CandidateSignalEvent, _frozen: FrozenFoldParameters) -> tuple[bool, str | None]:
            items = _extract_event_evidence(ev, evidence_by_event_id)
            assessment = assess_entry(
                items,
                trade_direction=ev.direction,
                decision_time=ev.decision_time,
                config=active_rec_cfg,
            )
            return assessment.allow_entry, assessment.reason

        return _filter

    def _make_combined_filter(active_rec_cfg: RecencyConfig):
        rec_f = _make_recency_filter(active_rec_cfg)

        def _filter(ev: CandidateSignalEvent, frozen: FrozenFoldParameters) -> tuple[bool, str | None]:
            reg_ok, reg_reason = _arm1_filter(ev, frozen)
            if not reg_ok:
                return False, reg_reason
            return rec_f(ev, frozen)

        return _filter

    # Run Arm 1: Regime-only
    arm1_report = run_walk_forward_validation(
        spec,
        trading_dates=trading_dates,
        events=events,
        config=wf_cfg,
        settings=active_settings,
        oos_event_filter=_arm1_filter,
    )

    # Run Arm 2: Recency-only
    arm2_report = run_walk_forward_validation(
        spec,
        trading_dates=trading_dates,
        events=events,
        config=wf_cfg,
        settings=active_settings,
        oos_event_filter=_make_recency_filter(rec_cfg),
    )

    # Run Arm 3: Combined Regime and Recency
    arm3_report = run_walk_forward_validation(
        spec,
        trading_dates=trading_dates,
        events=events,
        config=wf_cfg,
        settings=active_settings,
        oos_event_filter=_make_combined_filter(rec_cfg),
    )

    # Verify identical evaluation conditions across all 4 arms
    cond_payload = {
        "trading_dates": [str(d)[:10] for d in trading_dates],
        "event_ids": [e.event_id for e in events],
        "walk_forward_config": wf_cfg.to_dict(),
        "capital_inr": wf_cfg.initial_capital_inr,
        "risk_policy_id": "paper-risk-v2",
        "fidelity_tier": wf_cfg.fidelity_tier,
        "strategy_binding": spec.validation_binding()["binding_sha256"],
    }
    cond_hash = sha256_hex(canonical_json(normalize_json(cond_payload)))

    reports_by_arm: dict[AblationArmId, tuple[str, WalkForwardReport]] = {
        "ARM_0_FROZEN_BASELINE": ("Frozen baseline strategy (no regime or recency filtering)", arm0_report),
        "ARM_1_REGIME_ONLY": ("Causal regime filter only (shadow evaluation)", arm1_report),
        "ARM_2_RECENCY_ONLY": ("Causal 6-factor recency conviction/conflict gate only (shadow evaluation)", arm2_report),
        "ARM_3_COMBINED_REGIME_RECENCY": ("Combined causal regime and 6-factor recency gate (shadow evaluation)", arm3_report),
    }

    arm_results: dict[AblationArmId, AblationArmResult] = {}
    for arm_id, (desc, rep) in reports_by_arm.items():
        audit = _compute_skipped_winner_audit(arm_id, arm0_report, rep)
        a_hash = sha256_hex(
            canonical_json(
                normalize_json(
                    {
                        "arm_id": arm_id,
                        "conditions_sha256": cond_hash,
                        "report_hash": rep.report_hash,
                        "skipped_winner_audit": audit.to_dict(),
                    }
                )
            )
        )
        arm_results[arm_id] = AblationArmResult(
            arm_id=arm_id,
            description=desc,
            walk_forward_report=rep,
            skipped_winner_audit=audit,
            arm_hash=a_hash,
        )

    # +/-25% Half-Life Stability Neighborhood Check on Arm 3
    stability: HalflifeStabilityNeighborhood | None = None
    if arm0_report.has_valid_oos_folds and arm0_report.oos_performance is not None and arm3_report.oos_performance is not None:
        rep_low = run_walk_forward_validation(
            spec,
            trading_dates=trading_dates,
            events=events,
            config=wf_cfg,
            settings=active_settings,
            oos_event_filter=_make_combined_filter(rec_cfg.scaled_half_lives(0.75)),
        )
        rep_high = run_walk_forward_validation(
            spec,
            trading_dates=trading_dates,
            events=events,
            config=wf_cfg,
            settings=active_settings,
            oos_event_filter=_make_combined_filter(rec_cfg.scaled_half_lives(1.25)),
        )
        exp_low = rep_low.oos_performance.net_expectancy_inr if rep_low.oos_performance else None
        exp_base = arm3_report.oos_performance.net_expectancy_inr
        exp_high = rep_high.oos_performance.net_expectancy_inr if rep_high.oos_performance else None
        pnl_low = rep_low.oos_performance.net_pnl_inr if rep_low.oos_performance else 0.0
        pnl_base = arm3_report.oos_performance.net_pnl_inr
        pnl_high = rep_high.oos_performance.net_pnl_inr if rep_high.oos_performance else 0.0
        arm0_exp = arm0_report.oos_performance.net_expectancy_inr or 0.0

        is_stable = (
            exp_low is not None
            and exp_base is not None
            and exp_high is not None
            and exp_low >= arm0_exp
            and exp_high >= arm0_exp
        )
        stability = HalflifeStabilityNeighborhood(
            net_expectancy_minus_25pct=exp_low,
            net_expectancy_base=exp_base,
            net_expectancy_plus_25pct=exp_high,
            net_pnl_minus_25pct=pnl_low,
            net_pnl_base=pnl_base,
            net_pnl_plus_25pct=pnl_high,
            neighborhood_stable=is_stable,
        )

    # Evaluate blueprint improvement thresholds as research acceptance criteria (never proof of profitability)
    arm0_oos = arm0_report.oos_performance
    arm1_oos = arm1_report.oos_performance
    arm2_oos = arm2_report.oos_performance
    arm3_oos = arm3_report.oos_performance
    arm3_audit = arm_results["ARM_3_COMBINED_REGIME_RECENCY"].skipped_winner_audit

    if arm0_oos is None or arm3_oos is None or not arm0_report.has_valid_oos_folds:
        criteria = {
            "has_valid_oos_folds": False,
            "expectancy_improved_by_threshold": False,
            "drawdown_non_worse_or_reduced": False,
            "beats_single_factor_arms": False,
            "skipped_winner_rate_within_bound": False,
            "halflife_neighborhood_stable": False,
            "note": "Research acceptance criteria only; never interpreted as proof of profitability.",
        }
        research_passed = False
        recommended: AblationArmId = "ARM_0_FROZEN_BASELINE"
    else:
        base_exp = arm0_oos.net_expectancy_inr or 0.0
        arm3_exp = arm3_oos.net_expectancy_inr if arm3_oos.net_expectancy_inr is not None else -1e9
        arm1_exp = arm1_oos.net_expectancy_inr if (arm1_oos and arm1_oos.net_expectancy_inr is not None) else -1e9
        arm2_exp = arm2_oos.net_expectancy_inr if (arm2_oos and arm2_oos.net_expectancy_inr is not None) else -1e9

        if base_exp > 0:
            exp_improvement_pct = ((arm3_exp - base_exp) / abs(base_exp)) * 100.0
        elif arm3_exp > base_exp:
            exp_improvement_pct = 100.0
        else:
            exp_improvement_pct = 0.0

        exp_ok = exp_improvement_pct >= min_expectancy_improvement_pct and arm3_exp > 0
        dd_ok = arm3_oos.max_drawdown_inr <= arm0_oos.max_drawdown_inr
        beats_single = arm3_exp >= arm1_exp and arm3_exp >= arm2_exp
        skip_ok = arm3_audit.skipped_winner_rate <= max_skipped_winner_rate
        stab_ok = bool(stability and stability.neighborhood_stable)

        criteria = {
            "has_valid_oos_folds": True,
            "baseline_net_expectancy_inr": arm0_oos.net_expectancy_inr,
            "arm3_net_expectancy_inr": arm3_oos.net_expectancy_inr,
            "expectancy_improvement_pct": round(exp_improvement_pct, 2),
            "expectancy_improved_by_threshold": exp_ok,
            "baseline_max_drawdown_inr": arm0_oos.max_drawdown_inr,
            "arm3_max_drawdown_inr": arm3_oos.max_drawdown_inr,
            "drawdown_non_worse_or_reduced": dd_ok,
            "beats_single_factor_arms": beats_single,
            "skipped_winner_rate": arm3_audit.skipped_winner_rate,
            "max_skipped_winner_rate": max_skipped_winner_rate,
            "skipped_winner_rate_within_bound": skip_ok,
            "halflife_neighborhood_stable": stab_ok,
            "note": "Blueprint improvement thresholds serve strictly as research acceptance criteria, not proof of profitability.",
        }
        research_passed = bool(exp_ok and dd_ok and beats_single and skip_ok and stab_ok)
        recommended = "ARM_3_COMBINED_REGIME_RECENCY" if research_passed else "ARM_0_FROZEN_BASELINE"

    ablation_payload = {
        "engine_version": RECENCY_ENGINE_VERSION,
        "conditions_sha256": cond_hash,
        "recency_config_hash": rec_cfg.parameter_hash(),
        "arm_hashes": {k: v.arm_hash for k, v in arm_results.items()},
        "research_acceptance_passed": research_passed,
        "recommended_arm": recommended,
    }
    ablation_hash = sha256_hex(canonical_json(normalize_json(ablation_payload)))

    return AblationComparisonReport(
        engine_version=RECENCY_ENGINE_VERSION,
        strategy_id=spec.strategy_id,
        strategy_version=spec.strategy_version,
        shadow_only=True,
        proof_of_profitability=False,
        identical_evaluation_conditions_verified=True,
        evaluation_conditions_sha256=cond_hash,
        recency_config=rec_cfg,
        allowed_regimes=tuple(sorted(allowed_regimes)),
        arms=arm_results,
        halflife_stability=stability,
        research_acceptance_criteria=criteria,
        research_acceptance_passed=research_passed,
        recommended_arm=recommended,
        ablation_report_hash=ablation_hash,
    )
