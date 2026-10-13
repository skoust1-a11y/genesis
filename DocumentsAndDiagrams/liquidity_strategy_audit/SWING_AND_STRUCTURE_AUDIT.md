# Swing and structure audit

## Swing definition (`pivots`, `liquidity_signals.py` L81-85)

```python
[j for j in range(n, len(bars) - n) if bars[j][key] == pick(b[key] for b in bars[j - n : j + n + 1])]
```

| Property | Finding | Evidence |
|---|---|---|
| Window | 2 bars left, 2 bars right (`SWING = 2`) | L49 |
| Confirmation | Bar j is only returned when bars j+1 and j+2 exist, so a swing is usable from the close of bar j+2 | `test_swing_confirmed_only_after_two_right_bars` |
| Strictness | Non-strict: two equal highs inside the window are both swing highs | `test_equal_highs_both_pivot` |
| Repaint | None. The pivots of `bars[:m]` are exactly the full-day pivots with j <= m-3, so later bars never remove or move an earlier pivot | `RESULTS.md` Reproduction: 0 repainted signals |
| Session scope | Pivots use only today's bars; the previous day's swings are never used | `scan` L111, L119, L139, L144 |

**Strict vs non-strict (measured):** strict pivots give 541 trades, +0.0322R, against 544 trades, +0.0316R. The choice is immaterial (`RESULTS.md` Variants, "Strict pivots").

## Long structure: 15m close above the last 15m swing high (L107-114)

- The 15m bar is built from 5m bars by `to_15m` / `_bucket` (L64-78). It is evaluated on its third 5m bar (`_at(x.date, 10) == b.date`), which is exactly when the 15m bar completes. No partial-bar look-ahead.
- The swing high comes from `pivots(b15[:k])`: every **completed** 15m bar of the session before the current one, **including bars before the sweep**. So "last swing high" is often a pre-sweep high from the morning. The structure break is then a break of an older high, not necessarily a post-sweep change of character.
- Structure and entry can happen on the **same** 5m bar: when the 15m close breaks the swing and the same 5m close is above VWAP, PDL and the open, the entry is that bar.

### Reclaim and displacement alternatives (measured; full detail in `STRATEGY_VARIANT_COMPARISON.md`)

| Variant (long side only) | Long trades | Long expectancy R | CI95 |
|---|---|---|---|
| Original (15m swing break) | 184 | +0.077 | [-0.024, 0.186] |
| Extra step: a 15m close back above PDL before the swing break | 175 | +0.069 | [-0.038, 0.179] |
| Reclaim + displacement (body of the structure-break 15m bar >= 0.1 x daily ATR14) | 138 | +0.059 | [-0.061, 0.175] |
| 5m swing break (configuration C) | 234 | **-0.154** | **[-0.272, -0.036]** |

The 15m swing break is the best long trigger tested. Replacing it with a 5m swing break is the only change whose CI excludes zero, and it is **harmful**. The long side's 15m timeframe matters.

## Short structure: rejection, then 5m close below the last 5m swing low (L115-122)

- The rejection (15m close back below PDH) and the structure break are **never on the same bar** (the `elif` at L118). The structure check starts on the next 5m bar.
- `pivots(bars[:i])` uses completed 5m bars before bar i, confirmed (j <= i-3). The swing low can predate the raid.
- Variants (short side only): original 360 trades, +0.008R; 15m structure (B) 277 trades, -0.010R; no rejection step (D) 378 trades, +0.021R. None differs meaningfully.

## Asymmetry (Part 3)

| Configuration | Trades | Expectancy R | OOS R | Max DD R |
|---|---|---|---|---|
| A Long 15m, short 5m + rejection (original) | 544 | +0.032 | -0.028 | 20.3 |
| B Both 15m | 464 | +0.018 | 0.000 | 16.6 |
| C Both 5m | 589 | -0.066 | -0.136 | 57.5 |
| D A without the short rejection step | 562 | +0.036 | -0.025 | 23.7 |

The longs carry the strategy: 184 trades, +0.077R, PF 1.32, win rate 52.7%. The shorts are flat: 360 trades, +0.008R, PF 1.02. But the long side's edge is mostly time exits: only 2.7% of longs reach 2R (shorts 16.4%), and the average long win is 0.60R. The longs make money as a "close above the open after a sweep" drift trade, not as a run to the 2R target. The 15m long and 5m short asymmetry is **not** shown to be wrong. The data cannot separate A, B and D (their CIs overlap almost entirely). C is the only configuration that is clearly worse.

## Stop placement (L134-148)

| Side | Primary stop | Fallback | Asymmetry |
|---|---|---|---|
| Long | Last 15m swing low at or after the sweep's 15m bucket, -5 | Sweep low -5 | - |
| Short | Last 5m swing high at or after the raid bar, +5 | Raid high **+15** | 10 pts wider fallback on the short side |

The "sweep-extreme stop for both" variant (`stop_at="sweep"`) gives 653 trades but +0.010R, worse than the swing stops (+0.032R).

## Risks for live trading

- Swings depend on bar boundaries. Yahoo, Groww and Kite 5m bars agree to the median (0 pts, max 10.75 pts on the high, `RESULTS.md` Data quality), but a 1-2 pt difference at a pivot can move a swing and therefore the stop.
- If Yahoo's in-progress last bar is taken as complete (`main` L427-428), a swing can appear on bar j+2 before j+2 has really closed. This is a live-only repaint risk the backtest cannot see.
