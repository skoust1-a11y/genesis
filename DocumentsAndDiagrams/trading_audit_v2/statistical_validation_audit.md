# Statistical validation audit

Audit date: 4 Nov 2026.

## What has actually been tested

The only large-sample study in the tree is `DocumentsAndDiagrams/liquidity_strategy_audit/` (through 13 Oct 2026), on `liquidity_signals.scan` and its siblings inside the audit harness.

| Check | Result then | Applies to the daemon? |
|---|---|---|
| Sample size | 544 trades, ~51 a year | Not as a live forecast. Same function family, different arguments and a rewritten target (V2-08) |
| Expectancy | +0.032R, t ≈ 0.77, CI crosses 0 | No |
| Out of sample 2021–2026 | −0.028R | No |
| Multiple testing | 55 variants, none held after correction | The live gates are a 56th look, unregistered |
| Bootstrap P(10-year total < 0) | about 22% | About the old rule only |
| Profit concentration | Longs fewer than shorts; long profits partly from the time exit | Not re-cut |
| Expiry days | Skipping them did not help | Live blocks entries after 14:30 instead |
| Option vs spot | Model error large enough to change the sign of expectancy | Still true as a warning. Not re-fit |

This session did not recompute those statistics. Treat them as a filed result with a date, not as a live monitor.

## What the platform can calculate

`engine/metrics.performance` (research windows only):

| Metric | Present | Caveat |
|---|---|---|
| Gross profit, gross loss, profit factor | Yes | None if there is no loss, not infinity |
| Expectancy and a bootstrap CI of the mean | Yes | CI is on the trade list you pass, not a licence to trade |
| Average win, average loss | Yes | |
| Max consecutive losses | Yes | Consecutive wins are not returned |
| Sharpe, Sortino | Yes | Daily P&L change divided by starting capital, 252-day scaling. Sortino divides by `n`, Sharpe by `n-1`. Fine as a label, weak if most days are flat |
| Calmar, recovery factor, max drawdown | Yes | Drawdown from a P&L curve that starts at 0, peak tracked from 0 |
| Exposure | Yes | Days with a position over days in the curve |
| Win rate | No in this function | Trade store has it |
| Turnover, average R, time-of-day, regime | No | |

`trade_store.compute_metrics` (scanner rows): counts, win rate with a ±0.05R band called breakeven, sum of `est_pnl_inr`, profit factor. No drawdown, no Sharpe, no CI. `est_pnl_inr` is only as good as the writer. The journal writer uses delta 0.5 (V2-03).

## Research hygiene that is missing

| Need | State |
|---|---|
| Experiment log (code version, data file hash, parameter set, result) | Workbooks and markdown, not a run registry |
| Dataset version | Kaggle path is an argument. No hash stored next to the result |
| Strategy version tied to a result | `strategy_versions` exists for ingested markdown. The scanner's rules are the Python files plus `active_strategy_params.json`, which a post-market job rewrites |
| Pre-registration | Not present. October noted a 3R / 2-OTM change landed during that audit after results were seen |
| Deflated Sharpe / haircut for the number of trials | Discussed in the October write-up for the 55 variants. Not applied to later crew ideas |
| Regime report on the live book | Daily audit is three trades and a canned parameter table (3 Nov file) |

## Overfitting path that is live

1. A session produces winners or losers in the JSON log.
2. `ParameterAgent` emits a fixed recommendation if the counts match.
3. `save_calibrated_parameters` writes `micro_wick_stop_buffer` to 10 or 8.
4. Next morning `live_scanner` loads that buffer into `EntryAgent`.

That is parameter mining with the training set equal to "whatever just happened," and the objective function is a sentence. `dynamic_rr_ceiling` is stored and not read by the daemon. Changing it does nothing to targets. The file looks like control and is only half wired.

## How to read the 3 Nov audit without fooling yourself

`reports/daily_audit_2026-11-03.md` lists three trades, +156.50 points, about ₹15,946. At 3 lots × 65 = 195 quantity, 156.5 × 0.5 × 195 ≈ ₹15,259, close to the printed rupee once partial bookings are allowed. That identity is the delta-0.5 formula, not a broker statement. Three trades cannot support a win rate, a profit factor, or a lot-size increase. The same file recommends 4R and says the ADX gate removed 100% of false breakouts. Both lines are the canned text in `parameter_agent.py`.

## Decision rule this audit will accept later

A change to entries, stops, targets, or filters is discussable only after:

- the backtest engine for **that** function passes the forensic list in `backtesting_forensic_audit.md`
- the metric is index R with costs in points, or option P&L from real premiums
- the trial is counted against the trials already run
- the reported interval is out of sample

Until then, optimization is a way to manufacture a number.
