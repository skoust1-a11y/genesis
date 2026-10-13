# Equal highs and lows

Data: Kaggle NIFTY 1-minute bars rolled to 5m, 2015-01-02 to 2025-12-24 (2295 sessions; the first session and NIFTY expiry days are skipped for every strategy). VWAP: session time-weighted typical price (index has no volume). In-sample to 2020, out-of-sample 2021 on. Costs: 2 index pts a round trip. Results are index points and R; option P&L is not reported because no historical option prices are available.

## Equal high/low sweep: PASS

Baseline: 2R target, minimum risk 30 pts, 2 pts cost a trade, 2015-01-02 to 2025-12-24.

| Sample | Trades | Per year | Win % | Avg R | Avg R 95% CI | PF | Total R | Net pts | Max DD R | Sharpe | Sortino | MAE R | MFE R | Hold min |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| All | 224 | 24.4 | 50.9 | 0.162 | 0.03 to 0.31 | 1.471 | 36.4 | 2338 | 10.376 | 0.794 | 1.455 | 0.65 | 0.93 | 93 |
| In-sample (to 2020) | 86 | 16.2 | 50 | 0.229 | -0.01 to 0.46 | 1.666 | 19.7 | 1153 | 5.116 | 0.846 | 1.665 | 0.66 | 1 | 93 |
| Out-of-sample (2021+) | 138 | 35.6 | 51.4 | 0.121 | -0.04 to 0.29 | 1.35 | 16.6 | 1185 | 10.376 | 0.753 | 1.31 | 0.64 | 0.89 | 93 |
| Walk-forward 2018+ | 199 | 31.2 | 51.8 | 0.169 | 0.04 to 0.32 | 1.5 | 33.7 | 2139 | 10.376 | 0.936 | 1.724 | 0.64 | 0.96 | 91 |

Walk-forward targets by year: 2018: 3.0, 2019: 3.0, 2020: 1.5, 2021: 3.0, 2022: 3.0, 2023: 2.5, 2024: 1.5, 2025: 2.5

### Long vs short

| Side | Trades | Per year | Win % | Avg R | Avg R 95% CI | PF | Total R | Net pts | Max DD R | Sharpe | Sortino | MAE R | MFE R | Hold min |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Long | 95 | 10.3 | 56.8 | 0.219 | 0.03 to 0.4 | 1.802 | 20.8 | 1114 | 4.245 | 0.751 | 1.423 | 0.62 | 0.88 | 94 |
| Short | 129 | 14.1 | 46.5 | 0.121 | -0.06 to 0.31 | 1.304 | 15.6 | 1224 | 14.398 | 0.412 | 0.728 | 0.67 | 0.97 | 92 |

### Targets (one at a time, all else baseline)

| Target | Trades | Per year | Win % | Avg R | Avg R 95% CI | PF | Total R | Net pts | Max DD R | Sharpe | Sortino | MAE R | MFE R | Hold min |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1.0R | 224 | 24.4 | 58 | 0.088 | -0.02 to 0.2 | 1.258 | 19.7 | 1343 | 10.801 | 0.53 | 0.788 | 0.64 | 0.72 | 77 |
| 1.5R | 224 | 24.4 | 54 | 0.159 | 0.04 to 0.29 | 1.463 | 35.5 | 2112 | 8.639 | 0.827 | 1.422 | 0.64 | 0.86 | 86 |
| 2.0R | 224 | 24.4 | 50.9 | 0.162 | 0.03 to 0.31 | 1.471 | 36.4 | 2338 | 10.376 | 0.794 | 1.455 | 0.65 | 0.93 | 93 |
| 2.5R | 224 | 24.4 | 50.4 | 0.181 | 0.04 to 0.33 | 1.524 | 40.4 | 2548 | 10.376 | 0.838 | 1.618 | 0.65 | 0.98 | 96 |
| 3.0R | 224 | 24.4 | 50 | 0.181 | 0.04 to 0.34 | 1.526 | 40.6 | 2545 | 10.376 | 0.824 | 1.625 | 0.65 | 1 | 97 |
| opposing liquidity (2R if none) | 224 | 24.4 | 52.2 | 0.136 | 0.01 to 0.27 | 1.397 | 30.6 | 1970 | 8.686 | 0.703 | 1.223 | 0.64 | 0.89 | 92 |

### Confirmation and minimum risk (one at a time, all else baseline)

| Variant | Trades | Per year | Win % | Avg R | Avg R 95% CI | PF | Total R | Net pts | Max DD R | Sharpe | Sortino | MAE R | MFE R | Hold min |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| base | 224 | 24.4 | 50.9 | 0.162 | 0.03 to 0.31 | 1.471 | 36.4 | 2338 | 10.376 | 0.794 | 1.455 | 0.65 | 0.93 | 93 |
| min risk 0 | 423 | 46.1 | 41.8 | 0.039 | -0.06 to 0.15 | 1.091 | 16.5 | 1856 | 20.72 | 0.252 | 0.411 | 0.7 | 0.96 | 86 |
| min risk 10 | 423 | 46.1 | 41.8 | 0.039 | -0.06 to 0.15 | 1.091 | 16.5 | 1856 | 20.72 | 0.252 | 0.411 | 0.7 | 0.96 | 86 |
| min risk 20 | 347 | 37.8 | 45.2 | 0.064 | -0.04 to 0.17 | 1.159 | 22.3 | 1991 | 16.264 | 0.392 | 0.658 | 0.7 | 0.92 | 90 |
| no structure shift | 199 | 21.7 | 41.7 | 0.025 | -0.11 to 0.18 | 1.058 | 5 | 606 | 22.4 | 0.113 | 0.18 | 0.74 | 0.87 | 86 |
| 15m structure shift | 91 | 9.9 | 51.6 | 0.061 | -0.11 to 0.24 | 1.192 | 5.5 | 466 | 4.676 | 0.226 | 0.359 | 0.54 | 0.79 | 91 |

### Monte Carlo (1,000 resamples of the baseline trades)

| Total R p5 | Total R p50 | Total R p95 | Max DD R p50 | Max DD R p95 | P(total R <= 0) % |
|---|---|---|---|---|---|
| 11.5 | 36.4 | 61.8 | 8.2 | 15.3 | 1.3 |

### By number of touches

| touches | Trades | Win % | Avg R | Total R |
|---|---|---|---|---|
| 2 | 218 | 50.9 | 0.171 | 37.2 |
| 3 | 6 | 50 | -0.135 | -0.8 |

### Compared with any swing

| Strategy | Trades | Per year | Win % | Avg R | Avg R 95% CI | PF | Total R | Net pts | Max DD R | Sharpe | Sortino | MAE R | MFE R | Hold min |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Intraday swing sweep | 1310 | 142.7 | 40.3 | 0.033 | -0.03 to 0.09 | 1.078 | 43.1 | 3469 | 21.141 | 0.391 | 0.614 | 0.68 | 0.89 | 113 |
| Equal high/low sweep | 224 | 24.4 | 50.9 | 0.162 | 0.03 to 0.31 | 1.471 | 36.4 | 2338 | 10.376 | 0.794 | 1.455 | 0.65 | 0.93 | 93 |
