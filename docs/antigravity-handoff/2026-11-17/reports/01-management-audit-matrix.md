# Prompt 01 — Comprehensive Management Path Audit & Implementation Matrix

**Date**: 17 November 2026  
**Context**: Genesis Trading System — Research Audit  
**Scope**: Trace of `crew/manager.py`, `holding_agent.py`, `exit_agent.py`, risk/accounting functions (`paper_risk.py`, `paper_fees.py`, `forward_fees.py`), and the live candidate path (`forward_candidate_account.py`).

---

## 1. Executive Summary & Architectural Evolution

The management logic in Genesis has evolved across three distinct eras:

1. **Old Frozen Engine (`efde368` / 27-Trade Audit)**:
   - Sizing: Fixed 3 to 4 lots (195–260 shares) under an index points × $\Delta 0.5$ accounting proxy without true option bid/ask fees or slippage.
   - Initial stop exposure: INR 3,402.75 to 7,078.50 per trade.
   - Management: 5m structure trailing at $+1.5\text{R}$, 30-minute opening range retest / trendline signals, fixed 3R target. No `HoldingAgent` existed.
   - Flaws: Stale/forming bar usage in replay, precomputed opening range, index proxy masking actual option theta/decay and bid/ask spread.

2. **Later Archived / Current Crew (`holding_agent.py`, `exit_agent.py`, `manager.py`)**:
   - Introduced `HoldingAgent` positioned between `EntryAgent` and `ExitAgent`.
   - Added multi-factor diagnostics: 15m structural invalidation, Gamma Wall rejection, CVD order flow absorption, opposing key-level denial, VWAP breakdown/reclaim, bull/bear trap invalidation, consolidation flag runway extension, and healthy retest cushioning.
   - Flaws & Discrepancies Identified:
     - **Stop Loosening vs. Defeat**: `HoldingAgent` proposes `cushion_sl = entry_price - 8.0` for CALLs to cushion runners during shallow pullbacks. However, `TradeManager` assigns this to `trade["stop_loss"]`, while `ExitAgent` enforces `effective_sl = max(stop_loss, entry_price)` whenever `be_locked` is True. Thus `be_locked` completely defeats the cushion. If `be_locked` had not defeated it, the stop would have been illegally widened below breakeven into loss territory.
     - **Flip-and-Reverse Disconnect**: In `HoldingAgent`, lines 262 and 303 hardcode `action = "PREEMPTIVE_EXIT"` even within the flip block. Consequently, `FLIP_AND_REVERSE` is never emitted. Even if emitted, account gates require complete order fill and cash settlement; auto-flipping is not wired in the execution ledger.
     - **RC Quick-Cut**: The two-bar cut (`_mark_rc_quick_cut`) is purely a diagnostic tag in `manager.py` (`trade["rc_quick_cut"] = True`), never triggering a position exit.
     - **Unavailable Inputs**: Gamma Wall (Call/Put Wall from full option chain OI) and Order Flow CVD absorption are unavailable in historical spot data. Volume in Nifty index spot candles is zero, so `compute_session_vwap()` falls back to a typical price average ($(\text{H}+\text{L}+\text{C})/3$), which is a price-only TWAP, not true volume VWAP.

3. **Current Active Paper Candidate (`forward_candidate_account.py` & `paper_profit_trail.py`)**:
   - Implements a simplistic 20% giveback from peak net liquidation profit, armed after reaching 1R entry risk.
   - Retains initial stop loss and 15:10 IST squareoff. No 3R target, no 5m structure trailing, no holding diagnostics.
   - Problem: As observed on 17 November, healthy runners experiencing standard 5m/15m shallow retests are prematurely choked out (e.g. 09:38 CALL exiting at 09:43 at +1.4R before a major continuation).

---

## 2. Complete Management Path Implementation Matrix

