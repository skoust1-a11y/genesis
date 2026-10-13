# Winning setup search

2,550 rules with 60+ in-sample trades: 48 strategy/target pools x filters (side, open, VWAP, volatility, opening, time). Ranked by the t-stat of R per trade on 2015-2020 only; 2021-2025 untouched until the pick was fixed. Same data, exits and 2-pt cost as the research.

## Verdict: NO WINNER: the best in-sample rule lost out-of-sample

Pick (ranked 1st in-sample): PDH/PDL sweep (existing), target 2.5R, filters: 12:00 on.

| Sample | Trades | Per year | Win % | Avg R | Avg R 95% CI | PF | Total R | Net pts | Max DD R | Sharpe | Sortino | MAE R | MFE R | Hold min |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| All | 294 | 32 | 47.6 | 0.087 | -0.01 to 0.18 | 1.321 | 25.7 | 1946 | 14.742 | 0.576 | 1.041 | 0.49 | 0.74 | 104 |
| In-sample (to 2020) | 138 | 26 | 50 | 0.188 | 0.05 to 0.33 | 1.829 | 25.9 | 1221 | 6.663 | 1.099 | 2.299 | 0.43 | 0.77 | 102 |
| Out-of-sample (2021+) | 156 | 40.3 | 45.5 | -0.002 | -0.13 to 0.14 | 0.995 | -0.3 | 725 | 14.742 | -0.013 | -0.021 | 0.54 | 0.71 | 106 |

### The pick by year

| Year | Trades | Total R |
|---|---|---|
| 2015 | 22 | -4.5 |
| 2016 | 29 | 2.3 |
| 2017 | 13 | 2.1 |
| 2018 | 26 | 14 |
| 2019 | 29 | 12.3 |
| 2020 | 19 | -0.2 |
| 2021 | 29 | 9.8 |
| 2022 | 42 | -1.6 |
| 2023 | 22 | -4.8 |
| 2024 | 31 | 0.1 |
| 2025 | 32 | -3.7 |

### Monte Carlo (1,000 resamples)

| Total R p5 | Total R p50 | Total R p95 | Max DD R p50 | Max DD R p95 | P(total R <= 0) % |
|---|---|---|---|---|---|
| 2.3 | 25.3 | 50.2 | 9 | 16.4 | 3.6 |

## Does in-sample rank predict out-of-sample?

| Correlation of IS and OOS avg R across all rules | Top 20 in-sample positive OOS % | All rules positive OOS % |
|---|---|---|
| 0.183 | 40 | 70.5 |

If the top 20 are positive out-of-sample no more often than all rules, ranking found luck, not an edge.

## Searching every year: what 'find the winner, then trade it' would have earned

Each year from 2018 trades the rule with the best t-stat over the 3 years before it.

| Sample | Trades | Per year | Win % | Avg R | Avg R 95% CI | PF | Total R | Net pts | Max DD R | Sharpe | Sortino | MAE R | MFE R | Hold min |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Re-picked yearly, 2018+ | 290 | 45.5 | 42.8 | 0.063 | -0.05 to 0.18 | 1.182 | 18.2 | 585 | 15.745 | 0.444 | 0.768 | 0.61 | 0.87 | 130 |

| Year | Rule picked on the 3 years before | Trades | R that year |
|---|---|---|---|
| 2018 | PDH/PDL sweep (existing), 1.5R, high vol | 31 | 9.4 |
| 2019 | PDH/PDL sweep (existing), 1.5R, 12:00 on | 29 | 9.1 |
| 2020 | PDH/PDL sweep (existing), 1.5R, 12:00 on | 19 | -1.4 |
| 2021 | PDH/PDL sweep (existing), 1.0R, normal vol, 12:00 on | 16 | -0.1 |
| 2022 | Consolidation sweep, 3.0R, before 12:00 | 50 | 5.8 |
| 2023 | Previous hour sweep, 2.5R, with the day's open, high vol | 8 | -0.9 |
| 2024 | Confluence (2+ strategies, one event), 2.0R, 1+ ATR past VWAP, normal open | 82 | -3.1 |
| 2025 | Previous hour sweep, 2.0R, long | 55 | -0.5 |

## Top 20 in-sample rules and how they did after

| Strategy | Target | Filters | IS trades | IS avg R | IS t | OOS trades | OOS avg R | OOS total R |
|---|---|---|---|---|---|---|---|---|
| PDH/PDL sweep (existing) | 2.5R | 12:00 on | 138 | 0.188 | 2.57 | 156 | -0.002 | -0.3 |
| PDH/PDL sweep (existing) | 2.0R | 12:00 on | 138 | 0.178 | 2.52 | 156 | -0.004 | -0.6 |
| PDH/PDL sweep (existing) | 1.5R | normal open | 189 | 0.159 | 2.5 | 237 | -0.043 | -10.3 |
| PDH/PDL sweep (existing) | 3.0R | 12:00 on | 138 | 0.182 | 2.48 | 156 | 0.005 | 0.7 |
| Confluence (2+ strategies, one event) | 3.0R | strong open | 99 | 0.307 | 2.47 | 158 | 0.018 | 2.9 |
| PDH/PDL sweep (existing) | 3.0R | normal open | 189 | 0.182 | 2.46 | 237 | 0.012 | 2.9 |
| PDH/PDL sweep (existing) | 2.5R | normal open | 189 | 0.175 | 2.44 | 237 | -0.003 | -0.8 |
| Equal high/low sweep | 2.5R | with the day's open | 62 | 0.386 | 2.43 | 89 | 0.099 | 8.8 |
| PDH/PDL sweep (existing) | 3.0R | no filter | 258 | 0.148 | 2.43 | 303 | -0.01 | -2.9 |
| PDH/PDL sweep (existing) | 1.5R | 12:00 on | 138 | 0.159 | 2.42 | 156 | -0.004 | -0.6 |
| Equal high/low sweep | 3.0R | with the day's open | 62 | 0.397 | 2.42 | 89 | 0.097 | 8.6 |
| PDH/PDL sweep (existing) | 2.5R | no filter | 258 | 0.143 | 2.41 | 303 | -0.022 | -6.7 |
| Previous hour sweep | 2.5R | with the day's open, high vol | 87 | 0.295 | 2.4 | 126 | 0.051 | 6.4 |
| PDH/PDL sweep (existing) | 1.5R | no filter | 258 | 0.126 | 2.38 | 303 | -0.045 | -13.8 |
| PDH/PDL sweep (existing) | 2.0R | normal open | 189 | 0.162 | 2.37 | 237 | -0.039 | -9.3 |
| Previous hour sweep | 2.5R | with the day's open, high vol, normal open | 67 | 0.337 | 2.37 | 107 | 0.066 | 7 |
| PDH/PDL sweep (existing) | 1.0R | 12:00 on | 138 | 0.137 | 2.36 | 156 | -0.003 | -0.5 |
| Confluence (2+ strategies, one event) | 2.5R | strong open | 99 | 0.278 | 2.35 | 158 | -0.012 | -1.9 |
| PDH/PDL sweep (existing) | 3.0R | high vol | 109 | 0.253 | 2.31 | 125 | 0.018 | 2.3 |
| PDH/PDL sweep (existing) | 2.0R | no filter | 258 | 0.13 | 2.3 | 303 | -0.042 | -12.7 |
