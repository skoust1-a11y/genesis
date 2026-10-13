# Opening range liquidity

Data: Kaggle NIFTY 1-minute bars rolled to 5m, 2015-01-02 to 2025-12-24 (2295 sessions; the first session and NIFTY expiry days are skipped for every strategy). VWAP: session time-weighted typical price (index has no volume). In-sample to 2020, out-of-sample 2021 on. Costs: 2 index pts a round trip. Results are index points and R; option P&L is not reported because no historical option prices are available.

## Opening range behaviour (all sessions)

| Range | Sessions | Broken % | False break % of breaks | Continued 1 range % of breaks |
|---|---|---|---|---|
| or15 | 2295 | 99.5 | 74.3 | 47.6 |
| or30 | 2295 | 98.5 | 75.5 | 38.3 |

False break = a close back inside the range within 30 min of the first break. Continued = price reached one more range width past the break the same day.

## Opening range 15m sweep: FAIL

Baseline: 2R target, minimum risk 30 pts, 2 pts cost a trade, 2015-01-02 to 2025-12-24.

| Sample | Trades | Per year | Win % | Avg R | Avg R 95% CI | PF | Total R | Net pts | Max DD R | Sharpe | Sortino | MAE R | MFE R | Hold min |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| All | 430 | 46.8 | 37.4 | -0.043 | -0.14 to 0.05 | 0.904 | -18.6 | -283 | 30.859 | -0.299 | -0.457 | 0.72 | 0.85 | 183 |
| In-sample (to 2020) | 172 | 32.4 | 36 | -0.085 | -0.24 to 0.09 | 0.828 | -14.6 | 158 | 26.065 | -0.465 | -0.695 | 0.78 | 0.84 | 174 |
| Out-of-sample (2021+) | 258 | 66.6 | 38.4 | -0.015 | -0.13 to 0.11 | 0.964 | -4 | -440 | 21.591 | -0.132 | -0.205 | 0.68 | 0.86 | 189 |
| Walk-forward 2018+ | 375 | 58.8 | 36.3 | -0.021 | -0.13 to 0.09 | 0.952 | -7.8 | 508 | 21.968 | -0.154 | -0.252 | 0.71 | 0.92 | 189 |

Walk-forward targets by year: 2018: 3.0, 2019: 1.5, 2020: 3.0, 2021: 3.0, 2022: 3.0, 2023: 3.0, 2024: 3.0, 2025: 2.0

### Long vs short

| Side | Trades | Per year | Win % | Avg R | Avg R 95% CI | PF | Total R | Net pts | Max DD R | Sharpe | Sortino | MAE R | MFE R | Hold min |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Long | 234 | 25.5 | 42.3 | -0.036 | -0.16 to 0.1 | 0.921 | -8.4 | 421 | 22.18 | -0.177 | -0.269 | 0.74 | 0.81 | 189 |
| Short | 196 | 21.4 | 31.6 | -0.052 | -0.2 to 0.09 | 0.883 | -10.2 | -703 | 16.299 | -0.233 | -0.369 | 0.69 | 0.91 | 176 |

### Targets (one at a time, all else baseline)

| Target | Trades | Per year | Win % | Avg R | Avg R 95% CI | PF | Total R | Net pts | Max DD R | Sharpe | Sortino | MAE R | MFE R | Hold min |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1.0R | 430 | 46.8 | 47.7 | -0.061 | -0.15 to 0.02 | 0.864 | -26.2 | -297 | 37.837 | -0.489 | -0.653 | 0.71 | 0.68 | 156 |
| 1.5R | 430 | 46.8 | 39.5 | -0.059 | -0.15 to 0.03 | 0.868 | -25.5 | -359 | 36.13 | -0.438 | -0.628 | 0.72 | 0.79 | 175 |
| 2.0R | 430 | 46.8 | 37.4 | -0.043 | -0.14 to 0.05 | 0.904 | -18.6 | -283 | 30.859 | -0.299 | -0.457 | 0.72 | 0.85 | 183 |
| 2.5R | 430 | 46.8 | 35.8 | -0.043 | -0.14 to 0.06 | 0.906 | -18.3 | -123 | 33.243 | -0.282 | -0.449 | 0.72 | 0.89 | 188 |
| 3.0R | 430 | 46.8 | 35.6 | -0.031 | -0.14 to 0.08 | 0.932 | -13.2 | 248 | 28.234 | -0.195 | -0.323 | 0.72 | 0.91 | 190 |
| opposing liquidity (2R if none) | 430 | 46.8 | 38.6 | -0.065 | -0.16 to 0.03 | 0.855 | -28 | -639 | 34.357 | -0.469 | -0.692 | 0.72 | 0.83 | 180 |

