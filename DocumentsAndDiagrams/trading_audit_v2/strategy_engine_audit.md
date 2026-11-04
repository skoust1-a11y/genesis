# Strategy engine audit

Audit date: 4 Nov 2026. No rule was edited.

There is no single strategy. There are at least four generators. Only the first was put through a multi-year statistical study, and that study rejected a claim of edge.

## 1. PDH/PDL liquidity sweep — `liquidity_signals.scan`

**Hypothesis.** A sweep beyond the prior day's high or low that closes back inside, with the open and a VWAP side, continues to a multiple of the risk.

**Where it runs.** `backend/scripts/backtest_liquidity.py`, the October audit harness, and `DetectorAgent` (5-minute and 15-minute only; `bar_mins >= 3`). The live call sets `dynamic_targets=True` and then drops anything outside 10–55 points. The default backtest call does not pass `dynamic_targets`. `MIN_RISK` inside `scan` is 30, so the detector's floor of 10 does not add sweep trades; the ceiling of 55 removes the wide stops the October sample kept.

**Entry (code).** For each side, one trade. Stage 0: 5-minute extreme at least `min_sweep` (default 2) beyond PDH or PDL. Then either an immediate reclaim (`fast_reclaim`) or a 15-minute structure break (long: close above a prior 15-minute swing high; short: 15-minute close back under PDH, then a 5-minute close under a prior 5-minute swing low). Time window `FIRST_ENTRY` to `LAST_ENTRY`. Close must be through the level, the open, and the VWAP side. Optional trendline. Lunch 11:30–13:00 skipped or passed only if `is_midday_waterfall_active` says so.

**Stop.** Swing mode: 5 points beyond the 15-minute (or 5-minute) swing since the sweep, else 5 points beyond the sweep extreme. The live detector drops a result whose risk is outside 10–55 points. It does not replace that stop with the 22–35 point micro-wick clamp. That clamp is `EntryAgent`, which the daemon does not call for a normal candidate list.

**Exit.** `exit_of` / `staged_exit_of`: stop, then target, then time, on later 5-minute highs and lows. Target is `R_MULT` or a spot level when `dynamic_targets` is on.

**Look-ahead.** October audit: 0 of 568 signals changed when the future was cut. This session re-read the pivot rule (`pivots` needs `SWING` bars on both sides, and the scan passes `bars[:i]` / `b15[:k]`). That is lagged, not repainting, for this function. Live Yahoo bars are a separate risk; the daemon now drops the forming 5-minute bar before the tick.

**Invalidation of the profit claim.** October study, not re-run: 544 trades, +0.032R, 95% CI about [−0.048, +0.114], OOS 2021–2026 about −0.028R. 55 variants, none surviving a multiple-testing correction.

## 2. Breakout and retest — `scan_retest`

**Hypothesis.** A break of PDH/PDL (and optionally the first 30 minutes) that pulls back and holds is a continuation.

**Entry.** Break by `min_breakout`, pullback within `max_retest_depth`, close back with the trend, VWAP and open. Stop 5 points beyond the pullback extreme. One re-entry if `allow_reentry`. OR-30 uses the first six 5-minute bars, so the level is known only after 10:00 if the session starts at 09:15. That part is causal.

**Live.** The detector calls this function with `include_orh_orl=True`, `allow_reentry=True`, `dynamic_targets=True`, and `bar_minutes` of 1, 5, or 15. The default `backtest()` call uses the function defaults: no opening-range levels, no re-entry, no dynamic target, 5-minute bars only. On a 1-minute frame the function lowers `min_risk` from 30 to 10. Those 1-minute retests are not in the October 5-minute study. `TradeManager` resizes the stop with the sniper only if `df_1m` is passed. The daemon's `on_market_tick(...)` call has no `df_1m` argument, so that resize does not run.

## 3. Trendline reversal — `scan_trendline_reversal` and the detector

**Hypothesis.** A third touch of a trendline plus a break is a reversal.

**Research path.** `scan_trendline_reversal` inside the same backtest loop, same Black-76 exit. Not separately significant in the October variant table (see that folder). 

