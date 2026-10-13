# Intraday swing liquidity

Data: Kaggle NIFTY 1-minute bars rolled to 5m, 2015-01-02 to 2025-12-24 (2295 sessions; the first session and NIFTY expiry days are skipped for every strategy). VWAP: session time-weighted typical price (index has no volume). In-sample to 2020, out-of-sample 2021 on. Costs: 2 index pts a round trip. Results are index points and R; option P&L is not reported because no historical option prices are available.

## Intraday swing sweep: CONDITIONAL PASS

Baseline: 2R target, minimum risk 30 pts, 2 pts cost a trade, 2015-01-02 to 2025-12-24.

| Sample | Trades | Per year | Win % | Avg R | Avg R 95% CI | PF | Total R | Net pts | Max DD R | Sharpe | Sortino | MAE R | MFE R | Hold min |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| All | 1310 | 142.7 | 40.3 | 0.033 | -0.03 to 0.09 | 1.078 | 43.1 | 3469 | 21.141 | 0.391 | 0.614 | 0.68 | 0.89 | 113 |
| In-sample (to 2020) | 471 | 88.7 | 38.6 | 0.01 | -0.09 to 0.11 | 1.023 | 4.5 | 1056 | 20.598 | 0.092 | 0.141 | 0.66 | 0.86 | 107 |
| Out-of-sample (2021+) | 839 | 216.7 | 41.2 | 0.046 | -0.03 to 0.12 | 1.108 | 38.5 | 2413 | 21.141 | 0.667 | 1.06 | 0.7 | 0.9 | 116 |
| Walk-forward 2018+ | 1161 | 182 | 39.2 | 0.046 | -0.02 to 0.11 | 1.109 | 53.2 | 3891 | 21.141 | 0.573 | 0.959 | 0.69 | 0.97 | 120 |

Walk-forward targets by year: 2018: 3.0, 2019: 3.0, 2020: 3.0, 2021: 3.0, 2022: 3.0, 2023: 3.0, 2024: 2.0, 2025: 2.0

### Long vs short

| Side | Trades | Per year | Win % | Avg R | Avg R 95% CI | PF | Total R | Net pts | Max DD R | Sharpe | Sortino | MAE R | MFE R | Hold min |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Long | 669 | 72.9 | 42.5 | 0.034 | -0.04 to 0.11 | 1.082 | 22.9 | 2563 | 25.676 | 0.281 | 0.462 | 0.69 | 0.85 | 116 |
| Short | 641 | 69.8 | 38.1 | 0.032 | -0.05 to 0.12 | 1.073 | 20.2 | 906 | 17.002 | 0.244 | 0.413 | 0.67 | 0.93 | 111 |

### Targets (one at a time, all else baseline)

| Target | Trades | Per year | Win % | Avg R | Avg R 95% CI | PF | Total R | Net pts | Max DD R | Sharpe | Sortino | MAE R | MFE R | Hold min |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1.0R | 1310 | 142.7 | 49.5 | -0.024 | -0.07 to 0.02 | 0.942 | -31.8 | -30 | 33.076 | -0.352 | -0.467 | 0.68 | 0.69 | 95 |
| 1.5R | 1310 | 142.7 | 42.7 | 0.011 | -0.04 to 0.06 | 1.027 | 15.1 | 1706 | 23.843 | 0.148 | 0.216 | 0.68 | 0.82 | 107 |
| 2.0R | 1310 | 142.7 | 40.3 | 0.033 | -0.03 to 0.09 | 1.078 | 43.1 | 3469 | 21.141 | 0.391 | 0.614 | 0.68 | 0.89 | 113 |
| 2.5R | 1310 | 142.7 | 38.6 | 0.032 | -0.03 to 0.09 | 1.075 | 41.7 | 3551 | 27.554 | 0.36 | 0.59 | 0.69 | 0.93 | 118 |
| 3.0R | 1310 | 142.7 | 38.4 | 0.039 | -0.02 to 0.1 | 1.093 | 51.5 | 3936 | 25.832 | 0.429 | 0.729 | 0.69 | 0.96 | 120 |
| opposing liquidity (2R if none) | 1310 | 142.7 | 41.4 | 0.019 | -0.04 to 0.08 | 1.045 | 25.1 | 2561 | 28.616 | 0.228 | 0.36 | 0.68 | 0.87 | 112 |

### Confirmation and minimum risk (one at a time, all else baseline)

| Variant | Trades | Per year | Win % | Avg R | Avg R 95% CI | PF | Total R | Net pts | Max DD R | Sharpe | Sortino | MAE R | MFE R | Hold min |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| base | 1310 | 142.7 | 40.3 | 0.033 | -0.03 to 0.09 | 1.078 | 43.1 | 3469 | 21.141 | 0.391 | 0.614 | 0.68 | 0.89 | 113 |
| min risk 0 | 2318 | 252.5 | 36.5 | -0.007 | -0.05 to 0.04 | 0.985 | -16.6 | 2549 | 74.412 | -0.107 | -0.157 | 0.73 | 0.97 | 103 |
| min risk 10 | 2300 | 250.5 | 36.7 | -0.002 | -0.05 to 0.04 | 0.996 | -4.9 | 2597 | 62.045 | -0.032 | -0.047 | 0.73 | 0.96 | 103 |
| min risk 20 | 1864 | 203.1 | 38.4 | 0.018 | -0.03 to 0.07 | 1.04 | 33.8 | 3266 | 27.902 | 0.25 | 0.385 | 0.71 | 0.93 | 108 |
| no structure shift | 1180 | 128.5 | 37.6 | 0.016 | -0.05 to 0.08 | 1.035 | 19 | 1071 | 26.211 | 0.181 | 0.283 | 0.74 | 0.95 | 105 |
| 15m structure shift | 620 | 67.5 | 42.1 | 0.034 | -0.04 to 0.1 | 1.096 | 21.1 | 1332 | 20.502 | 0.301 | 0.49 | 0.6 | 0.8 | 94 |

### Monte Carlo (1,000 resamples of the baseline trades)

| Total R p5 | Total R p50 | Total R p95 | Max DD R p50 | Max DD R p95 | P(total R <= 0) % |
|---|---|---|---|---|---|
| -17.2 | 44.8 | 110.3 | 29.5 | 57.5 | 10.8 |

### Internal vs external

| external | Trades | Win % | Avg R | Total R |
|---|---|---|---|---|
| 0 | 929 | 41.2 | 0.057 | 53.1 |
| 1 | 381 | 38.1 | -0.026 | -10 |

### 5m vs 15m swing

| tf | Trades | Win % | Avg R | Total R |
|---|---|---|---|---|
| 15 | 242 | 39.3 | -0.074 | -17.8 |
| 5 | 1068 | 40.5 | 0.057 | 60.9 |
