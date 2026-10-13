# Bugs and recommendations

Severity: **H** means it changes results or can lose money, **M** misleads or is inconsistent, **L** is minor. File references are to the working tree on 2026-10-14. Details for each item are in `CODE_IMPLEMENTATION_AUDIT.md`.

## Fixed in this audit

| # | Sev | Bug | Fix | Test |
|---|---|---|---|---|
| 1 | H | Kaggle data contains 149 synthetic holiday sessions, which create fake narrow PDH/PDL values (44 signals, +14.33R in the workbooks) | `backtest_liquidity.kaggle_5m` drops sessions whose bars all share one OHLC | `test_kaggle_drops_flat_holiday_sessions` |
| 2 | M | The alert promised a 2-loss daily stop that does not exist (and could never bind) | `message()` L214 now says "Max 1 trade per side (so at most 2 a day)" | `test_alert_text_matches_rules` |

Neither fix changes an entry, exit or risk rule.

## Open: recommended fixes (not applied: they change behaviour or need a decision)

| # | Sev | Issue | Where | Recommendation |
|---|---|---|---|---|
| 3 | H | `RISK_PCT = 5` gives a 65% modelled chance of a 50% drawdown | L52 | Set to 1 (0.5 while forward testing). The 2026-10-13 plan itself says 1%. **Owner decision.** |
| 4 | H | Both 10-year workbooks are contaminated (fix 1) and capital-filtered | `DocumentsAndDiagrams/NIFTY_Liquidity_Backtest_10y_2026-10-14.xlsx` | Regenerate after fix 1. Report the every-signal R results alongside the sized ledger. |
| 5 | H | Live may treat Yahoo's still-forming bar as complete | `main` L427-428 | Only use a bar once the next bar exists, or switch to broker (Groww/Kite) 5m bars |
| 6 | H | Option rupee figures assume constant IV (+/-10% IV gives -0.24R / +0.41R) | `backtest_liquidity.option_exit` L191 | Label workbook rupee columns as modelled. Record real fills during the forward test. |
| 7 | M | The 14:25/14:30 entries are probably never sent live | `main` L452 | Keep the loop running until 14:40 and let `FRESH` decide |
| 8 | M | Live and backtest cooldowns count losses differently | `break_until` L372 vs `ledger` L282 | Use one definition: net index R < 0 on every signal, the one the audit uses |
| 9 | M | `expiry_for` is wrong on 8 days (2023-06, 2025-10) | `backtest_liquidity.py` L163 | Prefer NSE-listed expiries from `market_eod.db` when available |
| 10 | M | A missing NIFTYBEES VWAP blocks entries | `scan` L130, `session_data` L347 | Fall back to index TWAP when the proxy bar is missing (signals matched on 16 of 17 days) |
| 11 | M | A too-tight first setup ends the side for the day | `scan` L150-155 | Keep (it is the specification), but document it in the alert spec. Changing it is a new strategy. |
| 12 | M | The working-tree v3 exit/OTM change is undocumented as research-driven and post hoc | L12-13, L49 | Revert to the 2R/ATM baseline, or label v3 as "forward-test candidate". The docstring's "more trades fit the risk budget" is a capital artifact, not an edge. |
| 13 | M | Live PDH/PDL from Yahoo daily vs 5m-derived in the backtest | `session_data` L349 | Derive PDH/PDL from the previous session's 5m bars live too |
| 14 | L | Long structure can use a pre-sweep swing high | `scan` L111 | Document. Restricting it to post-sweep swings is a variant to forward-test, not a fix. |
| 15 | L | `FRESH` and the end-of-loop timing are untested | `main` | Add a unit test with a fake clock |
| 16 | L | `holidays()` covers 2020 onward only | `app.market.hours` | Add 2016-2019 NSE holidays for backtests (affects `expiry_for` before 2020) |

## Strategy recommendations

1. **Do not trade it live with real money.** The net edge is indistinguishable from zero and negative OOS (`STATISTICAL_VALIDATION.md`).
2. **Freeze the baseline (A) and stop searching filters.** The IS-selected combination lost significantly OOS. More filters on this sample will fit noise.
3. **Forward-test in paper mode for at least 6 months (about 25 trades) at 0.5-1% notional risk**, logging the real option fill, the exit premium and the alert latency per trade. Judge it against the pre-registered criteria in `LIVE_TRADING_READINESS.md`.
4. **If one change is made, make it the 2-pt minimum sweep (hypothesis 1).** It removes a loser class (1-pt pokes, inside the data-source error) for mechanical reasons, not fitted ones.
5. Candidates to track in parallel in the forward test (no deployment): D displacement 0.1 ATR, and the v3 exit (3R + breakeven at +1R).
6. Get NIFTY futures 5m data (volume) to test a real VWAP, and intraday option data to replace the Black-76 model. Both are prerequisites for any rupee claim.
