# Adding earlier Genesis features to the current strategy

Same 7,882 observations: 15 October–13 November 2026 (20 complete regular sessions), plus 16 November through 15:16:25 IST. Each independent account starts at INR 50,000.

**Model results, not actual historical option execution. Synthetic Black76 option books/Greeks use prior-session VIX; costs/news are declared assumptions. No untouched holdout is available.**

## Added code and rules

New backend module app/trading/hybrid_research_policy.py provides a separate versioned research identity. The frozen scanner/ledger policy is not edited or re-registered. Current zone entries remain; broader variants add confirmed PD retests, trendline CHoCH and equal-high/low sweeps on validated 1m/5m/15m candles. The frozen liquidity-sweep and OR30 helpers assume 5m bars, so those families remain 5m-only. Inline equal-level start stamps are corrected to true closes; source candles and prices are verified, future/stale signals rejected, and each native candle confirmation consumed once.

Same-bar priority is current zone first, then native5m/15m/1m in stable family order. News and previous-session/calendar evidence must be available for native entries. The completed 30-minute opening range is never provided early. Existing VWAP/TWAP and consolidation logic remains inside the frozen detector. Real historical OI/gamma evidence and the stateful HoldingAgent are unavailable and are not fabricated.

Earlier management uses monotonic underlying stops: entry stop after 0.7R; approximately +0.8R after2R; one whole-lot liquidity hop at the common3R first target; structure exit above current1.5R; 6/10-bar stagnation; and fresh unambiguous opposing-signal exit. Initial stops,15:00 entry cutoff,15:10 square-off, fresh held-contract bids, per-order costs and the uneconomic-partial guard remain. Underlying breakeven is not guaranteed net-option breakeven. No stop update is retroactively applied to its creating candle.

Dynamic selection is enabled only in its explicitly named variant: closest eligible absolute delta to0.50 among two ITM through six OTM, with the existing1–3whole-lot monetary sizing. All accounts retain INR5000 premium/daily limits and stop-risk min(INR1000,2%cash). No pyramid or conviction-based4lot upgrade is added.

| Policy | Closed | Wins / losses | Modeled net INR | Closed-curve drawdown INR | Hops |
| --- | ---: | ---: | ---: | ---: | ---: |
| Current zones + fixed 3R control | 19 | 9 / 10 | 14,679.73 | 1,929.28 | 0 |
| Current zones + selected 1R trail | 19 | 9 / 10 | 1,499.52 | 2,513.04 | 0 |
| Broader entries + fixed 3R | 24 | 7 / 17 | 5,933.28 | 6,145.41 | 0 |
| Current zones + earlier management | 19 | 8 / 11 | 10,751.97 | 2,396.08 | 6 |
| Broader entries + earlier management | 36 | 14 / 22 | 11,431.60 | 3,234.51 | 6 |
| Broader entries + dynamic selection + earlier management | 97 | 33 / 64 | 9,491.65 | 6,396.37 | 1 |

## Entry and exit coverage