| # | Condition & Rule | Trigger Threshold | Candle TF | Earliest Knowable Clock | Required Inputs & Provenance | Action Directives | Priority | Original Call-Site Wired? | Current Candidate Equivalent | Safe Proposed Adapter Behavior |
|---|---|---|---|---|---|---|---|---|---|---|
| **1** | **Protective Initial Stop Loss** | CMP $\le \text{SL}$ (CALL) / CMP $\ge \text{SL}$ (PUT) | Continuous Quote / 1m | Instantaneous on quote touch | Spot CMP, contract bid | Full exit at market/bid | 1 (Highest) | Yes (`ExitAgent` L88) | Yes (`CandidateAccount._eval_exit`) | Retain. Never widen. Fill at fresh held contract bid minus 1 tick. |
| **2** | **Intraday Squareoff** | Clock $\ge \text{15:10 IST}$ | System Clock | 15:10:00 IST | System time | Full exit (`INTRADAY_CLOSE`) | 2 | Yes (`ExitAgent` L177) | Yes (15:10 squareoff) | Retain unconditional 15:10 squareoff. |
| **3** | **0.7R Breakeven Milestone** | Underlying $R \ge 0.70$ | Tick / 1m close | When spot reaches $+0.7\text{R}$ | Spot CMP, Entry, Risk Pts | Tighten SL to $\text{Entry}$ (`be_locked = True`) | 3 | Yes (`TradeManager` L420) | None (Candidate uses profit trail) | Adopt monotonic ratchet: $\text{SL} \leftarrow \max(\text{SL}, \text{Entry})$ for CALL. |
| **4** | **2.0R Profit Lock Milestone** | Underlying $R \ge 2.00$ | Tick / 1m close | When spot reaches $+2.0\text{R}$ | Spot CMP, Entry, Risk Pts | Tighten SL to $\text{Entry} + 0.8\text{R}$ (CALL) / $\text{Entry} - 0.8\text{R}$ (PUT) | 3 | Yes (`TradeManager` L444) | None | Adopt monotonic ratchet: lock $+0.8\text{R}$ on remaining position. |
| **5** | **15M Structural Invalidation** | $R \le -0.30\text{R}$, bars $\ge 2$, 15m close breaks prior 15m low/high | 15m completed | 15m bar close (e.g. 09:30, 09:45) | Completed `df_15m` OHLC | Preemptive damage cut (`PREEMPTIVE_EXIT`) | 4 | Yes (`HoldingAgent` L114) | None | Implement using strictly completed 15m bars. Cuts loss early at $-0.3\text{R}$ to $-0.4\text{R}$. |
| **6** | **Thesis Trap Invalidation** | $R \le -0.35\text{R}$, bars $\ge 2$, bar range $\ge 12\text{pt}$, close near adverse extreme | 5m completed | 5m bar close | Completed `df_5m` OHLC, Entry price | Preemptive damage cut (`PREEMPTIVE_EXIT`) | 4 | Yes (`HoldingAgent` L238) | None | Implement using strictly completed 5m bars. Discard auto-flip unless flat & separate setup admitted. |
| **7** | **Opposing Key-Level Denial** | $R \ge 1.0\text{R}$, within 10pt of key resistance/support, red rejection wick $\ge 35\%$ or failed continuation | 5m completed | 5m bar close | Completed `df_5m`, Key levels (PDH/PDL/PDC/OR30) | Preemptive profit secure (`PREEMPTIVE_PROFIT`) | 5 | Yes (`HoldingAgent` L184) | None | Implement as thesis exhaustion exit when opposing macro level strongly rejects. |
| **8** | **5M Structure Trailing** | $R \ge 1.50\text{R}$, completed 5m candle closes below prior 5m low (CALL) / above prior 5m high (PUT) | 5m completed | 5m bar close | Completed `df_5m` OHLC | Structure trail exit (`STRUCTURE_TRAIL_EXIT`) | 6 | Yes (`ExitAgent` L190) | None | Core winner management: lets runners ride until 5m structural pivot is violated. |
| **9** | **Consolidation Flag Runway** | Bars in trade $\ge 6$, `be_locked = True`, $R \ge 0.20$, 2-bar 5m range $\le 16\text{pt}$ | 5m completed | 5m bar close | Completed `df_5m`, Trade state | HOLD; extends stagnation limit from 6 to 10 bars | 7 | Yes (`HoldingAgent` L333) | None | Validates healthy low-volatility pause; prevents premature delta-decay exit. |
| **10** | **Healthy Retest Cushion** | `be_locked = True`, $-0.10 \le R \le 0.60$ | Continuous / 5m | Instantaneous | Spot CMP, Trade state | HOLD directive | 7 | Buggy in archive (attempts stop widening, defeated by `be_locked`) | None | Grant HOLD against premature exit; NEVER loosen stop loss. Monotonic stop constraint strictly enforced. |
| **11** | **Delta Stagnation Time-Stop** | Bars $\ge 6$ (or 10 in Consolidation Flag) and $R < 1.80$ | 5m completed | 30m / 50m elapsed from entry | Elapsed 5m bars in trade | Market exit (`STAGNATION_TIME_STOP`) | 8 | Yes (`ExitAgent` L161) | None | Retain to prevent theta bleed when move fails to develop. |
| **12** | **Opposing Signal Reversal** | Fresh confirmed opposite strategy signal | 1m or 5m completed | Close of confirming signal bar | Verified signal stream | Full exit (`REVERSAL_EXIT`) | 8 | Yes (`ExitAgent` L150) | None | Exits open trade on fresh opposite confirmation. Re-entry requires normal account clearance. |
| **13** | **Target 3 (Unconditional Target)** | CMP reaches initial target ($+3.0\text{R}$ to $+4.5\text{R}$) | Tick / 1m close | Instantaneous on target touch | Spot CMP, Target price | Full exit (`TARGET_HIT`) | 9 | Yes (`ExitAgent` L140) | None | Tested as distinct target-capped variant vs. unlimited runner variant. |
| **14** | **Liquidity Hopping (Partial Booking)** | CMP reaches target, `lots >= 2`, not previously hopped | Tick / 1m close | Instantaneous on target touch | Spot CMP, Lots, Key levels | Book $\lfloor\text{lots}/2\rfloor$, ratchet SL to target $- 3\text{pt}$, promote target | 9 | Yes (`ExitAgent` L105) | None | Allowed only when position $\ge 2$ whole lots; charge partial fee and reserve exit costs. |
| **15** | **Gamma Wall Defense** | $R \ge 1.0\text{R}$, within 15pt of Call/Put Wall, wick rejection | 5m completed | 5m bar close | Live Option Chain OI Distribution | Preemptive profit exit | — | Yes (`HoldingAgent` L137) | None | **UNAVAILABLE historically**. Must remain cleanly disabled/unavailable when OI missing. |
| **16** | **Order Flow CVD Absorption** | $R \ge 1.0\text{R}$, CVD divergence / limit absorption | Tick / 1m stream | Instantaneous | Order Flow / CVD metrics | Preemptive profit exit | — | Yes (`HoldingAgent` L164) | None | **UNAVAILABLE historically**. Spot index volume is 0; do not fabricate CVD. |
| **17** | **VWAP Invalidation** | Bars $\ge 2$, $R \le -0.40$, 5m close violates VWAP $\pm 5\text{pt}$ | 5m completed | 5m bar close | True Volume VWAP | Preemptive damage cut | — | Fallback to typical price in archive | None | **UNAVAILABLE historically** without real volume. Price-only TWAP fallback is a distinct hypothesis. |
| **18** | **RC Quick-Cut** | Within 2 bars of entry, close violates entry candle low/high | 5m completed | 2nd bar close | Completed `df_5m` OHLC | Diagnostic flag only (`rc_quick_cut = True`) | — | Diagnostic only (`manager.py` L1013) | None | Retain as diagnostic logging; does NOT exit trade. |

