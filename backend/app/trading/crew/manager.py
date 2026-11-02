"""TradeManager: Master Orchestrator for the Live Trading Crew.

Coordinates execution across:
- EntryAgent (Setup detection, micro-wick execution, tight stops)
- TrendAgent (Macro trend slope, ADX/chop veto, dynamic R:R)
- RiskAgent (Capital gates, Safety Wall, daily loss circuit breaker)
- ExitAgent (Unconditional Target 3 exit, 5M structure trail, opposing reversal exit)
- AlertAgent (Telegram dispatches, ticket threads, CMP updates)
- JournalAgent (Active cache, SQLite SSOT, Excel synchronization)
"""

import hashlib
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
import pandas as pd

from app.trading.crew.alert_agent import AlertAgent
from app.trading.crew.entry_agent import DirectionalThesis, EntryAgent, TradeSignal, select_strike_with_delta_floor
from app.trading.crew.exit_agent import ExitAgent, ExitDecision
from app.trading.crew.journal_agent import JournalAgent
from app.trading.crew.order_flow_agent import OrderFlowAgent
from app.trading.crew.risk_agent import RiskAgent
from app.trading.crew.sniper_agent import SniperAgent
from app.trading.crew.trend_agent import TrendAgent
from app.trading.pre_market_crew.gamma_agent import GammaAgent
from app.liquidity_signals import DynamicLookoutWindow, detect_dual_trendline_squeeze

logger = logging.getLogger("genesis.trade_manager")


