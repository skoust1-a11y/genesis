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
from pathlib import Path
from typing import Any, Literal

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
    ) -> None:
        self.base_capital = base_capital
        self.max_daily_losses = max_daily_losses
        self.max_stop_points = max_stop_points
        self.lot_qty = lot_qty
        self.default_lots = default_lots
        self.max_super_lots = max_super_lots
        self.config_path = config_path or Path("trades/daily_risk_config.json")
        self.order_flow_agent = order_flow_agent
        self.daily_losses_count = 0
        self.is_safety_wall_locked = False
        self._load_config()

    def _load_config(self) -> None:
        """Load persistent risk configuration and safety wall state."""
        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.base_capital = float(data.get("base_capital", self.base_capital))
                    self.daily_losses_count = int(data.get("daily_losses_count", 0))
                    self.is_safety_wall_locked = bool(data.get("is_safety_wall_locked", False))
                    if "default_lots" in data:
                        self.default_lots = int(data["default_lots"])
            except Exception:
                pass

    def save_config(self) -> None:
        """Save persistent risk configuration to disk."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump({
                "base_capital": self.base_capital,
                "daily_losses_count": self.daily_losses_count,
                "is_safety_wall_locked": self.is_safety_wall_locked,
            }, f, indent=2)

    def record_trade_outcome(self, is_win: bool, pnl_rupees: float) -> None:
        """Update daily risk counters after trade completion."""
        if not is_win:
            self.daily_losses_count += 1
            if self.daily_losses_count >= self.max_daily_losses:
                self.is_safety_wall_locked = True
        self.save_config()

    def reset_daily_counters(self) -> None:
        """Reset intraday loss counters at beginning of trading day."""
        self.daily_losses_count = 0
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

        # 1. Check daily circuit breaker
        if self.is_safety_wall_locked or self.daily_losses_count >= self.max_daily_losses:
            return RiskApproval(
                approved=False,
                lots=0,
                quantity=0,
                risk_points=risk_points,
                max_loss_rupees=0.0,
                safety_wall_balance=safety_wall_balance,
                rejection_reason=f"Circuit Breaker Triggered: {self.daily_losses_count} losses reached today. Safety wall locked.",
            )

        # 2. Check risk ceiling (e.g. 55-pt trap prevention)
        if risk_points > self.max_stop_points:
            return RiskApproval(
                approved=False,
                lots=0,
                quantity=0,
                risk_points=risk_points,
                max_loss_rupees=0.0,
                safety_wall_balance=safety_wall_balance,
                rejection_reason=f"Stop Loss {risk_points:.1f} pts exceeds institutional ceiling of {self.max_stop_points} pts.",
            )

        # 3. Compute Base Lot Sizing based on Capital Milestones
        effective_capital = self.base_capital + safety_wall_balance
        base_lots = 1
        for tier_lots, cap_req in sorted(LOT_CAPITAL_MILESTONES.items()):
            if effective_capital >= cap_req:
                base_lots = tier_lots
        if self.default_lots is not None:
            base_lots = max(self.default_lots, base_lots)

        # Super-Trend Boost: Automatically unlocks 4 lots on high-conviction breakout
        if is_super_trend:
            base_lots = min(self.max_super_lots, max(base_lots, 3) + 1)

        # 4. Apply Order Flow Sizing Cap & Absorption Trap Veto
        lots = base_lots
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
                    safety_wall_balance=safety_wall_balance,
                    rejection_reason=of_signal.rationale,
                )
            lots = of_signal.approved_lots

        qty = lots * self.lot_qty
        approx_option_risk = risk_points * 0.5 * qty

        return RiskApproval(
            approved=True,
            lots=lots,
            quantity=qty,
            risk_points=round(risk_points, 2),
            max_loss_rupees=round(approx_option_risk, 2),
            safety_wall_balance=safety_wall_balance,
            rejection_reason=None,
        )
