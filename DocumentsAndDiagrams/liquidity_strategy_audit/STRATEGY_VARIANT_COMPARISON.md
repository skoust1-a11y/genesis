# Strategy variant comparison (Parts 3 and 11)

Net R, 2016-01-01 to 2026-10-13, every signal taken, expiry days skipped, 3/3 cooldown, 2R / 15:10 unless stated. IS = 2016-2020, OOS = 2021-2026.

## Configurations

| Config | Definition | Trades | Win % | Expectancy | CI95 | PF | Max DD R | IS | OOS | OOS CI95 | OOS PF |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **A Original** | Long 15m swing break, short rejection + 5m swing break | 544 | 45.2 | +0.032 | [-0.048, 0.114] | 1.09 | 20.3 | +0.125 | -0.028 | [-0.133, 0.075] | 0.93 |
| B Symmetric structure | Both sides 15m structure | 464 | 47.4 | +0.018 | [-0.051, 0.087] | 1.06 | 16.6 | +0.040 | +0.000 | [-0.092, 0.094] | 1.00 |
| C Long PDL reclaim | 15m close back above PDL required before the long structure break | 534 | 44.9 | +0.027 | [-0.054, 0.110] | 1.07 | 21.3 | +0.125 | -0.035 | [-0.139, 0.070] | 0.92 |
| D Displacement | Structure-break bar body >= 0.1 x daily ATR14 (both sides) | 452 | 46.0 | +0.057 | [-0.027, 0.144] | 1.17 | 17.7 | +0.120 | +0.008 | [-0.105, 0.120] | 1.02 |
| E VWAP source | Futures VWAP instead of the NIFTYBEES/TWAP proxy | - | - | **INSUFFICIENT DATA** (no futures intraday volume) | - | - | - | - | - | - | - |
| F ATR sweep depth | Sweep must exceed 0.025 x ATR14 (vs 1 pt) | 526 | 45.6 | +0.044 | [-0.034, 0.123] | 1.12 | 21.2 | +0.156 | -0.031 | [-0.136, 0.071] | 0.92 |
| G IS-selected combination | F + SMA50 trend + room >= 2R (all IS winners) | 74 | 37.8 | **-0.157** | [-0.378, 0.073] | 0.69 | 20.4 | +0.153 | **-0.280** | **[-0.525, -0.019]** | 0.50 |
| v3 working tree | A with 3R target and stop to entry at +1R | 545 | 41.1 | +0.058 | [-0.026, 0.143] | 1.17 | 17.5 | +0.148 | +0.001 | [-0.106, 0.112] | 1.00 |

Additional Part 3 configurations (detail in `SWING_AND_STRUCTURE_AUDIT.md`):

| Config | Trades | Expectancy | OOS |
|---|---|---|---|
| Both 5m structure | 589 | -0.066 | -0.136 (long side alone -0.154, CI excludes 0) |
| A without short rejection | 562 | +0.036 | -0.025 |
| Long reclaim + displacement | 431 | +0.062 | +0.011 |
| Displacement 0.2 ATR | 180 | +0.049 | +0.016 |

For E, the closest available evidence is the 60-day comparison of NIFTYBEES VWAP against index TWAP: signals were identical on 16 of 17 signal days (`VWAP_AND_DATA_QUALITY_AUDIT.md`). The VWAP filter barely changes trades once the open filter applies, so E is unlikely to matter much. That is a hypothesis, not a measurement.

## Ranking

Ranking criteria in order: OOS expectancy, max drawdown, simplicity, and how well the change is grounded in a mechanism rather than fitted.

| Rank | Config | Why |
|---|---|---|
| 1 | **D Displacement 0.1 ATR** | Best OOS among the entry variants (+0.008), lower DD (17.7R), simple and mechanistic. Still not significant. |
| 2 | B Symmetric 15m | OOS 0.000, lowest DD (16.6R), fewer trades. Simpler to explain. |
| 3 | A Original | Reference |
| 4 | F ATR sweep | IS gain does not carry to OOS |
| 5 | C Long reclaim | Slightly worse everywhere |
| - | E | Not testable |
| last | G | Overfit: significantly negative OOS |

The v3 working-tree exit change (3R + breakeven) stacks with any entry variant. It has the best overall numbers (+0.058, DD 17.5R, OOS +0.001), but it was chosen after looking at the 10-year results. It belongs in the forward test, not in production as a validated improvement.

**No configuration has an OOS confidence interval above zero.** The ranking is a ranking of noise-dominated estimates. It should guide what to forward-test, not what to trade.
