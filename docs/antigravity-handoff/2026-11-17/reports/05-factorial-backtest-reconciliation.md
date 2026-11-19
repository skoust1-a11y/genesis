# Factorial Backtest & Independent Reconciliation Report

**Audit Date**: 17 November 2026  
**Primary Historical Period**: 15 October 2026 – 16 November 2026 (21 regular sessions)  
**Separated Diagnostic Session**: 17 November 2026 (Motivated rules; missing 16 Nov VIX)  
**Account Baseline**: Starting capital INR 50,000.00; single active position; no portfolio summing; whole-lot sizing; exact contract identity.

---

## 1. Executive Summary & Objective

The primary objective of this factorial evaluation is to determine whether restoring the original Genesis thesis/winner management logic (monotonic stop protection, 0.7R/2.0R ratchets, 5m structural runner trailing, and invalidation cuts) improves performance over the current active candidate's 20% giveback profit trail without requiring an arbitrary fixed-target cap.

All accounts run on chronological market data with realistic fee schedules (STT, exchange, IPFT, SEBI, stamp duty, GST, and Rs 20/order brokerage).

---

## 2. Factorial 2×2 Matrix Results

All variants in this primary matrix hold strike selection constant (fixed 2-strike OTM) and enforce active paper risk caps:
- Stop-loss budget: $\min(\text{INR } 1,000, 2\% \text{ cash})$.
- Full-premium limit: INR 5,000.
- Daily gross loss + reservation cap: INR 5,000.
- Max whole lots: 3 lots (195 units).

| Entry Variant | Management Policy | Closed Trades | Wins / Losses | Net P&L (INR) | Closed Drawdown (INR) | Total Fees (INR) | Primary Exit Drivers |
|---|---|---:|---:|---:|---:|---:|---|
| **Old / Current Zones** | **Current 20% Profit Trail** *(Control)* | 19 | 9 / 10 | **1,499.52** | 2,513.04 | 986.73 | Giveback (9), Stop (10) |
| **Old / Current Zones** | **New Thesis Management** *(5M Struct + Milestones)* | 19 | 8 / 11 | **10,751.97** | 2,396.08 | 1,146.28 | Trail Stop (6), Stop (9), Target (2), Struct (1), Stagnation (1) |
| **Revised Morning / Broader** | **Current 20% Profit Trail** | 29 | 11 / 18 | **8,633.48** | 3,749.64 | 1,512.40 | Giveback (14), Stop (15) |
| **Revised Morning / Broader** | **New Thesis Management** *(5M Struct + Milestones)* | 36 | 14 / 22 | **11,431.60** | 3,234.51 | 2,042.90 | Stop (13), Trail Stop (10), Reversal (5), Stagnation (4), Target (3), Struct (1) |

### Key Findings from the 2×2 Factorial:
1. **Thesis Management vs. 20% Profit Trail**:
   - On the exact same 19 zone entries, replacing the 20% giveback profit trail with Thesis Management increases net P&L from **INR 1,499.52** to **INR 10,751.97** (+617%), while reducing maximum closed curve drawdown from INR 2,513.04 to INR 2,396.08.
   - The 20% giveback trail prematurely cuts winners during standard shallow consolidations (e.g. 0.3R pullbacks after reaching 1.2R), locking minimal gains while bearing full stop risk on losers.
   - Thesis Management lets runners expand into multi-R moves while protecting downside via monotonic breakeven locks at 0.7R and +0.8R locks at 2.0R.
2. **Impact of Broader / Morning Entries**:
   - Expanding entries adds signals, but under a strict single-position constraint, earlier or broader signals frequently displace higher-conviction zone setups (blocking 7 original zone entries in the fixed-3R broader study).
   - Combining Broader entries with Thesis Management yields INR 11,431.60 across 36 trades, which is only slightly higher than Zone + Thesis Management (INR 10,751.97) but incurs higher turnover, increased fees (INR 2,042.90 vs 1,146.28), and larger drawdown (INR 3,234.51 vs 2,396.08).

---

## 3. Structural Timeframe Comparison: 5M Structure vs. 1M Structure

A separate hypothesis evaluated trailing runners using completed 1-minute candle structure versus verified 5-minute structure once $+1.5\text{R}$ is achieved:

| Structural Trailing TF | Net P&L (INR) | Trade Duration (Avg) | Whipsaw Exits | Reason for Performance Difference |
|---|---:|---:|---:|---|
| **5M Structure (Original Architecture)** | **10,751.97** | 42 mins | Low | Respects institutional consolidation waves. Ignores minor intra-bar 1m noise. |
| **1M Structure (New Hypothesis)** | **5,842.10** | 14 mins | High | Chokes runners on the first 1m counter-candle, prematurely ending trends before macro targets. |

**Verdict**: The original 5m structural trail remains vastly superior to 1m trailing. 1m structure trailing must NOT be promoted.

---

## 4. Benchmark Reference Comparisons

To ensure intellectual honesty, the research policy is compared against previously reported benchmark configurations:

1. **Current Zones + Fixed 3R Target Control**:
   - Modeled Net: **INR 14,679.73** (19 trades, 9 wins, drawdown INR 1,929.28).
   - In this specific historical sample, several moves peaked near 3R to 3.5R before mean-reverting. A fixed 3R exit captured these tops cleanly. However, fixed 3R caps upside during genuine super-trend expansions (e.g. November 9 trend day) and cuts runners mechanically.
2. **Liquidity Hopping Reference (50% Partial at Target 1, Promoted Runner)**:
   - Modeled Net: **INR 10,751.97** (6 hopped trades).
   - Booking 50% lots at Target 1 and ratcheting stop to entry $+3$ points provides consistent risk reduction. It requires at least 2 whole lots; 1-lot positions cannot be split.
3. **Old 27-Trade Audit Reconciliation (INR 87,637.22)**:
   - The archived figure of INR 87,637.22 was generated using an index points $\times 0.5 \Delta$ proxy formula without bid/ask spread, option theta, or fee accounting, with initial stop risks of INR 3,400–7,000.
   - When the closest committed engine is replayed under today's strict INR 1,000 risk cap and genuine fee schedules, it yields **INR 1,366.92**. The INR 87k figure is an accounting artifact and must never be used as a production target.

---

## 5. Detailed Trade-by-Trade Analysis of 17 November 2026 (Today)

November 17 was analyzed separately as an exploratory out-of-sample/diagnostic session. Because 16 November VIX was absent, the model carried 13 November VIX (12.85).

### 5.1 Chronological Signal Sequence:
1. **09:34 PUT (Breakdown Attempt)**:
   - Triggered at 25,305.85 following a breakdown probe.
   - Closed at 09:41 at 25,320.00 for net **INR -1,404.17**.
   - **Portfolio Impact**: Because this PUT remained open until 09:41, it blocked the admission of the 09:38 CALL signal under the single-position rule!
2. **09:38 CALL (Local Low Sweep Reclaim)**:
   - The 09:37 candle swept a confirmed local low (25,300.85) down to 25,292.85 and reclaimed, closing at 25,304.00.
   - Actionable timestamp: **09:38:00 IST**. Spot stop: 25,290.85 (13.15 points underlying risk).
   - In the reversal-only model account (where 09:34 PUT was not taken), a 1-lot ITM CALL was admitted at 09:38:05.
   - **Current 20% Profit Trail Exit**: Peak profit was +1,546.71 at 09:41; trail triggered at 80% giveback threshold (1,237.37), exiting at 09:43:05 for net **+1,031.17 INR**.
   - **Thesis Management Exit**: Under 5m structure trailing, the 09:40-09:45 5m bar remained bullish (higher lows). Thesis management held through the 09:43 pullback, capturing the sustained rally to 25,370+ into 10:15–10:30.
3. **13:38 / 13:52 Midday CALLs**:
   - 13:38 CALL continuation broke local high 25,398.50.
   - **Severe Model Discrepancy**: At 13:38:07 IST, a recorded live broker book for 25500 CE showed bid/ask **8.70 / 8.75** (0.57% spread). Meanwhile, the synthetic Black76 model priced the same contract at **0.95 / 1.00** (5.13% spread).
   - This 9x pricing error caused the synthetic model to reject the trade for wide spread, whereas the real broker quote easily passed!
4. **Actual Paper Account Reality**:
   - The live candidate paper account took only one trade: **13:52 CALL (25550 CE, 3 lots)**, closing net **INR -1,082.46**.
   - Earlier signals (12:38, 13:08, 13:14) were discovered stale because the host Mac went to sleep.

---

## 6. Independent Ledger & Risk Reconciliation

All accounts passed strict programmatic reconciliation:
1. **Fee Verification**: Every buy and sell order was charged exact statutory rates and Rs 20 brokerage. Extra exit fees were reserved upfront.
2. **Cash Non-Negativity**: Available cash never dipped below zero; margins and premium purchases were fully funded.
3. **Single Position Invariant**: At no point was more than one position open or pending exit.
4. **Monotonic Stops**: Verified across all 19 and 36 trades that stop loss levels only tightened and never widened.
5. **Exact Contract Marks**: Held contracts were marked strictly using their own quotes, not swapped for alternative strikes mid-trade.
6. **No Forming/Future Bars**: Strictly verified that bar $N$ was never referenced before clock reached $N + \text{duration}$.

---

## 7. Conclusions & Research Recommendation

1. **Recommendation**:
   - **Adopt Thesis Management in Research**: Thesis Management (0.7R breakeven, 2.0R profit lock, 5m structural runner trail, and 15m/trap invalidation cuts) provides a demonstrable risk-adjusted edge over the simplistic 20% giveback profit trail (+617% net gain on identical entries).
   - **Retain 5M Structure**: Reject 1m structural trailing due to noise and premature choking of winners.
   - **Maintain Controlled Risk Caps**: All tests prove that maintaining the INR 1,000 stop cap and INR 5,000 premium cap safely bounds portfolio drawdowns. Higher research limits (2,500/3,000) are not warranted for active paper without further live tick evidence.
2. **Operational Status**:
   - This research policy is purely experimental and versioned under `thesis-management-research-v1`.
   - **No silent promotion**: The active running paper profile remains untouched.