---

## 3. Detailed Audit of Specific Path Items

### 3.1 Gamma Wall & Order Flow Absorption
- **Gamma Wall**: In `holding_agent.py`, lines 137–162 check proximity to `call_wall` and `put_wall`. In live trading, `GammaAgent` computes these from Upstox option chain open interest peaks. In historical candle datasets (Yahoo Finance / resampled spot), option chains and OI are completely absent. Therefore, historical Gamma Wall checks must be explicitly marked **UNAVAILABLE** and evaluated as `None`.
- **Order Flow CVD**: `order_flow_agent.py` requires tick-by-tick buyer vs. seller aggressive volume. Spot Nifty index candles contain 0 volume across historical providers. Fabricating volume or delta from typical price is strictly forbidden. This rule must evaluate as **UNAVAILABLE**.

### 3.2 VWAP Logic & Trap Recognition
- In `compute_session_vwap()`, when cumulative volume is zero, the code computes `tp.mean()` where $tp = (\text{H}+\text{L}+\text{C})/3$. This is a pure arithmetic TWAP of typical price across available bars, not VWAP.
- A true VWAP rule must fail as **UNAVAILABLE** when volume is 0. An arithmetic typical-price boundary is an explicit secondary hypothesis, not historical VWAP.
- **Trap Recognition**: Bull/Bear trap invalidation checks whether a bar with range $\ge 12$ points reverses aggressively through entry and closes near its extreme within the first few bars. This is entirely calculable from completed 5m OHLC and provides a genuine early risk reduction (cutting at $-0.35\text{R}$ vs. full $-1.0\text{R}$).

