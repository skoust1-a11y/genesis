# Backtesting forensic audit

Audit date: 4 Nov 2026. The 10-year harness was not executed in this session. Numbers cited from October are labelled as such.

## Which backtest is which

| Engine | Clock | Instrument that is filled | Costs | What a profit number means |
|---|---|---|---|---|
| `engine/sim.py` | One snapshot per day | Option premium on that snapshot | `cost_breakdown` / `leg_costs`, including 1 tick minimum slippage | Close-to-close option P&L for the markdown rules. Not an intraday NIFTY book |
| `backtest_liquidity.py` | 5-minute spot bars | Spot path for the signal; Black-76 for the rupee column | `order_charges` on the model premium. Docstring: **no slippage** | Index R, plus a model option rupee that the October audit refused to treat as evidence |
| Scanner journal | 10-second loop | Spot points × 0.5 × quantity | None on this path | Not a backtest. Included because those rows are easy to mistake for one |

Never read an index-point profit as an option profit. The liquidity ledger even stores both columns (`Futures Rs` = points × quantity, `Option Rs` = model premium). They diverge whenever IV or time does something the spot move does not.

## Statistical integrity

**Look-ahead in `scan()`.** October: truncated-data check, 0 mismatches. Re-read here: pivots require future bars *within the slice passed in*, and the slice is the past. 15-minute bars are consulted on the 5-minute bar that completes them. This is acceptable for that function. The detector calls it on whatever frame it was given, including 1-minute retests. A truncation test on that 1-minute path was not run.

**Same-bar fill in `sim.py`.** Entry uses the same day's snapshot the signal just read (`build_entry_proposal` during that day's loop). For a rule that needs the closing price, the fill is the close that created the signal. There is no next-open. Intrabar stop path does not exist: a stop and a target on the same day cannot be ordered. Status: still true (October G-08). For daily credit spreads this is the usual optimistic bias.

**Survivorship.** The index does not drop constituents inside this backtest. Option strikes that did not trade are invented by Black-76. That is model bias, not survivorship of stocks. Holiday sessions in the raw Kaggle file were a real contamination (October: 44 signals, about +14R). Confirm the file you run has that filter before quoting a workbook.

**Selection and multiple testing.** October ran 55 variants and found none that survived correction. Since then the live path added walls, a VIX band, a delta floor, lunch overrides, a 55-point risk cap, dynamic targets, and extra timeframes. The sniper stop rewrite is in the manager and is not fed 1-minute bars by the daemon, so it is not part of the live trade. These filters were not in the pre-registered October window.

**In-sample / out-of-sample.** `sim.py` has a single split path used by the review crew (October G-12: one 70/30 cut, not walk-forward). `ParameterAgent.evaluate_recent_parameters` is not a walk-forward: if any trade has negative `realized_r` it recommends a 10-point buffer and `expected_point_gain=+25`; if two trades made at least 2R it recommends a 4R ceiling and `+45` points. The ADX row always says the threshold "filtered out 100% of sideways false breakouts" with gain 0. Nothing is recomputed on held-out days (V2-06).

**Regime dependence.** October tables (by year, by volatility, by time of day) showed the small positive average was not stable out of sample, and longs were few. This session did not regenerate them. The live VIX corridor [11.5, 22] drops both very quiet and very violent days, so a future sample will not match an unfiltered backtest even if the signal matched.

## Execution realism — liquidity ledger

Present: brokerage and statutory charges via `order_charges` on the **model** premium, dated lot size, expiry calendar (monthly Thursday, then weekly Thursday, then weekly Tuesday), capital that cannot buy a lot pays for nothing, a loss-streak pause.

Absent on that path, by the script's own docstring and by reading `ledger()`:

- Slippage and the bid-ask spread
- Partial fills, rejections, freezes (`freeze_quantity` is parsed from Groww and not applied here)
- Market impact
- IV change between entry and exit (`option_exit` uses `plan["iv_pct"]`)
- Early exercise or expiry pin (expiry day is skipped entirely, so pin risk is never taken and never lost)
- A stop filled on the option while the spot stop is only touched by a wick that the option overshoots

`engine/costs.py` is stricter (tick floor, side-aware STT and stamp). The liquidity script does not call it. Using the research cost engine's quality as a claim about the liquidity workbook is wrong.

## Options-specific accuracy

`estimated_plan` and `option_exit`:

- Price = Black-76, undiscounted (the helper notes that discounting is under 1% for a short expiry)
- Forward = spot (`forward_basis` 0 in the estimate)
- Volatility = previous day's India VIX, constant
- Strike = `otm_steps` (live config says 2) times the strike step from spot at the signal
- Time = calendar time to the weekly expiry, minus a small hold so the stop and target are not priced at the entry instant

`option_pnl.plan_pnl` can imply IV from a live quote and a put-call forward. That is better, and it is a **now** quote. It cannot price 2016. The function's own comment: constant IV flatters calls in a rally and understates puts in a sell-off.

A spot target hit is not a premium target hit. Theta over a few hours on a 2-OTM weekly can eat a small spot win. The journal's 0.5 delta ignores theta completely.

## What is trustworthy enough to keep

- The sign and the rough size of the **index-point** expectancy of `scan()` as reported in October, with the CI, the holiday-session fix, and the statement that it is not significant.
- The cost *formula* in `engine/costs.py` as a formula. Rates still need a 2026 check (V2-18).

## What is not evidence

- Any workbook cell labelled option rupees under constant VIX
- Catalog fields `win_rate`, `net_pnl_inr`, `profit_factor` in `strategies/analyzer.py`
- `reports/daily_audit_*.md` rupee lines
- `ExecutionManager.total_slippage_points_saved`
- Safety-wall reserve

## Minimum bar before another optimization

Freeze one signal function. Run it only on completed bars. Report index R and, separately, option P&L from stored premiums or not at all. Include spread and charges. Hold out a period that was not used to pick gates. Do not add a parameter because two recent winners were large.
