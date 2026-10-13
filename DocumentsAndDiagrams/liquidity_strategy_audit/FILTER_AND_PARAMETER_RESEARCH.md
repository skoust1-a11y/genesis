# Filter and parameter research (Parts 6, 7 and 10)

Each row changes **one** thing against the original (A: +0.0316R, IS +0.125, OOS -0.028, 544 trades, max DD 20.3R). Net R, 2016-01-01 to 2026-10-13. Full table with CIs and PF: `RESULTS.md` Variants. **None of these differences is statistically significant** (55 configurations tested, Bonferroni |t| > 3.32; the best t-stat is 1.37).

## Exits (Part 6)

| Exit | Expectancy | OOS | Max DD R | Reached 2R | Note |
|---|---|---|---|---|---|
| 1.0R target | +0.004 | -0.050 | 22.3 | - | |
| 1.5R target | +0.012 | -0.054 | 26.7 | - | |
| **2.0R (original)** | +0.032 | -0.028 | 20.3 | 11.8% | |
| 2.5R | +0.043 | -0.014 | 20.5 | 11.8% | |
| 3.0R | +0.056 | +0.004 | 21.1 | 11.8% | Walk-forward picks 3R every year |
| Liquidity target (opposite PDH/PDL, min 1R) | +0.031 | -0.028 | 23.6 | 5.5% | |
| Opposite level clamped to 1-3R | +0.034 | -0.034 | 24.6 | | |
| Breakeven at +1R | +0.031 | -0.032 | 18.8 | 10.8% | 8.1% of trades reach 1R and then go back to entry |
| Trail 1R behind best price after +1R | +0.033 | -0.045 | 22.9 | 9.5% | |
| Partial: half at 1R, stop to entry | +0.017 | -0.041 | 20.4 | | |
| Structure exit (5m swing break, no target) | -0.019 | -0.069 | 34.5 | | Worst exit tested |
| **v3 working tree: 3R + breakeven at 1R** | +0.058 | +0.001 | 17.5 | | Best DD and expectancy, but chosen after seeing 10-year results |

Time exits:

| Time exit | Expectancy | OOS | Max DD R |
|---|---|---|---|
| 14:45 | +0.004 | -0.072 | 33.0 |
| 15:00 | -0.003 | -0.091 | 37.2 |
| **15:10 (original)** | +0.032 | -0.028 | 20.3 |
| 15:20 | +0.043 | -0.013 | 20.2 |

Reading: later exits and wider targets are monotonically (slightly) better. That is consistent with a weak afternoon drift in the entry's direction rather than a reversal that completes quickly. Targets above 2R add only through the time exit: the share of trades reaching 2R barely changes, while fewer trades are cut at 2R.

## Filters (Part 7)

| Filter | Trades | Expectancy | IS | OOS | Verdict |
|---|---|---|---|---|---|
| Expiry days skipped (original) | 544 | +0.032 | +0.125 | -0.028 | |
| Expiry days included | 658 | +0.049 | +0.122 | +0.008 | The expiry filter does not help on this evidence |
| Expiry days only | 128 | +0.060 | -0.019 | +0.084 | |
| Also skip the day before expiry | 422 | -0.007 | +0.074 | -0.066 | Harmful: the day before expiry is the best bucket |
| Min risk 0 | 671 | -0.022 | -0.002 | -0.042 | |
| Min risk 20 | 633 | -0.012 | +0.025 | -0.042 | |
| **Min risk 30 (original)** | 544 | +0.032 | +0.125 | -0.028 | Chosen in-sample |
| Min risk 40 | 445 | +0.027 | +0.139 | -0.031 | |
| Min risk 50 | 365 | +0.038 | +0.101 | +0.011 | Walk-forward drifts to 50 from 2023 |
| No cooldown | 568 | +0.015 | +0.128 | -0.055 | |
| Cooldown 2 losses / 3 days | 514 | +0.005 | +0.120 | -0.067 | |
| Cooldown 3 / 1 | 561 | +0.020 | +0.128 | -0.048 | |
| **Cooldown 3 / 3 (original)** | 544 | +0.032 | +0.125 | -0.028 | |
| Cooldown 3 / 5 | 527 | +0.046 | +0.137 | -0.014 | |
| Cooldown 4 / 3 | 554 | +0.025 | +0.132 | -0.042 | |
| Skip gap > 0.5 ATR | 455 | +0.004 | +0.098 | -0.059 | Reject |
| Skip high-VIX tercile (IS cut-off) | 392 | -0.012 | +0.084 | -0.066 | Reject: high VIX is the best bucket |
| Skip low-VIX tercile (IS cut-off) | 296 | +0.036 | +0.132 | -0.053 | Reject |
| With SMA50 trend only | 234 | +0.020 | **+0.185** | **-0.089** | Reject: classic IS winner, OOS loser |
| Against trend only | 318 | +0.002 | +0.037 | -0.018 | |
| Room to opposite level >= 2R | 184 | -0.038 | **+0.172** | **-0.123** | Reject: same pattern |
| Entries before 13:00 only | 374 | -0.011 | +0.071 | -0.060 | Reject |
| Strict pivots | 541 | +0.032 | +0.116 | -0.022 | Immaterial |
| Sweep-extreme stops | 653 | +0.010 | +0.020 | +0.002 | Worse |

