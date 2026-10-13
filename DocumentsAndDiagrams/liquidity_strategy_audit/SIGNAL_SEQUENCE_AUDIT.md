# Signal sequence audit

## State machine in `scan()` (`liquidity_signals.py` L95-155)

Each side runs its own pass over the session's 5m bars, with one stage step per bar at most:

| Stage | Long | Short | Advances on |
|---|---|---|---|
| 0 | wait for sweep | wait for raid | 5m `low < PDL-1` / `high > PDH+1` (L100) |
| 1 | wait for 15m close above last 15m swing high | wait for 15m close below PDH | the third 5m bar of a 15m bucket (L107) |
| 2 | **entry check** | wait for 5m close below last 5m swing low | every 5m bar (L118) |
| 3 | - | **entry check** | - |
| entry | close above VWAP, PDL and open, 09:30-14:30 | mirrored | first qualifying bar; then `break` (L155) |

## Sequencing properties (verified)

1. **Order is enforced.** Each condition is checked only after the previous one is met. The sweep must come first; a structure break before the sweep is ignored.
2. **No time limit between steps.** A 09:20 sweep can lead to a 14:25 entry. The sweep is never invalidated, even if price trades far beyond the level later. `ext` keeps tracking the extreme, and the fallback stop uses it.
3. **No re-arm.** Once a side's stage has advanced, it never resets. A second sweep after a failed setup does not restart the sequence.
4. **Same-bar coincidences.**
   - Long: the 15m structure break and the entry can be the same 5m bar.
   - Short: the rejection and the 5m structure break are never the same bar (the `elif`). The structure break and the entry can be the same bar.
   - The sweep bar can never be the structure or entry bar (`continue` at L104).
5. **Entry window gates the entry only.** Steps 0-2 may complete before 09:30. The first entry check that passes at or after 09:30 fires (L130). After 14:30 the side stops (L127-128).
6. **One attempt per side.** The first bar that passes the entry filters ends the side, whether or not the risk is at least 30 pts (L150-155). A too-tight setup therefore **blocks** that side for the rest of the day.
7. **Missing VWAP blocks.** `above_vwap.get(date) != long` is true when the value is `None`, so the bar is skipped (L130).

## Look-ahead and timing

| Check | Result |
|---|---|
| Uses only bars up to the entry bar | Yes: `test_scan_uses_no_future_bars`, and 0 of 568 signals change when re-scanned on truncated data |
| 15m values used before completion | No: evaluated on the bucket's last 5m bar (L107); the long stop uses only 15m bars ending by the entry close (L138) |
| Swing used before confirmation | No (see `SWING_AND_STRUCTURE_AUDIT.md`) |
| PDH/PDL | Previous session only (backtest: 5m-derived; live: Yahoo daily) |
| Day open | First 5m bar's open: known at 09:20, before any entry |
| VWAP / TWAP | Running value through the bar's close: no look-ahead |

## Live timing (not backtestable)

- The loop wakes 1 minute after each 5m boundary (L453) and treats bars that started 5 minutes or more ago as complete (L427-428). With Yahoo's typical delay a bar can arrive late (signal is late) or incomplete (signal can repaint).
- `FRESH` (L50) prints signals older than 15 minutes as "missed". A signal first seen late is never sent, and its side is marked `seen`, so no later signal on that side is sent either. This matches the "never chase" rule.
- The last scan happens at the first wake after 14:30 (14:31). Entries at 14:25-14:30 are sent only if Yahoo has those bars by then.
- **Not unit-tested:** the `FRESH` path and the end-of-loop timing in `main()`.

## Measured timing of baseline entries (544 trades)

| Entry close | Trades | Expectancy R | CI95 |
|---|---|---|---|
| 09:30-11:00 | 155 | +0.012 | [-0.156, 0.187] |
| 11:00-13:00 | 218 | -0.029 | [-0.158, 0.102] |
| 13:00-14:30 | 171 | **+0.126** | **[0.007, 0.241]** |

Average hold: 151 minutes. 348 of 544 trades (64%) end at the 15:10 time exit. Late entries do best in-sample. This is a single bucket among many tested (see the multiple-testing note in `STATISTICAL_VALIDATION.md`). "Entries before 13:00 only" as a rule gives -0.011R.
