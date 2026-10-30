"""BriefingAgent: Post-Market Report Generator and Telegram Executive Digest.

Responsibilities:
1. Compiles the daily "Mistakes & Improvements" Executive Audit Report.
2. Writes full forensic audit to reports/daily_audit_{date}.md.
3. Dispatches concise, high-impact Daily Digest to Telegram channel.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
import requests

from app.trading.post_market_crew.autopsy_agent import DayAutopsy
from app.trading.post_market_crew.code_quality_agent import CodeQualityReport
from app.trading.post_market_crew.parameter_agent import ParameterRecommendation
from app.trading.post_market_crew.safety_wall_agent import SafetyWallSummary

logger = logging.getLogger("genesis.briefing_agent")


class BriefingAgent:
    """Post-Market Executive Briefing and Report Generator."""

    def __init__(
        self,
        reports_dir: Path | None = None,
        bot_token: str | None = None,
        chat_id: str | None = None,
    ) -> None:
        self.reports_dir = reports_dir or Path("reports")
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.bot_token = bot_token or "8422787103:AAGfXnI-L4l-bI1pP7y3cT2d7kY"
        self.chat_id = chat_id or "-1002235941641"

    def _send_telegram(self, text: str) -> bool:
        """Send message via Telegram Bot API with error handling."""
        if not self.bot_token or not self.chat_id:
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
            logger.error(f"Failed to dispatch Telegram audit briefing: {e}")
            return False

    def generate_markdown_report(
        self,
        autopsy: DayAutopsy,
        params: list[ParameterRecommendation],
        safety: SafetyWallSummary,
        code: CodeQualityReport,
    ) -> Path:
        """Write full detailed post-market markdown report to disk."""
        report_path = self.reports_dir / f"daily_audit_{autopsy.date_str}.md"
        
        md_lines = [
            f"# 📋 Genesis Daily Post-Market Audit — {autopsy.date_str}",
            "",
            "## 1. Executive Summary",
            f"- **Net Realized Points:** `{autopsy.net_realized_points:+.2f} pts`",
            f"- **Today's Net Realized P&L:** `₹{safety.today_net_pnl:+,.2f}`",
            f"- **Trades Executed:** `{autopsy.trades_executed_count}`",
            f"- **Missed Setups Detected:** `{autopsy.trades_missed_count}`",
            f"- **Total Points Given Back:** `{autopsy.total_points_given_back:.2f} pts`",
            "",
            "---",
            "",
            "## 2. Forensic Trade Audit (Executed Trades)",
            "| Trade ID | Strategy | Realized Pts | Realized R | Peak R | Given Back | Verdict |",
            "| :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
        ]

        if autopsy.audited_trades:
            for t in autopsy.audited_trades:
                md_lines.append(
                    f"| `{t.trade_id}` | {t.strategy} | `{t.realized_points:+.1f}` | `{t.realized_r:+.2f}R` | "
                    f"`{t.peak_r:+.2f}R` | `{t.points_given_back:.1f} pts` | {t.exit_verdict} |"
                )
        else:
            md_lines.append("| _None_ | - | - | - | - | - | No trades executed today |")

        md_lines.extend([
            "",
            "---",
            "",
            "## 3. Missed Opportunities & Root Cause",
            "| Time | Setup | Direction | Potential Pts | Potential R | Root Cause |",
            "| :--- | :--- | :--- | :--- | :--- | :--- |",
        ])

        if autopsy.missed_setups:
            for m in autopsy.missed_setups:
                md_lines.append(
                    f"| `{m.timestamp}` | {m.strategy} | {m.direction} | `+{m.potential_points:.1f}` | "
                    f"`+{m.potential_r:.1f}R` | {m.skip_reason} (`{m.root_cause_category}`) |"
                )
        else:
            md_lines.append("| _None_ | - | - | - | - | Zero institutional setups missed today |")

        md_lines.extend([
            "",
            "---",
            "",
            "## 4. Parameter Recommendations & Edge Optimization",
            "| Parameter | Current | Recommended | Expected Edge | Rationale |",
            "| :--- | :--- | :--- | :--- | :--- |",
        ])

        for p in params:
            md_lines.append(
                f"| `{p.parameter_name}` | `{p.current_value}` | `{p.recommended_value}` | `+{p.expected_point_gain:.1f} pts` | {p.rationale} |"
            )

        md_lines.extend([
            "",
            "---",
            "",
            "## 5. Safety Wall & Tomorrow's Risk Limits",
            f"- **Today's Allocation to Reserve:** `₹{safety.added_to_reserve:+,.2f}`",
            f"- **Current Safety Wall Balance:** `₹{safety.current_reserve_balance:,.2f}`",
            f"- **Tomorrow's Approved Lot Sizing:** `🏷️ {safety.tomorrow_approved_lots} Lots`",
            f"- **Gate Status:** {safety.gate_evaluation_status}",
            f"- **Safety Action:** {safety.action_taken}",
            "",
            "---",
            "",
            "## 6. Code Quality & System Health",
            f"- **System Status:** `{code.system_health_status}`",
            f"- **Errors Logged:** `{code.total_errors_found}`",
            f"- **Warnings Logged:** `{code.total_warnings_found}`",
        ])

        for r in code.refactoring_recommendations:
            md_lines.append(f"- 🔧 *Refactor:* {r}")

        with open(report_path, "w", encoding="utf-8") as f:
            f.write("\n".join(md_lines))

        return report_path

    def dispatch_telegram_digest(
        self,
        autopsy: DayAutopsy,
        safety: SafetyWallSummary,
        code: CodeQualityReport,
    ) -> bool:
        """Send formatted high-level executive digest to Telegram."""
        pnl_emoji = "🟢 💰" if safety.today_net_pnl >= 0 else "🔴 🛑"
        
        msg = (
            f"📊 *DAILY POST-MARKET BRIEFING* — `{autopsy.date_str}`\n\n"
            f"{pnl_emoji} *Today's Realized P&L:* `₹{safety.today_net_pnl:+,.2f}` ({autopsy.net_realized_points:+.1f} pts)\n"
            f"📦 *Trades Taken:* `{autopsy.trades_executed_count}` | *Missed Setups:* `{autopsy.trades_missed_count}`\n"
            f"📉 *Points Given Back:* `{autopsy.total_points_given_back:.1f} pts`\n\n"
            f"🛡️ *Safety Wall Reserve:* `₹{safety.current_reserve_balance:,.2f}`\n"
            f"🏷️ *Tomorrow's Size:* `{safety.tomorrow_approved_lots} Lots`\n"
            f"⚙️ *System Health:* `{code.system_health_status}`\n\n"
            f"📋 _Detailed audit report generated in reports/daily_audit_{autopsy.date_str}.md_"
        )
        return self._send_telegram(msg)
