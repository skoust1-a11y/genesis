# Live trading readiness

**Status: NOT READY for live capital.** The alert system can keep running in alert-only mode; it places no orders and this audit enabled none.

## Checklist

| Area | Status | Evidence |
|---|---|---|
| Demonstrated net edge | **Fail** | +0.032R, CI [-0.048, 0.114]; OOS -0.028R |
| Survives multiple testing | **Fail** | Best variant t = 1.37 vs Bonferroni 3.32 |
| Out-of-sample stability | **Fail** | IS +0.125 vs OOS -0.028; 2023 -15.2R |
| Implementation matches the specification | Pass | 0 signal and 0 exit mismatches; no look-ahead |
| Historical data quality | Pass (after fix) | Fake holiday sessions removed; NSE EOD cross-check median 0 |
| VWAP source | Partial | NIFTYBEES proxy vs TWAP agree on 16 of 17 signal days; futures VWAP untested |
| Live data timeliness | **Fail** | Yahoo delay; a possibly incomplete last bar; 14:25/14:30 entries likely missed |
| Option pricing | Partial | Live chain when available; fallback P90 error 29-65%; backtest IV constant |
| Execution costs | Marginal | Breakeven at about 6 index pts round trip vs 4 assumed |
| Position sizing | **Fail** | 5% risk: 65% chance of a 50% drawdown (modelled) |
| Risk controls | Partial | 3-loss / 3-day cooldown in place; live and backtest definitions differ |
| Alert text consistent with rules | Pass (fixed) | `test_alert_text_matches_rules` |
| Uncommitted, unvalidated rule changes | **Fail** | Working tree changed to 3R / breakeven / 2 OTM during the audit |
| Order placement | N/A | None exists; keep it that way |

## Pre-registered forward-test pass criteria (paper, frozen rules)

Decide these before the test starts. Do not change them afterwards.

1. At least 25 trades (about 6 months), with the configuration frozen on day 1.
2. Mean **realised option R** (real fills, after charges) > 0, and mean index net R > 0.
3. Median alert latency (bar close to alert received) under 3 minutes; no repaint (a signal that disappears on the next scan).
4. Real entry premium within 15% of the alert's quoted premium on at least 80% of trades.
5. Max drawdown no worse than 10R. This is a stop-loss on the test itself, about half the 10-year historical maximum of 20.3R.

25 trades cannot prove an edge (see the sample-size section of `STATISTICAL_VALIDATION.md`). Passing only means the live implementation behaves like the backtest and costs are as assumed. Failing any criterion means stop.

## What must change before any live capital

1. Phase 1 and Phase 2 of `IMPLEMENTATION_ROADMAP.md`.
2. `RISK_PCT` at 1 or lower.
3. A forward test that passes the criteria above.