- Current zones + fixed 3R control: families {'ZONE': 19}; timeframes {'1m': 19}; strikes {'OTM': 19}; lots {3: 9, 2: 2, 1: 8}; exit reasons {'STOP_TOUCH': 4, 'TARGET': 8, 'TARGET_TOUCH': 1, 'STOP': 6}.
- Current zones + selected 1R trail: families {'ZONE': 19}; timeframes {'1m': 19}; strikes {'OTM': 19}; lots {3: 9, 2: 2, 1: 8}; exit reasons {'STOP_TOUCH': 4, 'PEAK_PROFIT_GIVEBACK': 9, 'STOP': 6}.
- Broader entries + fixed 3R: families {'TRENDLINE_CHOCH': 6, 'ZONE': 12, 'RETEST_OR30': 2, 'RETEST_PD': 2, 'EQHL_SWEEP_INLINE': 2}; timeframes {'1m': 16, '5m': 8}; strikes {'OTM': 24}; lots {1: 11, 3: 12, 2: 1}; exit reasons {'STOP_TOUCH': 9, 'SQUARE_OFF': 3, 'TARGET_TOUCH': 1, 'STOP': 6, 'TARGET': 5}.
- Current zones + earlier management: families {'ZONE': 19}; timeframes {'1m': 19}; strikes {'OTM': 19}; lots {3: 9, 2: 2, 1: 8}; exit reasons {'STAGNATION_TIME_STOP': 1, 'TRAILING_STOP_HIT': 6, 'STOP_LOSS_HIT': 9, 'TARGET_HIT': 2, 'STRUCTURE_TRAIL_EXIT': 1}.
- Broader entries + earlier management: families {'TRENDLINE_CHOCH': 8, 'ZONE': 19, 'RETEST_OR30': 3, 'RETEST_PD': 2, 'EQHL_SWEEP_INLINE': 4}; timeframes {'1m': 24, '5m': 12}; strikes {'OTM': 36}; lots {1: 19, 3: 14, 2: 3}; exit reasons {'STOP_LOSS_HIT': 13, 'STAGNATION_TIME_STOP': 4, 'TRAILING_STOP_HIT': 10, 'TARGET_HIT': 3, 'REVERSAL_EXIT': 5, 'STRUCTURE_TRAIL_EXIT': 1}.
- Broader entries + dynamic selection + earlier management: families {'TRENDLINE_CHOCH': 24, 'ZONE': 48, 'RETEST_PD': 15, 'RETEST_OR30': 6, 'EQHL_SWEEP_INLINE': 4}; timeframes {'5m': 26, '1m': 68, '15m': 3}; strikes {'OTM': 82, 'ATM': 15}; lots {1: 92, 2: 4, 3: 1}; exit reasons {'STAGNATION_TIME_STOP': 11, 'STOP_LOSS_HIT': 35, 'TARGET_HIT': 12, 'STRUCTURE_TRAIL_EXIT': 2, 'REVERSAL_EXIT': 19, 'TRAILING_STOP_HIT': 17, 'INTRADAY_CLOSE': 1}.

## Daily modeled net results

| Date | Zone3R | Current trail | Broad3R | Zone+management | Broad+management | Broad+dynamic+management |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 2026-10-15 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 716.48 |
| 2026-10-16 | 0.00 | 0.00 | -596.42 | 0.00 | -596.42 | -265.36 |
| 2026-10-19 | -603.33 | -603.33 | -543.60 | -224.24 | -690.80 | -1,770.82 |
| 2026-10-21 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | -1,149.05 |
| 2026-10-22 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 745.52 |
| 2026-10-23 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | -2,266.27 |
| 2026-10-26 | 2,362.98 | 1,148.98 | -360.17 | 941.21 | 2,045.54 | 1,609.57 |
| 2026-10-27 | 3,696.86 | 1,973.83 | -1,265.65 | 5,219.01 | 4,069.31 | 3,517.02 |
| 2026-10-28 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | -404.65 |
| 2026-10-29 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 1,342.20 |
| 2026-10-30 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 788.13 |
| 2026-11-02 | -1,236.80 | -1,236.80 | -1,877.32 | -1,236.80 | -1,724.07 | -1,959.64 |
| 2026-11-03 | -173.87 | -757.63 | -983.64 | -640.67 | -991.83 | -575.30 |
| 2026-11-04 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | -920.94 |
| 2026-11-05 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | -1,117.86 |
| 2026-11-06 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | -433.54 |
| 2026-11-09 | 9,348.82 | 1,077.80 | 9,348.82 | 7,143.40 | 7,143.40 | 9,454.98 |
| 2026-11-11 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| 2026-11-12 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| 2026-11-13 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | -317.59 |
| 2026-11-16 | 1,285.07 | -103.33 | 2,211.26 | -449.94 | 2,176.47 | 2,498.77 |

## Validation and deployment decision

Both production controls reproduce the prior canonical trades/fills exactly. Independent checks recompute fee components, cash and quantities, entry-time monetary limits and every dynamic selection; regenerate fill books, native-entry evidence, causal hop levels and complete temporal milestone/exit paths. Input receipts match the earlier sample. Entry cohorts can change when broader signals or longer/shorter holding periods alter admission; this is a full-account comparison, not automatically an identical-trade exit ablation.

Broader entries with fixed3R blocked 7 of the original zone entries because another position was held. Broader earlier-management variants retained all original zone entries in this sample. This helps explain why simply adding setups can reduce results under the one-position rule; see zone-entry-displacement.json.

Source, protocol, receipts, checks, journals and targeted tests are preserved beside this report. The interrupted pre-format run is excluded. Previously inspected data cannot validate a retrospectively selected winner. No operational manifest, journal, risk limit, supervisor or Telegram delivery was changed; the hybrid is research-only. This adds testable earlier features, not a claim to have recreated the full historical Genesis or its reported rupee return.
