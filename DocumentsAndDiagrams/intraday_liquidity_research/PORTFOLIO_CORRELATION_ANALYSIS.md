# Portfolio and correlation

Data: Kaggle NIFTY 1-minute bars rolled to 5m, 2015-01-02 to 2025-12-24 (2295 sessions; the first session and NIFTY expiry days are skipped for every strategy). VWAP: session time-weighted typical price (index has no volume). In-sample to 2020, out-of-sample 2021 on. Costs: 2 index pts a round trip. Results are index points and R; option P&L is not reported because no historical option prices are available.

Liquidity events: signals on the same day and side whose sweep-to-entry windows overlap share one event id (app.intraday_liquidity.group_events). Portfolios take the first signal of each event and never hold two trades at once, so one price move is one trade.

## Event overlap: % of the row strategy's events that also have a signal from the column strategy

| Strategy | pdh_pdl | swing | or15 | or30 | equal | range | hour |
|---|---|---|---|---|---|---|---|
| pdh_pdl | 100 | 29 | 12 | 11 | 4 | 16 | 17 |
| swing | 13 | 100 | 11 | 15 | 10 | 33 | 40 |
| or15 | 16 | 32 | 100 | 63 | 3 | 14 | 25 |
| or30 | 15 | 50 | 67 | 100 | 6 | 21 | 38 |
| equal | 10 | 58 | 5 | 11 | 100 | 36 | 71 |
| range | 13 | 63 | 9 | 12 | 12 | 100 | 40 |
| hour | 11 | 61 | 13 | 17 | 18 | 31 | 100 |

## Daily R correlation

| Strategy | pdh_pdl | swing | or15 | or30 | equal | range | hour |
|---|---|---|---|---|---|---|---|
| pdh_pdl | 1 | 0.16 | 0.04 | -0.03 | 0.03 | 0.16 | 0.08 |
| swing | 0.16 | 1 | 0.16 | 0.24 | 0.22 | 0.46 | 0.45 |
| or15 | 0.04 | 0.16 | 1 | 0.64 | 0.07 | 0.16 | 0.2 |
| or30 | -0.03 | 0.24 | 0.64 | 1 | 0.07 | 0.18 | 0.28 |
| equal | 0.03 | 0.22 | 0.07 | 0.07 | 1 | 0.2 | 0.35 |
| range | 0.16 | 0.46 | 0.16 | 0.18 | 0.2 | 1 | 0.32 |
| hour | 0.08 | 0.45 | 0.2 | 0.28 | 0.35 | 0.32 | 1 |

## Unique vs shared events

| Strategy | Unique trades | Unique avg R | Shared trades | Shared avg R |
|---|---|---|---|---|
| PDH/PDL sweep (existing) | 311 | 0.043 | 250 | 0.088 |
| Intraday swing sweep | 405 | -0.028 | 905 | 0.06 |
| Opening range 15m sweep | 124 | -0.216 | 306 | 0.027 |
| Opening range 30m sweep | 42 | -0.298 | 358 | 0.007 |
| Equal high/low sweep | 22 | 0.276 | 202 | 0.15 |
| Consolidation sweep | 142 | -0.085 | 542 | 0.059 |
| Previous hour sweep | 167 | -0.015 | 696 | 0.045 |

## Portfolios

| Portfolio | Trades | Per year | Win % | Avg R | Avg R 95% CI | PF | Total R | Net pts | Max DD R | Sharpe | Sortino | MAE R | MFE R | Hold min |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Combined: all, first per event | 1772 | 193 | 38.6 | 0.007 | -0.04 to 0.06 | 1.016 | 11.7 | 2448 | 26.642 | 0.088 | 0.135 | 0.68 | 0.87 | 140 |
| Combined: swing, equal, range, hour | 1451 | 158.1 | 39.5 | 0.02 | -0.03 to 0.08 | 1.047 | 29 | 2937 | 24.133 | 0.241 | 0.374 | 0.69 | 0.89 | 118 |
