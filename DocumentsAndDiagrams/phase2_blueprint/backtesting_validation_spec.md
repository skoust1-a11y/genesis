# Backtesting validation specification

Date: 4 Nov 2026. The October study was not executed again. This file says what may be claimed, and what a future run must show.

## Four layers, never collapsed

| Layer | Source today | May be called |
|---|---|---|
| 1. Index signal | `scan` / `scan_retest` / `scan_trendline_reversal` on spot bars, exits in points and R | "Index R for this call signature" |
| 2. Theoretical value | `black76` in `option_pnl.py` and `estimated_plan` | "Constant-IV model" |
| 3. Simulated execution | `backtest_liquidity.ledger` applies `order_charges` to the model premium. Docstring: no slippage. `engine/sim.py` fills the daily snapshot and uses `engine/costs.py`, including a one-tick slippage floor | "Model fill" or "daily close fill" |
| 4. Executable option result | Not in the repository | Nothing, until a premium print or a broker fill is stored |

Index profit is not option profit. A model premium is not a fill.

## October study, limits

Filed under `DocumentsAndDiagrams/liquidity_strategy_audit/`. It measured a `scan()` configuration on spot, reported about +0.032R with an interval through zero, and refused the option-rupee workbooks. **Not rerun on 4 Nov 2026.** Do not cite it as the daemon's expectancy (F-05).

## Defects still in the engines

**Daily simulator.** `engine/sim.py` enters on the same snapshot the signal just read. There is no next-bar fill and no path inside the day. `pnl_today` at entry is closed trades only, and a new entry is skipped when something already closed that day, so an open loss is invisible to the daily limit. Same reading as October G-05 and G-08. Not re-simulated with a fixture today.

**Liquidity ledger.** Charges on the model premium, dated lot size, expiry calendar, capital that cannot pay the premium takes zero lots. No spread, no partial, no IV change, expiry day skipped so pin risk is neither taken nor lost.

**Multiple testing.** October counted 55 variants. Live filters since then (walls, VIX band, 55-point cap, dynamic targets, 1-minute frames) are further looks. They are not a pre-registered holdout.

**Walk-forward.** `ParameterAgent` is not one (F-09). `engine` review uses a single split (October G-12, not retested).

## Required contents of a future result file

A result is publishable only if the file contains:

- git revision
- data path and a hash of the file used
- the function name and the full argument set
- VWAP definition
- index trades: R, win count, loss count, max drawdown in R
- a statement that option rupees are absent, or a column named `model_premium` that is not used to rank parameters
- the number of configurations tried before this one

## Truncation test

For the shared function, build signals on the full day and on the day cut after each bar. The signal at bar *i* must match. October reported this for the old `scan` sample. The 1-minute retest path has no such report. The parity spec makes it a required test before any live argument change is called "the same strategy".

## What this spec does not allow

Ranking stops or targets on model-premium rupees. Publishing `RESEARCH_CATALOG` literals as results. Using the safety-wall balance as a backtest equity curve.
