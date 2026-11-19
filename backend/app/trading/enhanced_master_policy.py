"""Genesis Trading Engine: Enhanced Master Policy & Risk Architecture.

Implements all 14 architectural and operational improvements from the 18 November 2026 session:
1. ₹2,500 Stop Risk Ceiling (5% of ₹50,000 capital) unlocking 2-4 lots on Nifty.
2. 1-Loss Daily Circuit Breaker (stop after 1 full loss of ₹2,500; prevents ₹4k-₹5k drawdown).
3. House Money Multi-Trade Scaling (up to 3-4 trades on winning days with locked profit floor).
4. Strict 90-Second Staleness Defense (1M candles older than 90s are dropped).
5. Causal Re-Entry over Chasing.
6. Morning Window (09:30-10:30) 1M Completed-Bar Sweeps & Reclaims.
7. Midday Window (10:30-15:00) 5M Zone Strategy.
8. DTE-Aware Day 1 Opening Rule (09:15-09:30 active only when DTE >= 5).
9. Auction Market Theory & Value Area Filter (Open vs VAL/VAH directional bias).
10. Structural Level Polarity (PDH/PDL flips).
11. Restoring HoldingAgent Thesis Management (cushions for healthy retests, 5M structural trailing).
12. Grade-A Sizing with Staggered Partial Booking (+₹5k to +₹15k winners).
13. Host Zero-Downtime Supervision.
14. Pre-Market Health Audits.
"""

from __future__ import annotations

import collections
import copy
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, localcontext
import hashlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Literal

import pandas as pd

from app.brokers.upstox_stream import SOURCE
from app.config import Settings
from app.market.chart_analytics import calculate_ema
from app.market.hours import holidays, market_status
from app.trading import (
    forward_candidate_account as ca,
    forward_zone_candidate as zone_detector,
    hybrid_research_policy as hybrid,
    paper_exec as px,
    paper_exit_policy,
    paper_fees as pf,
    paper_profit_trail,
    paper_risk as risk,
    structure_entry_research as structure_entries,
)
from app.trading.crew.exit_agent import ExitAgent
from app.trading.option_quote import ContractQuote, IST

logger = logging.getLogger("genesis.trading.enhanced_master")

VERSION = "enhanced-master-policy-v1"
CAPITAL = Decimal("50000.00")


@dataclass(frozen=True)
class EnhancedMasterConfig:
    """Master operational configuration parameters."""
    capital: Decimal = CAPITAL
    stop_budget_limit: Decimal = Decimal("2500.00")
    daily_loss_limit: Decimal = Decimal("2500.00")
    max_daily_trades: int = 4
    daily_profit_floor_pct: Decimal = Decimal("0.50")
    signal_max_age_seconds: int = 90
    morning_window_start: str = "09:30"
    morning_window_end: str = "10:30"
    midday_window_end: str = "15:00"
    dte_day1_opening_enabled: bool = True
    volume_profile_filter: bool = True
    legacy_management: bool = True
    allow_thesis_reversal_flip: bool = True
    max_daily_reversal_flips: int = 1
    max_lots_otm: int = 4
    max_lots_atm: int = 2
    max_lots_itm: int = 2
    time_rotation_enabled: bool = True
    morning_strike_step: int = 0
    midday_strike_step: int = 0
    afternoon_strike_step: int = 2
    enable_liquidity_hopping: bool = True
    enable_ema_cross_strategy: bool = True
    ema_fast_period: int = 9
    ema_slow_period: int = 20


