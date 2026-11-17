# Morning-window and 1-minute-entry comparison

All figures are synthetic-option modeled net after assumed fees, not executable historical profit. Identical underlying bars, quotes model,50000 capital, risk/lot/exit rules across six independently funded accounts. Stop ceiling2500 (5% cash, capped2500), ATM/ITM cap2, OTM4; premium/daily caps5000; one position;20% net profit giveback after1R;15:10 squareoff. Selection rotates by time on expiry only, retaining OTM on non-expiry days. No active changes.

## Historical results:15 October-16 November (21 sessions)

| Entry variant | Closed trades | Modeled net | Marked drawdown | Today17Nov net separately |
|---|---:|---:|---:|---:|
| Original expanded all-day | 77 | 12278.72 | 5489.42 | 7206.24 |
| Existing confirmation,09:30-11:00 | 29 | 8633.48 | 3749.64 | -1404.17 |
| Immediate reclaim +continuations,09:30-10:30 | 25 | 6910.70 | 5207.00 | -1404.17 |
| Immediate reclaim +continuations,09:30-11:00 | 32 | 8770.66 | 5207.00 | -1404.17 |
| Immediate reversals only,09:30-11:00 | 28 | -2850.56 | 4627.53 | -616.86 |
| Morning09:30-11:00 +afternoon12:00-15:00 | 74 | 13730.87 | 5772.38 | 1633.08 |

## What happened at today’s proposed opening entry

The9:37 candle swept a known local low and reclaimed it, closing25304.00. It becomes available at9:38. Initial spot stop25290.85; risk13.15points.

In the reversal-only account, the model admitted a one-lot ITM CALL at9:38:05. The unchanged20%-after1R profit trail exited at9:43:05 for+1031.17. It therefore does not represent holding the full9:37-to10:30 move. Later PUT reversals lost812.65 (9:53-9:55) and835.38 (10:10-10:14), producing today net-616.86.

In the broader morning accounts, a9:34 PUT was already open and blocked9:38 CALL, then closed9:41 for-1404.17. Subsequent entries can also be blocked by full-premium/daily-exposure gates; a signal is not a funded trade. A9:30 start gate alone does not prevent this opposite trade.

## Interpretation

Extending the direct-entry morning window from10:30 to11:00 improved historical modeled net from6910.70 to8770.66. However the existing slower-confirmation morning policy produced8633.48 with lower drawdown3749.64 versus5207.00. Faster entry has not established a clear risk-adjusted advantage. Pure immediate reversal entries lost2850.56 historically, despite capturing today’s desired9:38 entry.

Keeping the afternoon component increased historical modeled net to13730.87, versus8770.66 for direct-entry mornings only and12278.72 for original all-day control. This is a hypothesis for forward validation, not automatic promotion. These variants did not optimize time cutoffs or remove losing trades after inspecting outcomes.

The study does not prove that the best Indian-market trades generally occur in any particular hour. Portfolio paths differ after entry-window filtering; cannot add chosen winners across accounts. Entry window is not exit time; positions continue after10:30/11:00 if existing exit rules permit. No automatic opposite-signal liquidation, opening-range directional gate or VWAP filter was added.

## Validation and limitations

Five focused causal/timing tests passed; no pre9:30 admission in filtered lanes; stated cutoffs checked. Independent reconciliation passed564fills, fees, nonnegative cash, whole-lot quantities, lot/risk limits, daily exposure, one position and marked drawdown. Signal schedules and suppression receipts saved in accounts/signal-receipts.json.

Historical option books/IV surface/news are unavailable; model uses prior-available VIX and one-minute Black76 quotes. Today uses13Nov VIX because16Nov is missing, and recorded13:38 quotes previously differed from model by nearly9x. No verified morning rupee profit is recoverable without quotes. Minute marks miss the one-second profit monitor and stop ordering. Today motivated these rules, so it is in-sample. Research limits2500/4-lot are not active1000/3-lot settings. Protocol and source hashes saved separately.
