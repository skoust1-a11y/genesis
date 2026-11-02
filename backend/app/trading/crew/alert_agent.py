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
import os
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
        # Defaults to project environment settings
        self.bot_token = (
            bot_token
            or os.getenv("ALERT_TELEGRAM_BOT_TOKEN")
            or os.getenv("TELEGRAM_BOT_TOKEN")
            or "8910290265:AAHJ7rHpcd9Y97hcKfGvCbbSwD0PZoK4aks"
        )
        self.chat_id = (
            chat_id
            or os.getenv("SIGNALS_TELEGRAM_CHAT_ID")
            or os.getenv("ALERT_TELEGRAM_CHAT_ID")
            or "-5301947207"
        )
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

    def _fetch_live_option_pricing(self, contract: str, spot: float, direction: str) -> tuple[float, float]:
        """Fetch real-time option LTP and delta from live broker feed, with Black-76 fallback."""
        try:
            import re
            from app.brokers.groww import GrowwQuotes
            g = GrowwQuotes()
            page = g._page("NIFTY")
            contracts = page.get("optionChain", {}).get("optionContracts", [])
            m = re.search(r"(\d+)\s*(CE|PE)", contract.upper())
            if m:
                strike_val = float(m.group(1))
                opt_type = m.group(2).lower()
                for c in contracts:
                    if c.get("strikePrice", 0) / 100 == strike_val:
                        info = c.get(opt_type, {})
                        ltp = float(info.get("liveData", {}).get("ltp") or 0.0)
                        delta = abs(float(info.get("greeks", {}).get("delta") or 0.40))
                        if ltp > 0:
                            return ltp, delta
        except Exception as e:
            logger.debug(f"Live Groww option quote fetch fallback: {e}")

        # Fallback to Black-76 with standard market DTE and IV
        try:
            import re
            from app.trading.option_pnl import black76
            m = re.search(r"(\d+)\s*(CE|PE)", contract.upper())
            strike_val = float(m.group(1)) if m else spot
            opt_type = m.group(2).upper() if m else ("CE" if direction == "CALL" else "PE")
            years = 1.0 / 365.0
            p = black76(forward=spot, strike=strike_val, years=years, iv=0.125, option_type=opt_type)
            return round(max(p, 5.0), 1), 0.35
        except Exception:
            return 50.0, 0.40

    def send_entry_alert(
        self,
        signal: TradeSignal,
        safety_wall_balance: float = 0.0,
        is_safety_wall_locked: bool = False,
        nearest_wall_name: str | None = None,
        nearest_wall_dist: float | None = None,
    ) -> bool:
        """Dispatch institutional Option Entry Zone ticket alert with Directional Thesis."""
        direction_emoji = "🟢 🚀" if signal.direction == "CALL" else "🔴 📉"
        tag = "🎯 *OPTION ENTRY ZONE TICKET*" if not self.is_paper_trading else "🧪 *OPTION ENTRY ZONE TICKET (Forward Test)*"

        # Resolve strike name
        contract = signal.suggested_strike
        if "CE" not in contract and "PE" not in contract:
            step = 50
            atm_strike = round(signal.entry_price / step) * step
            contract = f"NIFTY {int(atm_strike)} {'CE' if signal.direction == 'CALL' else 'PE'}"

        # Real Live Option Pricing & Dynamic Delta
        live_ltp, live_delta = self._fetch_live_option_pricing(contract, signal.entry_price, signal.direction)
        opt_cmp = round(live_ltp, 1)
        opt_low = round(max(1.0, opt_cmp - 2.5), 1)
        opt_high = round(opt_cmp + 2.0, 1)
        opt_max = round(opt_cmp + 4.0, 1)

        # Precise option stop loss & staged targets using actual Delta
        delta_eff = max(0.25, min(0.70, live_delta))
        opt_sl = round(max(1.0, opt_cmp - (signal.risk_points * delta_eff)), 1)
        opt_risk = round(opt_cmp - opt_sl, 1)
        opt_risk_pct = round((opt_risk / opt_cmp) * 100, 1) if opt_cmp > 0 else 15.0
        per_lot_risk = round(opt_risk * 65)
        total_risk_rupees = per_lot_risk * signal.lots

        # Staged Option Profit Targets
        target_pts = signal.target_points or (signal.risk_points * signal.rr_ratio)
        opt_t1 = round(opt_cmp + (target_pts * 0.40 * delta_eff), 1)
        opt_t2 = round(opt_cmp + (target_pts * 0.70 * delta_eff), 1)
        opt_t3 = round(opt_cmp + (target_pts * 1.00 * delta_eff), 1)

        # Directional Thesis details
        primary_bet = signal.thesis.primary_bet if signal.thesis else (
            f"Bullish BSL Expansion to {signal.target_price:,.1f}" if signal.direction == "CALL" else f"Bearish SSL Expansion to {signal.target_price:,.1f}"
        )
        targeted_pool = signal.thesis.targeted_pool if signal.thesis else signal.liquidity_pool
        invalidation_price = signal.thesis.invalidation_price if signal.thesis else signal.stop_loss
        invalidation_cond = signal.thesis.invalidation_condition if signal.thesis else (
            f"5M candle body close below {signal.stop_loss:,.1f} kills thesis" if signal.direction == "CALL" else f"5M candle body close above {signal.stop_loss:,.1f} kills thesis"
        )
        stagnation_bars = signal.thesis.stagnation_timeout_bars if signal.thesis else 6
        gamma_clearance = signal.thesis.gamma_wall_clearance_pts if signal.thesis else (nearest_wall_dist if nearest_wall_dist is not None else 100.0)
        conviction = signal.thesis.thesis_conviction if signal.thesis else ("SUPER_TREND" if signal.lots >= 4 else "HIGH")
        wall_name = nearest_wall_name or ("Call Wall" if signal.direction == "CALL" else "Put Wall")
        safety_status = f"`₹{safety_wall_balance:,.2f}` Locked ({'Halted ⛔' if is_safety_wall_locked else 'Active & Guarded 🛡️'})" if safety_wall_balance > 0 else "Guarded Baseline 🛡️"

        # Compact, high-signal mobile layout
        time_display = signal.timestamp[-8:] if len(signal.timestamp) >= 8 else signal.timestamp
        msg = (
            f"{tag} {direction_emoji}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 *Contract:* `{contract}`\n"
            f"⚡ *Strategy:* {signal.strategy} | *Regime:* {signal.regime} (ADX {signal.adx:.0f})\n"
            f"💰 *Size:* `{signal.lots} Lots` ({signal.quantity} Qty) | *Risk:* ~₹{total_risk_rupees:,.0f}\n\n"
            f"🎯 *OPTION TRADE PLAN:*\n"
            f"• *Buy Range:* `Rs {opt_low:.1f} – Rs {opt_high:.1f}` (CMP ~Rs {opt_cmp:.1f})\n"
            f"• *Max Chase Limit:* `Rs {opt_max:.1f}`\n"
            f"• *Hard SL Price:* `Rs {opt_sl:.1f}` (-{opt_risk_pct:.0f}% | {opt_risk:.1f} pts)\n"
            f"• *Targets:* T1 `Rs {opt_t1:.1f}` (+1R) | T2 `Rs {opt_t2:.1f}` (+2R) | T3 `Rs {opt_t3:.1f}+`\n\n"
            f"📍 *SPOT & LEVELS:*\n"
            f"• *Index:* Entry `{signal.entry_price:,.1f}` | SL `{signal.stop_loss:,.1f}` | Target `{signal.target_price:,.1f}`\n"
            f"• *Nearest Wall:* `{wall_name}` ({gamma_clearance:,.0f} pts clearance)\n"
            f"• *Thesis:* {primary_bet}\n\n"
            f"⚙️ *EXECUTION RULES:*\n"
            f"1. Limit buy in zone `[Rs {opt_low:.1f} - Rs {opt_high:.1f}]` | DO NOT CHASE > `Rs {opt_max:.1f}`\n"
            f"2. Hard SL at `Rs {opt_sl:.1f}` | Move SL to Cost once T1 (+1R) is hit\n"
            f"3. Book 50% at T2 (+2R) & trail runner | 30m chop timeout ({stagnation_bars} bars)\n"
            f"⏰ *Time:* `{time_display}` IST"
        )
        return self._send(msg)

    def send_trailing_update(
        self,
        symbol: str,
        strike: str,
        milestone: str,
        cmp: float,
        r_multiple: float,
        opt_premium: float,
        new_sl: float,
        action_instruction: str,
        status_note: str = "",
    ) -> bool:
        """Dispatch post-entry trailing stop and milestone alert."""
        msg = (
            f"🔔 *POST-ENTRY TRAILING UPDATE ({milestone})* 🔔\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 *Asset:* `{strike}`\n"
            f"📍 *Spot CMP:* `{cmp:,.2f}` (*{r_multiple:+.2f}R* in profit)\n"
            f"📈 *Estimated Option CMP:* `~Rs {opt_premium:,.2f}`\n\n"
            f"🛡️ *TRAILING ACTION REQUIRED:*\n"
            f"👉 *{action_instruction}*\n"
            f"• *New Trailing SL:* `Rs {new_sl:,.2f}`\n"
            f"• *Status:* _{status_note}_\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )
        return self._send(msg)

    def send_liquidity_hop_alert(
        self,
        symbol: str,
        strike: str,
        conquered_level_name: str,
        conquered_price: float,
        promoted_target_name: str,
        promoted_target_price: float,
        ratcheted_sl_spot: float,
        ratcheted_sl_opt: float,
        projected_opt_target: float,
        locked_points: float,
        remaining_lots: int,
    ) -> bool:
        """Dispatch Level Migration / Liquidity Hopping alert."""
        msg = (
            f"🚀 *LIQUIDITY POOL CONQUERED & PROMOTED* 🚀\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 *Asset:* `{strike}`\n"
            f"⚡ *Conquered Pool:* `{conquered_level_name}` (`{conquered_price:,.2f}`)\n\n"
            f"🔄 *DYNAMIC RE-EVALUATION:*\n"
            f"• *Conquered Level:* `{conquered_price:,.2f}` (Now Structural Floor)\n"
            f"• *Promoted Target:* `{promoted_target_name}` (`{promoted_target_price:,.2f}`)\n"
            f"• *Projected Option Premium:* `Rs {projected_opt_target:,.2f}+`\n\n"
            f"🛡️ *RATCHETED STOP LOSS (S/R FLIP):*\n"
            f"👉 *Move Stop Loss higher to Rs {ratcheted_sl_opt:,.2f} (Spot {ratcheted_sl_spot:,.2f})!*\n"
            f"• *Status:* `+{locked_points:.1f} pts` option profit locked in. Zero downside risk.\n"
            f"• *Action:* Remaining `{remaining_lots} Lots` attacking `{promoted_target_name}` pool!\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )
        return self._send(msg)

    def send_milestone_alert(self, symbol: str, milestone_name: str, cmp: float, move_pts: float) -> bool:
        """Dispatch milestone update (+1R, +2R, Trailing stop to breakeven)."""
        msg = (
            f"🔔 *MILESTONE REACHED* 🔔\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 *Asset:* {symbol}\n"
            f"🏆 *Milestone:* `{milestone_name}`\n"
            f"📍 *CMP:* `{cmp:.2f}` ({move_pts:+.2f} pts in profit)\n"
            f"🛡️ *Action:* Trailing stop engaged to protect capital.\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
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
        approx_opt_pts = decision.realized_points * 0.5

        msg = (
            f"{emoji} *TRADE CLOSED: {decision.action}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 *Instrument:* `{strike}`\n"
            f"🏁 *Exit Price (Spot):* `{decision.exit_price:,.2f}`\n"
            f"📈 *Realized Move:* `{decision.realized_points:+.2f} pts` (*{decision.realized_r:+.2f}R*)\n"
            f"🎯 *Option Points:* `{approx_opt_pts:+.2f} pts`\n"
            f"💵 *Estimated P&L:* `₹{approx_pnl_rupees:+,.2f}` on {lots} Lots ({total_qty} Qty)\n"
            f"📋 *Reason:* _{decision.exit_reason}_\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
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