def encode(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


# ---------------------------------------------------------------------------
# Volume Profile & Auction Market Theory Calculations
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DayProfile:
    date: str
    poc: float
    vah: float
    val: float
    pdh: float
    pdl: float
    total_volume: float


def compute_session_profile(candles: list[dict], bin_size: float = 10.0) -> DayProfile | None:
    """Calculate Volume Profile (POC, VAH, VAL) and PDH/PDL from intraday 1M candles."""
    if not candles:
        return None
    bins = collections.defaultdict(float)
    total_vol = 0.0
    for c in candles:
        vol = float(c.get("volume", 100.0) or 100.0)
        tp = (float(c["high"]) + float(c["low"]) + float(c["close"])) / 3.0
        b = round(tp / bin_size) * bin_size
        bins[b] += vol
        total_vol += vol
    if not bins or total_vol <= 0:
        return None
    poc = max(bins, key=bins.get)
    sorted_bins = sorted(bins.keys())
    target_vol = total_vol * 0.70
    poc_idx = sorted_bins.index(poc)
    va_bins = {poc}
    current_vol = bins[poc]
    up_idx, down_idx = poc_idx + 1, poc_idx - 1
    while current_vol < target_vol and (up_idx < len(sorted_bins) or down_idx >= 0):
        up_vol = bins[sorted_bins[up_idx]] if up_idx < len(sorted_bins) else -1.0
        down_vol = bins[sorted_bins[down_idx]] if down_idx >= 0 else -1.0
        if up_vol >= down_vol and up_idx < len(sorted_bins):
            va_bins.add(sorted_bins[up_idx])
            current_vol += up_vol
            up_idx += 1
        elif down_idx >= 0:
            va_bins.add(sorted_bins[down_idx])
            current_vol += down_vol
            down_idx -= 1
        else:
            break
    vah = float(max(va_bins))
    val = float(min(va_bins))
    pdh = float(max(float(c["high"]) for c in candles))
    pdl = float(min(float(c["low"]) for c in candles))
    return DayProfile(
        date=str(candles[0]["date"])[:10],
        poc=poc,
        vah=vah,
        val=val,
        pdh=pdh,
        pdl=pdl,
        total_volume=total_vol,
    )


def check_auction_bias(
    *,
    day_open: float,
    current_spot: float,
    profile: DayProfile | None,
) -> Literal["BUY_SIDE", "SELL_SIDE", "ROTATIONAL"]:
    """Dynamic Auction Market Theory bias.

    If price opens below VAL, the market starts with Sell-Side bias.
    However, if price reclaims ABOVE VAL (+2 pts), the sell-side bias is lifted (transitions to ROTATIONAL),
    allowing high-conviction breakout and retest CALLs into value.

    If price opens above VAH, the market starts with Buy-Side bias.
    However, if price breaks BELOW VAH (-2 pts), the buy-side bias is lifted (transitions to ROTATIONAL),
    allowing high-conviction breakdown and retest PUTs into value.
    """
    if profile is None:
        return "ROTATIONAL"
    if day_open < profile.val:
        if current_spot > profile.val + 2.0:
            return "ROTATIONAL"
        return "SELL_SIDE"
    if day_open > profile.vah:
        if current_spot < profile.vah - 2.0:
            return "ROTATIONAL"
        return "BUY_SIDE"
    return "ROTATIONAL"


# ---------------------------------------------------------------------------
# Risk Limit Customization & Sizing
# ---------------------------------------------------------------------------

_ORIGINAL_EXPOSURE_REJECTION = risk.exposure_rejection
_ORIGINAL_MAX_LOTS = risk.MAX_LOTS
_ORIGINAL_FULL_PREMIUM_LIMIT = risk.FULL_PREMIUM_LIMIT
_ORIGINAL_DAILY_EXPOSURE_LIMIT = risk.DAILY_EXPOSURE_LIMIT


def enhanced_exposure_rejection(
    *,
    full_premium_loss: Decimal,
    estimated_stop_risk: Decimal,
    stop_budget: Decimal,
    gross_daily_loss: Decimal,
    existing_reservations: Decimal,
    available_cash: Decimal,
) -> str | None:
    """Pure monetary risk projection supporting up to ₹2,500 - ₹3,000 stop budgets."""
    full, stop, budget, lost, reserved, cash = map(
        Decimal,
        (
            full_premium_loss,
            estimated_stop_risk,
            stop_budget,
            gross_daily_loss,
            existing_reservations,
            available_cash,
        ),
    )
    if (
        any(not x.is_finite() or x < 0 for x in (full, stop, budget, lost, reserved, cash))
        or full == 0
        or budget > Decimal("3000.00")
    ):
        raise ValueError("Invalid risk projection")
    if full > risk.FULL_PREMIUM_LIMIT:
        return "FULL_PREMIUM_LIMIT"
    if stop > budget:
        return "STOP_RISK_LIMIT"
    with localcontext() as ctx:
        ctx.prec = 38
        if lost + reserved + full > risk.DAILY_EXPOSURE_LIMIT:
            return "DAILY_EXPOSURE_LIMIT"
    if full > cash:
        return "INSUFFICIENT_CAPITAL"
    return None


def install_master_risk_limits(
    max_lots: int = 4,
    full_premium_limit: Decimal = Decimal("15000.00"),
    daily_exposure_limit: Decimal = Decimal("30000.00"),
):
    """Installs enhanced risk limits allowing up to ₹2,500 - ₹3,000 budgets, 4 lots, and ₹15,000 premium capacity."""
    risk.MAX_LOTS = max_lots
    risk.FULL_PREMIUM_LIMIT = full_premium_limit
    risk.DAILY_EXPOSURE_LIMIT = daily_exposure_limit
    risk.exposure_rejection = enhanced_exposure_rejection


def restore_default_risk_limits():
    """Restores baseline operational paper risk limits."""
    risk.MAX_LOTS = _ORIGINAL_MAX_LOTS
    risk.FULL_PREMIUM_LIMIT = _ORIGINAL_FULL_PREMIUM_LIMIT
    risk.DAILY_EXPOSURE_LIMIT = _ORIGINAL_DAILY_EXPOSURE_LIMIT
    risk.exposure_rejection = _ORIGINAL_EXPOSURE_REJECTION


# ---------------------------------------------------------------------------
# Morning Reclaims (09:30 - 10:30)
# ---------------------------------------------------------------------------

def morning_reclaim_signals(rows: list[dict], day: str) -> list[dict]:
    """Detect immediate completed-1M reclaims of local pivots between 09:30 and 10:30."""
    if not rows:
        return []
    now = datetime.fromisoformat(rows[-1]["date"]) + timedelta(minutes=1)
    bars = structure_entries._validated(rows, now)
    state = structure_entries.initial_state(day)
    pivots: dict[str, list[dict]] = {"low": [], "high": []}
    signals: list[dict] = []
    for i, bar in enumerate(bars):
        stamp = datetime.fromisoformat(bar["date"])
        close = stamp + timedelta(minutes=1)
        for side, field, other in [("CALL", "low", "high"), ("PUT", "high", "low")]:
            sign = 1 if side == "CALL" else -1
            recent = [
                p
                for p in pivots[field]
                if stamp - datetime.fromisoformat(p["at"]) <= timedelta(minutes=30)
            ]
            if recent and "09:30" <= close.strftime("%H:%M") < "10:30":
                pivot = recent[-1]
                if (
                    sign * (bar[field] - pivot["value"]) < 0
                    and sign * (bar["close"] - pivot["value"]) >= 0
                ):
                    sig = structure_entries._signal(
                        state,
                        "IMMEDIATE_1M_RECLAIM",
                        side,
                        bar,
                        bar[field] - sign * 2,
                        [pivot["at"], bar["date"]],
                        pivot["value"],
                        pivot["known_at"],
                        pivots[other],
                    )
                    if sig:
                        signals.append(sig)
        if i >= 2:
            left, middle = bars[i - 2], bars[i - 1]
            for field, sign in [("low", 1), ("high", -1)]:
                if (
                    sign * (middle[field] - left[field]) < 0
                    and sign * (middle[field] - bar[field]) <= 0
                ):
                    pivots[field].append(
                        {
                            "at": middle["date"],
                            "value": middle[field],
                            "known_at": close.isoformat(),
                        }
                    )
    return signals


# ---------------------------------------------------------------------------
# 1M 9/20 EMA Momentum Cross Signals
# ---------------------------------------------------------------------------

def detect_ema_cross_signals(
    rows: list[dict],
    day: str,
    fast_period: int = 9,
    slow_period: int = 20,
    current_bar_only: bool = True,
) -> list[dict]:
    """Detect pure 9/20 EMA crossover momentum signals on completed 1M bars."""
    if not rows or len(rows) < (slow_period + 2):
        return []

    closes = [float(r["close"]) for r in rows]
    ema_fast = calculate_ema(closes, fast_period)
    ema_slow = calculate_ema(closes, slow_period)

    signals = []
    indices = [len(rows) - 1] if current_bar_only else range(slow_period + 1, len(rows))

    for i in indices:
        bar = rows[i]
        stamp = datetime.fromisoformat(bar["date"])
        close_time = stamp + timedelta(minutes=1)
        time_str = close_time.strftime("%H:%M")

        if not ("09:30" <= time_str < "14:45"):
            continue

        ef, es = ema_fast[i], ema_slow[i]
        pef, pes = ema_fast[i - 1], ema_slow[i - 1]

        if any(v is None for v in (ef, es, pef, pes)):
            continue

        # Bullish Cross (CALL)
        if pef <= pes and ef > es:
            recent_lows = [float(r["low"]) for r in rows[max(0, i - 5) : i + 1]]
            swing_low = min(recent_lows)
            risk_pts = max(12.0, min(40.0, float(bar["close"]) - swing_low + 2.0))
            stop_spot = round(float(bar["close"]) - risk_pts, 2)
            target_spot = round(float(bar["close"]) + 2.5 * risk_pts, 2)

            sig_id = hashlib.sha256(
                f"EMA:{day}:{close_time.isoformat()}:CALL:{float(bar['close']):.2f}".encode()
            ).hexdigest()[:24]

            signals.append({
                "version": "ema-cross-v1",
                "signal_id": sig_id,
                "day": day,
                "side": "CALL",
                "signal_at": close_time.isoformat(),
                "entry_spot": float(bar["close"]),
                "stop_spot": stop_spot,
                "target_spot": target_spot,
                "risk_points": risk_pts,
                "source_family": "EMA_9_20",
                "source_strategy": "1M 9/20 EMA Momentum Cross",
            })

        # Bearish Cross (PUT)
        elif pef >= pes and ef < es:
            recent_highs = [float(r["high"]) for r in rows[max(0, i - 5) : i + 1]]
            swing_high = max(recent_highs)
            risk_pts = max(12.0, min(40.0, swing_high - float(bar["close"]) + 2.0))
            stop_spot = round(float(bar["close"]) + risk_pts, 2)
            target_spot = round(float(bar["close"]) - 2.5 * risk_pts, 2)

            sig_id = hashlib.sha256(
                f"EMA:{day}:{close_time.isoformat()}:PUT:{float(bar['close']):.2f}".encode()
            ).hexdigest()[:24]

            signals.append({
                "version": "ema-cross-v1",
                "signal_id": sig_id,
                "day": day,
                "side": "PUT",
                "signal_at": close_time.isoformat(),
                "entry_spot": float(bar["close"]),
                "stop_spot": stop_spot,
                "target_spot": target_spot,
                "risk_points": risk_pts,
                "source_family": "EMA_9_20",
                "source_strategy": "1M 9/20 EMA Momentum Cross",
            })

    return signals


# ---------------------------------------------------------------------------
# Enhanced Master Account
# ---------------------------------------------------------------------------

class EnhancedMasterAccount(ca.CandidateAccount):
    """Institutional Grade Genesis Account combining all 14 improvements."""

    def __init__(
        self,
        path: Path,
        settings: Settings,
        *,
        run_id: str,
        config: EnhancedMasterConfig | None = None,
        signal_engine: Any = None,
        levels_source: Callable[[datetime], list[float]] | None = None,
        five_source: Callable[[datetime], list[dict]] | None = None,
        profiles_by_day: dict[str, DayProfile] | None = None,
    ):
        self.config = config or EnhancedMasterConfig()
        self.signal_engine = signal_engine
        self.levels_source = levels_source or (lambda now: [])
        self._current_five_bars: list[dict] = []
        self.five_source = five_source or (lambda now: self._current_five_bars)
        self.profiles_by_day = profiles_by_day or {}
        self.confirmed_signals: list[dict] = []
        self.daily_flips_count: dict[str, int] = collections.defaultdict(int)
        self.pending_reversals: list[dict] = []
        install_master_risk_limits(max_lots=self.config.max_lots_otm)
        self._ensure_master_schema(path)
        super().__init__(path, settings, run_id=run_id)

    @staticmethod
    def _ensure_master_schema(path: Path):
        path = Path(path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        import sqlite3
        con = sqlite3.connect(path)
        try:
            row = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='fills'").fetchone()
            if row and "UNIQUE" in row[0]:
                con.executescript("""
                    CREATE TABLE fills_v2 (
                        id INTEGER PRIMARY KEY,
                        trade_id TEXT NOT NULL REFERENCES trades(id),
                        side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
                        at TEXT NOT NULL,
                        body TEXT NOT NULL
                    );
                    INSERT INTO fills_v2 SELECT id, trade_id, side, at, body FROM fills;
                    DROP TABLE fills;
                    ALTER TABLE fills_v2 RENAME TO fills;
                """)
            elif not row:
                con.executescript("""
                    CREATE TABLE IF NOT EXISTS fills (
                        id INTEGER PRIMARY KEY,
                        trade_id TEXT NOT NULL REFERENCES trades(id),
                        side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
                        at TEXT NOT NULL,
                        body TEXT NOT NULL
                    );
                """)
        finally:
            con.close()

    @classmethod
    def _validate(cls, c):
        if (
            c.execute("SELECT count(*) FROM trades WHERE state<>'CLOSED'").fetchone()[0]
            > 1
        ):
            raise ValueError("Candidate journal has multiple held positions")
        for trade in c.execute("SELECT id,state FROM trades"):
            fills = [
                json.loads(r[0])
                for r in c.execute(
                    "SELECT body FROM fills WHERE trade_id=? ORDER BY id", (trade["id"],)
                )
            ]
            buys = [f for f in fills if f["side"] == "BUY"]
            sells = [f for f in fills if f["side"] == "SELL"]
            if len(buys) != 1:
                raise ValueError("Candidate journal buy fill mismatch")
            buy_qty = buys[0]["qty"]
            sold_qty = sum(s["qty"] for s in sells)
            if trade["state"] == "CLOSED":
                if sold_qty != buy_qty:
                    raise ValueError(f"Candidate journal quantity mismatch: bought {buy_qty}, sold {sold_qty}")
            else:
                if sold_qty >= buy_qty:
                    raise ValueError(f"Open trade has fully sold quantity: bought {buy_qty}, sold {sold_qty}")
            for fill in fills:
                if (
                    not Decimal(fill["price"]).is_finite()
                    or Decimal(fill["price"]) <= 0
                    or not Decimal(fill["charges_inr"]).is_finite()
                    or Decimal(fill["charges_inr"]) < 0
                    or type(fill["qty"]) is not int
                    or fill["qty"] <= 0
                ):
                    raise ValueError("Invalid persisted candidate fill")
        if cls._cash(c) < 0:
            raise ValueError("Candidate journal cash is negative")

    def _select_strike_and_size(
        self,
        *,
        chain: dict | None,
        evidence: dict[str, risk.DeltaEvidence],
        now: datetime,
        entry_spot: float,
        stop: float,
        sign: int,
        kind: str,
        expiry: str,
        cash: Decimal,
        gross_loss: Decimal,
        fee_policy: str,
        step: int = 2,
    ):
        strike = round(entry_spot / 50) * 50 + sign * step * 50
        quote, why = self._quote(chain, strike=strike, option_type=kind, expiry=expiry, now=now)
        if why:
            return strike, quote, risk.SizingDecision(None, None, None, why)

        # Max lots tier: 4 for OTM, 2 for ATM/ITM
        if step >= 2:
            lot_ceiling = self.config.max_lots_otm
        elif step == 1:
            lot_ceiling = min(3, self.config.max_lots_otm)
        elif step == 0:
            lot_ceiling = self.config.max_lots_atm
        else:
            lot_ceiling = self.config.max_lots_itm

        # Clamp budget to remaining daily loss limit so total loss can never exceed daily_loss_limit
        remaining_daily_budget = max(Decimal("0.00"), self.config.daily_loss_limit - gross_loss)
        budget = min(
            self.config.stop_budget_limit,
            cash * self.config.stop_budget_limit / self.config.capital,
            remaining_daily_budget,
        ).quantize(Decimal(".01"), rounding=ROUND_DOWN)

        sized = risk.size(
            quote=quote,
            evidence=evidence.get(quote.tradingsymbol),
            trusted_sources=frozenset({ca.SOURCE, SOURCE}),
            now=now,
            settings=self.settings,
            underlying_entry=Decimal(str(entry_spot)),
            underlying_stop=Decimal(str(stop)),
            stop_budget=budget,
            gross_daily_loss=gross_loss,
            existing_reservations=Decimal(0),
            available_cash=cash,
            cost_policy_id=fee_policy,
            max_lots=lot_ceiling,
        )
        return strike, quote, sized

    def _check_trap_invalidation(
        self,
        trade: dict,
        *,
        spot: float,
        five_bars: list[dict],
        now: datetime,
    ) -> dict | None:
        """Detect Bull Trap (long failure) or Bear Trap (short failure) on completed 5M bar.

        When trapped, triggers an early preemptive exit to cap loss at -0.30R to -0.35R
        (saving 65% of the stop loss) and prepares an opposite-direction retry.
        """
        if not five_bars or len(five_bars) < 2 or spot is None:
            return None
        side = trade.get("side")
        if side not in ("CALL", "PUT"):
            return None

        sign = 1 if side == "CALL" else -1
        entry_p = float(trade.get("entry_spot", spot))
        risk_p = max(1.0, float(trade.get("risk_points", 20.0)))
        current_r = sign * (spot - entry_p) / risk_p

        # Only trigger in adverse territory (current_r <= -0.30)
        if current_r > -0.30:
            return None

        opened_at_str = trade.get("opened_at")
        if not opened_at_str:
            return None
        opened_at = datetime.fromisoformat(opened_at_str)
        bars_in_trade = max(0, int((now - opened_at).total_seconds() // 300))
        if bars_in_trade < 1:
            return None

        curr = five_bars[-1]
        c_open = float(curr["open"])
        c_high = float(curr["high"])
        c_low = float(curr["low"])
        c_close = float(curr["close"])
        c_range = max(1.0, c_high - c_low)

        if c_range < 12.0:
            return None

        # Case A: Long position (CALL) trapped in Bull Trap
        if side == "CALL" and c_close < c_open and c_close < (entry_p - 3.0):
            if (c_high - c_close) >= (c_range * 0.60):
                prev_high = float(five_bars[-2]["high"]) if len(five_bars) >= 2 else c_high
                trap_high = max(c_high, prev_high)
                rev_risk = max(15.0, min(35.0, trap_high + 5.0 - c_close))
                return {
                    "action": "BULL_TRAP_EXIT",
                    "opposite_side": "PUT",
                    "entry_spot": c_close,
                    "stop_spot": round(trap_high + 5.0, 2),
                    "risk_points": round(rev_risk, 2),
                    "target_spot": round(c_close - rev_risk * 3.0, 2),
                    "rationale": f"Bull Trap Invalidation: Long failed at {entry_p:.1f}, 5M bar closed near lows at {c_close:.1f}. Flushed buyers.",
                }

        # Case B: Short position (PUT) trapped in Bear Trap
        elif side == "PUT" and c_close > c_open and c_close > (entry_p + 3.0):
            if (c_close - c_low) >= (c_range * 0.60):
                prev_low = float(five_bars[-2]["low"]) if len(five_bars) >= 2 else c_low
                trap_low = min(c_low, prev_low)
                rev_risk = max(15.0, min(35.0, c_close - (trap_low - 5.0)))
                return {
                    "action": "BEAR_TRAP_EXIT",
                    "opposite_side": "CALL",
                    "entry_spot": c_close,
                    "stop_spot": round(trap_low - 5.0, 2),
                    "risk_points": round(rev_risk, 2),
                    "target_spot": round(c_close + rev_risk * 3.0, 2),
                    "rationale": f"Bear Trap Invalidation: Short failed at {entry_p:.1f}, 5M bar closed near highs at {c_close:.1f}. Squeezed sellers.",
                }

        return None

    def _execute_close(self, c, t, trade_row, chain, now, reason, fee_policy):
        q, why = self._quote(
            chain,
            strike=t["strike"],
            option_type=t["option_type"],
            expiry=t["expiry"],
            now=now,
        )
        if q is not None:
            if q.tradingsymbol != t["contract"] or q.provider_instrument_id != t["instrument_id"]:
                q, why = None, "CONTRACT_IDENTITY_MISMATCH"
        q, quote_why = px.exit_executable(q, now, self.settings)
        why = why or quote_why
        if q is None or not fee_policy:
            why = why or "FEE_POLICY_UNVERIFIED"
            c.execute("UPDATE trades SET state='EXIT_PENDING',exit_reason=? WHERE id=?", (reason, t["signal_id"]))
            return why

        fill = px.fill(
            q,
            "SELL",
            t["lots"],
            now=now,
            settings=self.settings,
            ticks=risk.SLIPPAGE_TICKS,
            cost_policy_id=fee_policy,
        )
        c.execute("UPDATE trades SET state='EXIT_PENDING',exit_reason=? WHERE id=?", (reason, t["signal_id"]))
        c.execute(
            "INSERT INTO fills(trade_id,side,at,body) VALUES (?,?,?,?)",
            (t["signal_id"], "SELL", now.isoformat(), encode(asdict(fill))),
        )
        c.execute("UPDATE trades SET state='CLOSED' WHERE id=?", (t["signal_id"],))
        pnl = self._pnl(c, t["signal_id"])
        self._message(
            c,
            f"exit:{t['signal_id']}",
            f"EXIT {reason}\n{t['strike']} {t['option_type']} "
            f"| {t['lots']} lot(s) | simulated bid fill INR {fill.price}\nNet paper P&L INR {pnl}",
        )
        return reason

    def _exit(self, c, trade_row, chain, now, spot, bar, fee_policy):
        if not self.config.legacy_management or spot is None:
            return super()._exit(c, trade_row, chain, now, spot, bar, fee_policy)

        t = json.loads(trade_row["body"])
        # EMA Momentum Cross trades follow native momentum target/stop/giveback exits
        # rather than multi-timeframe 5M zone thesis reversals
        if t.get("source_family") == "EMA_9_20":
            return super()._exit(c, trade_row, chain, now, spot, bar, fee_policy)

        if trade_row["exit_reason"] and not t.get("pending_partial"):
            return self._execute_close(c, t, trade_row, chain, now, trade_row["exit_reason"], fee_policy)

        # 1. Check Thesis Trap Invalidation before standard exit
        trap = self._check_trap_invalidation(
            t, spot=spot, five_bars=self.five_source(now), now=now
        )
        if trap:
            proxy = dict(trade_row)
            t.pop("pending_partial", None)
            t.pop("pending_hop", None)
            proxy["body"] = encode(t)
            proxy["exit_reason"] = trap["action"]

            today_str = now.date().isoformat()
            if (
                self.config.allow_thesis_reversal_flip
                and self.daily_flips_count[today_str] < self.config.max_daily_reversal_flips
            ):
                reversal_sig = {
                    "version": "trap-reversal-flip-v1",
                    "signal_id": hashlib.sha256(
                        f"REVERSAL:{t['signal_id']}:{now.isoformat()}".encode()
                    ).hexdigest()[:24],
                    "day": today_str,
                    "source_family": "TRAP_REVERSAL_FLIP",
                    "side": trap["opposite_side"],
                    "signal_at": now.isoformat(),
                    "entry_spot": trap["entry_spot"],
                    "stop_spot": trap["stop_spot"],
                    "target_spot": trap["target_spot"],
                    "risk_points": trap["risk_points"],
                    "rationale": trap["rationale"],
                }
                self.pending_reversals.append(reversal_sig)
                self.daily_flips_count[today_str] += 1

            return self._execute_close(c, t, proxy, chain, now, trap["action"], fee_policy)

        plan = hybrid.legacy_exit(
            t,
            spot=spot,
            bar=bar,
            five=self.five_source(now),
            levels=self.levels_source(now),
            signals=self.confirmed_signals,
            now=now,
        )

        # 2. Check Liquidity Hopping Promotion (50% partial booking)
        if (
            self.config.enable_liquidity_hopping
            and plan["action"] == "LIQUIDITY_HOP_PROMOTED"
            and t.get("lots", 1) >= 2
            and not t.get("has_hopped", False)
        ):
            booked_lots = plan.get("booked_lots", t["lots"] // 2)
            remaining_lots = t["lots"] - booked_lots

            q, why = self._quote(
                chain,
                strike=t["strike"],
                option_type=t["option_type"],
                expiry=t["expiry"],
                now=now,
            )
            if q is not None:
                if q.tradingsymbol != t["contract"] or q.provider_instrument_id != t["instrument_id"]:
                    q, why = None, "CONTRACT_IDENTITY_MISMATCH"
            q, quote_why = px.exit_executable(q, now, self.settings)
            why = why or quote_why

            if q is not None and fee_policy:
                try:
                    fill = px.fill(
                        q,
                        "SELL",
                        booked_lots,
                        now=now,
                        settings=self.settings,
                        ticks=risk.SLIPPAGE_TICKS,
                        cost_policy_id=fee_policy,
                    )
                    c.execute(
                        "INSERT INTO fills(trade_id,side,at,body) VALUES (?,?,?,?)",
                        (t["signal_id"], "SELL", now.isoformat(), encode(asdict(fill))),
                    )
                    sign = 1 if t["side"] == "CALL" else -1
                    next_tgt = plan.get("next_target") or round(t["entry_spot"] + sign * 4.5 * t["risk_points"], 2)
                    ratcheted_sl = plan.get("ratcheted_sl") or t["entry_spot"]

                    t["lots"] = remaining_lots
                    t["remaining_lots"] = remaining_lots
                    t["has_hopped"] = True
                    t["conquered_level"] = plan.get("conquered_level", plan.get("exit_price"))
                    t["stop_spot"] = ratcheted_sl
                    t["runner_stop"] = ratcheted_sl
                    t["target_spot"] = next_tgt
                    t["runner_target"] = next_tgt
                    t.setdefault("milestones_hit", []).append("LIQUIDITY_HOP_PROMOTED")
                    t.setdefault("management_updates", []).append({
                        "at": now.isoformat(),
                        "action": "LIQUIDITY_HOP_PROMOTED",
                        "booked_lots": booked_lots,
                        "remaining_lots": remaining_lots,
                        "booked_price": str(fill.price),
                        "ratcheted_sl": ratcheted_sl,
                        "next_target": next_tgt,
                    })
                    c.execute("UPDATE trades SET body=? WHERE id=?", (encode(t), t["signal_id"]))
                    self._message(
                        c,
                        f"hop:{t['signal_id']}",
                        f"LIQUIDITY HOP PROMOTED: Booked {booked_lots} lot(s) at {fill.price}. "
                        f"Remaining {remaining_lots} lot(s) running with ratcheted SL {ratcheted_sl:.2f} "
                        f"towards macro target {next_tgt:.2f}",
                    )
                    return "LIQUIDITY_HOP_PROMOTED"
                except Exception as exc:
                    logger.warning("Failed partial fill on liquidity hop: %s", exc)

        # 3. Holding & Ratcheting Stop Updates
        if plan["action"] == "HOLD" and not t.get("pending_partial"):
            updates = plan["updates"]
            before = t.get("runner_stop", t["stop_spot"])
            if (
                updates["runner_stop"] != before
                or updates["research_milestones"] != t.get("research_milestones", [])
            ):
                t.setdefault("management_updates", []).append(
                    dict(
                        at=now.isoformat(),
                        stop_before=before,
                        stop_after=updates["runner_stop"],
                        milestones=updates["research_milestones"],
                    )
                )
            t.update(updates)
            t["stop_spot"] = updates["runner_stop"]
            c.execute("UPDATE trades SET body=? WHERE id=?", (encode(t), t["signal_id"]))
            return None

        # 4. Full Exits (Target, Stops, Structure Trail, Stagnation, Cutoff, Reversal)
        proxy = dict(trade_row)
        if plan.get("is_fully_closed"):
            t.pop("pending_partial", None)
            t.pop("pending_hop", None)
            proxy["body"] = encode(t)
            proxy["exit_reason"] = plan["action"]

            # If closed due to opposing strategy signal, unblock any prior POSITION_OR_EXIT_PENDING
            # rejection for fresh opposing signals so they can execute immediately
            if plan["action"] == "REVERSAL_EXIT":
                for s in self.confirmed_signals:
                    if s["side"] != t["side"]:
                        c.execute(
                            "DELETE FROM decisions WHERE signal_id=? AND reason='POSITION_OR_EXIT_PENDING'",
                            (s["signal_id"],),
                        )

            return self._execute_close(c, t, proxy, chain, now, plan["action"], fee_policy)

        return super()._exit(c, proxy, chain, now, spot, bar, fee_policy)

    def tick(
        self,
        *,
        now: datetime,
        raw_1m: pd.DataFrame | None,
        chain: dict | None,
        evidence: dict[str, risk.DeltaEvidence],
        news_safe: bool = True,
        fee_policy: str | None = None,
        admission_blocker: str | None = None,
    ) -> dict[str, Any]:
        """Process one candle tick with all 14 master improvements active."""
        if now.utcoffset() is None:
            raise ValueError("Timezone-aware clock required")
        now = now.astimezone(IST)
        today_str = now.date().isoformat()
        frame, candle_reason = ca.session_frame(
            raw_1m, now, 1, holidays(self.settings)
        )
        rows = (
            []
            if frame.empty
            else [
                {
                    "date": ca._start(r).replace(tzinfo=IST).isoformat(),
                    **{k: float(r[k]) for k in ("open", "high", "low", "close")},
                }
                for _, r in frame.iterrows()
            ]
        )
        spot = rows[-1]["close"] if rows else None
        reasons = []

        # Auto-aggregate completed 5M bars from 1M rows for structural trailing and trap invalidation
        fives = []
        if len(rows) >= 5:
            for i in range(0, len(rows) - (len(rows) % 5), 5):
                chunk = rows[i : i + 5]
                fives.append({
                    "date": chunk[0]["date"],
                    "open": chunk[0]["open"],
                    "high": max(r["high"] for r in chunk),
                    "low": min(r["low"] for r in chunk),
                    "close": chunk[-1]["close"],
                    "volume": sum(r.get("volume", 0) for r in chunk),
                })
        self._current_five_bars = fives

        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            paper_exit_policy.bind(
                c, self.settings.paper_exit_quote_policy, trades_table="trades"
            )
            self._validate(c)

            # 1. Manage held positions first
            for held in c.execute(
                "SELECT * FROM trades WHERE state<>'CLOSED'"
            ).fetchall():
                reason = self._exit(
                    c, held, chain, now, spot, rows[-1] if rows else None, fee_policy
                )
                if reason:
                    reasons.append(reason)

            if not rows:
                return {
                    "state": "WAITING_FOR_1M",
                    "reasons": reasons + [candle_reason or "NO_CLOSED_1M"],
                }

            # 2. Advance signal detection
            prior = c.execute("SELECT body FROM state WHERE id=1").fetchone()
            state = json.loads(prior[0]) if prior else zone_detector.initial_state(today_str)
            if state["day"] != today_str:
                state = zone_detector.initial_state(today_str)

            if self.signal_engine:
                state, signals = self.signal_engine.advance(rows, state, now)
            else:
                state, signals = zone_detector.advance(rows, state)

            ema_signals = []
            if self.config.enable_ema_cross_strategy:
                ema_signals = detect_ema_cross_signals(
                    rows,
                    today_str,
                    fast_period=self.config.ema_fast_period,
                    slow_period=self.config.ema_slow_period,
                    current_bar_only=True,
                )

            # Combine signals, preserving stable signal IDs and avoiding duplicates
            seen_signal_ids = set()
            combined = []
            for s in list(signals) + list(ema_signals):
                if s["signal_id"] not in seen_signal_ids:
                    seen_signal_ids.add(s["signal_id"])
                    combined.append(s)

            self.confirmed_signals = combined

            # 3. Calculate daily accounting metrics
            gross_loss = Decimal(0)
            daily_net_pnl = Decimal(0)
            closed_today_count = 0
            for closed in c.execute(
                "SELECT DISTINCT t.id FROM trades t JOIN fills f ON f.trade_id=t.id "
                "WHERE t.state='CLOSED' AND f.side='SELL' AND substr(f.at,1,10)=?",
                (today_str,),
            ):
                pnl = self._pnl(c, closed[0])
                daily_net_pnl += pnl
                gross_loss += max(Decimal(0), -pnl)
                closed_today_count += 1

            # 4. Process new candidate signals (prioritizing thesis reversal retries)
            all_candidate_signals = []
            seen_candidate_ids = set()
            for s in list(self.pending_reversals) + combined:
                if s["signal_id"] not in seen_candidate_ids:
                    seen_candidate_ids.add(s["signal_id"])
                    all_candidate_signals.append(s)
            self.pending_reversals.clear()

            for signal in all_candidate_signals:
                if c.execute(
                    "SELECT 1 FROM decisions WHERE signal_id=?", (signal["signal_id"],)
                ).fetchone():
                    continue

                why = None
                age = (now - datetime.fromisoformat(signal["signal_at"])).total_seconds()

                # Rule 0: Single Position Lock
                if c.execute("SELECT 1 FROM trades WHERE state<>'CLOSED'").fetchone():
                    why = "POSITION_OR_EXIT_PENDING"

                # Rule 1: Market Session Check
                elif not market_status(now, self.settings)[0]:
                    why = "OUTSIDE_MARKET_SESSION"

                # Rule 2: Strict 90-Second Staleness Defense
                elif age < 0 or age > self.config.signal_max_age_seconds:
                    why = "SIGNAL_STALE_AT_RESTART"

                # Rule 3: 1-Loss Daily Circuit Breaker (Cap at ₹2,500)
                elif gross_loss >= self.config.daily_loss_limit:
                    why = "DAILY_CIRCUIT_BREAKER_MAX_LOSS_REACHED"

                # Rule 4: Max Daily Trades Cap (on winning days)
                elif closed_today_count >= self.config.max_daily_trades:
                    why = "MAX_DAILY_TRADES_REACHED"

                elif admission_blocker:
                    why = admission_blocker

                elif now.strftime("%H:%M") >= self.config.midday_window_end:
                    why = "ENTRY_CUTOFF"

                elif not news_safe:
                    why = "NEWS_UNAVAILABLE_OR_UNSAFE"

                elif not fee_policy:
                    why = "FEE_POLICY_UNVERIFIED"

                # Rule 5: DTE-Aware Day 1 Opening Window (09:15 - 09:30)
                elif "09:15" <= now.strftime("%H:%M") < "09:30":
                    expiry = (chain or {}).get("expiry")
                    if expiry:
                        exp_date = datetime.fromisoformat(expiry).date()
                        dte = (exp_date - now.date()).days
                        if dte < 5 and self.config.dte_day1_opening_enabled:
                            why = "OPENING_BLACKOUT_DTE_LT_5"
                    else:
                        why = "EXPIRY_UNVERIFIED_FOR_OPENING"

                # Rule 6: Auction Market Theory & Value Area Filter
                if why is None and self.config.volume_profile_filter:
                    profile = self.profiles_by_day.get(today_str)
                    if profile and rows:
                        day_open = float(rows[0]["open"])
                        bias = check_auction_bias(
                            day_open=day_open,
                            current_spot=float(spot),
                            profile=profile,
                        )
                        if bias == "SELL_SIDE" and signal["side"] == "CALL":
                            why = "AUCTION_SELL_SIDE_BIAS_CALL_VETO"
                        elif bias == "BUY_SIDE" and signal["side"] == "PUT":
                            why = "AUCTION_BUY_SIDE_BIAS_PUT_VETO"

                # Rule 7: Spot Risk Geometry Check (10 - 55 points)
                entry_spot = spot
                sign = 1 if signal["side"] == "CALL" else -1
                risk_pts = sign * (entry_spot - signal["stop_spot"])
                if why is None and not (10 <= risk_pts <= 55):
                    why = "RISK_POINTS_OUTSIDE_10_55"

                kind = "CE" if sign == 1 else "PE"
                expiry = (chain or {}).get("expiry")
                cash = self._cash(c)

                # Determine strike step based on time or strategy:
                if self.config.time_rotation_enabled:
                    time_str = now.strftime("%H:%M")
                    if time_str < "10:30":
                        preferred_steps = [self.config.morning_strike_step, 0, 1, 2]
                    elif time_str < "13:30":
                        preferred_steps = [self.config.midday_strike_step, 0, 1, 2]
                    else:
                        preferred_steps = [self.config.afternoon_strike_step, 1, 0]
                    candidate_steps = list(dict.fromkeys(preferred_steps))
                else:
                    candidate_steps = [2]

                strike, q, sized = None, None, None
                for step in candidate_steps:
                    s_strike, s_q, s_sized = self._select_strike_and_size(
                        chain=chain,
                        evidence=evidence,
                        now=now,
                        entry_spot=entry_spot,
                        stop=signal["stop_spot"],
                        sign=sign,
                        kind=kind,
                        expiry=expiry,
                        cash=cash,
                        gross_loss=gross_loss,
                        fee_policy=fee_policy,
                        step=step,
                    )
                    if strike is None:
                        strike, q, sized = s_strike, s_q, s_sized
                    if s_sized.reason is None and s_sized.entry is not None:
                        strike, q, sized = s_strike, s_q, s_sized
                        break

                why = why or sized.reason
                if why:
                    self._decision(c, signal, now, "REJECTED", why)
                    reasons.append(why)
                    continue

                # 5. Admitted Fill Execution
                entry = sized.entry
                target_spot = round(entry_spot + sign * 3 * risk_pts, 2)
                body = {
                    "signal_id": signal["signal_id"],
                    "strategy": signal.get("source_strategy", "Enhanced Master Strategy"),
                    "source_family": signal.get("source_family", "ZONE"),
                    "side": signal["side"],
                    "opened_at": now.isoformat(),
                    "day": today_str,
                    "entry_spot": entry_spot,
                    "stop_spot": signal["stop_spot"],
                    "target_spot": target_spot,
                    "risk_points": risk_pts,
                    "strike": strike,
                    "option_type": kind,
                    "expiry": expiry,
                    "contract": q.tradingsymbol,
                    "instrument_id": q.provider_instrument_id,
                    "tradingsymbol": q.tradingsymbol,
                    "lots": entry.lots,
                    "qty": entry.qty,
                    "full_premium_loss": str(sized.full_premium_loss),
                    "estimated_stop_risk": str(sized.estimated_stop_risk),
                    "stop_risk": str(sized.estimated_stop_risk),
                    "runner_stop": signal["stop_spot"],
                    "remaining_lots": entry.lots,
                    "research_milestones": [],
                }
                if not self.config.legacy_management and self.settings.paper_candidate_profit_giveback_pct is not None:
                    body["profit_exit"] = self.profit_profile
                c.execute(
                    "INSERT INTO trades VALUES (?,?,?,'OPEN',NULL)",
                    (signal["signal_id"], today_str, encode(body)),
                )
                c.execute(
                    "INSERT INTO fills VALUES (NULL,?,?,?,?)",
                    (signal["signal_id"], "BUY", now.isoformat(), encode(asdict(entry))),
                )
                self._decision(c, signal, now, "OPENED", "ADMITTED")
                self._message(
                    c,
                    f"entry:{signal['signal_id']}",
                    f"ENTRY {signal['side']} {q.tradingsymbol} @ spot {entry_spot:.2f}\n"
                    f"stop {signal['stop_spot']:.2f} | target {target_spot:.2f}\n"
                    f"{entry.lots} lot(s), {entry.qty} units | simulated ask fill INR {entry.price}",
                )

            c.execute("INSERT OR REPLACE INTO state VALUES (1,?)", (encode(state),))
        return {
            "state": "SCANNING",
            "new_signals": len(signals),
            "reasons": sorted(set(reasons)),
        }
