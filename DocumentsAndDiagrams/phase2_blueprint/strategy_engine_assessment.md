# Strategy engine assessment

Date: 4 Nov 2026. Rules were not changed. This is what the code does.

## Strategies that can emit a daemon candidate

### A. PDH/PDL sweep — `liquidity_signals.scan`

**Hypothesis.** A sweep beyond the prior day high or low that closes back inside, with the open and a VWAP side, continues.

**Research call.** One pass per day on 5-minute bars. `stop_at` swing or sweep. No dynamic target unless a caller passes it. `backtest()` does not.

**Live call.** Same function, `dynamic_targets=True`, spot levels from the session, then discard risk outside 10–55. Not run on 1-minute frames (`bar_mins >= 3`).

**Entry.** Stage machine in `scan`: sweep of at least `min_sweep` (default 2), then 15-minute structure or a fast reclaim, inside the level and the open, VWAP side, time window. Lunch is skipped or passed only through `is_midday_waterfall_active`.

**Stop.** 5 points beyond the swing since the sweep, else 5 points beyond the extreme. `MIN_RISK` is 30.

**Target.** `R_MULT` times risk, or a spot level when dynamic targets are on. Live manager then **replaces** this with `risk * TrendAgent.recommended_target_r`.

**Size.** Not inside `scan`. Daemon: 3 lots, or 4 if ADX ≥ 25 and at least 28 bars.

**Invalidation.** The stop, or the manager's exit on spot. Not an option premium stop.

**Look-ahead.** October truncation test said `scan` did not repaint. Not rerun. The pivot helper needs bars on both sides of a slice that is the past. That reading still holds.

### B. Breakout and retest — `scan_retest`

**Hypothesis.** Break of PDH/PDL, pullback, hold.

**Research call.** Defaults: 5-minute, no opening range, no re-entry.

**Live call.** `include_orh_orl=True`, `allow_reentry=True`, `dynamic_targets=True`, and `bar_minutes` 1, 5, or 15. On a frame of 2 minutes or less the function lowers min risk from 30 to 10. Those 1-minute trades are not in the October 5-minute sample.

**Stop.** 5 points beyond the pullback extreme, then the 10–55 filter.

### C. Trendline reversal — `scan_trendline_reversal`

**Live call.** `require_choch=True`, `max_risk=55`, dynamic targets, re-entry, all frames including 1-minute.

**Research call.** `backtest()` passes `skip_lunch` only, when the include flag is on. Not the same argument list.

### D. Equal high / equal low — detector after the three functions

A last-bar wick rule in `DetectorAgent`, not `scan()`. No October study of this block was rerun. Treat it as a separate strategy with no filed expectancy.

### E. Waterfall — `EntryAgent.evaluate_waterfall`

Runs on the daemon if no candidate fired and the clock is 11:30–13:45. Uses `default_stop_buffer`, which the parameter file can change. Not part of `backtest()`.

### F. `EntryAgent.evaluate_sweep`

22–35 point clamp. Runs only when `candidate_setups is None`. The daemon always passes a list. Not the live book. Still able to run from tests and other callers.

### G. Research-engine markdown structures

`engine/sim.py` fills whatever the strategy document says, on daily option closes. Catalog text in `analyzer.py` is not this engine's output (F-10).

## Parity gaps that change the trade

| Item | Backtest default | Daemon |
|---|---|---|
| Dynamic target inside the signal | Off | On, then replaced again by the trend agent |
| Opening-range retest | Off | On |
| Re-entry | Off | On |
| 1-minute retest and trendline | Off | On |
| Risk above 55 | Kept if `MIN_RISK` passes | Dropped |
| VWAP series | NIFTYBEES or TWAP | Bars in hand |
| Exit | Completed 5-minute high/low in `exit_of` | Spot CMP on a 10-second loop, so a stop can fire inside a bar |
| Sniper | Not in `backtest()` | Code present, `df_1m` not passed, so off |

## Do not do in the implementation phase

Do not retune `MIN_RISK`, R, ADX, or OTM steps while aligning the calls. Alignment first. A new study second. The October result does not license a search.
