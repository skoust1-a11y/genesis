"""PostMarketManager: Master Orchestrator for Post-Market Auditing & Self-Improvement.

Executes the post-market review pipeline after 15:30 IST:
1. AutopsyAgent -> Reconstructs today's tape, audits exits & flags missed trades
2. ParameterAgent -> Tests edge optimization & dynamic R:R
3. SafetyWallAgent -> Sweeps 50% profit reserve & sets tomorrow's lot size
4. CodeQualityAgent -> Audits logs, warnings & latency bottlenecks
5. BriefingAgent -> Compiles Daily Markdown Report & dispatches Telegram summary
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any
import pandas as pd

from app.trading.post_market_crew.autopsy_agent import AutopsyAgent, DayAutopsy
from app.trading.post_market_crew.briefing_agent import BriefingAgent
from app.trading.post_market_crew.code_quality_agent import CodeQualityAgent, CodeQualityReport
from app.trading.post_market_crew.parameter_agent import ParameterAgent, ParameterRecommendation
from app.trading.post_market_crew.safety_wall_agent import SafetyWallAgent, SafetyWallSummary

logger = logging.getLogger("genesis.post_market_manager")


class PostMarketManager:
    """Master Orchestrator for the Post-Market Refactoring Crew."""

    def __init__(
        self,
        base_capital: float = 50_000.0,
        reports_dir: Path | None = None,
        trades_dir: Path | None = None,
    ) -> None:
        self.reports_dir = reports_dir or Path("reports")
        self.trades_dir = trades_dir or Path("trades")
        
        self.autopsy_agent = AutopsyAgent()
        self.parameter_agent = ParameterAgent()
        self.safety_wall_agent = SafetyWallAgent(base_capital=base_capital)
        self.code_quality_agent = CodeQualityAgent()
        self.briefing_agent = BriefingAgent(reports_dir=self.reports_dir)

    def run_daily_review(
        self,
        df_intraday: pd.DataFrame,
        pdh: float,
        pdl: float,
        date_str: str | None = None,
        executed_trades: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Execute the end-to-end post-market audit sequence."""
        d = date_str or datetime.now().strftime("%Y-%m-%d")
        
        # Load executed trades if not passed
        trades = executed_trades or []
        if not trades:
            exec_file = self.trades_dir / "live_executed_trades.json"
            if exec_file.exists():
                try:
                    with open(exec_file, "r", encoding="utf-8") as f:
                        all_trades = json.load(f)
                    for t in all_trades:
                        t_date = str(t.get("Date") or t.get("date") or t.get("timestamp") or t.get("exit_timestamp") or "")
                        if d in t_date:
                            # Normalize keys
                            norm_t = {
                                "trade_id": str(t.get("trade_id") or f"TRADE_{t.get('Time', 'INTRA')}"),
                                "strategy": str(t.get("Strategy") or t.get("strategy") or "UNKNOWN"),
                                "direction": "CALL" if str(t.get("Side", "")).upper() in ["LONG", "CALL"] else "PUT",
                                "entry_price": float(t.get("Entry Spot") or t.get("entry_price") or 0.0),
                                "exit_price": float(t.get("Exit Spot") or t.get("exit_price") or 0.0),
                                "risk_points": float(t.get("Risk Pts") or t.get("risk_points") or 25.0),
                                "realized_points": float(t.get("Points") or t.get("realized_points") or 0.0),
                                "realized_r": float(t.get("R-Multiple") or t.get("realized_r") or 0.0),
                                "pnl_rupees": float(t.get("Est P&L (Rs)") or t.get("pnl_rupees") or 0.0),
                                "lots": int(t.get("Lots") or t.get("lots") or 1),
                                "timestamp": str(t.get("Time") or t.get("timestamp") or ""),
                                "exit_reason": str(t.get("Exit Reason") or t.get("exit_reason") or ""),
                            }
                            trades.append(norm_t)
                except Exception as e:
                    logger.error(f"Error loading executed trades: {e}")
                    trades = []

        logger.info(f"Running Post-Market Review for {d} with {len(trades)} executed trades.")

        # Step 1: Autopsy
        autopsy = self.autopsy_agent.run_daily_autopsy(
            df_intraday=df_intraday,
            executed_trades=trades,
            pdh=pdh,
            pdl=pdl,
            date_str=d,
        )

        # Step 2: Parameter Calibration & Closed-Loop Auto-Tuning
        param_recs = self.parameter_agent.evaluate_recent_parameters(
            df_recent=df_intraday,
            executed_trades=trades,
        )
        self.parameter_agent.save_calibrated_parameters(
            param_recs,
            config_path=self.trades_dir / "active_strategy_params.json",
        )

        # Step 3: Safety Wall Reserve
        safety_summary = self.safety_wall_agent.process_daily_pnl(today_trades=trades)

        # Step 4: Code & Log Quality
        code_report = self.code_quality_agent.audit_system_health()

        # Step 5: Briefing & Reports
        report_file = self.briefing_agent.generate_markdown_report(
            autopsy=autopsy,
            params=param_recs,
            safety=safety_summary,
            code=code_report,
        )
        self.briefing_agent.dispatch_telegram_digest(
            autopsy=autopsy,
            safety=safety_summary,
            code=code_report,
        )

        return {
            "date": d,
            "report_path": str(report_file),
            "autopsy": autopsy,
            "safety_wall": safety_summary,
            "code_quality": code_report,
            "parameters": param_recs,
        }
