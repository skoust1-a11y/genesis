# Previous hour liquidity

Data: Kaggle NIFTY 1-minute bars rolled to 5m, 2015-01-02 to 2025-12-24 (2295 sessions; the first session and NIFTY expiry days are skipped for every strategy). VWAP: session time-weighted typical price (index has no volume). In-sample to 2020, out-of-sample 2021 on. Costs: 2 index pts a round trip. Results are index points and R; option P&L is not reported because no historical option prices are available.

## Previous hour sweep: CONDITIONAL PASS

Baseline: 2R target, minimum risk 30 pts, 2 pts cost a trade, 2015-01-02 to 2025-12-24.

| Sample | Trades | Per year | Win % | Avg R | Avg R 95% CI | PF | Total R | Net pts | Max DD R | Sharpe | Sortino | MAE R | MFE R | Hold min |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| All | 863 | 94 | 41.6 | 0.033 | -0.04 to 0.1 | 1.084 | 28.8 | 1821 | 15.376 | 0.344 | 0.556 | 0.66 | 0.86 | 109 |
| In-sample (to 2020) | 348 | 65.6 | 40.5 | 0.019 | -0.08 to 0.13 | 1.048 | 6.6 | 932 | 10.379 | 0.169 | 0.268 | 0.65 | 0.84 | 104 |
| Out-of-sample (2021+) | 515 | 133 | 42.3 | 0.043 | -0.05 to 0.14 | 1.108 | 22.2 | 889 | 15.376 | 0.518 | 0.847 | 0.67 | 0.87 | 111 |
| Walk-forward 2018+ | 747 | 117.1 | 40.3 | 0.057 | -0.02 to 0.14 | 1.143 | 42.3 | 2461 | 16.399 | 0.602 | 1.044 | 0.67 | 0.92 | 113 |

Walk-forward targets by year: 2018: 3.0, 2019: 3.0, 2020: 3.0, 2021: 3.0, 2022: 3.0, 2023: 3.0, 2024: 2.0, 2025: 2.0

### Long vs short

| Side | Trades | Per year | Win % | Avg R | Avg R 95% CI | PF | Total R | Net pts | Max DD R | Sharpe | Sortino | MAE R | MFE R | Hold min |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Long | 432 | 47.1 | 44.7 | 0.062 | -0.03 to 0.16 | 1.162 | 26.7 | 1256 | 15.292 | 0.425 | 0.709 | 0.67 | 0.83 | 114 |
| Short | 431 | 46.9 | 38.5 | 0.005 | -0.1 to 0.11 | 1.012 | 2.1 | 566 | 34.43 | 0.033 | 0.055 | 0.66 | 0.89 | 103 |

### Targets (one at a time, all else baseline)

| Target | Trades | Per year | Win % | Avg R | Avg R 95% CI | PF | Total R | Net pts | Max DD R | Sharpe | Sortino | MAE R | MFE R | Hold min |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1.0R | 863 | 94 | 49.8 | -0.014 | -0.07 to 0.04 | 0.965 | -11.9 | -73 | 24.731 | -0.172 | -0.236 | 0.66 | 0.68 | 92 |
| 1.5R | 863 | 94 | 43.1 | 0.011 | -0.05 to 0.08 | 1.027 | 9.2 | 816 | 17.156 | 0.118 | 0.178 | 0.66 | 0.79 | 103 |
| 2.0R | 863 | 94 | 41.6 | 0.033 | -0.04 to 0.1 | 1.084 | 28.8 | 1821 | 15.376 | 0.344 | 0.556 | 0.66 | 0.86 | 109 |
| 2.5R | 863 | 94 | 40.7 | 0.041 | -0.03 to 0.11 | 1.103 | 35.3 | 2185 | 16.899 | 0.403 | 0.68 | 0.66 | 0.89 | 112 |
| 3.0R | 863 | 94 | 40.2 | 0.043 | -0.03 to 0.12 | 1.108 | 37.2 | 2144 | 16.399 | 0.413 | 0.714 | 0.66 | 0.91 | 114 |
| opposing liquidity (2R if none) | 863 | 94 | 42.6 | 0.021 | -0.05 to 0.09 | 1.054 | 18.3 | 1265 | 15.755 | 0.223 | 0.354 | 0.66 | 0.82 | 106 |

### Confirmation and minimum risk (one at a time, all else baseline)

| Variant | Trades | Per year | Win % | Avg R | Avg R 95% CI | PF | Total R | Net pts | Max DD R | Sharpe | Sortino | MAE R | MFE R | Hold min |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| base | 863 | 94 | 41.6 | 0.033 | -0.04 to 0.1 | 1.084 | 28.8 | 1821 | 15.376 | 0.344 | 0.556 | 0.66 | 0.86 | 109 |
| min risk 0 | 1398 | 152.3 | 38.1 | -0.01 | -0.07 to 0.04 | 0.977 | -14.2 | 1297 | 55.374 | -0.127 | -0.193 | 0.7 | 0.9 | 102 |
| min risk 10 | 1394 | 151.9 | 38.2 | -0.008 | -0.06 to 0.05 | 0.982 | -11.2 | 1308 | 52.355 | -0.1 | -0.152 | 0.7 | 0.9 | 102 |
| min risk 20 | 1187 | 129.3 | 40.5 | 0.031 | -0.03 to 0.09 | 1.074 | 36.2 | 2125 | 17.836 | 0.36 | 0.574 | 0.68 | 0.89 | 105 |
| no structure shift | 845 | 92 | 36.8 | -0.031 | -0.1 to 0.04 | 0.931 | -26.4 | -917 | 37.132 | -0.31 | -0.478 | 0.73 | 0.88 | 107 |
| 15m structure shift | 308 | 33.6 | 45.5 | 0.034 | -0.06 to 0.13 | 1.107 | 10.5 | 219 | 9.288 | 0.234 | 0.372 | 0.56 | 0.72 | 89 |

### Monte Carlo (1,000 resamples of the baseline trades)

| Total R p5 | Total R p50 | Total R p95 | Max DD R p50 | Max DD R p95 | P(total R <= 0) % |
|---|---|---|---|---|---|
| -18.4 | 28.7 | 75.5 | 24.1 | 46.4 | 17.7 |

### Value beyond PDH/PDL and swing signals

| Hour signals | Trades | Per year | Win % | Avg R | Avg R 95% CI | PF | Total R | Net pts | Max DD R | Sharpe | Sortino | MAE R | MFE R | Hold min |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| only this strategy in the event | 167 | 18.2 | 41.3 | -0.015 | -0.16 to 0.13 | 0.963 | -2.4 | -352 | 9.719 | -0.066 | -0.104 | 0.65 | 0.81 | 100 |
| event shared with another strategy | 696 | 75.8 | 41.7 | 0.045 | -0.03 to 0.12 | 1.113 | 31.3 | 2173 | 14.385 | 0.401 | 0.657 | 0.67 | 0.87 | 111 |