class TradeManager:
    """Institutional Trade Orchestration Engine for Live Market Operations."""

    def __init__(
        self,
        base_capital: float = 50_000.0,
        is_paper_trading: bool = True,
        trades_dir: Path | None = None,
        db_path: Path | None = None,
        alert_agent: AlertAgent | None = None,
    ) -> None:
        self.trend_agent = TrendAgent()
        self.order_flow_agent = OrderFlowAgent()
        self.gamma_agent = GammaAgent()
        # Architecture C: Strictly fixed at 3 to 4 lots (195-260 Qty) baseline
        self.risk_agent = RiskAgent(
            base_capital=base_capital,
            default_lots=3,
            max_super_lots=4,
            order_flow_agent=self.order_flow_agent,
            frozen_baseline=True,
        )
        self.sniper_agent = SniperAgent()
        self.entry_agent = EntryAgent(
            trend_agent=self.trend_agent,
            risk_agent=self.risk_agent,
            otm_steps=2,
        )
        # Architecture C: Unconditional square-off at 15:10 IST
        self.exit_agent = ExitAgent(intraday_cutoff_time="15:10")
        self.alert_agent = alert_agent or AlertAgent(is_paper_trading=is_paper_trading)
        self.journal_agent = JournalAgent(trades_dir=trades_dir, db_path=db_path)
        self.is_paper_trading = is_paper_trading
        self.executed_order_signatures: set[str] = set()

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
        current_time_str: str | None = None,
        df_15m: pd.DataFrame | None = None,
        india_vix: float | None = None,
    ) -> dict[str, Any]:
        """Process a real-time market tick across all agents in the crew."""
        active_trades = self.journal_agent.load_active_trades(date_str)
        date_val = date_str or datetime.now().strftime("%Y-%m-%d")
        time_val = current_time_str or datetime.now().strftime("%H:%M")

        # Query GammaAgent for live call_wall and put_wall
        gamma_info = self.gamma_agent.evaluate_option_chain(current_spot=cmp)
        call_wall = gamma_info.call_wall
        put_wall = gamma_info.put_wall

        results: dict[str, Any] = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "cmp": cmp,
            "call_wall": call_wall,
            "put_wall": put_wall,
            "actions": [],
            "active_count": len(active_trades),
        }

        # ---------------------------------------------------------------------
        # 1. Manage Active Positions via ExitAgent
        # ---------------------------------------------------------------------
        for trade_id, trade in list(active_trades.items()):
            # Track bars in trade (5-minute increments based on entry timestamp)
            entry_ts_str = str(trade.get("timestamp") or trade.get("entry_time") or "")
            bars_in_trade = 0
            if entry_ts_str:
                try:
                    if current_time_str and len(entry_ts_str) >= 5:
                        t0 = datetime.strptime(entry_ts_str[-5:], "%H:%M")
                        t1 = datetime.strptime(current_time_str[-5:], "%H:%M")
                        elapsed_mins = (t1 - t0).total_seconds() / 60.0
                        bars_in_trade = max(0, int(elapsed_mins // 5))
                    elif len(entry_ts_str) >= 19:
                        entry_dt = datetime.strptime(entry_ts_str[:19], "%Y-%m-%d %H:%M:%S")
                        elapsed_secs = (datetime.now() - entry_dt).total_seconds()
                        bars_in_trade = max(0, int(elapsed_secs // 300))
                except Exception:
                    bars_in_trade = int(trade.get("bars_in_trade", 0))
            else:
                bars_in_trade = int(trade.get("bars_in_trade", 0))
            trade["bars_in_trade"] = bars_in_trade

            direction = trade.get("direction", "CALL")
            opposing_signal_detected = False

            # If long, check if a short sweep or waterfall breakdown forms
            if direction == "CALL":
                short_signal = self.entry_agent.evaluate_sweep(
                    df_5m, pdh, pdl, key_levels, call_wall=call_wall, put_wall=put_wall
                )
                if short_signal and short_signal.direction == "PUT":
                    opposing_signal_detected = True
            else:
                long_signal = self.entry_agent.evaluate_sweep(
                    df_5m, pdh, pdl, key_levels, call_wall=call_wall, put_wall=put_wall
                )
                if long_signal and long_signal.direction == "CALL":
                    opposing_signal_detected = True

            exit_decision = self.exit_agent.evaluate_exit(
                active_trade=trade,
                cmp=cmp,
                df_5m=df_5m,
                opposing_signal_detected=opposing_signal_detected,
                current_time_str=time_val,
                key_levels=key_levels,
                enable_liquidity_hopping=True,
                bars_in_trade=bars_in_trade,
            )

            if exit_decision.action == "LIQUIDITY_HOP_PROMOTED":
                # Liquidity Hopping: Conquered level becomes support, target promoted to next pool
                conquered_lvl = exit_decision.conquered_level or trade["target_price"]
                promoted_tgt = exit_decision.next_target or (trade["target_price"] + 40.0)
                ratcheted_sl = exit_decision.ratcheted_sl or conquered_lvl
                booked_lots = exit_decision.booked_lots
                remaining_lots = exit_decision.remaining_lots

                trade["lots"] = remaining_lots
                trade["target_price"] = promoted_tgt
                trade["stop_loss"] = ratcheted_sl
                trade["has_hopped"] = True
                trade["conquered_level"] = conquered_lvl
                trade.setdefault("milestones_hit", set()).add("HOP_PROMOTED")

                # Estimate option prices for Telegram alert (~0.50 delta model)
                risk_p = float(trade.get("risk_points", 25.0))
                opt_entry = round(max(40.0, risk_p * 4.5), 1)
                opt_ratcheted_sl = round(opt_entry + (risk_p * 0.5), 1)
                projected_opt_target = round(opt_entry + (risk_p * 1.5), 1)
                locked_pts = round(opt_ratcheted_sl - opt_entry, 1)

                strike = trade.get("suggested_strike", "NIFTY OPTION")

                self.alert_agent.send_liquidity_hop_alert(
                    symbol=trade.get("symbol", "NIFTY"),
                    strike=strike,
                    conquered_level_name=f"Level {conquered_lvl:.0f}",
                    conquered_price=conquered_lvl,
                    promoted_target_name=f"Macro Pool {promoted_tgt:.0f}",
                    promoted_target_price=promoted_tgt,
                    ratcheted_sl_spot=ratcheted_sl,
                    ratcheted_sl_opt=opt_ratcheted_sl,
                    projected_opt_target=projected_opt_target,
                    locked_points=locked_pts,
                    remaining_lots=remaining_lots,
                )

                self.journal_agent.save_active_trades(active_trades, date_str)
                results["actions"].append({
                    "action": "LIQUIDITY_HOP_PROMOTED",
                    "trade_id": trade_id,
                    "conquered_level": conquered_lvl,
                    "next_target": promoted_tgt,
                    "ratcheted_sl": ratcheted_sl,
                    "booked_lots": booked_lots,
                    "remaining_lots": remaining_lots,
                    "reason": exit_decision.exit_reason,
                })

            elif exit_decision.is_fully_closed:
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
                    realized_r=exit_decision.realized_r,
                )
                results["actions"].append({
                    "action": exit_decision.action,
                    "trade_id": trade_id,
                    "pnl_rupees": float(closed_trade.get("pnl_rupees", 0.0) if closed_trade else 0.0),
                    "realized_points": exit_decision.realized_points,
                    "realized_r": exit_decision.realized_r,
                    "reason": exit_decision.exit_reason,
                })
                # Position squared off; update active list
                del active_trades[trade_id]
                self.journal_agent.save_active_trades(active_trades, date_str)
            else:
                # Active Position: Post-entry Trailing Milestone Monitoring (+1.0R and +2.0R)
                current_r = exit_decision.realized_r
                milestones = trade.setdefault("milestones_hit", set())
                strike = trade.get("suggested_strike", "NIFTY OPTION")
                risk_p = float(trade.get("risk_points", 25.0))
                opt_entry = round(max(40.0, risk_p * 4.5), 1)

                if current_r >= 1.0 and "1R" not in milestones:
                    milestones.add("1R")
                    opt_prem = round(opt_entry + (risk_p * 0.5), 1)
                    self.alert_agent.send_trailing_update(
                        symbol=trade.get("symbol", "NIFTY"),
                        strike=strike,
                        milestone="+1R Milestone",
                        cmp=cmp,
                        r_multiple=current_r,
                        opt_premium=opt_prem,
                        new_sl=opt_entry,
                        action_instruction="MOVE STOP LOSS TO COST (BREAKEVEN) NOW!",
                        status_note="Trade is now 100% RISK-FREE. Capital fully protected.",
                    )
                    self.journal_agent.save_active_trades(active_trades, date_str)
                    results["actions"].append({
                        "action": "TRAILING_UPDATE_1R",
                        "trade_id": trade_id,
                        "realized_r": current_r,
                        "new_sl": opt_entry,
                    })

                elif current_r >= 2.0 and "2R" not in milestones:
                    milestones.add("2R")
                    opt_prem = round(opt_entry + (risk_p * 1.0), 1)
                    locked_sl = round(opt_entry + (risk_p * 0.5), 1)
                    remaining_lots = max(1, int(trade.get("lots", 2)) // 2)
                    self.alert_agent.send_trailing_update(
                        symbol=trade.get("symbol", "NIFTY"),
                        strike=strike,
                        milestone="+2R Target",
                        cmp=cmp,
                        r_multiple=current_r,
                        opt_premium=opt_prem,
                        new_sl=locked_sl,
                        action_instruction="BOOK 50% LOTS NOW & LOCK +1R ON RUNNERS!",
                        status_note=f"Lock partial profit. Trail remaining {remaining_lots} lots with 5M low.",
                    )
                    self.journal_agent.save_active_trades(active_trades, date_str)
                    results["actions"].append({
                        "action": "TRAILING_UPDATE_2R",
                        "trade_id": trade_id,
                        "realized_r": current_r,
                        "new_sl": locked_sl,
                    })
                else:
                    self.journal_agent.save_active_trades(active_trades, date_str)

        # ---------------------------------------------------------------------
        # 2. Evaluate New Entries via Candidate Setups or EntryAgent (if Flat)
        # ---------------------------------------------------------------------
        if len(active_trades) == 0:
            # Check Circuit Breaker upfront
            if self.risk_agent.is_safety_wall_locked or self.risk_agent.daily_losses_count >= self.risk_agent.max_daily_losses or self.risk_agent.daily_cumulative_r <= -2.0:
                logger.info("Circuit Breaker Active: Trading halted for the day.")
                results["actions"].append({
                    "action": "CIRCUIT_BREAKER_HALT",
                    "reason": f"Safety wall locked ({self.risk_agent.daily_losses_count} losses, {self.risk_agent.daily_cumulative_r:.1f}R).",
                })
                return results

            # Gate A: 09:15 - 09:30 AM Opening Blackout
            if "09:15" <= time_val < "09:30":
                results["actions"].append({
                    "action": "VETO_OPENING_BLACKOUT",
                    "reason": "Opening Blackout (09:15-09:30 IST): Avoiding opening IV crush.",
                })
                return results

            # Gate B: Lunch Lull Gate (11:30 - 13:00 IST) - Hard freeze on low-volume chop
            if "11:30" <= time_val < "13:00":
                # Lunch Lull Gate – previously required volume >=1.8× avg.
                # New logic: lower threshold to 1.5× and allow override for high‑R candidates.
                has_waterfall_volume = False
                if df_5m is not None and len(df_5m) >= 10 and "volume" in df_5m.columns:
                    recent_vol = float(df_5m["volume"].iloc[-1])
                    avg_vol = float(df_5m["volume"].iloc[-10:-1].mean())
                    if avg_vol > 0 and recent_vol >= avg_vol * 1.5:
                        has_waterfall_volume = True

                # Check for high‑R candidate override (risk_points >=30 ≈ 2.5R expected)
                override_allowed = False
                if not has_waterfall_volume and candidate_setups:
                    for cand in candidate_setups:
                        if getattr(cand, "risk_points", 0) >= 30:
                            override_allowed = True
                            break

                if not has_waterfall_volume and not override_allowed:
                    results["actions"].append({
                        "action": "VETO_LUNCH_LULL",
                        "reason": "Lunch Lull Gate (11:30-13:00 IST): Low volume and no high‑R candidate override.",
                    })
                    return results

            # Gate C: Terminal Expiry Zone (14:30 - 15:30 IST) - Block new orders
            if time_val >= "14:30":
                results["actions"].append({
                    "action": "VETO_TERMINAL_EXPIRY_ZONE",
                    "reason": "Terminal Expiry Zone (>= 14:30 IST): Order submission blocked.",
                })
                return results

            # Gate D: Vega & Volatility Filters - India VIX Corridor [11.5, 22.0]
            vix_reading = india_vix
            if vix_reading is None:
                try:
                    from app.api.market import get_quote
                    q = get_quote("^INDIAVIX")
                    vix_reading = float(q.get("regularMarketPrice", 13.5)) if q else 13.5
                except Exception:
                    vix_reading = 13.5

            if vix_reading < 11.5:
                results["actions"].append({
                    "action": "VETO_VIX_CORRIDOR",
                    "reason": f"India VIX {vix_reading:.2f} < 11.5 (insufficient range).",
                })
                return results
            if vix_reading > 22.0:
                results["actions"].append({
                    "action": "VETO_VIX_CORRIDOR",
                    "reason": f"India VIX {vix_reading:.2f} > 22.0 (IV crush risk).",
                })
                return results

            # Check dual-trendline squeeze & consolidation lockout
            squeeze = None
            if df_5m is not None and len(df_5m) >= 10:
                bars_list = [
                    {"high": float(row["high"]), "low": float(row["low"]), "open": float(row["open"]), "close": float(row["close"])}
                    for _, row in df_5m.iterrows()
                ]
                squeeze = detect_dual_trendline_squeeze(bars_list)

            if squeeze and squeeze.is_in_consolidation:
                logger.info(
                    f"Consolidation Lockout Active: {squeeze.pattern} (Span: {squeeze.span_pts:.1f} pts < 35-40 pts). Skipping new entries."
                )
                results["actions"].append({
                    "action": "LOCKOUT_CHOP",
                    "reason": f"Dual-trendline squeeze active ({squeeze.pattern}, span {squeeze.span_pts:.1f} pts). Chop lockout.",
                })
                return results

            candidate_signal: TradeSignal | None = None

            # Process candidates from Scanner Crew
            if candidate_setups:
                for cand in candidate_setups:
                    if not getattr(cand, "is_valid_for_trade_crew", True):
                        continue

                    # Dynamic Lookout Horizon Check
                    cand_time = str(getattr(cand, "timestamp", "") or getattr(cand, "time", ""))
                    cand_tf = str(getattr(cand, "timeframe", "30M"))
                    lookout = DynamicLookoutWindow(
                        setup_name=getattr(cand, "strategy", "Setup"),
                        base_tf=cand_tf,
                        multiplier=4.0,
                        detected_time_str=cand_time,
                    )
                    if cand_time and not lookout.is_valid_window(time_val):
                        logger.info(f"Candidate setup {getattr(cand, 'strategy', 'Setup')} expired: exceeded {lookout.max_lookout_minutes}m lookout horizon.")
                        continue

                    # Gate E: Re-entry Transition Blackout (13:00 - 13:30 IST)
                    # Requires 15M structure flip confirmation to prevent double stop-out fakeouts
                    if "13:00" <= time_val < "13:30":
                        has_15m_flip = False
                        if df_15m is not None and len(df_15m) >= 2:
                            c15_curr = df_15m.iloc[-1]
                            c15_prev = df_15m.iloc[-2]
                            if cand.direction == "CALL" and float(c15_curr["close"]) > float(c15_prev["high"]):
                                has_15m_flip = True
                            elif cand.direction == "PUT" and float(c15_curr["close"]) < float(c15_prev["low"]):
                                has_15m_flip = True
                        elif df_5m is not None and len(df_5m) >= 6:
                            # 3x 5M bars proxy for 15M flip
                            last_c = df_5m.iloc[-1]
                            ref_high = df_5m["high"].iloc[-6:-3].max()
                            ref_low = df_5m["low"].iloc[-6:-3].min()
                            if cand.direction == "CALL" and float(last_c["close"]) > ref_high:
                                has_15m_flip = True
                            elif cand.direction == "PUT" and float(last_c["close"]) < ref_low:
                                has_15m_flip = True

                        if not has_15m_flip:
                            logger.info("Vetoed candidate in 13:00-13:30 transition blackout without 15M structure flip.")
                            continue

                    # Gate F: Gamma Wall & Vacuum Gate for candidates
                    cand_risk = getattr(cand, "risk_points", 25.0)
                    min_runway = max(45.0, cand_risk * 1.8)
                    if cand.direction == "CALL":
                        dist_to_call_wall = call_wall - cand.trigger_price
                        if dist_to_call_wall < 15.0 or abs(cand.trigger_price - call_wall) < 15.0:
                            logger.info(f"Candidate CALL vetoed: within 15 pts of Call Wall ({call_wall:.1f}).")
                            continue
                        if dist_to_call_wall < min_runway:
                            logger.info(f"Candidate CALL vetoed: only {dist_to_call_wall:.1f} pts runway to Call Wall (< {min_runway:.1f} pts).")
                            continue
                    else:
                        dist_to_put_wall = cand.trigger_price - put_wall
                        if dist_to_put_wall < 15.0 or abs(cand.trigger_price - put_wall) < 15.0:
                            logger.info(f"Candidate PUT vetoed: within 15 pts of Put Wall ({put_wall:.1f}).")
                            continue
                        if dist_to_put_wall < min_runway:
                            logger.info(f"Candidate PUT vetoed: only {dist_to_put_wall:.1f} pts runway to Put Wall (< {min_runway:.1f} pts).")
                            continue

                    # Gate G: EQH / EQL Liquidity Magnet & Trap Gate
                    # Prevents shorting under un-swept EQH buy-side liquidity or buying above un-swept EQL sell-side liquidity
                    is_sweep_strat = "SWEEP" in cand.strategy.upper() or "EQUAL" in cand.strategy.upper()
                    if not is_sweep_strat and df_5m is not None and len(df_5m) >= 6:
                        from app.trading.scanner_crew.levels_agent import LevelsAgent
                        _la = LevelsAgent()
                        eqh_list, eql_list = _la.detect_equal_highs_lows(df_5m)
                        if cand.direction == "PUT":
                            # Only veto short if entering directly beneath EQH magnet (<= 20 pts)
                            veto_eqh = any(0.0 < (eqh - cand.trigger_price) <= 20.0 for eqh in eqh_list)
                            if veto_eqh:
                                logger.info("Candidate PUT vetoed by EQH Magnet Gate: within 20 pts below un-swept EQH pool.")
                                continue
                            # Only veto short if directly crashing into un-swept EQL support (<= 15 pts)
                            veto_eql = any(0.0 < (cand.trigger_price - eql) <= 15.0 for eql in eql_list)
                            if veto_eql:
                                logger.info("Candidate PUT vetoed by EQL Support Gate: within 15 pts above un-swept EQL support.")
                                continue
                        else:
                            # Only veto long if entering directly above EQL magnet (<= 20 pts)
                            veto_eql = any(0.0 < (cand.trigger_price - eql) <= 20.0 for eql in eql_list)
                            if veto_eql:
                                logger.info("Candidate CALL vetoed by EQL Magnet Gate: within 20 pts above un-swept EQL pool.")
                                continue
                            # Only veto long if directly crashing into un-swept EQH resistance (<= 15 pts)
                            veto_eqh = any(0.0 < (eqh - cand.trigger_price) <= 15.0 for eqh in eqh_list)
                            if veto_eqh:
                                logger.info("Candidate CALL vetoed by EQH Resistance Gate: within 15 pts below un-swept EQH resistance.")
                                continue

                    # Dynamic target and regime from TrendAgent
                    regime = self.trend_agent.evaluate_regime(df_5m, cand.trigger_price, cand.direction, key_levels)
                    if not regime.is_breakout_allowed:
                        continue

                    # Detect Super-Trend momentum (ADX >= 25, Volatile Expansion, or flag)
                    is_super = getattr(cand, "is_super_trend", False) or regime.adx >= 25.0 or regime.regime == "VOLATILE_EXPANSION"

                    # Risk approval with Order Flow & Super-Trend Sizing (Fixed 3 Lots standard / 4 Lots super-trend)
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

                    # Categorize liquidity pool
                    strat_upper = cand.strategy.upper()
                    if "SWEEP" in strat_upper:
                        l_pool = "BSL_SWEEP" if cand.direction == "PUT" else "SSL_SWEEP"
                    elif "OR" in strat_upper or "RETEST" in strat_upper or "BREAKOUT" in strat_upper:
                        l_pool = "BSL_EXPANSION" if cand.direction == "CALL" else "SSL_EXPANSION"
                    else:
                        l_pool = "BSL_EXPANSION" if cand.direction == "CALL" else "SSL_EXPANSION"

                    # 2-OTM Strike Selection with Delta >= 0.35 floor
                    strike, est_delta = select_strike_with_delta_floor(
                        entry_price=cand.trigger_price,
                        direction=cand.direction,
                        otm_steps=2,
                        min_delta=0.35,
                    )

                    clearance = (call_wall - cand.trigger_price) if cand.direction == "CALL" else (cand.trigger_price - put_wall)
                    opposing_wall = call_wall if cand.direction == "CALL" else put_wall
                    thesis = DirectionalThesis(
                        primary_bet=f"{'Bullish' if cand.direction == 'CALL' else 'Bearish'} {l_pool} to {target_p:,.1f}",
                        targeted_pool=f"{cand.strategy} ({cand.trigger_price:,.1f}) & Wall ({opposing_wall:,.1f})",
                        invalidation_price=round(cand.tentative_stop, 2),
                        invalidation_condition=f"5M candle body close {'below' if cand.direction == 'CALL' else 'above'} {cand.tentative_stop:,.1f} kills thesis",
                        stagnation_timeout_bars=6,
                        gamma_wall_clearance_pts=round(clearance, 1),
                        thesis_conviction="SUPER_TREND" if is_super else "HIGH",
                    )

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
                        suggested_strike=strike,
                        timestamp=cand.timestamp,
                        rationale=f"{cand.rationale} | Dynamic R:R {regime.recommended_target_r:.1f}R (Delta: {est_delta:.2f})",
                        regime=regime.regime,
                        adx=regime.adx,
                        base_timeframe=cand_tf,
                        liquidity_pool=l_pool,
                        lookout_duration_mins=lookout.max_lookout_minutes,
                        lookout_status="TRIGGERED",
                        thesis=thesis,
                    )
                    break

            # Fallback to internal sweep/waterfall check if no candidates passed
            if not candidate_signal:
                # Gate E check for internal sweep during 13:00 - 13:30
                allow_internal = True
                if "13:00" <= time_val < "13:30":
                    allow_internal = False
                if allow_internal:
                    candidate_signal = self.entry_agent.evaluate_sweep(
                        df_5m, pdh, pdl, key_levels, call_wall=call_wall, put_wall=put_wall
                    )
                    if not candidate_signal and morning_floor is not None:
                        from app.trading.scanner_crew.levels_agent import LevelsAgent
                        _la = LevelsAgent()
                        eqh_list, eql_list = _la.detect_equal_highs_lows(df_5m)
                        current_c = float(df_5m.iloc[-1]["close"]) if df_5m is not None and not df_5m.empty else morning_floor
                        veto_waterfall = any(0.0 < (eqh - current_c) <= 20.0 for eqh in eqh_list) or any(0.0 < (current_c - eql) <= 15.0 for eql in eql_list)
                        if not veto_waterfall:
                            candidate_signal = self.entry_agent.evaluate_waterfall(
                                df_5m, morning_floor, key_levels, put_wall=put_wall
                            )
                        else:
                            logger.info("Waterfall PUT entry vetoed by EQH/EQL Magnet Gate (un-swept liquidity pool nearby).")

            if candidate_signal:
                # -----------------------------------------------------------------
                # Idempotent Order Authorization & Duplicate Suppression
                # -----------------------------------------------------------------
                raw_sig = f"{date_val}_{candidate_signal.strategy}_{candidate_signal.direction}_{candidate_signal.suggested_strike}_{round(candidate_signal.entry_price, 0)}"
                idempotent_key = hashlib.sha256(raw_sig.encode("utf-8")).hexdigest()

                is_duplicate = False
                for t in active_trades.values():
                    if t.get("idempotent_key") == idempotent_key:
                        is_duplicate = True
                        break
                if idempotent_key in self.executed_order_signatures:
                    is_duplicate = True

                if is_duplicate:
                    logger.warning(
                        f"Duplicate Execution Suppressed: Order signature {idempotent_key[:12]} already active/executed today."
                    )
                    results["actions"].append({
                        "action": "DUPLICATE_ORDER_SUPPRESSED",
                        "idempotent_key": idempotent_key,
                        "strategy": candidate_signal.strategy,
                        "strike": candidate_signal.suggested_strike,
                        "reason": "Duplicate execution suppressed to prevent double order placement.",
                    })
                    return results

                # Mark signature executed
                self.executed_order_signatures.add(idempotent_key)

                new_trade = self.journal_agent.record_entry(candidate_signal, date_str)
                new_trade["idempotent_key"] = idempotent_key
                # Save updated active trade with signature
                active_trades_now = self.journal_agent.load_active_trades(date_str)
                if new_trade.get("trade_id") in active_trades_now:
                    active_trades_now[new_trade["trade_id"]]["idempotent_key"] = idempotent_key
                    self.journal_agent.save_active_trades(active_trades_now, date_str)

                # Dispatch Telegram Entry Alert with Complete Directional Thesis
                nearest_wall = "Call Wall" if candidate_signal.direction == "CALL" else "Put Wall"
                clearance_val = (call_wall - candidate_signal.entry_price) if candidate_signal.direction == "CALL" else (candidate_signal.entry_price - put_wall)
                self.alert_agent.send_entry_alert(
                    signal=candidate_signal,
                    safety_wall_balance=self.risk_agent.safety_wall_reserve,
                    is_safety_wall_locked=self.risk_agent.is_safety_wall_locked,
                    nearest_wall_name=nearest_wall,
                    nearest_wall_dist=clearance_val,
                )

                results["actions"].append({
                    "action": "ENTRY_OPENED",
                    "trade_id": new_trade.get("trade_id"),
                    "direction": candidate_signal.direction,
                    "strike": candidate_signal.suggested_strike,
                    "entry_price": candidate_signal.entry_price,
                    "target": candidate_signal.target_price,
                    "dynamic_rr": candidate_signal.rr_ratio,
                    "lots": candidate_signal.lots,
                    "idempotent_key": idempotent_key,
                })

        return results