### Confirmation and minimum risk (one at a time, all else baseline)

| Variant | Trades | Per year | Win % | Avg R | Avg R 95% CI | PF | Total R | Net pts | Max DD R | Sharpe | Sortino | MAE R | MFE R | Hold min |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| base | 430 | 46.8 | 37.4 | -0.043 | -0.14 to 0.05 | 0.904 | -18.6 | -283 | 30.859 | -0.299 | -0.457 | 0.72 | 0.85 | 183 |
| min risk 0 | 620 | 67.5 | 36.8 | -0.039 | -0.13 to 0.05 | 0.918 | -24.4 | -495 | 38.088 | -0.31 | -0.48 | 0.74 | 0.91 | 170 |
| min risk 10 | 620 | 67.5 | 36.8 | -0.039 | -0.13 to 0.05 | 0.918 | -24.4 | -495 | 38.088 | -0.31 | -0.48 | 0.74 | 0.91 | 170 |
| min risk 20 | 568 | 61.9 | 36.8 | -0.051 | -0.14 to 0.04 | 0.893 | -29 | -535 | 44.898 | -0.39 | -0.597 | 0.74 | 0.88 | 173 |
| no structure shift | 775 | 84.4 | 32.3 | -0.007 | -0.09 to 0.07 | 0.985 | -5.7 | 122 | 35.605 | -0.062 | -0.1 | 0.73 | 1.01 | 136 |
| 15m structure shift | 16 | 1.7 | 25 | -0.291 | -0.66 to 0.16 | 0.397 | -4.7 | -334 | 6.634 | -0.453 | -0.574 | 0.7 | 0.59 | 131 |

### Monte Carlo (1,000 resamples of the baseline trades)

| Total R p5 | Total R p50 | Total R p95 | Max DD R p50 | Max DD R p95 | P(total R <= 0) % |
|---|---|---|---|---|---|
| -54.2 | -19 | 17.1 | 33.6 | 61.4 | 78.7 |

## Opening range 30m sweep: FAIL

Baseline: 2R target, minimum risk 30 pts, 2 pts cost a trade, 2015-01-02 to 2025-12-24.

| Sample | Trades | Per year | Win % | Avg R | Avg R 95% CI | PF | Total R | Net pts | Max DD R | Sharpe | Sortino | MAE R | MFE R | Hold min |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| All | 400 | 43.6 | 39 | -0.025 | -0.12 to 0.07 | 0.944 | -10.1 | -1125 | 26.389 | -0.164 | -0.258 | 0.71 | 0.87 | 165 |
| In-sample (to 2020) | 165 | 31.1 | 39.4 | 0.004 | -0.17 to 0.17 | 1.008 | 0.6 | 551 | 12.065 | 0.019 | 0.031 | 0.74 | 0.89 | 160 |
| Out-of-sample (2021+) | 235 | 60.7 | 38.7 | -0.045 | -0.18 to 0.09 | 0.899 | -10.7 | -1676 | 26.389 | -0.36 | -0.552 | 0.7 | 0.86 | 169 |
| Walk-forward 2018+ | 352 | 55.2 | 38.4 | -0.018 | -0.13 to 0.09 | 0.959 | -6.5 | -844 | 25.592 | -0.129 | -0.211 | 0.71 | 0.92 | 170 |

Walk-forward targets by year: 2018: 3.0, 2019: 1.5, 2020: 3.0, 2021: 3.0, 2022: 3.0, 2023: 3.0, 2024: 2.0, 2025: 2.0

### Long vs short

