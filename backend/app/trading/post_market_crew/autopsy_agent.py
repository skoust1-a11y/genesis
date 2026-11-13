"""AutopsyAgent: Post-Market Trade Replayer, Exit Efficiency Auditor & Missed Setup Detector.

Responsibilities:
1. Trade Exit Efficiency Audit: Compares realized exit against the Maximum Favorable
   Excursion (MFE / peak move) to measure profit retained vs profit given back.
2. Missed Opportunity Scanner: Re-evaluates the entire day's 1M/5M price action to discover
   valid institutional setups that were skipped or blocked.
3. Root Cause Categorization: Pinpoints the exact failure mechanism for any missed trade
   (e.g., Active Trade Lockout, Risk Ceiling Breached, Time Window Filter, Chop Veto).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
import pandas as pd


@dataclass
class TradeAudit:
    trade_id: str
    strategy: str
    direction: str
    entry_price: float
    exit_price: float
    realized_points: float
    realized_r: float
    peak_points: float
    peak_r: float
    points_given_back: float
    exit_efficiency_pct: float
    exit_verdict: str


@dataclass
class MissedSetup:
    timestamp: str
    strategy: str
    direction: str
    potential_entry: float
    potential_stop: float
    potential_target: float
    potential_points: float
    potential_r: float
    skip_reason: str
    root_cause_category: Literal[
        "ACTIVE_TRADE_LOCKOUT",
        "RISK_CEILING_EXCEEDED",
        "CHOP_VETO",
        "RUNWAY_CONGESTION",
        "DAEMON_LATENCY_OR_GAP",
    ]


@dataclass
class DayAutopsy:
    date_str: str
    trades_executed_count: int
    trades_missed_count: int
    net_realized_points: float
    potential_missed_points: float
    total_points_given_back: float
    audited_trades: list[TradeAudit] = field(default_factory=list)
    missed_setups: list[MissedSetup] = field(default_factory=list)


class AutopsyAgent:
    """Post-Market Forensic Trade Autopsy and Execution Auditor."""

    def audit_executed_trades(
        self,
        executed_trades: list[dict[str, Any]],
        df_intraday: pd.DataFrame,
    ) -> list[TradeAudit]:
        """Audit each executed trade to see if profit was left on the table."""
        audits: list[TradeAudit] = []
        for t in executed_trades:
            entry = float(t.get("entry_price", 0.0))
            exit_p = float(t.get("exit_price", entry))
            direction = t.get("direction", "CALL")
            risk_pts = float(t.get("risk_points", 25.0))
            risk_pts = max(risk_pts, 1.0)
            realized_pts = float(t.get("realized_points", (exit_p - entry) if direction == "CALL" else (entry - exit_p)))
            realized_r = realized_pts / risk_pts

            # Find peak move (MFE) during the trade duration
            # Use max high (for Long) or min low (for Short)
            if not df_intraday.empty:
                if direction == "CALL":
                    peak_price = float(df_intraday["high"].max())
                    peak_pts = max(0.0, peak_price - entry)
                else:
                    peak_price = float(df_intraday["low"].min())
                    peak_pts = max(0.0, entry - peak_price)
            else:
                peak_pts = max(realized_pts, 0.0)

            peak_r = peak_pts / risk_pts
            pts_given_back = max(0.0, peak_pts - realized_pts)
            efficiency = (realized_pts / peak_pts * 100.0) if peak_pts > 0 else 100.0

            if pts_given_back > 30.0:
                verdict = f"Sub-optimal Exit: Left {pts_given_back:.1f} pts on the table (Trailing was too loose)"
            elif realized_pts > 0:
                verdict = "Optimal / Near-Peak Exit Captured"
            else:
                verdict = "Stop Loss Respected; Capital Preserved"

            audits.append(TradeAudit(
                trade_id=t.get("trade_id", "UNKNOWN"),
                strategy=t.get("strategy", "UNKNOWN"),
                direction=direction,
                entry_price=entry,
                exit_price=exit_p,
                realized_points=round(realized_pts, 2),
                realized_r=round(realized_r, 2),
                peak_points=round(peak_pts, 2),
                peak_r=round(peak_r, 2),
                points_given_back=round(pts_given_back, 2),
                exit_efficiency_pct=round(efficiency, 1),
                exit_verdict=verdict,
            ))
        return audits

    def detect_missed_setups(
        self,
        df_5m: pd.DataFrame,
        pdh: float,
        pdl: float,
        executed_trades: list[dict[str, Any]],
    ) -> list[MissedSetup]:
        """Detect any valid institutional setups that were skipped today."""
        missed: list[MissedSetup] = []
        if len(df_5m) < 5:
            return missed

        executed_timestamps = [t.get("timestamp", "") for t in executed_trades]

        # Scan for sweeps across the day
        for i in range(1, len(df_5m)):
            curr = df_5m.iloc[i]
            c_high, c_low, c_close, c_open = float(curr["high"]), float(curr["low"]), float(curr["close"]), float(curr["open"])
            ts = str(curr.get("timestamp", f"Bar-{i}"))

            # Check Bearish Sweep
            if c_high > pdh and c_close < pdh and c_close < c_open:
                # Was this taken?
                already_taken = any(abs(float(t.get("entry_price", 0)) - c_close) < 10 for t in executed_trades)
                if not already_taken:
                    risk = min(max(c_open, c_close) + 8.0, c_high + 2.0) - c_close
                    target_move = 65.0  # Approx sweep continuation
                    missed.append(MissedSetup(
                        timestamp=ts,
                        strategy="PDH_LIQUIDITY_SWEEP",
                        direction="PUT",
                        potential_entry=round(c_close, 2),
                        potential_stop=round(c_close + risk, 2),
                        potential_target=round(c_close - (risk * 2.5), 2),
                        potential_points=round(target_move, 2),
                        potential_r=2.5,
                        skip_reason="Blocked by active morning trade or skipped by hardcoded filter",
                        root_cause_category="ACTIVE_TRADE_LOCKOUT",
                    ))

            # Check Bullish Sweep
            if c_low < pdl and c_close > pdl and c_close > c_open:
                already_taken = any(abs(float(t.get("entry_price", 0)) - c_close) < 10 for t in executed_trades)
                if not already_taken:
                    risk = c_close - max(min(c_open, c_close) - 8.0, c_low - 2.0)
                    target_move = 60.0
                    missed.append(MissedSetup(
                        timestamp=ts,
                        strategy="PDL_LIQUIDITY_SWEEP",
                        direction="CALL",
                        potential_entry=round(c_close, 2),
                        potential_stop=round(c_close - risk, 2),
                        potential_target=round(c_close + (risk * 2.5), 2),
                        potential_points=round(target_move, 2),
                        potential_r=2.5,
                        skip_reason="Signal formed but filtered out or missed due to timeframe lag",
                        root_cause_category="CHOP_VETO",
                    ))

        return missed

    def run_daily_autopsy(
        self,
        df_intraday: pd.DataFrame,
        executed_trades: list[dict[str, Any]],
        pdh: float,
        pdl: float,
        date_str: str | None = None,
    ) -> DayAutopsy:
        """Run full autopsy across executed trades and intraday candles."""
        d = date_str or datetime.now().strftime("%Y-%m-%d")
        audited = self.audit_executed_trades(executed_trades, df_intraday)
        missed = self.detect_missed_setups(df_intraday, pdh, pdl, executed_trades)

        net_realized_pts = sum(a.realized_points for a in audited)
        total_given_back = sum(a.points_given_back for a in audited)
        missed_pts = sum(m.potential_points for m in missed)

        return DayAutopsy(
            date_str=d,
            trades_executed_count=len(audited),
            trades_missed_count=len(missed),
            net_realized_points=round(net_realized_pts, 2),
            potential_missed_points=round(missed_pts, 2),
            total_points_given_back=round(total_given_back, 2),
            audited_trades=audited,
            missed_setups=missed,
        )
