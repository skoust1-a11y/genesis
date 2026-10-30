"""TradeManager: Master Orchestrator for the Live Trading Crew.

Coordinates execution across:
- EntryAgent (Setup detection, micro-wick execution, tight stops)
- TrendAgent (Macro trend slope, ADX/chop veto, dynamic R:R)
- RiskAgent (Capital gates, Safety Wall, daily loss circuit breaker)
- ExitAgent (Unconditional Target 3 exit, 5M structure trail, opposing reversal exit)
- AlertAgent (Telegram dispatches, ticket threads, CMP updates)
- JournalAgent (Active cache, SQLite SSOT, Excel synchronization)
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
import pandas as pd

from app.trading.crew.alert_agent import AlertAgent
from app.trading.crew.entry_agent import EntryAgent, TradeSignal
from app.trading.crew.exit_agent import ExitAgent, ExitDecision
from app.trading.crew.journal_agent import JournalAgent
from app.trading.crew.risk_agent import RiskAgent
from app.trading.crew.trend_agent import TrendAgent

logger = logging.getLogger("genesis.trade_manager")


class TradeManager:
    """Institutional Trade Orchestration Engine for Live Market Operations."""

    def __init__(
        self,
        base_capital: float = 50_000.0,
        is_paper_trading: bool = True,
        trades_dir: Path | None = None,
        db_path: Path | None = None,
    ) -> None:
        self.trend_agent = TrendAgent()
        self.risk_agent = RiskAgent(base_capital=base_capital)
        self.entry_agent = EntryAgent(trend_agent=self.trend_agent, risk_agent=self.risk_agent)
        self.exit_agent = ExitAgent()
        self.alert_agent = AlertAgent(is_paper_trading=is_paper_trading)
        self.journal_agent = JournalAgent(trades_dir=trades_dir, db_path=db_path)
        self.is_paper_trading = is_paper_trading

    def on_market_tick(
        self,
        cmp: float,
        df_5m: pd.DataFrame,
        pdh: float,
        pdl: float,
        key_levels: list[float] | None = None,
        morning_floor: float | None = None,
        candidate_setups: list[Any] | None = None,
        date_str: str | None = None,
    ) -> dict[str, Any]:
        """Process a real-time market tick across all agents in the crew."""
        active_trades = self.journal_agent.load_active_trades(date_str)
        results: dict[str, Any] = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "cmp": cmp,
            "actions": [],
            "active_count": len(active_trades),
        }

        # ---------------------------------------------------------------------
        # 1. Manage Active Positions via ExitAgent
        # ---------------------------------------------------------------------
        for trade_id, trade in list(active_trades.items()):
            # Check for candidate opposing signals for auto-flip
            direction = trade.get("direction", "CALL")
            opposing_signal_detected = False
            
            # If long, check if a short sweep or waterfall breakdown forms
            if direction == "CALL":
                short_signal = self.entry_agent.evaluate_sweep(df_5m, pdh, pdl, key_levels)
                if short_signal and short_signal.direction == "PUT":
                    opposing_signal_detected = True
            else:
                long_signal = self.entry_agent.evaluate_sweep(df_5m, pdh, pdl, key_levels)
                if long_signal and long_signal.direction == "CALL":
                    opposing_signal_detected = True

            exit_decision = self.exit_agent.evaluate_exit(
                active_trade=trade,
                cmp=cmp,
                df_5m=df_5m,
                opposing_signal_detected=opposing_signal_detected,
            )

            if exit_decision.is_fully_closed:
                closed_trade = self.journal_agent.record_exit(trade_id, exit_decision, date_str)
                lots = int(trade.get("lots", 1))
                self.alert_agent.send_exit_alert(
                    symbol=trade.get("symbol", "NIFTY"),
                    strike=trade.get("suggested_strike", "NIFTY OPTION"),
                    decision=exit_decision,
                    lots=lots,
                )
                self.risk_agent.record_trade_outcome(
                    is_win=(exit_decision.realized_points > 0),
                    pnl_rupees=float(closed_trade.get("pnl_rupees", 0.0) if closed_trade else 0.0),
                )
                results["actions"].append({
                    "action": exit_decision.action,
                    "trade_id": trade_id,
                    "realized_points": exit_decision.realized_points,
                    "realized_r": exit_decision.realized_r,
                    "reason": exit_decision.exit_reason,
                })
                # Position squared off; update active list
                del active_trades[trade_id]

        # ---------------------------------------------------------------------
        # 2. Evaluate New Entries via Candidate Setups or EntryAgent (if Flat)
        # ---------------------------------------------------------------------
        if len(active_trades) == 0:
            candidate_signal: TradeSignal | None = None

            # Process candidates from Scanner Crew
            if candidate_setups:
                for cand in candidate_setups:
                    if not getattr(cand, "is_valid_for_trade_crew", True):
                        continue
                    
                    # Dynamic target and regime from TrendAgent
                    regime = self.trend_agent.evaluate_regime(df_5m, cand.trigger_price, cand.direction, key_levels)
                    if not regime.is_breakout_allowed:
                        continue

                    # Detect Super-Trend momentum (ADX >= 25, Volatile Expansion, or flag)
                    is_super = getattr(cand, "is_super_trend", False) or regime.adx >= 25.0 or regime.regime == "VOLATILE_EXPANSION"

                    # Risk approval with Order Flow & Super-Trend Sizing (Scales to 4 lots)
                    risk_appr = self.risk_agent.evaluate_order(
                        entry_price=cand.trigger_price,
                        stop_loss=cand.tentative_stop,
                        df_5m=df_5m,
                        direction=cand.direction,
                        is_super_trend=is_super,
                    )
                    if not risk_appr.approved or risk_appr.lots <= 0:
                        continue

                    target_pts = cand.risk_points * regime.recommended_target_r
                    target_p = cand.trigger_price + target_pts if cand.direction == "CALL" else cand.trigger_price - target_pts

                    candidate_signal = TradeSignal(
                        strategy=cand.strategy,
                        symbol=cand.symbol,
                        direction=cand.direction,
                        entry_price=cand.trigger_price,
                        stop_loss=cand.tentative_stop,
                        target_price=round(target_p, 2),
                        risk_points=cand.risk_points,
                        target_points=round(target_pts, 2),
                        rr_ratio=regime.recommended_target_r,
                        lots=risk_appr.lots,
                        quantity=risk_appr.quantity,
                        suggested_strike=cand.suggested_strike or f"NIFTY {cand.direction}",
                        timestamp=cand.timestamp,
                        rationale=f"{cand.rationale} | Dynamic R:R {regime.recommended_target_r:.1f}R",
                        regime=regime.regime,
                        adx=regime.adx,
                    )
                    break

            # Fallback to internal sweep/waterfall check if no candidates passed
            if not candidate_signal:
                candidate_signal = self.entry_agent.evaluate_sweep(df_5m, pdh, pdl, key_levels)
                if not candidate_signal and morning_floor is not None:
                    candidate_signal = self.entry_agent.evaluate_waterfall(df_5m, morning_floor, key_levels)

            if candidate_signal:
                new_trade = self.journal_agent.record_entry(candidate_signal, date_str)
                self.alert_agent.send_entry_alert(candidate_signal)
                results["actions"].append({
                    "action": "ENTRY_OPENED",
                    "trade_id": new_trade.get("trade_id"),
                    "direction": candidate_signal.direction,
                    "strike": candidate_signal.suggested_strike,
                    "entry_price": candidate_signal.entry_price,
                    "target": candidate_signal.target_price,
                    "dynamic_rr": candidate_signal.rr_ratio,
                    "lots": candidate_signal.lots,
                })

        return results
