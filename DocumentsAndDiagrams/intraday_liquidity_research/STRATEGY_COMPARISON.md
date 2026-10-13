# Strategy comparison

Data: Kaggle NIFTY 1-minute bars rolled to 5m, 2015-01-02 to 2025-12-24 (2295 sessions; the first session and NIFTY expiry days are skipped for every strategy). VWAP: session time-weighted typical price (index has no volume). In-sample to 2020, out-of-sample 2021 on. Costs: 2 index pts a round trip. Results are index points and R; option P&L is not reported because no historical option prices are available.

| Strategy | Trades | Per year | Win % | PF | Avg R | Total R | Max DD R | Sharpe | Sortino | IS avg R | OOS avg R | WF avg R | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| PDH/PDL sweep (existing) | 561 | 61.1 | 41.5 | 1.184 | 0.063 | 35.2 | 18.772 | 0.486 | 0.894 | 0.148 | -0.01 | 0.063 | FAIL |
| Intraday swing sweep | 1310 | 142.7 | 40.3 | 1.078 | 0.033 | 43.1 | 21.141 | 0.391 | 0.614 | 0.01 | 0.046 | 0.046 | CONDITIONAL PASS |
| Opening range 15m sweep | 430 | 46.8 | 37.4 | 0.904 | -0.043 | -18.6 | 30.859 | -0.299 | -0.457 | -0.085 | -0.015 | -0.021 | FAIL |
| Opening range 30m sweep | 400 | 43.6 | 39 | 0.944 | -0.025 | -10.1 | 26.389 | -0.164 | -0.258 | 0.004 | -0.045 | -0.018 | FAIL |
| Equal high/low sweep | 224 | 24.4 | 50.9 | 1.471 | 0.162 | 36.4 | 10.376 | 0.794 | 1.455 | 0.229 | 0.121 | 0.169 | PASS |
| Consolidation sweep | 684 | 74.5 | 37.4 | 1.069 | 0.029 | 19.9 | 24.465 | 0.25 | 0.406 | 0.046 | 0.02 | 0.057 | CONDITIONAL PASS |
| Previous hour sweep | 863 | 94 | 41.6 | 1.084 | 0.033 | 28.8 | 15.376 | 0.344 | 0.556 | 0.019 | 0.043 | 0.057 | CONDITIONAL PASS |
| Combined: all, first per event | 1772 | 193 | 38.6 | 1.016 | 0.007 | 11.7 | 26.642 | 0.088 | 0.135 | 0.02 | -0.002 |  |  |
| Combined: swing, equal, range, hour | 1451 | 158.1 | 39.5 | 1.047 | 0.02 | 29 | 24.133 | 0.241 | 0.374 | 0.019 | 0.021 |  |  |

![Equity curves](charts/equity_curves.png)

![Drawdowns](charts/drawdowns.png)

![Strategy comparison](charts/strategy_comparison.png)

Verdict rules: PASS = positive in- and out-of-sample, OOS profit factor 1.2+, bootstrap 95% interval of R per trade above zero and positive walk-forward. CONDITIONAL PASS = positive out-of-sample and positive in-sample or walk-forward. FAIL otherwise. INSUFFICIENT DATA = under 30 trades or under 15 out-of-sample.

## Picks

| Question | Answer |
|---|---|
| Best individual setup (total R) | Intraday swing sweep |
| Most consistent (out-of-sample R) | Equal high/low sweep |
| Best risk-adjusted (Sharpe) | Equal high/low sweep |
| Lowest drawdown | Equal high/low sweep |
| Best long | Equal high/low sweep |
| Best short | Equal high/low sweep |
| Most redundant (event overlap) | Equal high/low sweep |

## Final verdict

1. Measurable edge: Intraday swing sweep (CONDITIONAL PASS), Equal high/low sweep (PASS), Consolidation sweep (CONDITIONAL PASS), Previous hour sweep (CONDITIONAL PASS).
2. Swing vs PDH/PDL: swing 0.046R vs PDH/PDL -0.01R a trade out-of-sample; Sharpe 0.3912 vs 0.4865 over the whole period.
3. Opening range: 15m FAIL, 30m FAIL; 15m only-this-strategy events -0.216R, 30m -0.298R a trade.
4. Equal highs/lows: 0.162R a trade vs 0.033R for any swing (PASS).
5. Previous hour: CONDITIONAL PASS; its only-this-strategy events -0.015R a trade.
6. Trending days (ex post): PDH/PDL sweep (existing) (+0.594R a trade).
7. Sideways days (ex post): Opening range 30m sweep (+0.066R a trade).
8. Independence: highest daily R correlation 0.64 (or30 / or15); most redundant: Equal high/low sweep (see the event overlap table).
9. Portfolio: 'Combined: swing, equal, range, hour': 1451 trades, 0.02R a trade, Sharpe 0.2414, max drawdown 24.133R; best single passing setup Equal high/low sweep: 0.162R a trade, Sharpe 0.7939, max drawdown 10.376R. Signals sharing their event with another strategy beat solo ones for 6 of 7 strategies (confluence of liquidity types matters more than any one type).
10. Reject: PDH/PDL sweep (existing), Opening range 15m sweep, Opening range 30m sweep.
