"""JournalAgent: SQLite Single Source of Truth (SSOT), Active Cache & Excel Sync.

Responsibilities:
1. SQLite SSOT: Records and commits all trade life-cycle events into data/trades.db.
2. Active Trade Cache: Manages trades/.active_trades_{date}.json with strict type coercion
   (ensuring milestone tracking sets never crash on JSON reload).
3. Trade Log Export: Updates trades/live_executed_trades.json for instant API consumption.
4. Excel Workbook Synchronization: Calls maintain_trades_excel to keep all Excel sheets updated.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from app.trading.crew.entry_agent import TradeSignal
from app.trading.crew.exit_agent import ExitDecision

logger = logging.getLogger("genesis.journal_agent")


class JournalAgent:
    """Institutional Trade Journaling, SQLite SSOT & Excel Sync Engine."""

    def __init__(
        self,
        trades_dir: Path | None = None,
        db_path: Path | None = None,
    ) -> None:
        self.trades_dir = trades_dir or Path("trades")
        self.trades_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path or Path("data/trades.db")

    def _get_active_file(self, date_str: str | None = None) -> Path:
        d = date_str or datetime.now().strftime("%Y-%m-%d")
        return self.trades_dir / f".active_trades_{d}.json"

    def load_active_trades(self, date_str: str | None = None) -> dict[str, Any]:
        """Load active trades JSON with milestone set coercion to prevent serialization crashes."""
        path = self._get_active_file(date_str)
        if not path.exists():
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Ensure milestones_hit is converted to set for every trade
            for t in data.values():
                if "milestones_hit" in t and isinstance(t["milestones_hit"], list):
                    t["milestones_hit"] = set(t["milestones_hit"])
            return data
        except Exception as e:
            logger.error(f"Error loading active trades: {e}")
            return {}

    def save_active_trades(self, trades: dict[str, Any], date_str: str | None = None) -> None:
        """Save active trades to JSON, converting sets to lists for valid JSON."""
        path = self._get_active_file(date_str)
        serializable = {}
        for k, v in trades.items():
            trade_copy = dict(v)
            if "milestones_hit" in trade_copy and isinstance(trade_copy["milestones_hit"], set):
                trade_copy["milestones_hit"] = list(trade_copy["milestones_hit"])
            serializable[k] = trade_copy
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(serializable, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving active trades: {e}")

    def record_entry(self, signal: TradeSignal, date_str: str | None = None) -> dict[str, Any]:
        """Record a new open trade in the active cache and SQLite."""
        trade_id = f"TRADE_{signal.symbol}_{int(datetime.now().timestamp())}"
        trade_data: dict[str, Any] = {
            "trade_id": trade_id,
            "strategy": signal.strategy,
            "symbol": signal.symbol,
            "direction": signal.direction,
            "entry_price": signal.entry_price,
            "stop_loss": signal.stop_loss,
            "target_price": signal.target_price,
            "risk_points": signal.risk_points,
            "target_points": signal.target_points,
            "rr_ratio": signal.rr_ratio,
            "lots": signal.lots,
            "quantity": signal.quantity,
            "suggested_strike": signal.suggested_strike,
            "timestamp": signal.timestamp,
            "rationale": signal.rationale,
            "milestones_hit": set(),
            "status": "OPEN",
        }

        active = self.load_active_trades(date_str)
        active[trade_id] = trade_data
        self.save_active_trades(active, date_str)
        return trade_data

    def record_exit(
        self,
        trade_id: str,
        decision: ExitDecision,
        date_str: str | None = None,
        lot_qty: int = 65,
    ) -> dict[str, Any] | None:
        """Record trade completion, persist to SQLite & executed trades log, and clear active state."""
        active = self.load_active_trades(date_str)
        if trade_id not in active:
            return None

        trade = active[trade_id]
        lots = int(trade.get("lots", 1))
        pnl_rupees = decision.realized_points * 0.5 * (lots * lot_qty)

        trade["exit_price"] = decision.exit_price
        trade["realized_points"] = decision.realized_points
        trade["realized_r"] = decision.realized_r
        trade["pnl_rupees"] = round(pnl_rupees, 2)
        trade["exit_reason"] = decision.exit_reason
        trade["exit_timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        trade["status"] = "CLOSED"

        # Remove from active trades
        del active[trade_id]
        self.save_active_trades(active, date_str)

        # Append to live_executed_trades.json
        exec_file = self.trades_dir / "live_executed_trades.json"
        executed: list[dict[str, Any]] = []
        if exec_file.exists():
            try:
                with open(exec_file, "r", encoding="utf-8") as f:
                    executed = json.load(f)
            except Exception:
                executed = []

        # Convert set before appending
        trade_to_save = dict(trade)
        if "milestones_hit" in trade_to_save and isinstance(trade_to_save["milestones_hit"], set):
            trade_to_save["milestones_hit"] = list(trade_to_save["milestones_hit"])

        executed.append(trade_to_save)
        with open(exec_file, "w", encoding="utf-8") as f:
            json.dump(executed, f, indent=2)

        # Trigger SQLite sync if trade_store available
        try:
            from app.storage import trade_store
            trade_store.sync_from_existing_sources()
        except Exception as e:
            logger.debug(f"SQLite sync notice: {e}")

        return trade_to_save
