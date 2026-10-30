"""ExitAgent: Target 3 Unconditional Exits, Structure Trailing & Auto-Flip Engine.

Responsibilities:
1. Unconditional Target Exit: Instantly fires exit when CMP reaches or exceeds target price,
   completely decoupled from milestone tracking loops (preventing today's stuck trade).
2. Dynamic 5M Structure Trailing: Once a trade hits +1.5R, trails runners using 5-minute
   candle structure (exiting when prior candle low/high is breached).
3. Opposing Signal Auto-Flip: Instantly squares off open positions when an opposing strategy
   signal is detected, clearing the way for immediate reversal entries.
4. Theta / 15:15 IST Cutoff: Auto-squares off remaining positions before market close to
   eliminate overnight option decay risk.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
import pandas as pd


@dataclass
class ExitDecision:
    action: Literal[
        "HOLD",
        "TARGET_HIT",
        "STRUCTURE_TRAIL_EXIT",
        "STOP_LOSS_HIT",
        "REVERSAL_EXIT",
        "INTRADAY_CLOSE",
    ]
    exit_price: float
    realized_points: float
    realized_r: float
    exit_reason: str
    remaining_lots: int = 0
    is_fully_closed: bool = False


class ExitAgent:
    """Institutional Trade Exit and Runner Protection Engine."""

    def __init__(
        self,
        intraday_cutoff_time: str = "15:15",
        structure_trail_min_r: float = 1.5,
    ) -> None:
        self.intraday_cutoff_time = intraday_cutoff_time
        self.structure_trail_min_r = structure_trail_min_r

    def evaluate_exit(
        self,
        active_trade: dict[str, Any],
        cmp: float,
        df_5m: pd.DataFrame | None = None,
        opposing_signal_detected: bool = False,
        current_time_str: str | None = None,
    ) -> ExitDecision:
        """Evaluate open trade against hard targets, structure trailing, stops, and reversals."""
        direction = active_trade.get("direction", "CALL")
        entry_price = float(active_trade.get("entry_price", cmp))
        stop_loss = float(active_trade.get("stop_loss", cmp))
        target_price = float(active_trade.get("target_price", cmp))
        risk_pts = float(active_trade.get("risk_points", abs(entry_price - stop_loss)))
        risk_pts = max(risk_pts, 1.0)  # Avoid division by zero
        lots = int(active_trade.get("lots", 1))

        # Calculate current move
        current_move = (cmp - entry_price) if direction == "CALL" else (entry_price - cmp)
        current_r = current_move / risk_pts

        # 1. Stop Loss Hit
        is_stop_hit = (cmp <= stop_loss) if direction == "CALL" else (cmp >= stop_loss)
        if is_stop_hit:
            return ExitDecision(
                action="STOP_LOSS_HIT",
                exit_price=stop_loss,
                realized_points=round((stop_loss - entry_price) if direction == "CALL" else (entry_price - stop_loss), 2),
                realized_r=-1.0,
                exit_reason=f"Stop Loss hit at {stop_loss:.2f}",
                remaining_lots=0,
                is_fully_closed=True,
            )

        # 2. Target Hit (Unconditional)
        is_target_hit = (cmp >= target_price) if direction == "CALL" else (cmp <= target_price)
        if is_target_hit:
            return ExitDecision(
                action="TARGET_HIT",
                exit_price=target_price,
                realized_points=round((target_price - entry_price) if direction == "CALL" else (entry_price - target_price), 2),
                realized_r=round(abs(target_price - entry_price) / risk_pts, 2),
                exit_reason=f"Target reached at {target_price:.2f} (+{current_r:.2f}R)",
                remaining_lots=0,
                is_fully_closed=True,
            )

        # 3. Opposing Strategy Signal Detected -> Reversal Auto-Exit
        if opposing_signal_detected:
            return ExitDecision(
                action="REVERSAL_EXIT",
                exit_price=cmp,
                realized_points=round(current_move, 2),
                realized_r=round(current_r, 2),
                exit_reason=f"Opposing signal auto-square-off at CMP {cmp:.2f}",
                remaining_lots=0,
                is_fully_closed=True,
            )

        # 4. Intraday Cutoff (15:15 IST)
        t_str = current_time_str or datetime.now().strftime("%H:%M")
        if t_str >= self.intraday_cutoff_time:
            return ExitDecision(
                action="INTRADAY_CLOSE",
                exit_price=cmp,
                realized_points=round(current_move, 2),
                realized_r=round(current_r, 2),
                exit_reason=f"Intraday cutoff ({self.intraday_cutoff_time} IST) reached",
                remaining_lots=0,
                is_fully_closed=True,
            )

        # 5. Structure Trailing on Runners (Once >= +1.5R achieved)
        if current_r >= self.structure_trail_min_r and df_5m is not None and len(df_5m) >= 2:
            last_candle = df_5m.iloc[-1]
            prior_candle = df_5m.iloc[-2]
            
            if direction == "CALL":
                # Exits if 5M candle closes below prior candle low
                if float(last_candle["close"]) < float(prior_candle["low"]):
                    return ExitDecision(
                        action="STRUCTURE_TRAIL_EXIT",
                        exit_price=cmp,
                        realized_points=round(current_move, 2),
                        realized_r=round(current_r, 2),
                        exit_reason=f"Structure Trail: 5M candle closed below prior low ({prior_candle['low']:.2f})",
                        remaining_lots=0,
                        is_fully_closed=True,
                    )
            else:
                # Exits if 5M candle closes above prior candle high
                if float(last_candle["close"]) > float(prior_candle["high"]):
                    return ExitDecision(
                        action="STRUCTURE_TRAIL_EXIT",
                        exit_price=cmp,
                        realized_points=round(current_move, 2),
                        realized_r=round(current_r, 2),
                        exit_reason=f"Structure Trail: 5M candle closed above prior high ({prior_candle['high']:.2f})",
                        remaining_lots=0,
                        is_fully_closed=True,
                    )

        # Still active
        return ExitDecision(
            action="HOLD",
            exit_price=cmp,
            realized_points=round(current_move, 2),
            realized_r=round(current_r, 2),
            exit_reason=f"Holding active trade. Current move: {current_move:+.2f} pts ({current_r:+.2f}R)",
            remaining_lots=lots,
            is_fully_closed=False,
        )
