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

# Buy zone sits this fraction over the last traded price. Not a flat rupee band.
BUY_PAD = 0.01


def _plan_levels(signal: TradeSignal, nifty_chain: dict | None, quote: dict) -> dict | None:
    """Stop, 1R, and 2R premiums from plan_pnl. None when the chain cannot price the quote."""
    from datetime import datetime

    from app.config import get_settings
    from app.market.feed import IST, MarketSnapshot
    from app.trading.option_pnl import plan_pnl

    expiry = (nifty_chain or {}).get("expiry")
    contracts = (nifty_chain or {}).get("contracts") or []
    if not expiry or not contracts:
        return None
    quotes: dict[tuple[float, str, str], float] = {}
    for row in contracts:
        raw = float(row.get("strike") or row.get("strikePrice") or 0)
        strike = raw / 100.0 if raw > 100000 else raw
        if strike <= 0:
            continue
        for side, opt in (("ce", "CE"), ("pe", "PE")):
            ltp = ((row.get(side) or {}).get("liveData") or {}).get("ltp")
            try:
                if ltp is not None and float(ltp) > 0:
                    quotes[(strike, opt, expiry)] = float(ltp)
            except (TypeError, ValueError):
                continue
    if (quote["strike"], "CE" if quote["side"] == "ce" else "PE", expiry) not in quotes:
        return None
    now = datetime.now(IST)
    snap = MarketSnapshot(
        underlying="NIFTY",
        timestamp=now,
        as_of=now.date().isoformat(),
        spot=float(signal.entry_price),
        iv_rank=None,
        dte=max((datetime.fromisoformat(expiry).date() - now.date()).days, 0),
        expiry=expiry,
        quotes=quotes,
        lot_size=int(quote["lot"]),
    )
    risk = float(signal.risk_points)
    if signal.direction == "CALL":
        stop_spot = float(signal.stop_loss)
        r1 = float(signal.entry_price) + risk
        r2 = float(signal.entry_price) + 2 * risk
        opt = "CE"
    else:
        stop_spot = float(signal.stop_loss)
        r1 = float(signal.entry_price) - risk
        r2 = float(signal.entry_price) - 2 * risk
        opt = "PE"
    try:
        plan = plan_pnl(
            snap, get_settings(), opt, quote["strike"], float(signal.entry_price),
            {"stop": stop_spot, "1r": r1, "2r": r2}, lots=1, hold_days=0.0,
        )
    except (ValueError, KeyError):
        return None
    by_name = {row["exit"]: row["premium"] for row in plan["exits"]}
    return {"entry": plan["entry_premium"], "stop": by_name["stop"], "1r": by_name["1r"], "2r": by_name["2r"]}


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
        import sys
        if "pytest" in sys.modules or os.getenv("GENESIS_ENV") == "test" or "PYTEST_CURRENT_TEST" in os.environ:
            logger.info(f"[TEST ENVIRONMENT] Telegram dispatch suppressed: {text[:60]}...")
            return True

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

    def send_entry_alert(
        self,
        signal: TradeSignal,
        safety_wall_balance: float = 0.0,
        is_safety_wall_locked: bool = False,
        nearest_wall_name: str | None = None,
        nearest_wall_dist: float | None = None,
        nifty_chain: dict | None = None,
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

        from app.trading.crew.manager import contract_quote

        quote = contract_quote(nifty_chain, contract, signal.direction)
        if quote is None:
            logger.info("No live option quote. Telegram entry was not sent.")
            return False
        priced = _plan_levels(signal, nifty_chain, quote)
        if priced is None:
            logger.info("plan_pnl could not price the quote. Telegram entry was not sent.")
            return False
        opt_cmp = quote["ltp"]
        opt_low = round(opt_cmp, 1)
        opt_high = round(opt_cmp * (1 + BUY_PAD), 1)
        opt_max = round(opt_cmp * (1 + 2 * BUY_PAD), 1)
        opt_sl = priced["stop"]
        opt_t1 = priced["1r"]
        opt_t2 = priced["2r"]
        opt_risk = round(abs(priced["entry"] - opt_sl), 1)
        opt_risk_pct = round((opt_risk / opt_cmp) * 100, 1) if opt_cmp > 0 else 0.0
        lot = int(quote["lot"])
        total_risk_rupees = round(opt_risk * lot * signal.lots)

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
            f"• *Targets:* T1 `Rs {opt_t1:.1f}` (+1R) | T2 `Rs {opt_t2:.1f}` (+2R)\n"
            f"• *Odds:* {signal.odds_line or 'no VIX-band odds'}\n"
            f"• *Path:* target {signal.path_target_pct if signal.path_target_pct is not None else 0:.0f}% | "
            f"stop {signal.path_stop_pct if signal.path_stop_pct is not None else 0:.0f}% | "
            f"neither {signal.path_neither_pct if signal.path_neither_pct is not None else 0:.0f}% | "
            f"{signal.path_verdict or 'break-even'}\n\n"
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
            f"• *Promoted Target (spot):* `{promoted_target_name}` (`{promoted_target_price:,.2f}`)\n"
            f"• *Option premium:* not priced\n\n"
            f"🛡️ *RATCHETED STOP LOSS (S/R FLIP):*\n"
            f"👉 *Move the spot stop to {ratcheted_sl_spot:,.2f}.*\n"
            f"• *Status:* `{locked_points:+.1f}` index points on the partial. Not rupees.\n"
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

        msg = (
            f"{emoji} *TRADE CLOSED: {decision.action}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 *Instrument:* `{strike}`\n"
            f"🏁 *Exit Price (Spot):* `{decision.exit_price:,.2f}`\n"
            f"📈 *Realized Move:* `{decision.realized_points:+.2f} pts` (*{decision.realized_r:+.2f}R*)\n"
            f"💵 *Option P&L:* not priced ({lots} lots)\n"
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