### 3.3 The Retest Cushion & `be_locked` Defeat
- In `holding_agent.py`:
  ```python
  if be_locked and -0.10 <= current_r <= 0.60:
      cushion_sl = (entry_price - 8.0) if direction == "CALL" else (entry_price + 8.0)
      return HoldingDecision(state="HEALTHY_RETEST", action="HOLD", cushion_sl=cushion_sl)
  ```
- If `cushion_sl` were applied directly to a CALL position, the stop loss would move from `entry_price` (where loss is 0) down to `entry_price - 8.0` (re-introducing risk).
- In `exit_agent.py`:
  ```python
  effective_sl = stop_loss
  if active_trade.get("be_locked"):
      effective_sl = max(stop_loss, entry_price) if direction == "CALL" else min(stop_loss, entry_price)
  ```
  `ExitAgent` takes `max(stop_loss, entry_price)`, resetting `effective_sl` to `entry_price`.
- **Conclusion**: The purported cushion was non-functional in practice because `be_locked` overrode it.
- **Safe Adaptation**: Uphold the fundamental invariant of **Monotonic Stop Tightening**:
  $$\text{Stop}_{\text{new}} = \max(\text{Stop}_{\text{prior}}, \text{Stop}_{\text{proposed}}) \quad (\text{for CALLs})$$
  $$\text{Stop}_{\text{new}} = \min(\text{Stop}_{\text{prior}}, \text{Stop}_{\text{proposed}}) \quad (\text{for PUTs})$$
  The healthy retest condition acts as a **HOLD directive** (preventing premature stagnation or profit-choking exits) rather than a stop-loosening mechanism.

### 3.4 Milestones, Structure Trailing, and Stagnation
- **0.7R Milestone**: Once underlying gain reaches $+0.7\text{R}$, stop moves to exact entry price (`be_locked = True`).
- **2.0R Milestone**: Once underlying gain reaches $+2.0\text{R}$, stop locks $+0.8\text{R}$ (`entry + 0.8 * risk`).
- **5M Structure Trailing**: Once $R \ge 1.5\text{R}$, position trails using the prior completed 5m candle low (for CALL) or high (for PUT). This allows open profits to expand freely during sustained trends while exiting cleanly upon a structural reversal.
- **Consolidation Runway**: When a position is risk-free (`be_locked = True`) and moving in a tight consolidation box ($\le 16$ points across two 5m bars), the stagnation counter is extended from 6 bars (30m) to 10 bars (50m), avoiding premature chop exits during normal midday flag formations.

### 3.5 Auto-Flip & Reversal Execution
- In `holding_agent.py`, candidate setups with `strategy="Trap Reversal Flip [5M]"` were instantiated, but the returned action was overridden to `"PREEMPTIVE_EXIT"`.
- In `CandidateAccount` (and live ledger), single-position constraints strictly forbid opening a second position while one is active or pending exit.
- Emitted reversals must be treated as independent setups: the open trade exits first, settles its fill and fees, and only then may a newly confirmed setup pass normal pre-flight, capital, and spread gates.

---

## 4. Versioned Protocol for Prompt 02 Implementation

1. **Module Name**: `backend/app/trading/thesis_management_policy.py`
2. **Version Tag**: `thesis-management-research-v1`
3. **Core Directives**:
   - **Monotonic Stops**: Strictly non-widening stops across all updates.
   - **Thesis Health**: 5m structure trailing ($\ge 1.5\text{R}$), 0.7R breakeven, 2.0R profit lock, consolidation runway extension (10 bars).
   - **Invalidation Exits**: 15m structural breakdown ($R \le -0.3\text{R}$), 5m trap invalidation ($R \le -0.35\text{R}$), opposing key-level denial ($R \ge 1.0\text{R}$).
   - **Completed Bars Only**: Strictly closed candle references; zero future or forming bar peeking.
   - **Unavailable Factor Safety**: Gracefully handle missing Gamma Walls, Order Flow, and VWAP by bypassing those rules without error.
   - **Whole-Lot Sizing**: Minimum 2 lots required for any partial booking/hopping; reserve all fees.
   - **Execution Realism**: All fill pricing determined by held-contract bid/ask quotes with actual fee schedules; index levels serve strictly as triggers.
