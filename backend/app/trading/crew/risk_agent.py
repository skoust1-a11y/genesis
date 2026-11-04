"""RiskAgent: Institutional Sizing, Safety Wall Defense & Loss Circuit Breakers.

Responsibilities:
1. Enforces the 4-Gate Scale-Up Law & Safety Wall defined in app.trading.scaling_law.
2. Daily Loss Circuit Breaker: Enforces hard daily loss cap (e.g., 2 consecutive losses
   or max daily drawdown) to prevent tilt/revenge trading.
3. Position Sizing: Dynamically sizes lots based on available trading capital, Safety Wall
   reserves, and per-trade stop-loss risk.
4. Risk Cap Enforcement: Rejects any setup where stop-loss distance exceeds 55 points,
   enforcing tight 25-35 point micro-wick execution.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

# No method name is trusted until a real option premium is booked.
PREMIUM_PNL_METHODS: frozenset[str] = frozenset()


from app.trading.scaling_law import (
    LOT_CAPITAL_MILESTONES,
    ScaleUpLawConfig,
    ScalingTierState,
    evaluate_scaling,
    check_daily_circuit_breaker,
)


@dataclass
class RiskApproval:
    approved: bool
    lots: int
    quantity: int
    risk_points: float
    max_loss_rupees: float
    safety_wall_balance: float
    rejection_reason: str | None = None


class RiskAgent:
    """Institutional Risk Controller & Sizing Engine."""

    def __init__(
        self,
        base_capital: float = 50_000.0,
        max_daily_losses: int = 2,
        max_stop_points: float = 55.0,
        lot_qty: int = 65,
        default_lots: int | None = None,
        max_super_lots: int = 4,
        config_path: Path | None = None,
        order_flow_agent: Any | None = None,
        frozen_baseline: bool = True,
    ) -> None:
        self.base_capital = base_capital
        self.max_daily_losses = max_daily_losses
        self.max_stop_points = max_stop_points
        self.lot_qty = lot_qty
        self.default_lots = default_lots
        self.max_super_lots = max_super_lots
        self.config_path = config_path or Path("trades/daily_risk_config.json")
        self.order_flow_agent = order_flow_agent
        self.frozen_baseline = frozen_baseline
        self.safety_wall_reserve = 0.0
        self.daily_losses_count = 0
        self.daily_cumulative_r = 0.0
        self.is_safety_wall_locked = False
        self.session_date: str | None = None
        self.session_blocked = False
        self.manual_lock = False
        self._persist_manual_lock = False
        self.reserve_basis = "index_points_x_0.5_frozen"
        self._load_config()

    def _load_config(self) -> None:
        """Load persistent risk configuration and safety wall state."""
        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.base_capital = float(data.get("base_capital", self.base_capital))
                    self.safety_wall_reserve = float(data.get("safety_wall_reserve", self.safety_wall_reserve))
                    self.daily_losses_count = int(data.get("daily_losses_count", 0))
                    self.daily_cumulative_r = float(data.get("daily_cumulative_r", 0.0))
                    self.is_safety_wall_locked = bool(data.get("is_safety_wall_locked", False))
                    self.session_date = data.get("session_date")
                    if "manual_lock" in data:
                        self._persist_manual_lock = True
                        self.manual_lock = bool(data["manual_lock"])
                    if data.get("reserve_basis"):
                        self.reserve_basis = str(data["reserve_basis"])
                    if "default_lots" in data and self.default_lots is None:
                        self.default_lots = int(data["default_lots"])
            except Exception:
                # A risk file we cannot read is not a fresh day.
                self.session_blocked = True

    def save_config(self) -> None:
        """Save persistent risk configuration to disk."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "base_capital": self.base_capital,
            "safety_wall_reserve": self.safety_wall_reserve,
            "effective_capital": self.base_capital + self.safety_wall_reserve,
            "tomorrow_approved_lots": self.max_super_lots if self.default_lots is not None else 2,
            "default_lots": self.default_lots or 3,
            "daily_losses_count": self.daily_losses_count,
            "daily_cumulative_r": round(self.daily_cumulative_r, 2),
            "is_safety_wall_locked": self.is_safety_wall_locked,
            "session_date": self.session_date,
            "reserve_basis": self.reserve_basis,
        }
        if self._persist_manual_lock:
            payload["manual_lock"] = self.manual_lock
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

    def record_trade_outcome(
        self,
        is_win: bool,
        pnl_rupees: float | None = None,
        realized_r: float = 0.0,
        pnl_method: str | None = None,
    ) -> None:
        """Update daily R counters. The reserve moves only on a priced option premium."""
        priced = pnl_method in PREMIUM_PNL_METHODS and pnl_rupees is not None
        if is_win and priced and pnl_rupees > 0:
            self.safety_wall_reserve += round(pnl_rupees * 0.50, 2)

        if is_win or realized_r > 0:
            self.daily_losses_count = 0
        elif realized_r <= -0.5 or (priced and pnl_rupees is not None and pnl_rupees <= -500.0):
            self.daily_losses_count += 1

        self.daily_cumulative_r += realized_r

        # Daily Circuit Breaker: Halt if 2 consecutive losses or cumulative drawdown <= -2.0R
        if self.daily_losses_count >= self.max_daily_losses or self.daily_cumulative_r <= -2.0:
            self.is_safety_wall_locked = True

        self.save_config()

    def begin_session(self, today: date) -> bool:
        """Reset daily counters when the Asia/Kolkata date changes. A failed save blocks entries."""
        key = today.isoformat()
        if self.session_date == key and not self.session_blocked:
            return True
        snapshot = (
            self.daily_losses_count,
            self.daily_cumulative_r,
            self.is_safety_wall_locked,
            self.session_date,
        )
        self.daily_losses_count = 0
        self.daily_cumulative_r = 0.0
        if not self.manual_lock:
            self.is_safety_wall_locked = False
        self.session_date = key
        try:
            self.save_config()
        except Exception:
            self.daily_losses_count, self.daily_cumulative_r, self.is_safety_wall_locked, self.session_date = snapshot
            self.session_blocked = True
            return False
        self.session_blocked = False
        return True

    def reset_daily_counters(self) -> None:
        """Reset intraday loss counters at beginning of trading day."""
        self.daily_losses_count = 0
        self.daily_cumulative_r = 0.0
        if not self.manual_lock:
            self.is_safety_wall_locked = False
        self.save_config()

    def evaluate_order(
        self,
        entry_price: float,
        stop_loss: float,
        safety_wall_balance: float = 0.0,
        df_5m: Any | None = None,
        direction: str | None = None,
        is_super_trend: bool = False,
    ) -> RiskApproval:
        """Determine if order satisfies institutional risk rules and calculate sizing with Order Flow."""
        risk_points = abs(entry_price - stop_loss)
        eff_safety = self.safety_wall_reserve or safety_wall_balance

        # 1. Check daily circuit breaker (consecutive losses >= 2 or cumulative drawdown <= -2.0R)
        if self.session_blocked or self.manual_lock or self.is_safety_wall_locked or self.daily_losses_count >= self.max_daily_losses or self.daily_cumulative_r <= -2.0:
            if self.session_blocked:
                reason = "Session reset failed. Entries are blocked until the risk file can be saved."
            elif self.manual_lock:
                reason = "Manual lock is set. Entries are blocked."
            else:
                reason = f"Circuit Breaker Triggered: {self.daily_losses_count} losses or {self.daily_cumulative_r:.1f}R daily drawdown. Safety wall locked."
            return RiskApproval(
                approved=False,
                lots=0,
                quantity=0,
                risk_points=risk_points,
                max_loss_rupees=0.0,
                safety_wall_balance=eff_safety,
                rejection_reason=reason,
            )

        # 2. Check risk ceiling (55-pt trap prevention)
        if risk_points > self.max_stop_points:
            return RiskApproval(
                approved=False,
                lots=0,
                quantity=0,
                risk_points=risk_points,
                max_loss_rupees=0.0,
                safety_wall_balance=eff_safety,
                rejection_reason=f"Stop Loss {risk_points:.1f} pts exceeds institutional ceiling of {self.max_stop_points} pts.",
            )

        # 3. Lot Sizing Policy
        # Under Architecture C frozen baseline: STRICTLY FIXED at 3 Lots (standard) or 4 Lots (super-trend)
        if self.frozen_baseline and self.default_lots is not None:
            base_lots = self.max_super_lots if is_super_trend else self.default_lots
            lots = base_lots
        else:
            effective_capital = self.base_capital + eff_safety
            base_lots = 1
            for tier_lots, cap_req in sorted(LOT_CAPITAL_MILESTONES.items()):
                if effective_capital >= cap_req:
                    base_lots = tier_lots
            if self.default_lots is not None:
                base_lots = max(self.default_lots, base_lots)

            if is_super_trend:
                base_lots = min(self.max_super_lots, max(base_lots, 3) + 1)

            lots = base_lots
            if not is_super_trend:
                if risk_points > 40.0:
                    lots = min(lots, 2)
                elif risk_points <= 40.0 and self.default_lots is not None:
                    lots = max(lots, self.default_lots)

        # 4. Apply Order Flow Sizing Cap & Absorption Trap Veto
        if self.order_flow_agent is not None and df_5m is not None and direction is not None:
            of_signal = self.order_flow_agent.evaluate_sizing_cap(
                df_5m=df_5m,
                direction=direction,
                base_permitted_lots=base_lots,
            )
            if of_signal.is_absorption_trap:
                return RiskApproval(
                    approved=False,
                    lots=0,
                    quantity=0,
                    risk_points=risk_points,
                    max_loss_rupees=0.0,
                    safety_wall_balance=eff_safety,
                    rejection_reason=of_signal.rationale,
                )
            if not (self.frozen_baseline and self.default_lots is not None):
                lots = of_signal.approved_lots

        qty = lots * self.lot_qty
        approx_option_risk = risk_points * 0.5 * qty

        return RiskApproval(
            approved=True,
            lots=lots,
            quantity=qty,
            risk_points=round(risk_points, 2),
            max_loss_rupees=round(approx_option_risk, 2),
            safety_wall_balance=eff_safety,
            rejection_reason=None,
        )