**Late signals (Part 7):** the live system never sends a signal older than 15 minutes (`FRESH`, L50). A historical test of delayed execution would need the entry moved to a later bar's close. Not run separately. The 5m entry-price sensitivity is covered by the cost test (each extra 1 index pt of slippage costs about 0.017R; the edge is gone at about 6 pts total).

## Sweep definition (Part 3)

| Sweep threshold | Trades | Expectancy | IS | OOS |
|---|---|---|---|---|
| > 1 pt (original) | 544 | +0.032 | +0.125 | -0.028 |
| > 2 pts | 540 | +0.044 | +0.138 | -0.016 |
| > 5 pts | 516 | +0.043 | +0.172 | -0.037 |
| > 10 pts | 483 | +0.043 | +0.158 | -0.022 |
| > 20 pts | 390 | +0.000 | +0.062 | -0.030 |
| > 0.01 x ATR | 539 | +0.036 | +0.125 | -0.023 |
| > 0.025 x ATR | 526 | +0.044 | +0.156 | -0.031 |
| > 0.05 x ATR | 502 | +0.046 | +0.158 | -0.029 |
| > 0.1 x ATR | 421 | +0.051 | +0.176 | -0.027 |

Raising the minimum sweep from 1 pt to 2 pts removes the 1-2 pt sweeps (6 trades, all losers) and is the cleanest "fix": it is mechanically motivated (a 1-pt poke is noise, within the 1-3 pt data-source difference on PDH/PDL). ATR-normalised depth adds nothing over a fixed 2-pt floor out-of-sample.

## Part 10: ten improvement hypotheses, tested one at a time

Confidence is in the hypothesis being a real improvement, not in the number. "Before" is A (+0.032, OOS -0.028).

| # | Hypothesis | Mechanism | Code change (research harness) | After (all) | After (OOS) | Confidence | Complexity | Recommendation |
|---|---|---|---|---|---|---|---|---|
| 1 | Require > 2 pt sweep | A 1-pt poke is data noise, not a stop run | `sweep_pts=2` | +0.044 | -0.016 | Low-medium | 1 constant | **Adopt in paper trading** (removes a known loser class, no fitting) |
| 2 | Long: PDL reclaim before structure | Confirms the sweep failed | `long_reclaim=True` | +0.027 | -0.035 | Low | Small | Reject |
| 3 | Displacement (structure bar body >= 0.1 ATR) | Real order flow behind the break | `disp=0.1` | +0.057 | +0.008 | Low | Small | Monitor; do not adopt |
| 4 | Long reclaim + displacement | 2 and 3 together | both | +0.062 | +0.011 | Low | Small | Monitor; do not adopt |
| 5 | 3R target | Winners run into the close | `r_mult=3` | +0.056 | +0.004 | Low | 1 constant | Optional; WF agrees, not significant |
| 6 | 3R + breakeven at 1R (working tree v3) | 5 plus protecting open profit | `mode="be", r_mult=3` | +0.058 | +0.001 | Low | Small | Do not deploy until forward-tested; chosen post hoc |
| 7 | 15:20 time exit | Same drift, 10 more minutes | `exit_t="15:20"` | +0.043 | -0.013 | Low | 1 constant | Reject (liquidity and closing-auction risk for 10 min, no evidence) |
| 8 | Min risk 50 | Tight stops get noise-stopped | `min_risk=50` | +0.038 | +0.011 | Low | 1 constant | Monitor (WF drifts here) |
| 9 | Trade with the SMA50 trend only | Reversals against trend fail | day filter | +0.020 | **-0.089** | Very low | Small | **Reject** (overfit) |
| 10 | Room to opposite level >= 2R | Target must be reachable | day filter | -0.038 | **-0.123** | Very low | Small | **Reject** (overfit) |

Extra hypotheses also rejected: skip high VIX, skip low VIX, skip large gaps, entries before 13:00 only, symmetric 5m structure (C: long side -0.154, CI excludes 0).

**Combined test (G):** every candidate whose IS expectancy beat A by more than 0.02R was combined (sweep > 0.025 ATR + SMA50 trend + room >= 2R). 74 trades, IS +0.153, **OOS -0.280 (CI [-0.525, -0.019])**. Stacking in-sample winners produced a significantly losing strategy out-of-sample: direct evidence that the sample cannot support parameter search.
