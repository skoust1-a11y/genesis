"""PreMarketManager: Master Orchestrator for 08:30–09:14 IST Pre-Market Setup.

Responsibilities:
1. Coordinates GapAgent, GammaAgent, and EventRiskAgent.
2. Ingests daily_risk_config.json and active_strategy_params.json.
3. Compiles the 09:05 IST Pre-Market Battleplan.
4. Dispatches the Morning Gameplan to Telegram.
5. Persists trades/pre_market_plan.json for live scanner consumption.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
import httpx

from app.trading.pre_market_crew.event_agent import EventRiskAgent, EventRiskStatus
from app.trading.pre_market_crew.gamma_agent import GammaAgent, GammaAnalysis
from app.trading.pre_market_crew.gap_agent import GapAgent, GapAnalysis

logger = logging.getLogger("genesis.pre_market_manager")


@dataclass
class PreMarketBattleplan:
    date: str
    pdc: float
    expected_open: float
    gap_points: float
    gap_bias: str
    regime: str
    call_wall: float
    put_wall: float
    pcr: float
    max_pain: float
    is_opening_trap: bool
    trap_warning: str | None
    event_risk: str
    approved_lots_standard: int
    approved_lots_super: int
    otm_steps: int
    dispatch_timestamp: str


class PreMarketManager:
    """Master Controller for Pre-Market Preparation & Morning Gameplan."""

    def __init__(
        self,
        trades_dir: Path | None = None,
        bot_token: str | None = None,
        chat_id: str | None = None,
    ) -> None:
        self.trades_dir = trades_dir or Path("trades")
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")

        self.gap_agent = GapAgent()
        self.gamma_agent = GammaAgent()
        self.event_agent = EventRiskAgent()

    def run_pre_market_setup(
        self,
        pdc: float,
        gift_nifty_price: float | None = None,
        chain_data: list[dict[str, Any]] | None = None,
        manual_event: str | None = None,
        date_str: str | None = None,
    ) -> PreMarketBattleplan:
        """Run pre-flight checks and compile morning gameplan."""
        d = date_str or datetime.now().strftime("%Y-%m-%d")

        # 1. Evaluate Overnight Gap
        gap_info = self.gap_agent.evaluate_gap(pdc=pdc, gift_nifty_price=gift_nifty_price)

        # 2. Evaluate Option Chain OI Walls
        gamma_info = self.gamma_agent.evaluate_option_chain(
            current_spot=gap_info.expected_open,
            chain_data=chain_data,
        )

        # 3. Evaluate Event Risk
        event_info = self.event_agent.evaluate_calendar_risk(date_str=d, manual_event_name=manual_event)

        # 4. Load Sizing and Parameters from disk
        std_lots = 3
        super_lots = 4
        otm = 2

        risk_cfg_file = self.trades_dir / "daily_risk_config.json"
        if risk_cfg_file.exists():
            try:
                with open(risk_cfg_file, "r", encoding="utf-8") as rf:
                    rdata = json.load(rf)
                    std_lots = int(rdata.get("default_lots") or rdata.get("tomorrow_approved_lots") or 3)
                    super_lots = int(rdata.get("max_super_lots") or 4)
            except Exception:
                pass

        params_file = self.trades_dir / "active_strategy_params.json"
        if params_file.exists():
            try:
                with open(params_file, "r", encoding="utf-8") as pf:
                    pdata = json.load(pf)
                    otm = int(pdata.get("otm_steps") or 2)
            except Exception:
                pass

        plan = PreMarketBattleplan(
            date=d,
            pdc=pdc,
            expected_open=gap_info.expected_open,
            gap_points=gap_info.gap_points,
            gap_bias=gap_info.bias,
            regime=gap_info.regime,
            call_wall=gamma_info.call_wall,
            put_wall=gamma_info.put_wall,
            pcr=gamma_info.pcr,
            max_pain=gamma_info.max_pain,
            is_opening_trap=gamma_info.is_opening_trap,
            trap_warning=gamma_info.trap_warning,
            event_risk=event_info.risk_summary,
            approved_lots_standard=std_lots,
            approved_lots_super=super_lots,
            otm_steps=otm,
            dispatch_timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

        # 5. Persist to disk for live scanner ingestion
        self._save_plan(plan)

        # 6. Dispatch to Telegram
        self.dispatch_telegram_battleplan(plan)

        return plan

    def _save_plan(self, plan: PreMarketBattleplan) -> None:
        """Save pre-market plan to disk."""
        self.trades_dir.mkdir(parents=True, exist_ok=True)
        out_file = self.trades_dir / "pre_market_plan.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump({
                "date": plan.date,
                "pdc": plan.pdc,
                "expected_open": plan.expected_open,
                "gap_points": plan.gap_points,
                "gap_bias": plan.gap_bias,
                "regime": plan.regime,
                "call_wall": plan.call_wall,
                "put_wall": plan.put_wall,
                "pcr": plan.pcr,
                "max_pain": plan.max_pain,
                "is_opening_trap": plan.is_opening_trap,
                "trap_warning": plan.trap_warning,
                "approved_lots_standard": plan.approved_lots_standard,
                "approved_lots_super": plan.approved_lots_super,
                "otm_steps": plan.otm_steps,
                "dispatch_timestamp": plan.dispatch_timestamp,
            }, f, indent=2)

    def dispatch_telegram_battleplan(self, plan: PreMarketBattleplan) -> bool:
        """Send 09:05 IST Pre-Market Morning Battleplan to Telegram."""
        if not self.bot_token or not self.chat_id:
            logger.debug("Telegram credentials not configured; skipping pre-market message.")
            return False

        trap_banner = f"\n⚠️ *{plan.trap_warning}*\n" if plan.is_opening_trap else ""

        msg = (
            f"🌅 *GENESIS PRE-MARKET BATTLEPLAN* — `{plan.date}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 *Gift Nifty Bias:* `{plan.gap_bias}` ({plan.gap_points:+.1f} pts)\n"
            f"📊 *Expected Open:* `{plan.expected_open:.1f}` (PDC: `{plan.pdc:.1f}`)\n"
            f"🏛️ *Opening Regime:* `{plan.regime}`\n"
            f"{trap_banner}"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🧱 *Call Wall (Resistance):* `{plan.call_wall:.0f}`\n"
            f"🛡️ *Put Wall (Support Floor):* `{plan.put_wall:.0f}`\n"
            f"⚖️ *Max Pain Strike:* `{plan.max_pain:.0f}` | *PCR:* `{plan.pcr:.2f}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏷️ *Approved Sizing:* `{plan.approved_lots_standard} Lots` (Scales to `{plan.approved_lots_super} Lots` on Super-Trend)\n"
            f"🎯 *Option Selection:* `{plan.otm_steps}-OTM` Contracts\n"
            f"📅 *Event Risk:* `{plan.event_risk}`\n\n"
            f"_Scanner Crew armed for 09:15 opening bell._"
        )

        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            resp = httpx.post(url, json={"chat_id": self.chat_id, "text": msg, "parse_mode": "Markdown"}, timeout=5.0)
            return resp.status_code == 200
        except Exception as e:
            logger.warning(f"Telegram dispatch failed: {e}")
            return False
