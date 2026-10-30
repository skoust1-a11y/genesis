"""AlertAgent: Telegram Notification Dispatcher with Ticket Threads & CMP Heartbeats.

Responsibilities:
1. Entry Ticket Alerts: Dispatches clear, structured Telegram messages with exact strike,
   lots, stop loss, dynamic target, and strategy rationale.
2. Milestone & Trailing Updates: Alerts user on +1R, +2R milestones and structure trails.
3. Exit & P&L Dispatch: Broadcasts realized points, R-multiple, and rupee P&L.
4. Heartbeat Monitor: Sends periodic CMP progress updates during active trades.
5. Chop / Runway Veto Alerts: Transparently notifies user when a low-probability trap is avoided.
"""

from __future__ import annotations

import logging
from typing import Any, Literal
import requests

from app.trading.crew.entry_agent import TradeSignal
from app.trading.crew.exit_agent import ExitDecision

logger = logging.getLogger("genesis.alert_agent")


class AlertAgent:
    """Institutional Telegram Notification & Ticket Alert Engine."""

    def __init__(
        self,
        bot_token: str | None = None,
        chat_id: str | None = None,
        is_paper_trading: bool = True,
    ) -> None:
        # Defaults to project settings if not provided
        self.bot_token = bot_token or "8422787103:AAGfXnI-L4l-bI1pP7y3cT2d7kY"
        self.chat_id = chat_id or "-1002235941641"
        self.is_paper_trading = is_paper_trading

    def _send(self, text: str) -> bool:
        """Send message via Telegram Bot API with error handling."""
        if not self.bot_token or not self.chat_id:
            logger.warning("Telegram bot_token or chat_id not configured.")
            return False
            
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        try:
            resp = requests.post(url, json=payload, timeout=8)
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"Failed to dispatch Telegram message: {e}")
            return False

    def send_entry_alert(self, signal: TradeSignal) -> bool:
        """Dispatch standard Trade Trigger alert."""
        direction_emoji = "🟢 🚀" if signal.direction == "CALL" else "🔴 📉"
        tag = "⚡ *TRADE TRIGGER*" if not self.is_paper_trading else "🧪 *TRIGGER (Forward Test)*"

        msg = (
            f"{tag} {direction_emoji}\n\n"
            f"🎯 *Strategy:* {signal.strategy}\n"
            f"📊 *Instrument:* `{signal.suggested_strike}`\n"
            f"📍 *Direction:* {signal.direction}\n"
            f"💰 *Entry (Spot):* `{signal.entry_price:.2f}`\n"
            f"🛑 *Stop Loss:* `{signal.stop_loss:.2f}` ({signal.risk_points:.1f} pts risk)\n"
            f"🏁 *Target:* `{signal.target_price:.2f}` ({signal.target_points:.1f} pts / *+{signal.rr_ratio:.1f}R*)\n"
            f"📦 *Position Size:* `{signal.lots} Lots` ({signal.quantity} Qty)\n"
            f"📈 *Regime / ADX:* {signal.regime} (ADX: {signal.adx:.1f})\n"
            f"📝 *Rationale:* _{signal.rationale}_\n"
            f"⏰ *Time:* {signal.timestamp}"
        )
        return self._send(msg)

    def send_milestone_alert(self, symbol: str, milestone_name: str, cmp: float, move_pts: float) -> bool:
        """Dispatch milestone update (+1R, +2R, Trailing stop to breakeven)."""
        msg = (
            f"🔔 *MILESTONE REACHED* 🔔\n\n"
            f"📊 *Asset:* {symbol}\n"
            f"🏆 *Milestone:* `{milestone_name}`\n"
            f"📍 *CMP:* `{cmp:.2f}` ({move_pts:+.2f} pts in profit)\n"
            f"🛡️ *Action:* Trailing stop engaged to protect capital."
        )
        return self._send(msg)

    def send_exit_alert(
        self,
        symbol: str,
        strike: str,
        decision: ExitDecision,
        lots: int,
        lot_qty: int = 65,
    ) -> bool:
        """Dispatch final trade exit and realized P&L report."""
        is_profit = decision.realized_points > 0
        emoji = "🎉 💰" if is_profit else "🛑 ⚠️"
        total_qty = lots * lot_qty
        # Approx rupee P&L using 0.5 option delta
        approx_pnl_rupees = decision.realized_points * 0.5 * total_qty

        msg = (
            f"{emoji} *TRADE CLOSED: {decision.action}*\n\n"
            f"📊 *Instrument:* `{strike}`\n"
            f"🏁 *Exit Price (Spot):* `{decision.exit_price:.2f}`\n"
            f"📈 *Realized Move:* `{decision.realized_points:+.2f} pts` (*{decision.realized_r:+.2f}R*)\n"
            f"💵 *Estimated P&L:* `₹{approx_pnl_rupees:+,.2f}` on {lots} Lots\n"
            f"📋 *Reason:* _{decision.exit_reason}_"
        )
        return self._send(msg)

    def send_veto_alert(self, strategy: str, veto_reason: str) -> bool:
        """Notify user when a chop trap or congested runway is avoided."""
        msg = (
            f"🛡️ *ENTRY VETOED (Capital Preserved)*\n\n"
            f"Strategy: `{strategy}`\n"
            f"Reason: _{veto_reason}_\n"
            f"Action: Preserved capital; waiting for clean trending structure."
        )
        return self._send(msg)
