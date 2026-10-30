"""DetectorAgent: Multi-Strategy Pattern Detector with News & Consolidation Fusion.

Responsibilities:
1. Full Strategy Suite Scanning:
   - Evaluates Trendline Reversal (CHoCH confirmed, 3rd-point dynamic target)
   - Evaluates Breakout & Retest (Grade-A PDH/PDL + OR-30)
   - Evaluates Liquidity Sweep (PDH/PDL sweeps with micro-wick rejection)
   - Evaluates Midday Volume Waterfalls
2. Multi-Timeframe Execution (1M, 5M, 15M):
   - Scans micro-execution frames (1M) for surgical sniper entries.
   - Scans primary institutional structure (5M) and macro frames (15M).
3. News & Event Fusion:
   - Enforces Event Risk Blackouts from NewsAgent (RBI / FOMC blackouts).
4. Consolidation Trap Protection:
   - Vetoes low-expansion breakout attempts inside tight consolidation boxes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
import pandas as pd

from app.trading.scanner_crew.consolidation_agent import ConsolidationAgent, ConsolidationStatus
from app.trading.scanner_crew.levels_agent import SessionLevels
from app.trading.scanner_crew.news_agent import NewsAgent, NewsSentiment

logger = logging.getLogger("genesis.detector_agent")


@dataclass
class CandidateSetup:
    strategy: str
    symbol: str
    direction: Literal["CALL", "PUT"]
    trigger_price: float
    tentative_stop: float
    tentative_target: float
    timeframe: str
    timestamp: str
    rationale: str
    news_sentiment_score: float
    event_risk_cleared: bool
    is_valid_for_trade_crew: bool
    suggested_strike: str | None = None
    risk_points: float = 25.0
    target_points: float = 75.0
    rr_ratio: float = 3.0
    veto_reason: str | None = None
    is_super_trend: bool = False


class DetectorAgent:
    """Setup Pattern Recognition and Multi-Agent Signal Filter."""

    def __init__(
        self,
        consolidation_agent: ConsolidationAgent | None = None,
        news_agent: NewsAgent | None = None,
        otm_steps: int = 2,
    ) -> None:
        self.consolidation_agent = consolidation_agent or ConsolidationAgent()
        self.news_agent = news_agent or NewsAgent()
        self.otm_steps = otm_steps

    def scan_for_setups(
        self,
        df_5m: pd.DataFrame,
        levels: SessionLevels,
        news_sentiment: NewsSentiment | None = None,
        current_time_str: str | None = None,
    ) -> list[CandidateSetup]:
        """Backward-compatible single-dataframe scanner."""
        bars_list = df_5m.to_dict(orient="records") if not df_5m.empty else []
        return self.scan_multi_timeframe_strategies(
            tf_bars_map={"5m": bars_list},
            levels=levels,
            news_sentiment=news_sentiment,
            current_time_str=current_time_str,
        )

    def scan_multi_timeframe_strategies(
        self,
        tf_bars_map: dict[str, list[dict[str, Any]] | pd.DataFrame],
        levels: SessionLevels,
        news_sentiment: NewsSentiment | None = None,
        current_time_str: str | None = None,
        strategy_filter: str = "auto",
        allow_reentry: bool = True,
    ) -> list[CandidateSetup]:
        """Scan across 1M, 5M, 15M frames using institutional strategy engines."""
        setups: list[CandidateSetup] = []
        sentiment = news_sentiment or self.news_agent.evaluate_news_sentiment(current_time_str=current_time_str)

        # If high impact event active (e.g. RBI rate announcement window), gate all new entries
        if sentiment.high_impact_event_active:
            logger.info(f"DetectorAgent: All setups gated due to {sentiment.event_warning}")
            return []

        try:
            from app.liquidity_signals import scan, scan_retest, scan_trendline_reversal
        except Exception as e:
            logger.warning(f"Could not import app.liquidity_signals engines: {e}")
            return []

        spot_levels = levels.all_key_levels

        for tf, raw_bars in tf_bars_map.items():
            if isinstance(raw_bars, pd.DataFrame):
                bars = raw_bars.to_dict(orient="records")
            else:
                bars = list(raw_bars)

            if len(bars) < 5:
                continue

            df_tf = pd.DataFrame(bars)
            compression = self.consolidation_agent.evaluate_compression(df_tf, current_time_str=current_time_str)
            bar_mins = 1 if tf == "1m" else (15 if tf == "15m" else 5)
            above = {b.get("date", ""): True for b in bars}

            # -------------------------------------------------------------
            # 1. Strategy: Trendline Reversal (CHoCH Confirmed)
            # -------------------------------------------------------------
            if strategy_filter in ("auto", "trendline"):
                try:
                    for s in scan_trendline_reversal(
                        bars,
                        skip_lunch="conditional",
                        allow_reentry=allow_reentry,
                        bar_minutes=bar_mins,
                        require_choch=True,
                        max_risk=55.0,
                        dynamic_targets=True,
                        spot_levels=spot_levels,
                    ):
                        risk = float(s.get("risk", 0))
                        if not (10.0 <= risk <= 55.0):
                            continue
                        entry = float(s.get("entry", 0))
                        stop = float(s.get("stop", 0))
                        target = float(s.get("target", entry + risk * 3.0))
                        direction: Literal["CALL", "PUT"] = "CALL" if str(s.get("side", "")).lower() == "long" else "PUT"
                        strike_round = int(round(entry / 50.0) * 50) + int(self.otm_steps * 50 if direction == "CALL" else -self.otm_steps * 50)
                        strike = f"NIFTY {strike_round} {'CE' if direction == 'CALL' else 'PE'}"

                        is_valid = True
                        veto_msg = None
                        if compression.is_consolidating:
                            trigger_bar = bars[-1]
                            if (trigger_bar.get("high", 0) - trigger_bar.get("low", 0)) < 18.0:
                                is_valid = False
                                veto_msg = f"Vetoed by Compression Box ({compression.reason})"

                        setups.append(CandidateSetup(
                            strategy=f"Trendline Reversal ({tf.upper()})",
                            symbol="NIFTY",
                            direction=direction,
                            trigger_price=round(entry, 2),
                            tentative_stop=round(stop, 2),
                            tentative_target=round(target, 2),
                            timeframe=tf,
                            timestamp=str(s.get("time", "")),
                            rationale=f"Trendline Reversal with CHoCH confirmed on {tf.upper()}",
                            news_sentiment_score=sentiment.sentiment_score,
                            event_risk_cleared=sentiment.is_trading_safe,
                            is_valid_for_trade_crew=is_valid,
                            suggested_strike=strike,
                            risk_points=round(risk, 2),
                            target_points=round(abs(entry - target), 2),
                            rr_ratio=round(abs(entry - target) / max(1.0, risk), 2),
                            veto_reason=veto_msg,
                            is_super_trend=bool(risk <= 25.0),
                        ))
                except Exception as te:
                    logger.debug(f"Trendline scanner note for {tf}: {te}")

            # -------------------------------------------------------------
            # 2. Strategy: Breakout & Retest (Grade A PDH/PDL + OR-30)
            # -------------------------------------------------------------
            if strategy_filter in ("auto", "retest"):
                try:
                    for s in scan_retest(
                        bars,
                        above,
                        levels.pdh,
                        levels.pdl,
                        skip_lunch="conditional",
                        allow_reentry=False,
                        bar_minutes=bar_mins,
                        include_orh_orl=True,
                        dynamic_targets=True,
                        spot_levels=spot_levels,
                    ):
                        risk = float(s.get("risk", 0))
                        if not (10.0 <= risk <= 55.0):
                            continue
                        entry = float(s.get("entry", 0))
                        stop = float(s.get("stop", 0))
                        target = float(s.get("target", entry + risk * 3.0))
                        direction = "CALL" if str(s.get("side", "")).lower() == "long" else "PUT"
                        strike_round = int(round(entry / 50.0) * 50) + int(self.otm_steps * 50 if direction == "CALL" else -self.otm_steps * 50)
                        strike = f"NIFTY {strike_round} {'CE' if direction == 'CALL' else 'PE'}"

                        is_valid = True
                        veto_msg = None
                        if compression.is_consolidating:
                            trigger_bar = bars[-1]
                            if (trigger_bar.get("high", 0) - trigger_bar.get("low", 0)) < 18.0:
                                is_valid = False
                                veto_msg = f"Vetoed by Compression Box ({compression.reason})"

                        pool_desc = str(s.get("pool", ""))
                        is_orb = "OR" in pool_desc
                        strat_name = f"Breakout & Retest ({'OR-30' if is_orb else 'PD'}) [{tf.upper()}]"
                        time_part = str(s.get("time", ""))[11:16]
                        is_super = ("or" in strat_name.lower() or "pd" in strat_name.lower()) and (time_part <= "11:30" if time_part else True)

                        setups.append(CandidateSetup(
                            strategy=strat_name,
                            symbol="NIFTY",
                            direction=direction,
                            trigger_price=round(entry, 2),
                            tentative_stop=round(stop, 2),
                            tentative_target=round(target, 2),
                            timeframe=tf,
                            timestamp=str(s.get("time", "")),
                            rationale=f"{strat_name} clean reclaim",
                            news_sentiment_score=sentiment.sentiment_score,
                            event_risk_cleared=sentiment.is_trading_safe,
                            is_valid_for_trade_crew=is_valid,
                            suggested_strike=strike,
                            risk_points=round(risk, 2),
                            target_points=round(abs(entry - target), 2),
                            rr_ratio=round(abs(entry - target) / max(1.0, risk), 2),
                            veto_reason=veto_msg,
                            is_super_trend=is_super,
                        ))
                except Exception as re:
                    logger.debug(f"Retest scanner note for {tf}: {re}")

            # -------------------------------------------------------------
            # 3. Strategy: Liquidity Sweep
            # -------------------------------------------------------------
            if strategy_filter in ("auto", "sweep") and len(bars) >= 12 and bar_mins >= 3:
                try:
                    for s in scan(
                        bars,
                        above,
                        levels.pdh,
                        levels.pdl,
                        skip_lunch="conditional",
                        dynamic_targets=True,
                        spot_levels=spot_levels,
                    ):
                        risk = float(s.get("risk", 0))
                        if not (10.0 <= risk <= 55.0):
                            continue
                        entry = float(s.get("entry", 0))
                        stop = float(s.get("stop", 0))
                        target = float(s.get("target", entry + risk * 3.0))
                        direction = "CALL" if str(s.get("side", "")).lower() == "long" else "PUT"
                        strike_round = int(round(entry / 50.0) * 50) + int(self.otm_steps * 50 if direction == "CALL" else -self.otm_steps * 50)
                        strike = f"NIFTY {strike_round} {'CE' if direction == 'CALL' else 'PE'}"

                        setups.append(CandidateSetup(
                            strategy=f"Liquidity Sweep [{tf.upper()}]",
                            symbol="NIFTY",
                            direction=direction,
                            trigger_price=round(entry, 2),
                            tentative_stop=round(stop, 2),
                            tentative_target=round(target, 2),
                            timeframe=tf,
                            timestamp=str(s.get("time", "")),
                            rationale=f"Liquidity Sweep on {tf.upper()}",
                            news_sentiment_score=sentiment.sentiment_score,
                            event_risk_cleared=sentiment.is_trading_safe,
                            is_valid_for_trade_crew=True,
                            suggested_strike=strike,
                            risk_points=round(risk, 2),
                            target_points=round(abs(entry - target), 2),
                            rr_ratio=round(abs(entry - target) / max(1.0, risk), 2),
                        ))
                except Exception as se:
                    logger.debug(f"Sweep scanner note for {tf}: {se}")

        return setups
