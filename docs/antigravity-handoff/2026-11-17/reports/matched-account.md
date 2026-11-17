# Matched account backtest

Modeled options P&L; 1-minute closes, Black76 using prior-available VIX and synthetic 1% spread plus tick rounding. Flat assumed fees/news. No real historical books, IV surface, intraminute profit monitoring or execution liquidity. Fixed two-strike OTM selection in both lanes. One-position, max 3 lots, existing risk budgets and 20% net-profit giveback after 1R. All output journals isolated; no live changes or delivery.

| Lane | Closed | Net after fees | Fees | Max marked drawdown | Wins / losses |
|---|---:|---:|---:|---:|---:|
| current | 20 | 4078.34 | 1040.41 | 3273.56 | 10 / 10 |
| expanded | 79 | 14209.40 | 4075.10 | 5972.17 | 38 / 41 |

## Comparable period through 16 November

- current: 19 closed; modeled net 1499.52.
- expanded: 71 closed; modeled net 11160.59.

## Issues and limitations

- November 17 uses the last available VIX close from November 13; November 16 VIX is absent. Treat today as an exploratory extension, separately from the common period.
- Entry rules were motivated by November 17. This is an in-sample diagnostic, not independent validation.
- Model delta/IV can change which options fit the risk budget. Actual quote replay can differ materially.
- One-minute model marks miss one-second peaks; the trail trigger can overshoot its 20% threshold.
- Entry simulation uses ask plus one tick, exit bid minus one tick; stops fill at the next observed model quote, not an invented stop premium.
- One minute OHLC can show a stop touch without its timestamp; no favorable ordering is inferred.
- Equity drawdown uses one-minute liquidation marks and estimated exit fees. No option-margin leverage, partial lots, liquidity hopping or dynamic strike selection.
- The adapter control matched every deployed-control fill and net result. Earlier control through November 16 matched the prior replay exactly.
- Active paper strategy is unchanged.

## Rejected candidates

- current: [{'reason': 'ENTRY_CUTOFF', 'count': 1}, {'reason': 'FULL_PREMIUM_LIMIT', 'count': 42}, {'reason': 'POSITION_OR_EXIT_PENDING', 'count': 2}, {'reason': 'RISK_POINTS_OUTSIDE_10_55', 'count': 2}, {'reason': 'SPREAD_WIDE', 'count': 8}]
- expanded: [{'reason': 'DAILY_EXPOSURE_LIMIT', 'count': 98}, {'reason': 'FULL_PREMIUM_LIMIT', 'count': 747}, {'reason': 'POSITION_OR_EXIT_PENDING', 'count': 279}, {'reason': 'SPREAD_WIDE', 'count': 46}, {'reason': 'STOP_RISK_LIMIT', 'count': 5}]

## Independent reconciliation and practical interpretation

Ledger verification passed for all three isolated accounts: 238 fills; recomputed fees and P&L, nonnegative cash, whole lots, single position, entry risk/full-premium/daily exposure limits and marked drawdown. The entry stop-risk estimate is not a guaranteed loss cap; the largest expanded modeled loss was 1,066.54.

Common-period expanded improvement: 9661.07. Expanded return on starting 50,000 capital: 22.32118%; marked drawdown 5972.17. The earlier fixed-3R model remains higher at 14,679.73 through November 16; it uses a different exit policy.

### Today’s requested entries under account constraints

- 09:39 CALL rejected: earlier 09:34 PUT remained open until 09:41. Detecting a reversal does not authorize closing an opposite position under the unchanged exit policy.
- 10:48 CALL rejected: already holding the 09:55 CALL until 11:14. The strategy was already exposed to the upward move.
- 13:04 PUT rejected: already holding the 12:37 PUT until 13:13. The downward move was already being traded.
- 13:38 CALL rejected by the synthetic spread check. This is NOT proof that the real contract failed eligibility.

### Material model discrepancy

A nearby recorded 13:38:07 broker book for 25500 CE was bid 8.70 / ask 8.75 (spread approximately 0.573%), passing the quote gate. The model at 13:38:05 priced about 0.95 / 1.00 (spread approximately 5.128%), rejecting it. This demonstrates material model error in both premium and trade admission. The full-period modeled profit is a research comparison, not evidence of executable historical profit. Real quotes for the entire period are unavailable; no profitable hypothetical fills have been substituted into the ledger. Receipt and hash: review.json.

The backtest assumes uninterrupted operation. Today’s modeled current-policy PUT at 12:38 is counterfactual; the actual paper service missed that entry during Mac sleep. Do not compare this modeled daily P&L directly with the operational account.

No exit reversal rule, dynamic strike selection, liquidity hopping or risk-limit relaxation was introduced. Active paper policy and operational journals were unchanged.
