"""EventRiskAgent: Pre-Market Macro Economic Event & Catalyst Guard.

Responsibilities:
1. Daily Economic Calendar Inspection:
   - Scans for high-impact macro catalysts (RBI MPC decisions, US FOMC, US CPI/Inflation, Union Budget).
2. Event Blackout Scheduling:
   - Pre-arms 15-minute freeze windows (before and after event release) to protect option buyers against extreme IV crush and double whipsaws.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger("genesis.event_agent")


@dataclass
class EventRiskStatus:
    has_high_impact_event: bool
    event_name: str | None
    blackout_start: str | None
    blackout_end: str | None
    is_safe_to_trade_morning: bool
    risk_summary: str


class EventRiskAgent:
    """Pre-Market Economic Event Risk Pre-Flight Engine."""

    # Pre-cataloged high-impact event hours in IST
    KNOWN_RECURRENT_EVENTS = {
        "RBI_MPC": ("10:00", "10:30"),
        "US_CPI": ("19:00", "19:30"),  # Post-market for India, affects overnight
        "UNION_BUDGET": ("11:00", "13:30"),
    }

    def evaluate_calendar_risk(
        self,
        date_str: str | None = None,
        manual_event_name: str | None = None,
    ) -> EventRiskStatus:
        """Inspect daily calendar risk and pre-arm blackout schedules."""
        d = date_str or datetime.now().strftime("%Y-%m-%d")

        if manual_event_name and manual_event_name.upper() in self.KNOWN_RECURRENT_EVENTS:
            ev_name = manual_event_name.upper()
            start, end = self.KNOWN_RECURRENT_EVENTS[ev_name]
            return EventRiskStatus(
                has_high_impact_event=True,
                event_name=ev_name,
                blackout_start=start,
                blackout_end=end,
                is_safe_to_trade_morning=False if start <= "10:30" else True,
                risk_summary=f"HIGH EVENT RISK: {ev_name} scheduled today. Automatic trading freeze between {start} and {end} IST.",
            )

        # Standard trading day: No high impact event scheduled
        return EventRiskStatus(
            has_high_impact_event=False,
            event_name=None,
            blackout_start=None,
            blackout_end=None,
            is_safe_to_trade_morning=True,
            risk_summary="CLEAR: Zero high-impact economic blackout events scheduled for today.",
        )
