"""GapAgent: Overnight Global Sentiment & Gift Nifty Opening Gap Analyzer.

Analyzes overnight momentum and opening gap expectations:
- Gift Nifty (SGX) proxy vs Previous Day Close (PDC)
- Global sentiment drift (US Markets, DXY, Asia)
- Classifies expected opening regime: GAP_AND_GO, GAP_FILL_RETEST, or EXHAUSTION_FADE.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger("genesis.gap_agent")


@dataclass
class GapAnalysis:
    pdc: float
    expected_open: float
    gap_points: float
    gap_percent: float
    bias: Literal["BULLISH", "BEARISH", "NEUTRAL"]
    regime: Literal["GAP_AND_GO", "GAP_FILL_RETEST", "EXHAUSTION_FADE", "FLAT_OPEN"]
    rationale: str


class GapAgent:
    """Overnight Gap & Morning Bias Evaluation Engine."""

    def __init__(
        self,
        trend_gap_threshold: float = 35.0,
        exhaustion_gap_threshold: float = 85.0,
    ) -> None:
        self.trend_gap_threshold = trend_gap_threshold
        self.exhaustion_gap_threshold = exhaustion_gap_threshold

    def evaluate_gap(
        self,
        pdc: float,
        gift_nifty_price: float | None = None,
        global_sentiment: float = 0.0,  # -1.0 to +1.0
    ) -> GapAnalysis:
        """Evaluate expected opening gap and classify session opening structure."""
        if gift_nifty_price is None or gift_nifty_price <= 0:
            # Fallback to neutral estimation based on global sentiment drift
            gift_nifty_price = pdc + (global_sentiment * 25.0)

        gap_pts = round(gift_nifty_price - pdc, 2)
        gap_pct = round((gap_pts / pdc) * 100, 2) if pdc > 0 else 0.0

        if gap_pts >= self.trend_gap_threshold:
            bias: Literal["BULLISH", "BEARISH", "NEUTRAL"] = "BULLISH"
        elif gap_pts <= -self.trend_gap_threshold:
            bias = "BEARISH"
        else:
            bias = "NEUTRAL"

        # Classify regime
        if abs(gap_pts) >= self.exhaustion_gap_threshold:
            regime: Literal["GAP_AND_GO", "GAP_FILL_RETEST", "EXHAUSTION_FADE", "FLAT_OPEN"] = "EXHAUSTION_FADE"
            rationale = (
                f"Large opening gap ({gap_pts:+.1f} pts / {gap_pct:+.2f}%). "
                f"High risk of initial profit-taking fade into previous day extreme."
            )
        elif abs(gap_pts) >= self.trend_gap_threshold:
            if (gap_pts > 0 and global_sentiment >= 0) or (gap_pts < 0 and global_sentiment <= 0):
                regime = "GAP_AND_GO"
                rationale = (
                    f"Strong directional gap ({gap_pts:+.1f} pts) aligned with global drift. "
                    f"High probability of momentum continuation above opening range."
                )
            else:
                regime = "GAP_FILL_RETEST"
                rationale = (
                    f"Moderate opening gap ({gap_pts:+.1f} pts). "
                    f"Expect initial retest of PDC ({pdc:.1f}) before directional expansion."
                )
        else:
            regime = "FLAT_OPEN"
            rationale = f"Flat open anticipated ({gap_pts:+.1f} pts). Standard OR-30 and level boundaries apply."

        return GapAnalysis(
            pdc=pdc,
            expected_open=round(gift_nifty_price, 2),
            gap_points=gap_pts,
            gap_percent=gap_pct,
            bias=bias,
            regime=regime,
            rationale=rationale,
        )
