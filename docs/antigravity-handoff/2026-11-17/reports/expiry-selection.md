# Expiry day: strike and entry-time comparison

Four historical expiry sessions before today: 19 October, 27 October, 3 November and 9 November. Identical expanded candidate stream; independent 50,000 accounts; initial stop estimate capped at 1,000, whole lots 1-3, full-premium/daily-exposure gates, one position, 20% peak-net-profit giveback armed at 1R. Entries after 15:00 excluded; square-off 15:10. Time groups refer to entry time, even if exit is in a later group.

## Modeled expiry-only results through 16 November

| Expiry-day policy | Before 11:00 | 11:00-13:30 | After 13:30 | Expiry-only total |
|---|---:|---:|---:|---:|
| ITM | 3976.90 (9 trades) | -367.53 (14 trades) | No admitted trades | 3609.37 |
| ATM | 5204.97 (14 trades) | 5751.02 (33 trades) | -1043.27 (9 trades) | 9912.72 |
| OTM | 4995.24 (13 trades) | 5729.93 (23 trades) | -133.68 (4 trades) | 10591.49 |
| TIME_ROTATION | 3976.90 (9 trades) | 3317.01 (36 trades) | -239.24 (6 trades) | 7054.67 |

ITM means one strike in the money relative to nearest-50 ATM. OTM means two strikes out, as in the active selector. TIME_ROTATION tests ITM before 11:00, ATM 11:00-13:30, OTM thereafter; no fallback when selected contract fails risk. Non-expiry sessions all keep OTM. The time boundaries were specified for testing, not optimized. Trades differ because sizing/position/risk gates differ; do not combine the best cell from each account into an imaginary portfolio.

ATM was slightly ahead in the opening and midday buckets; OTM lost less in the late bucket. Late OTM was still negative and has only four trades. ITM was not best under these limits. Simple early-ITM rotation did not improve the OTM control. These findings are conditional on the option-price model.

## Recorded quotes: today’s late CALL examples

| Entry signal | One-strike ITM | ATM | Two-strike OTM |
|---|---|---|---|
| 13:38 | Rejected: one-lot stop estimate 1,415.30 | Rejected: one-lot stop estimate 1,029.92 | 2 lots; shadow net +760.96; exit 13:53:39 |
| 13:52 | Rejected: one-lot stop estimate 1,001.48 | 1 lot; shadow net +718.87; exit 14:08:27 | 3 lots; net -1,082.46; stop exit 14:52:12 |

The 13:52 OTM result reconciles to the actual closed paper trade. Other eligible paths are recorded-book shadows, not executed trades. Snapshots are roughly 11 seconds apart, not the full one-second monitoring path. Each signal starts with a separate flat 50,000 account and no previous daily loss. The 13:38 shadow overlaps the 13:52 entry, so these returns cannot be added as one-position portfolio profit. No historical quotes exist here for the earlier morning setups.

## Limitations and practical conclusion

- Four historical expiry sessions are too few to establish stable superiority or a time cutoff. Today is exploratory because November 16 VIX is unavailable; its model uses November 13 VIX. Full five-session tables are in accounts/report.md.
- Black76/prior-available VIX and synthetic spreads are materially imperfect: around 13:38 the model gave about 0.95/1.00 while a recorded quote was 8.70/8.75. Model profit and admission rankings are not proof of executable historical returns.
- No historical IV surface, real depth, latency, one-second profit peaks or historical news replay. Fees/news are assumed in the historical model.
- No risk limit was relaxed to admit ITM. Better per-unit response does not establish better account P&L or eligibility.
- Clock time alone does not select the best contract: today’s two nearby late entries produced different feasible/better choices. A future selector needs current quotes, spread, delta, stop distance and whole-lot risk checks as well as time-to-expiry.
- 30 focused tests passed. Independent fee/cash/risk/concurrency reconciliation covered 656 modeled fills; strike/time mapping and bucket arithmetic checks passed. Recorded eligible-path P&L reconciles independently.
- Research only. No active paper selector, exit rule, Telegram delivery or live-order setting was changed.
