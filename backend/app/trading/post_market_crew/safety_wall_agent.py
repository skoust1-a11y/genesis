"""SafetyWallAgent: Capital Preservation, Profit Reserve Allocation & Sizing Engine.

Responsibilities:
1. Report today's priced net. Unpriced rows leave the reserve where it was loaded.
2. Do not move reserve_balance, current lots, or tomorrow's lot tier.
3. Do not open the risk file and do not call _save_state from the P&L path.
4. Tomorrow's Risk Dispatch: the priced net figure stays. Neither file is written.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Empty until a real option premium exists. Unpriced index points must not move the reserve.
PREMIUM_PNL_METHODS: frozenset[str] = frozenset()


@dataclass
class SafetyWallSummary:
    today_net_pnl: float | None
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
        """Report today's priced net. The reserve and both files stay where they were loaded."""
        priced = [
            float(t["pnl_rupees"])
            for t in today_trades
            if t.get("pnl_method") in PREMIUM_PNL_METHODS and t.get("pnl_rupees") is not None
        ]
        if not priced:
            return SafetyWallSummary(
                today_net_pnl=None,
                added_to_reserve=0.0,
                current_reserve_balance=round(self.reserve_balance, 2),
                current_tier_lots=self.current_lots,
                tomorrow_approved_lots=self.current_lots,
                gate_evaluation_status=f"Reserve unchanged at ₹{self.reserve_balance:,.2f}. Option rupees are not priced.",
                action_taken="Reserve unchanged. Option rupees are not priced, so the safety wall did not move.",
            )

        today_net_pnl = sum(priced)
        return SafetyWallSummary(
            today_net_pnl=round(today_net_pnl, 2),
            added_to_reserve=0.0,
            current_reserve_balance=round(self.reserve_balance, 2),
            current_tier_lots=self.current_lots,
            tomorrow_approved_lots=self.current_lots,
            gate_evaluation_status=(
                f"Effective Capital: ₹{self.base_capital + self.reserve_balance:,.2f} | "
                f"Reserve: ₹{self.reserve_balance:,.2f}"
            ),
            action_taken="Reserve was not moved and the risk file was not written.",
        )