| Side | Trades | Per year | Win % | Avg R | Avg R 95% CI | PF | Total R | Net pts | Max DD R | Sharpe | Sortino | MAE R | MFE R | Hold min |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Long | 207 | 22.5 | 44 | -0.005 | -0.14 to 0.14 | 0.988 | -1.1 | 112 | 13.434 | -0.025 | -0.039 | 0.74 | 0.82 | 174 |
| Short | 193 | 21 | 33.7 | -0.046 | -0.18 to 0.1 | 0.9 | -8.9 | -1237 | 22.722 | -0.2 | -0.319 | 0.69 | 0.93 | 156 |

### Targets (one at a time, all else baseline)

| Target | Trades | Per year | Win % | Avg R | Avg R 95% CI | PF | Total R | Net pts | Max DD R | Sharpe | Sortino | MAE R | MFE R | Hold min |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1.0R | 400 | 43.6 | 48.5 | -0.06 | -0.15 to 0.02 | 0.865 | -24.2 | -1504 | 35.582 | -0.468 | -0.629 | 0.71 | 0.68 | 139 |
| 1.5R | 400 | 43.6 | 41.2 | -0.036 | -0.13 to 0.06 | 0.92 | -14.5 | -1300 | 28.116 | -0.251 | -0.37 | 0.71 | 0.8 | 156 |
| 2.0R | 400 | 43.6 | 39 | -0.025 | -0.12 to 0.07 | 0.944 | -10.1 | -1125 | 26.389 | -0.164 | -0.258 | 0.71 | 0.87 | 165 |
| 2.5R | 400 | 43.6 | 37.2 | -0.033 | -0.14 to 0.06 | 0.926 | -13.3 | -1284 | 26.496 | -0.211 | -0.34 | 0.72 | 0.9 | 170 |
| 3.0R | 400 | 43.6 | 36.8 | -0.027 | -0.13 to 0.08 | 0.941 | -10.6 | -1083 | 28.517 | -0.162 | -0.271 | 0.72 | 0.93 | 173 |
| opposing liquidity (2R if none) | 400 | 43.6 | 39.8 | -0.061 | -0.15 to 0.03 | 0.866 | -24.3 | -1845 | 31.235 | -0.419 | -0.623 | 0.71 | 0.81 | 161 |

### Confirmation and minimum risk (one at a time, all else baseline)

| Variant | Trades | Per year | Win % | Avg R | Avg R 95% CI | PF | Total R | Net pts | Max DD R | Sharpe | Sortino | MAE R | MFE R | Hold min |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| base | 400 | 43.6 | 39 | -0.025 | -0.12 to 0.07 | 0.944 | -10.1 | -1125 | 26.389 | -0.164 | -0.258 | 0.71 | 0.87 | 165 |
| min risk 0 | 599 | 65.3 | 37.4 | -0.029 | -0.11 to 0.06 | 0.94 | -17.4 | -1350 | 32.871 | -0.22 | -0.346 | 0.74 | 0.93 | 155 |
| min risk 10 | 599 | 65.3 | 37.4 | -0.029 | -0.11 to 0.06 | 0.94 | -17.4 | -1350 | 32.871 | -0.22 | -0.346 | 0.74 | 0.93 | 155 |
| min risk 20 | 534 | 58.2 | 37.5 | -0.031 | -0.12 to 0.06 | 0.935 | -16.4 | -1300 | 33.874 | -0.223 | -0.351 | 0.73 | 0.91 | 158 |
| no structure shift | 613 | 66.8 | 33.9 | -0.045 | -0.13 to 0.04 | 0.908 | -27.7 | -2019 | 41.775 | -0.34 | -0.537 | 0.75 | 0.94 | 141 |
| 15m structure shift | 18 | 2 | 33.3 | -0.239 | -0.57 to 0.14 | 0.443 | -4.3 | -378 | 6.64 | -0.414 | -0.53 | 0.65 | 0.67 | 127 |

### Monte Carlo (1,000 resamples of the baseline trades)

| Total R p5 | Total R p50 | Total R p95 | Max DD R p50 | Max DD R p95 | P(total R <= 0) % |
|---|---|---|---|---|---|
| -46.2 | -10.6 | 22.2 | 28.5 | 54.9 | 70.1 |