**Live path.** Detector plus trend-agent ADX gate. Stop buffer overwritten from `active_strategy_params.json` (`micro_wick_stop_buffer` 10 as read this session). `dynamic_rr_ceiling` is written by the parameter agent and **not read** by `live_scanner.py` (only `otm_steps` and `micro_wick_stop_buffer` are applied). The ceiling in the JSON does not change targets.

## 4. Live micro-wick sweep — `EntryAgent.evaluate_sweep`

**Hypothesis.** One 5-minute bar that wicks through PDH or the call wall and closes back is a short (mirror for longs), with a stop forced into 22–35 points.

**This is not `scan()`.** It does not require the 15-minute structure break, the open filter, or VWAP. It runs only when `candidate_setups is None`. The daemon always passes a list, including an empty one, so this function is not the normal live entry. It remains in the process as a fallback for other callers.

**Stop, if it did run.** `max(22, min(35, max(high + 3 - entry, buffer)))` on the short side. That is a different trade from the swing stop. Do not describe the daily book as this rule.

**Strike.** `select_strike_with_delta_floor`: walk in from 2 OTM until absolute delta ≥ 0.35 if the chain has greeks. If the chain has no delta, the function returns the ATM label and delta 0.0, and `TradeManager` then **drops** the trade (`est_delta < 0.35`). Missing greeks mean no entry, not a guessed strike. The no-chain formula (0.50 − 0.065 per step) is only for callers that pass no chain.

**Position size.** Frozen 3 lots, or 4 if at least 28 bars and ADX ≥ 25. Not the capital and margin math in `resolve_strike_and_lots`, which the live crew does not call.

## 5. Midday waterfall — `EntryAgent.evaluate_waterfall`

Used only if no candidate fired and the clock is 11:30–13:45. Equal-high/low proximity can veto it. Volume on the index is not a reliable input (Yahoo index volume is often zero), so the lunch gate's 1.5× volume test usually fails and the name-based override (V2-19) decides.

## 6. Research-engine structures — `strategies` markdown + `sim.py`

Iron condors, spreads, long calls and puts expressed as rules, filled on daily option closes. Hypothesis, stops and targets are whatever the markdown `rules:` block says. The catalog in `strategies/analyzer.py` (`RESEARCH_CATALOG`) publishes win rates and rupee P&L as Python literals (V2-10). Those figures are not computed from `sim.py` when the catalog is served.

## 7. Stock flag — `trading/stock_flag.py`

A side alert. It does not share the NIFTY risk file. Not audited trade by trade.

## Cross-cutting defects

| Issue | Where | Effect |
|---|---|---|
| Same function, different call | Detector calls `scan` / `scan_retest` / `scan_trendline_reversal`, then `TradeManager` replaces the target with `risk * trend-agent R` and applies VIX, wall, lunch, and delta gates the October `backtest()` does not | October R does not transfer (V2-08) |
| Overlapping generators in one backtest | `backtest()` extends sweep, retest, trendline, and optional equal-level signals into one ledger | One day can contribute several trades. Correlation is ignored when the ledger sums rupees |
| Live second entry | `can_take_entry` when `be_locked` | Two NIFTY option bets in the same direction or opposite; no net delta cap (V2-12) |
| Re-entry | Detector and retest `allow_reentry`; idempotency key includes timestamp and rounded price | A later bar is a new key. Restart drops the in-memory set (V2-13) |
| Exit on spot, P&L on a fake delta | `ExitAgent` compares CMP to spot stop and target; `record_exit` multiplies points by 0.5 | An option can hit a premium stop while spot has not, and the reverse (V2-03) |
| Stops checked on spot prints inside the daemon loop (10 s), not only on bar close | `on_market_tick` uses `cmp` | Live exit can fire intrabar. `scan` exits use completed 5-minute highs and lows. Another live/research split |

## Rules that are clear enough to trade on paper, and still unproven

The live gates that are unambiguous: no new trade before 09:30, no new trade at or after 14:30, no trade if VIX is missing or outside [11.5, 22], no trade if the 5-minute bar is stale, no trade if walls are missing, one fresh trade unless the open one is marked breakeven. Those are controls, not an edge.
