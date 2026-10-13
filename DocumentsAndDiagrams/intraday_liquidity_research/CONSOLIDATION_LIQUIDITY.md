# Consolidation liquidity

Data: Kaggle NIFTY 1-minute bars rolled to 5m, 2015-01-02 to 2025-12-24 (2295 sessions; the first session and NIFTY expiry days are skipped for every strategy). VWAP: session time-weighted typical price (index has no volume). In-sample to 2020, out-of-sample 2021 on. Costs: 2 index pts a round trip. Results are index points and R; option P&L is not reported because no historical option prices are available.

## Box behaviour

| Boxes | Broken (close outside) | Continued one box height % | Reversed to a close past the far side % |
|---|---|---|---|
| 5015 | 4880 | 63.8 | 31.5 |

## Consolidation sweep: CONDITIONAL PASS

Baseline: 2R target, minimum risk 30 pts, 2 pts cost a trade, 2015-01-02 to 2025-12-24.

| Sample | Trades | Per year | Win % | Avg R | Avg R 95% CI | PF | Total R | Net pts | Max DD R | Sharpe | Sortino | MAE R | MFE R | Hold min |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| All | 684 | 74.5 | 37.4 | 0.029 | -0.05 to 0.11 | 1.069 | 19.9 | 2387 | 24.465 | 0.25 | 0.406 | 0.69 | 0.91 | 117 |
| In-sample (to 2020) | 242 | 45.6 | 37.6 | 0.046 | -0.09 to 0.18 | 1.108 | 11.1 | 1517 | 13.845 | 0.317 | 0.525 | 0.69 | 0.92 | 113 |
| Out-of-sample (2021+) | 442 | 114.2 | 37.3 | 0.02 | -0.07 to 0.12 | 1.048 | 8.8 | 870 | 24.465 | 0.209 | 0.336 | 0.69 | 0.9 | 119 |
| Walk-forward 2018+ | 617 | 96.7 | 36.5 | 0.057 | -0.03 to 0.15 | 1.138 | 35.3 | 2990 | 27.737 | 0.514 | 0.907 | 0.69 | 1 | 123 |

Walk-forward targets by year: 2018: 3.0, 2019: 3.0, 2020: 3.0, 2021: 3.0, 2022: 3.0, 2023: 3.0, 2024: 2.0, 2025: 3.0

### Long vs short

| Side | Trades | Per year | Win % | Avg R | Avg R 95% CI | PF | Total R | Net pts | Max DD R | Sharpe | Sortino | MAE R | MFE R | Hold min |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Long | 345 | 37.6 | 38 | -0 | -0.11 to 0.11 | 1 | -0 | 347 | 20.983 | -0.001 | -0.001 | 0.71 | 0.84 | 118 |
| Short | 339 | 36.9 | 36.9 | 0.059 | -0.06 to 0.18 | 1.136 | 20 | 2040 | 13.81 | 0.32 | 0.553 | 0.67 | 0.98 | 115 |

### Targets (one at a time, all else baseline)

| Target | Trades | Per year | Win % | Avg R | Avg R 95% CI | PF | Total R | Net pts | Max DD R | Sharpe | Sortino | MAE R | MFE R | Hold min |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1.0R | 684 | 74.5 | 49.7 | -0.011 | -0.08 to 0.06 | 0.974 | -7.4 | 693 | 27.943 | -0.115 | -0.156 | 0.69 | 0.7 | 97 |
| 1.5R | 684 | 74.5 | 39.8 | 0.003 | -0.07 to 0.08 | 1.007 | 2 | 1302 | 25.927 | 0.028 | 0.042 | 0.69 | 0.83 | 109 |
| 2.0R | 684 | 74.5 | 37.4 | 0.029 | -0.05 to 0.11 | 1.069 | 19.9 | 2387 | 24.465 | 0.25 | 0.406 | 0.69 | 0.91 | 117 |
| 2.5R | 684 | 74.5 | 36.1 | 0.032 | -0.05 to 0.12 | 1.075 | 21.8 | 2403 | 25.157 | 0.262 | 0.443 | 0.69 | 0.95 | 121 |
| 3.0R | 684 | 74.5 | 35.8 | 0.049 | -0.04 to 0.14 | 1.116 | 33.6 | 3048 | 22.657 | 0.383 | 0.682 | 0.69 | 0.98 | 124 |
| opposing liquidity (2R if none) | 684 | 74.5 | 38.3 | 0.022 | -0.06 to 0.1 | 1.053 | 15.2 | 1825 | 25.709 | 0.187 | 0.313 | 0.69 | 0.9 | 116 |

### Confirmation and minimum risk (one at a time, all else baseline)

| Variant | Trades | Per year | Win % | Avg R | Avg R 95% CI | PF | Total R | Net pts | Max DD R | Sharpe | Sortino | MAE R | MFE R | Hold min |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| base | 684 | 74.5 | 37.4 | 0.029 | -0.05 to 0.11 | 1.069 | 19.9 | 2387 | 24.465 | 0.25 | 0.406 | 0.69 | 0.91 | 117 |
| min risk 0 | 1339 | 145.9 | 34.4 | 0.004 | -0.05 to 0.07 | 1.009 | 5.6 | 1863 | 37.346 | 0.047 | 0.071 | 0.72 | 1 | 106 |
| min risk 10 | 1328 | 144.7 | 34.4 | 0.002 | -0.06 to 0.07 | 1.004 | 2.7 | 1833 | 36.981 | 0.022 | 0.034 | 0.72 | 0.99 | 107 |
| min risk 20 | 1036 | 112.9 | 35.3 | -0.003 | -0.07 to 0.07 | 0.994 | -3.1 | 1736 | 30.971 | -0.03 | -0.047 | 0.72 | 0.94 | 112 |
| no structure shift | 562 | 61.2 | 35.1 | -0.02 | -0.11 to 0.07 | 0.956 | -11.2 | 137 | 31.685 | -0.153 | -0.239 | 0.75 | 0.92 | 105 |
| 15m structure shift | 294 | 32 | 42.5 | 0.056 | -0.06 to 0.16 | 1.152 | 16.4 | 1690 | 16.066 | 0.326 | 0.544 | 0.61 | 0.86 | 107 |

### Monte Carlo (1,000 resamples of the baseline trades)

| Total R p5 | Total R p50 | Total R p95 | Max DD R p50 | Max DD R p95 | P(total R <= 0) % |
|---|---|---|---|---|---|
| -27.8 | 18.3 | 65.2 | 24.1 | 48.2 | 24.1 |
