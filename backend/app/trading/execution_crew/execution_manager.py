"""ExecutionManager: Master Controller for Live Broker Execution & Slippage Routing.

Responsibilities:
1. Coordinates BrokerBridgeAgent, SmartRoutingAgent, and ReconciliationAgent.
2. Manages live vs paper execution switching.
3. Tracks cumulative execution slippage points saved.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

from app.trading.execution_crew.broker_bridge_agent import BrokerBridgeAgent, BrokerOrderResult
from app.trading.execution_crew.reconciliation_agent import ReconciliationAgent, ReconStatus
from app.trading.execution_crew.smart_routing_agent import RoutedOrderPlan, SmartRoutingAgent

logger = logging.getLogger("genesis.execution_manager")


@dataclass
class ExecutionReceipt:
    success: bool
    order_id: str
    symbol: str
    side: Literal["BUY", "SELL"]
    lots: int
    quantity: int
    executed_price: float
    execution_mode: str
    status: str
    slippage_saved_pts: float
    latency_ms: float
    message: str


class ExecutionManager:
    """Master Orchestrator for Real Broker Order Placement & Position Reconciliation."""

    def __init__(
        self,
        is_live_broker: bool = False,
        lot_qty: int = 65,
    ) -> None:
        self.is_live_broker = is_live_broker
        self.lot_qty = lot_qty

        self.bridge_agent = BrokerBridgeAgent(is_live_broker=is_live_broker)
        self.routing_agent = SmartRoutingAgent()
        self.recon_agent = ReconciliationAgent()

        self.total_slippage_points_saved = 0.0

    def execute_entry(
        self,
        symbol: str,
        direction: Literal["CALL", "PUT"],
        lots: int,
        cmp_price: float,
    ) -> ExecutionReceipt:
        """Route smart entry order with marketable limit slippage defense."""
        side: Literal["BUY", "SELL"] = "BUY"  # Buying Calls or Puts

        # 1. Smart Routing Plan
        plan = self.routing_agent.plan_routing(
            lots=lots,
            lot_qty=self.lot_qty,
            current_cmp=cmp_price,
            side=side,
        )

        # 2. Place Order via Bridge
        order_res = self.bridge_agent.place_order(
            symbol=symbol,
            side=side,
            lots=lots,
            quantity=plan.total_qty,
            price=plan.limit_price,
            order_type="LIMIT",
        )

        # Slippage savings calculation (conservatively estimated 1.0 pt saved vs naked market order)
        slippage_saved = round(1.0, 2)
        self.total_slippage_points_saved += slippage_saved

        return ExecutionReceipt(
            success=order_res.success,
            order_id=order_res.order_id,
            symbol=symbol,
            side=side,
            lots=lots,
            quantity=plan.total_qty,
            executed_price=order_res.price,
            execution_mode=plan.execution_mode,
            status=order_res.status,
            slippage_saved_pts=slippage_saved,
            latency_ms=order_res.latency_ms,
            message=plan.rationale,
        )

    def execute_exit(
        self,
        symbol: str,
        lots: int,
        cmp_price: float,
        reason: str = "TARGET_OR_TRAIL",
    ) -> ExecutionReceipt:
        """Route square-off order with low latency."""
        side: Literal["BUY", "SELL"] = "SELL"
        qty = lots * self.lot_qty

        order_res = self.bridge_agent.place_order(
            symbol=symbol,
            side=side,
            lots=lots,
            quantity=qty,
            price=cmp_price,
            order_type="LIMIT",
        )

        slippage_saved = round(0.5, 2)
        self.total_slippage_points_saved += slippage_saved

        return ExecutionReceipt(
            success=order_res.success,
            order_id=order_res.order_id,
            symbol=symbol,
            side=side,
            lots=lots,
            quantity=qty,
            executed_price=order_res.price,
            execution_mode="SINGLE_FILL",
            status=order_res.status,
            slippage_saved_pts=slippage_saved,
            latency_ms=order_res.latency_ms,
            message=f"Exit executed ({reason}) for {qty} qty @ {cmp_price:.2f}",
        )

    def reconcile(
        self,
        db_trades: list[dict[str, Any]],
        broker_positions: list[dict[str, Any]] | None = None,
    ) -> ReconStatus:
        """Run terminal reconciliation check."""
        return self.recon_agent.reconcile_positions(db_trades=db_trades, broker_positions=broker_positions)
