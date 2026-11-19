"""Dual-Agent Conditional & Regime-Adaptive EMA + Supertrend + Monotonic Level-Ratchet Policy.

All thresholds, gates, and sizing rules are evaluated via explicit conditional predicates
driven by live market state (5M ATR, trade initial risk, macro EMA(200) alignment,
liquidity-sweep detection, and portfolio risk budget) rather than rigid hardcoded constants:

- Separate asymmetric CEAgent (CALL) and PEAgent (PUT) driven by `AgentSideConfig` + `MarketContext`.
- Dynamic ATR-adaptive buffers, risk bands, and 1R validation milestones (with safe clamps).
- Conditional Supertrend gate: required unless a verified liquidity sweep-and-reclaim is active
  inside the opening reversal window.
- Macro-regime conditional lot ceiling: counter-200EMA trades cap at `counter_macro_max_lots`
  and dynamically unlock to `aligned_max_lots` once 5M EMA(200) is conquered.
- Explicit `ConditionCheck` audit trail attached to every proposal and management decision.
- Pure deterministic Python with strictly closed-bar inputs; zero LLM or network dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
import hashlib
import json
from typing import Literal
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
VERSION = "dual-agent-ema-st-ratchet-v2-conditional"

Side = Literal["CALL", "PUT"]
ThesisStage = Literal["PROBING", "VALIDATED", "EXPANDING", "CLOSED"]
ActionKind = Literal[
    "HOLD",
    "ENTER",
    "PYRAMID_ADD",
    "SCALE_OUT_TRIM",
    "EXIT_MONOTONIC_SL",
    "EXIT_5M_CROSS_END",
    "EXIT_EOD_1510",
    "VETO_CONFLICT",
]


@dataclass(frozen=True)
class ConditionCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class MarketContext:
    """Live closed-bar market snapshot passed into conditional evaluators."""
    time_hhmm: str
    base_levels: list[float]
    atr_5m: float | None = None
    st_dir_5m: int | None = None
    st_val_5m: float | None = None
    ema20_5m: float | None = None
    ema200_5m: float | None = None
    recent_liquidity_sweep: bool = False


@dataclass(frozen=True)
class AgentSideConfig:
    side: Side
    allow_sweep_st_override: bool  # True for CE morning sweeps, False for PE (strict ST always)
    sweep_override_until: str  # Opening reversal window end ("10:00")
    r_target_atr_mult: float  # Dynamic 1R = clamp(atr_5m * mult, r_min, r_max)
    r_target_min_pts: float
    r_target_max_pts: float
    first_sl_lock_fraction: float  # Fraction of 1R locked as guaranteed-green SL (e.g., 0.5)
    use_5m_ema20_level: bool
    use_5m_ema200_level: bool
    supertrend_period: int = 10
    supertrend_multiplier: float = 3.0
    trail_supertrend_period: int = 10
    trail_supertrend_multiplier: float = 3.0
    min_risk_atr_mult: float = 0.4
    max_risk_atr_mult: float = 3.0
    min_risk_floor_pts: float = 8.0
    max_risk_ceil_pts: float = 55.0


CE_DEFAULT_CONFIG = AgentSideConfig(
    side="CALL",
    allow_sweep_st_override=True,
    sweep_override_until="10:00",
    r_target_atr_mult=2.0,
    r_target_min_pts=25.0,
    r_target_max_pts=40.0,
    first_sl_lock_fraction=0.5,
    use_5m_ema20_level=True,
    use_5m_ema200_level=True,
    supertrend_period=10,
    supertrend_multiplier=3.0,
    trail_supertrend_period=10,
    trail_supertrend_multiplier=3.0,
    min_risk_floor_pts=8.0,
    max_risk_ceil_pts=55.0,
)

PE_DEFAULT_CONFIG = AgentSideConfig(
    side="PUT",
    allow_sweep_st_override=True,
    sweep_override_until="13:15",
    r_target_atr_mult=1.8,
    r_target_min_pts=22.0,
    r_target_max_pts=35.0,
    first_sl_lock_fraction=0.5,
    use_5m_ema20_level=True,
    use_5m_ema200_level=True,
    supertrend_period=7,
    supertrend_multiplier=2.0,
    trail_supertrend_period=10,
    trail_supertrend_multiplier=3.0,
    min_risk_floor_pts=8.0,
    max_risk_ceil_pts=40.0,
)


@dataclass(frozen=True)
class DualAgentConfig:
    ce: AgentSideConfig = CE_DEFAULT_CONFIG
    pe: AgentSideConfig = PE_DEFAULT_CONFIG
    entry_start: str = "09:30"
    entry_cutoff: str = "13:15"
    pyramid_cutoff: str = "14:30"
    square_off_time: str = "15:10"
    buffer_atr_mult: float = 0.25
    buffer_min_pts: float = 3.0
    buffer_max_pts: float = 8.0
    aligned_max_lots: int = 3
    counter_macro_max_lots: int = 2
    lot_size: int = 65
    pyramid_min_profit_atr_mult: float = 0.8
    pyramid_min_profit_floor_pts: float = 12.0
    blue_sky_atr_mult: float = 2.5
    blue_sky_min_step_pts: float = 40.0
    max_trade_risk_inr: float = 3000.0
    max_consecutive_losses: int = 2
    avoid_0dte: bool = True
    supertrend_period: int = 10
    supertrend_multiplier: float = 3.0
    displacement_check_mins: int = 10
    displacement_atr_mult: float = 0.8
    displacement_floor_pts: float = 12.0


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def resolve_dynamic_buffer(cfg: DualAgentConfig, atr_5m: float | None) -> float:
    if atr_5m is None or atr_5m <= 0:
        return (cfg.buffer_min_pts + cfg.buffer_max_pts) / 2.0
    return clamp(atr_5m * cfg.buffer_atr_mult, cfg.buffer_min_pts, cfg.buffer_max_pts)


def resolve_dynamic_1r(side_cfg: AgentSideConfig, atr_5m: float | None, initial_risk_pts: float) -> float:
    if atr_5m is not None and atr_5m > 0:
        target = atr_5m * side_cfg.r_target_atr_mult
    else:
        target = max(initial_risk_pts, side_cfg.r_target_min_pts)
    return clamp(target, side_cfg.r_target_min_pts, side_cfg.r_target_max_pts)


def resolve_dynamic_risk_band(side_cfg: AgentSideConfig, atr_5m: float | None) -> tuple[float, float]:
    if atr_5m is None or atr_5m <= 0:
        return side_cfg.min_risk_floor_pts, side_cfg.max_risk_ceil_pts
    lo = max(side_cfg.min_risk_floor_pts, atr_5m * side_cfg.min_risk_atr_mult)
    hi = min(side_cfg.max_risk_ceil_pts, max(side_cfg.min_risk_floor_pts + 10.0, atr_5m * side_cfg.max_risk_atr_mult))
    return lo, hi


def resolve_dynamic_max_lots(cfg: DualAgentConfig, side: Side, spot: float, ema200_5m: float | None) -> int:
    """Conditionally allow full pyramid lots when aligned with 5M EMA(200), else cap lower."""
    if ema200_5m is None:
        return cfg.aligned_max_lots
    macro_aligned = (spot >= ema200_5m) if side == "CALL" else (spot <= ema200_5m)
    return cfg.aligned_max_lots if macro_aligned else cfg.counter_macro_max_lots


@dataclass(frozen=True)
class StrikeSelection:
    strike: int
    step: int  # <0 ITM, 0 ATM, >0 OTM
    tier: Literal["ITM", "ATM", "OTM"]
    reason: str


def select_dynamic_strike(
    *,
    spot: float,
    side: Side,
    dte: int,
    time_hhmm: str,
    quote_and_risk_fn,
    max_risk_inr: float,
    max_premium_inr: float,
) -> StrikeSelection | None:
    """Conditionally select ITM / ATM / OTM strike by DTE, time-of-day, and ranked fallback.

    Preference order (no hardcoded fixed strike):
    - Near expiry (DTE <= 1):
        * < 11:00 IST: Shallow ITM (-1) -> ATM (0) -> OTM (+1, +2) [shields against theta burn]
        * 11:00–13:15: ATM (0) -> ITM (-1) -> OTM (+1, +2)
        * >= 13:15:    OTM (+1) -> ATM (0) -> OTM (+2)
    - Fresh/mid week (DTE >= 2):
        * < 11:00 IST: Shallow ITM (-1) -> ATM (0) -> OTM (+1, +2)
        * >= 11:00:    ATM (0) -> OTM (+1) -> OTM (+2)
    Falls back automatically down the ranked list if a strike breaches risk or capital caps.
    """
    atm = int(round(spot / 50.0) * 50)
    if dte <= 1:
        if time_hhmm < "11:00":
            steps = (-1, 0, 1, 2)
        elif time_hhmm < "13:15":
            steps = (0, -1, 1, 2)
        else:
            steps = (1, 0, 2)
    else:
        if time_hhmm < "11:00":
            steps = (-1, 0, 1, 2)
        else:
            steps = (0, 1, 2)

    for step in steps:
        strike = atm + (step * 50 if side == "CALL" else -step * 50)
        tier: Literal["ITM", "ATM", "OTM"] = "ITM" if step < 0 else ("ATM" if step == 0 else "OTM")
        est_risk, prem_cost = quote_and_risk_fn(strike)
        if est_risk <= max_risk_inr and prem_cost <= max_premium_inr:
            return StrikeSelection(
                strike=strike,
                step=step,
                tier=tier,
                reason=f"Dynamic {tier} (step={step:+d}, DTE={dte}, time={time_hhmm}, risk=Rs{est_risk:.0f})",
            )
    return None


RULES = {
    "version": VERSION,
    "live_registration": False,
    "conditional_atr_adaptive": True,
    "entry_timeframe": "1m",
    "management_timeframe": "5m",
    "next_candle_close_confirmation": True,
    "monotonic_sl_only": True,
    "avoid_0dte": True,
    "max_trade_risk_inr": 3000.0,
    "max_consecutive_losses": 2,
}
RULE_HASH = hashlib.sha256(json.dumps(RULES, sort_keys=True).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Pure Deterministic Indicator Helpers (Closed-Bar Only)
# ---------------------------------------------------------------------------


def compute_ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    out: list[float] = []
    k = 2.0 / (period + 1.0)
    for i, v in enumerate(values):
        out.append(v if i == 0 else v * k + out[-1] * (1.0 - k))
    return out


def compute_atr(bars: list[dict[str, float]], period: int = 10) -> list[float]:
    if not bars:
        return []
    tr = [bars[0]["high"] - bars[0]["low"]]
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
    return compute_ema(tr, period)


def compute_supertrend(
    bars: list[dict[str, float]],
    period: int = 10,
    multiplier: float = 3.0,
) -> tuple[list[int], list[float], list[float]]:
    """Return (st_dir, st_val, atr) where st_dir is +1 (bull/green) or -1 (bear/red)."""
    if not bars:
        return [], [], []
    atr = compute_atr(bars, period)
    st_dir = [1] * len(bars)
    st_val = [0.0] * len(bars)
    fub = [0.0] * len(bars)
    flb = [0.0] * len(bars)
    for i, bar in enumerate(bars):
        hl2 = (bar["high"] + bar["low"]) / 2.0
        ub = hl2 + multiplier * atr[i]
        lb = hl2 - multiplier * atr[i]
        if i == 0:
            fub[i], flb[i], st_val[i] = ub, lb, lb
            continue
        prev_close = bars[i - 1]["close"]
        fub[i] = ub if (ub < fub[i - 1] or prev_close > fub[i - 1]) else fub[i - 1]
        flb[i] = lb if (lb > flb[i - 1] or prev_close < flb[i - 1]) else flb[i - 1]
        if st_dir[i - 1] == 1:
            if bar["close"] < flb[i]:
                st_dir[i], st_val[i] = -1, fub[i]
            else:
                st_dir[i], st_val[i] = 1, flb[i]
        else:
            if bar["close"] > fub[i]:
                st_dir[i], st_val[i] = 1, flb[i]
            else:
                st_dir[i], st_val[i] = -1, fub[i]
    return st_dir, st_val, atr


def tighten_stop(side: Side, current_sl: float, candidate_sl: float) -> float:
    return max(current_sl, candidate_sl) if side == "CALL" else min(current_sl, candidate_sl)


def select_non_0dte_expiry(
    trade_day: date,
    holidays: set[date],
    expiry_weekday: int = 1,
    avoid_0dte: bool = True,
) -> tuple[date, int]:
    def _resolve(d: date) -> date:
        days_ahead = (expiry_weekday - d.weekday()) % 7
        exp = d + timedelta(days=days_ahead)
        while exp.weekday() >= 5 or exp in holidays:
            exp -= timedelta(days=1)
        if exp < d:
            next_base = d + timedelta(days=7 - ((d.weekday() - expiry_weekday) % 7))
            exp = next_base
            while exp.weekday() >= 5 or exp in holidays:
                exp -= timedelta(days=1)
        return exp

    exp = _resolve(trade_day)
    dte = (exp - trade_day).days
    if avoid_0dte and dte == 0:
        exp = _resolve(trade_day + timedelta(days=1))
        dte = (exp - trade_day).days
    return exp, dte


# ---------------------------------------------------------------------------
# State & Decision Data Structures
# ---------------------------------------------------------------------------


@dataclass
class PendingCross:
    side: Side
    level: float
    cross_time: str
    cross_close: float


@dataclass
class AgentProposal:
    agent_name: Literal["CEAgent", "PEAgent"]
    side: Side
    entry_spot: float
    initial_sl: float
    risk_pts: float
    conquered_level: float
    dynamic_1r_pts: float
    time_hhmm: str
    conditions: list[ConditionCheck]
    thesis_summary: str


@dataclass
class LotEntry:
    entry_spot: float
    entry_prem: float
    entered_at: str


@dataclass
class PositionState:
    side: Side
    strike: int
    expiry: str
    dte: int
    entry_time: str
    entry_spot: float
    initial_risk_pts: float
    stop_spot: float
    lots: list[LotEntry] = field(default_factory=list)
    mfe_pts: float = 0.0
    stage: ThesisStage = "PROBING"
    fast_displaced: bool = False
    pending_5m_exit: bool = False
    realized_gross_inr: float = 0.0
    total_fees_inr: float = 0.0
    adds: int = 0
    trims: int = 0
    thesis_log: list[str] = field(default_factory=list)


@dataclass
class ManagementDecision:
    action: ActionKind
    new_sl: float
    stage: ThesisStage
    reason: str
    exit_spot: float | None = None
    add_lot_allowed: bool = False
    conditions: list[ConditionCheck] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Conditional Directional Agents (CEAgent & PEAgent)
# ---------------------------------------------------------------------------


class DirectionalAgent:
    """Conditional asymmetric signal generator with explicit predicate audit trail."""

    def __init__(self, side_cfg: AgentSideConfig, root_cfg: DualAgentConfig):
        self.cfg = side_cfg
        self.root = root_cfg
        self.agent_name: Literal["CEAgent", "PEAgent"] = (
            "CEAgent" if side_cfg.side == "CALL" else "PEAgent"
        )
        self.pending: PendingCross | None = None

    def reset_day(self) -> None:
        self.pending = None

    def _effective_levels(self, ctx: MarketContext) -> list[float]:
        lvls = list(ctx.base_levels)
        if self.cfg.use_5m_ema20_level and ctx.ema20_5m is not None:
            lvls.append(ctx.ema20_5m)
        if self.cfg.use_5m_ema200_level and ctx.ema200_5m is not None:
            lvls.append(ctx.ema200_5m)
        return sorted(set(lvls))

    def _evaluate_supertrend_condition(self, ctx: MarketContext) -> ConditionCheck:
        is_call = self.cfg.side == "CALL"
        sweep_override_active = (
            self.cfg.allow_sweep_st_override
            and ctx.recent_liquidity_sweep
            and ctx.time_hhmm < self.cfg.sweep_override_until
        )
        if sweep_override_active:
            return ConditionCheck(
                "supertrend_gate",
                True,
                f"Bypassed conditionally via verified liquidity sweep before {self.cfg.sweep_override_until}",
            )
        if ctx.st_dir_5m is None:
            return ConditionCheck("supertrend_gate", True, "5M Supertrend not yet initialized")
        aligned = (ctx.st_dir_5m == 1) if is_call else (ctx.st_dir_5m == -1)
        return ConditionCheck(
            "supertrend_gate",
            aligned,
            f"5M Supertrend dir={ctx.st_dir_5m} ({'aligned' if aligned else 'opposing'})",
        )

    def on_1m_close(
        self,
        *,
        ctx: MarketContext,
        prev_e9: float,
        prev_e20: float,
        curr_close: float,
        curr_e9: float,
        curr_e20: float,
    ) -> AgentProposal | None:
        if ctx.time_hhmm < self.root.entry_start or ctx.time_hhmm >= self.root.entry_cutoff:
            self.pending = None
            return None

        levels = self._effective_levels(ctx)
        is_call = self.cfg.side == "CALL"
        buf = resolve_dynamic_buffer(self.root, ctx.atr_5m)

        # 1. Evaluate Bar t+1 confirmation conditions if a cross was armed on Bar t
        if self.pending is not None:
            pe = self.pending
            self.pending = None

            c_cross = ConditionCheck(
                "next_bar_cross_holds",
                (curr_e9 > curr_e20) if is_call else (curr_e9 < curr_e20),
                f"1M EMA9={curr_e9:.2f} vs EMA20={curr_e20:.2f}",
            )
            c_level = ConditionCheck(
                "next_bar_beyond_level",
                (curr_close > pe.level) if is_call else (curr_close < pe.level),
                f"Close={curr_close:.2f} vs Level={pe.level:.2f}",
            )
            c_st = self._evaluate_supertrend_condition(ctx)

            init_sl = (pe.level - buf) if is_call else (pe.level + buf)
            risk_pts = abs(curr_close - init_sl)
            min_r, max_r = resolve_dynamic_risk_band(self.cfg, ctx.atr_5m)
            c_risk = ConditionCheck(
                "dynamic_risk_band",
                min_r <= risk_pts <= max_r,
                f"Risk={risk_pts:.1f}pt within [{min_r:.1f}, {max_r:.1f}] (ATR={ctx.atr_5m})",
            )

            checks = [c_cross, c_level, c_st, c_risk]
            if all(ch.passed for ch in checks):
                dyn_1r = resolve_dynamic_1r(self.cfg, ctx.atr_5m, risk_pts)
                thesis = (
                    f"{self.agent_name} confirmed {self.cfg.side} at {ctx.time_hhmm}: "
                    f"1M 9/20 cross ({pe.cross_time}) confirmed on Bar t+1 beyond {pe.level:.2f}; "
                    f"SL={init_sl:.2f} (risk={risk_pts:.1f}pt, buf={buf:.1f}pt), "
                    f"dynamic 1R={dyn_1r:.1f}pt -> lock {dyn_1r * self.cfg.first_sl_lock_fraction:.1f}pt."
                )
                return AgentProposal(
                    agent_name=self.agent_name,
                    side=self.cfg.side,
                    entry_spot=curr_close,
                    initial_sl=init_sl,
                    risk_pts=risk_pts,
                    conquered_level=pe.level,
                    dynamic_1r_pts=dyn_1r,
                    time_hhmm=ctx.time_hhmm,
                    conditions=checks,
                    thesis_summary=thesis,
                )

        # 2. Check for a fresh Bar t cross strictly beyond a marked level
        if is_call:
            if prev_e9 <= prev_e20 and curr_e9 > curr_e20:
                valid = [l for l in levels if curr_close > l and curr_e9 > l and curr_e20 > l]
                if valid:
                    self.pending = PendingCross("CALL", max(valid), ctx.time_hhmm, curr_close)
        else:
            if prev_e9 >= prev_e20 and curr_e9 < curr_e20:
                valid = [l for l in levels if curr_close < l and curr_e9 < l and curr_e20 < l]
                if valid:
                    self.pending = PendingCross("PUT", min(valid), ctx.time_hhmm, curr_close)
        return None


# ---------------------------------------------------------------------------
# Arbiter & Conditional Post-Entry Thesis Manager
# ---------------------------------------------------------------------------


class DualAgentArbiter:
    """Coordinates CEAgent & PEAgent proposals and evaluates conditional position rules."""

    def __init__(self, config: DualAgentConfig = DualAgentConfig()):
        self.config = config
        self.ce_agent = DirectionalAgent(config.ce, config)
        self.pe_agent = DirectionalAgent(config.pe, config)
        self.consecutive_losses: int = 0

    def reset_day(self) -> None:
        self.ce_agent.reset_day()
        self.pe_agent.reset_day()
        self.consecutive_losses = 0

    def record_trade_outcome(self, net_pnl_inr: float) -> None:
        self.consecutive_losses = (self.consecutive_losses + 1) if net_pnl_inr <= 0 else 0

    def arbitrate_entry(
        self,
        ce_proposal: AgentProposal | None,
        pe_proposal: AgentProposal | None,
        *,
        estimated_1lot_risk_inr: float | None = None,
    ) -> tuple[ActionKind, AgentProposal | None, str]:
        if self.consecutive_losses >= self.config.max_consecutive_losses:
            return "HOLD", None, f"CIRCUIT_BREAKER ({self.consecutive_losses} consecutive losses)"
        if ce_proposal is not None and pe_proposal is not None:
            return "VETO_CONFLICT", None, "Simultaneous CE/PE conflict veto (chop)"
        chosen = ce_proposal or pe_proposal
        if chosen is None:
            return "HOLD", None, "NO_SIGNAL"
        if (
            estimated_1lot_risk_inr is not None
            and estimated_1lot_risk_inr > self.config.max_trade_risk_inr
        ):
            return (
                "HOLD",
                None,
                f"RISK_CAP_EXCEEDED ({estimated_1lot_risk_inr:.2f} > {self.config.max_trade_risk_inr:.2f})",
            )
        return "ENTER", chosen, chosen.thesis_summary

    def evaluate_position(
        self,
        pos: PositionState,
        *,
        ctx: MarketContext,
        curr_1m_high: float,
        curr_1m_low: float,
        curr_1m_close: float,
        mins_since_entry: int,
        new_5m_closed: bool,
        prev_5m: dict[str, float] | None,
        curr_5m: dict[str, float] | None,
    ) -> ManagementDecision:
        side_cfg = self.config.ce if pos.side == "CALL" else self.config.pe
        is_call = pos.side == "CALL"
        buf = resolve_dynamic_buffer(self.config, ctx.atr_5m)
        dyn_1r = resolve_dynamic_1r(side_cfg, ctx.atr_5m, pos.initial_risk_pts)
        disp_thresh = max(
            self.config.displacement_floor_pts,
            (ctx.atr_5m * self.config.displacement_atr_mult) if ctx.atr_5m else 0.0,
        )
        checks: list[ConditionCheck] = []

        # Update MFE & conditional fast-displacement check
        mfe = (curr_1m_high - pos.entry_spot) if is_call else (pos.entry_spot - curr_1m_low)
        pos.mfe_pts = max(pos.mfe_pts, mfe)
        if mins_since_entry <= self.config.displacement_check_mins and pos.mfe_pts >= disp_thresh:
            pos.fast_displaced = True
        checks.append(ConditionCheck("fast_displacement", pos.fast_displaced, f"MFE={pos.mfe_pts:.1f} vs {disp_thresh:.1f}pt"))

        # Priority 1: Hard Monotonic SL
        if (is_call and curr_1m_low <= pos.stop_spot) or (not is_call and curr_1m_high >= pos.stop_spot):
            pos.stage = "CLOSED"
            return ManagementDecision(
                "EXIT_MONOTONIC_SL", pos.stop_spot, "CLOSED",
                f"Monotonic SL hit at {pos.stop_spot:.2f}", exit_spot=pos.stop_spot, conditions=checks,
            )

        # Priority 2: EOD Square-Off
        if ctx.time_hhmm >= self.config.square_off_time:
            pos.stage = "CLOSED"
            return ManagementDecision(
                "EXIT_EOD_1510", pos.stop_spot, "CLOSED",
                f"Mandatory EOD square-off at {ctx.time_hhmm}", exit_spot=curr_1m_close, conditions=checks,
            )

        # Priority 3: 5M EMA(9/20) reverse cross (Bar t trims 1 lot, Bar t+1 exits 100%)
        if new_5m_closed and prev_5m is not None and curr_5m is not None:
            rev = (
                (prev_5m["e9"] >= prev_5m["e20"] and curr_5m["e9"] < curr_5m["e20"])
                if is_call
                else (prev_5m["e9"] <= prev_5m["e20"] and curr_5m["e9"] > curr_5m["e20"])
            )
            still_rev = (curr_5m["e9"] < curr_5m["e20"]) if is_call else (curr_5m["e9"] > curr_5m["e20"])
            if pos.pending_5m_exit:
                if still_rev:
                    pos.stage = "CLOSED"
                    return ManagementDecision(
                        "EXIT_5M_CROSS_END", pos.stop_spot, "CLOSED",
                        "Confirmed 5M EMA(9/20) reverse cross on Bar t+1", exit_spot=curr_1m_close, conditions=checks,
                    )
                pos.pending_5m_exit = False
            elif rev:
                pos.pending_5m_exit = True
                if len(pos.lots) > 1:
                    return ManagementDecision(
                        "SCALE_OUT_TRIM", pos.stop_spot, pos.stage,
                        "5M reverse cross warning on Bar t -> trim 1 lot", exit_spot=curr_1m_close, conditions=checks,
                    )

        # Priority 4: Conditional ATR-adaptive 1R -> 0.5R First-SL Lock
        if pos.mfe_pts >= dyn_1r:
            lock_pts = dyn_1r * side_cfg.first_sl_lock_fraction
            lock_sl = (pos.entry_spot + lock_pts) if is_call else (pos.entry_spot - lock_pts)
            new_sl = tighten_stop(pos.side, pos.stop_spot, lock_sl)
            if new_sl != pos.stop_spot:
                pos.stop_spot = new_sl
                if pos.stage == "PROBING":
                    pos.stage = "VALIDATED"
                    pos.thesis_log.append(
                        f"{ctx.time_hhmm}: Dynamic 1R (+{dyn_1r:.1f}pt) hit -> SL locked {pos.stop_spot:.2f}"
                    )

        # Priority 5: 5M Monotonic Level Ratchet + Supertrend Trail + Conditional Macro-Regime Pyramid
        add_allowed = False
        if new_5m_closed and curr_5m is not None:
            lvls = list(ctx.base_levels)
            if side_cfg.use_5m_ema20_level and ctx.ema20_5m is not None:
                lvls.append(ctx.ema20_5m)
            if side_cfg.use_5m_ema200_level and ctx.ema200_5m is not None:
                lvls.append(ctx.ema200_5m)
            lvls = sorted(set(lvls))

            conquered_new = False
            if is_call:
                for lvl in lvls:
                    if curr_5m["close"] > lvl and curr_5m["e9"] > lvl and curr_5m["e20"] > lvl:
                        cand = lvl - buf
                        if cand > pos.stop_spot:
                            pos.stop_spot = cand
                            conquered_new = True
                if curr_5m["st_dir"] == 1:
                    pos.stop_spot = tighten_stop("CALL", pos.stop_spot, curr_5m["st_val"])
            else:
                for lvl in reversed(lvls):
                    if curr_5m["close"] < lvl and curr_5m["e9"] < lvl and curr_5m["e20"] < lvl:
                        cand = lvl + buf
                        if cand < pos.stop_spot:
                            pos.stop_spot = cand
                            conquered_new = True
                if curr_5m["st_dir"] == -1:
                    pos.stop_spot = tighten_stop("PUT", pos.stop_spot, curr_5m["st_val"])

            dyn_max_lots = resolve_dynamic_max_lots(self.config, pos.side, curr_1m_close, ctx.ema200_5m)
            pyr_min_profit = max(
                self.config.pyramid_min_profit_floor_pts,
                (ctx.atr_5m * self.config.pyramid_min_profit_atr_mult) if ctx.atr_5m else 0.0,
            )
            blue_sky_step = max(
                self.config.blue_sky_min_step_pts,
                (ctx.atr_5m * self.config.blue_sky_atr_mult) if ctx.atr_5m else 0.0,
            )
            st_aligned = (curr_5m["st_dir"] == 1) if is_call else (curr_5m["st_dir"] == -1)
            in_blue_sky = (
                not any(lvl > curr_5m["close"] for lvl in lvls)
                if is_call
                else not any(lvl < curr_5m["close"] for lvl in lvls)
            )

            if len(pos.lots) < dyn_max_lots and ctx.time_hhmm < self.config.pyramid_cutoff:
                last_spot = pos.lots[-1].entry_spot
                profit_from_last = (curr_1m_close - last_spot) if is_call else (last_spot - curr_1m_close)
                if conquered_new and profit_from_last >= pyr_min_profit:
                    add_allowed = True
                    pos.stage = "EXPANDING"
                    checks.append(
                        ConditionCheck(
                            "pyramid_level_conquered",
                            True,
                            f"Conquered level, profit={profit_from_last:.1f}>={pyr_min_profit:.1f}pt, cap={dyn_max_lots}",
                        )
                    )
                elif in_blue_sky and st_aligned and profit_from_last >= blue_sky_step:
                    add_allowed = True
                    pos.stage = "EXPANDING"
                    checks.append(
                        ConditionCheck(
                            "pyramid_blue_sky_atr",
                            True,
                            f"Blue-sky ATR expansion, profit={profit_from_last:.1f}>={blue_sky_step:.1f}pt, cap={dyn_max_lots}",
                        )
                    )

        if add_allowed:
            return ManagementDecision(
                "PYRAMID_ADD", pos.stop_spot, pos.stage,
                f"Conquered level on 5M close; SL ratcheted to {pos.stop_spot:.2f}",
                add_lot_allowed=True, conditions=checks,
            )
        return ManagementDecision("HOLD", pos.stop_spot, pos.stage, "Thesis active", conditions=checks)


def find_equal_high_levels(
    closed_5m: list[dict[str, Any]],
    atr_5m: float | None,
    lookback_bars: int = 120,
) -> list[float]:
    """Causal 3-bar swing highs within `lookback_bars` completed 5M candles forming Equal Highs (EQH)."""
    sub = closed_5m[-lookback_bars:]
    if len(sub) < 5:
        return []
    tol = max(5.0, min(15.0, (atr_5m if atr_5m is not None and atr_5m > 0 else 15.0) * 0.5))
    swings = [
        (j, float(sub[j]["high"]))
        for j in range(1, len(sub) - 1)
        if float(sub[j]["high"]) >= float(sub[j - 1]["high"])
        and float(sub[j]["high"]) > float(sub[j + 1]["high"])
    ]
    eqh: list[float] = []
    for a in range(len(swings)):
        for b in range(a + 1, len(swings)):
            if swings[b][0] - swings[a][0] >= 2 and abs(swings[b][1] - swings[a][1]) <= tol:
                eqh.append(max(swings[a][1], swings[b][1]))
    eqh.sort()
    dedup: list[float] = []
    for lvl in eqh:
        if not dedup or lvl - dedup[-1] > tol * 0.5:
            dedup.append(lvl)
    return dedup


def detect_confirmed_eqh_reversal(
    recent_1m: list[dict[str, Any]],
    eqh_levels: list[float],
    last_5m: dict[str, Any] | None,
) -> tuple[bool, float | None]:
    """Return `(True, sweep_high)` only when an EQH sweep is confirmed by a red 5M close below 5M EMA(9) and below EQH."""
    if not recent_1m or not eqh_levels or last_5m is None:
        return False, None
    curr_close = float(recent_1m[-1]["close"])
    swept_lvl: float | None = None
    sweep_idx = -1
    for lvl in eqh_levels:
        for k in range(len(recent_1m) - 1, -1, -1):
            if float(recent_1m[k]["high"]) >= lvl:
                if curr_close < lvl:
                    swept_lvl = lvl
                    sweep_idx = k
                break
        if swept_lvl is not None:
            break
    if swept_lvl is None:
        return False, None

    e9_5 = last_5m.get("e9")
    m5_red_below_e9 = (
        float(last_5m["close"]) < float(last_5m["open"])
        and e9_5 is not None
        and float(last_5m["close"]) < float(e9_5)
        and float(last_5m["close"]) < swept_lvl
    )
    if not m5_red_below_e9:
        return False, None

    sweep_hi = max(float(b["high"]) for b in recent_1m[max(0, sweep_idx - 2) :])
    return True, sweep_hi
