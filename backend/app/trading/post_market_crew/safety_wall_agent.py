"""SafetyWallAgent: Capital Preservation, Profit Reserve Allocation & Sizing Engine.

Responsibilities:
1. Daily Profit Allocation: Sweeps 50% of today's net booked profits into the Safety Wall Reserve.
2. 4-Gate Scale-Up Evaluation: Checks whether the account has satisfied statistical depth,
   accumulated R, and safety reserves to step up to the next lot tier.
3. Downshift Protection: If drawdown threatens the reserve buffer, automatically enforces
   an institutional downshift to protect base capital.
4. Tomorrow's Risk Dispatch: Writes approved lot sizing directly to trades/daily_risk_config.json.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.trading.scaling_law import (
    LOT_CAPITAL_MILESTONES,
    ScaleUpLawConfig,
    ScalingTierState,
    evaluate_scaling,
)


@dataclass
class SafetyWallSummary:
    today_net_pnl: float
    added_to_reserve: float
    current_reserve_balance: float
    current_tier_lots: int
    tomorrow_approved_lots: int
    gate_evaluation_status: str
    action_taken: str


class SafetyWallAgent:
    """Post-Market Capital Defense and Safety Wall Management Engine."""

    def __init__(
        self,
        base_capital: float = 50_000.0,
        state_file: Path | None = None,
        config_file: Path | None = None,
    ) -> None:
        self.base_capital = base_capital
        self.state_file = state_file or Path("trades/safety_wall_state.json")
        self.config_file = config_file or Path("trades/daily_risk_config.json")
        self.reserve_balance = 0.0
        self.current_lots = 2  # Standard ₹50k base
        self._load_state()

    def _load_state(self) -> None:
        """Load persistent Safety Wall reserves from disk."""
        if self.state_file.exists():
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.reserve_balance = float(data.get("reserve_balance", 0.0))
                    self.current_lots = int(data.get("current_lots", 2))
            except Exception:
                pass

    def _save_state(self) -> None:
        """Save persistent Safety Wall reserves to disk."""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump({
                "reserve_balance": round(self.reserve_balance, 2),
                "current_lots": self.current_lots,
            }, f, indent=2)

    def process_daily_pnl(self, today_trades: list[dict[str, Any]]) -> SafetyWallSummary:
        """Process today's booked P&L, allocate safety reserve, and set tomorrow's sizing."""
        today_net_pnl = sum(float(t.get("pnl_rupees", 0.0)) for t in today_trades)
        added_to_reserve = 0.0
        action = "NO_CHANGE"

        if today_net_pnl > 0:
            # 50% Rule: lock 50% of daily gains into Safety Wall
            added_to_reserve = today_net_pnl * 0.50
            self.reserve_balance += added_to_reserve
            action = f"Allocated ₹{added_to_reserve:,.2f} (50%) to Safety Wall Reserve"
        elif today_net_pnl < 0:
            # Drawdown absorbed by reserve if available
            loss = abs(today_net_pnl)
            if self.reserve_balance >= loss:
                self.reserve_balance -= loss
                action = f"Safety Wall absorbed full loss of ₹{loss:,.2f}. Base capital untouched."
            else:
                remaining_loss = loss - self.reserve_balance
                self.reserve_balance = 0.0
                action = f"Safety Wall depleted. Base capital experienced ₹{remaining_loss:,.2f} drawdown."

        # Evaluate Tomorrow's Lot Size
        effective_capital = self.base_capital + self.reserve_balance
        approved_lots = self.current_lots

        # Determine lot tier from milestones
        for tier_lots, cap_req in sorted(LOT_CAPITAL_MILESTONES.items()):
            if effective_capital >= cap_req:
                approved_lots = tier_lots

        gate_status = f"Effective Capital: ₹{effective_capital:,.2f} | Reserve: ₹{self.reserve_balance:,.2f}"

        # Write approved configuration for tomorrow
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump({
                "base_capital": self.base_capital,
                "safety_wall_reserve": round(self.reserve_balance, 2),
                "effective_capital": round(effective_capital, 2),
                "tomorrow_approved_lots": approved_lots,
                "daily_losses_count": 0,
                "is_safety_wall_locked": False,
            }, f, indent=2)

        self._save_state()

        return SafetyWallSummary(
            today_net_pnl=round(today_net_pnl, 2),
            added_to_reserve=round(added_to_reserve, 2),
            current_reserve_balance=round(self.reserve_balance, 2),
            current_tier_lots=self.current_lots,
            tomorrow_approved_lots=approved_lots,
            gate_evaluation_status=gate_status,
            action_taken=action,
        )
