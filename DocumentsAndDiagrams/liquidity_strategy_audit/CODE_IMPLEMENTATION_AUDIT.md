# Code implementation audit

Scope: `backend/app/liquidity_signals.py` (live signals), `backend/scripts/backtest_liquidity.py` (10-year workbook), `backend/app/trading/option_pnl.py` (Black-76), and the audit harness `backend/scripts/audit_liquidity.py`. Line numbers refer to the working tree on 2026-10-14.

## Signal pipeline, end to end (live)

1. `main()` L397: if today is an expiry day (`weekly_expiry` L241), it sends "no trades" and exits.
2. `break_until()` L372 replays the last 60 days of Yahoo 5m bars through `scan()` + `exit_of()` as if every signal had been taken. If 3 consecutive index losses fall inside the last 3 trading days' window, it exits.
3. Every 5 minutes (wakes 1 minute after each 5m boundary, L453), `session_data()` L345 fetches Yahoo index 5m bars, NIFTYBEES 5m bars (VWAP side) and Yahoo daily bars (PDH/PDL).
4. Bars with start time up to now minus 5 minutes are treated as complete (L427-428).
5. `scan()` L88 runs on the whole session so far. Each new side is added to `seen` (L437-439). Signals older than `FRESH` = 15 min are printed but not sent (L440).
6. `option_plan()` L221 uses the Groww chain via `plan_pnl`, or `estimated_plan()` L249 (Black-76 at VIX) as a fallback. Then `message()` L159 and `charts()` L319 build the alert, sent through `alerts.send` / `send_photo`.
7. The loop ends after 14:30 (L452).

## Verified correct

| Check | Result | Evidence |
|---|---|---|
| Replica equivalence | `scan_v()` defaults equal production `scan()` on every day | `RESULTS.md` Reproduction: 0 mismatches; `test_scan_v_matches_production` |
| Exit equivalence | `run_exit()` equals the specified 2R exit and the working-tree `exit_of()` | 0 mismatches each |
| No repaint | Re-scanning with bars only up to the entry bar gives the same signal | 0 changes over 568 signals; `test_scan_uses_no_future_bars` |
| No early signal | Removing the entry bar removes the signal | 0 cases |
| Swing confirmation | A pivot needs two completed bars on its right | `test_swing_confirmed_only_after_two_right_bars` |
| Stop/target tie | Stop counts first when both are touched in one bar (conservative) | `exit_of` L361-364 |
| Entry bar excluded from exit | The first bar checked for stop/target is the one after the entry bar | see note below |

Note on the entry bar: `s["time"]` is the bar's **close** time (`_at(date, 5)`) and the loop skips bars whose start is before it. The entry bar starts 5 minutes before `s["time"]`, so it is skipped, and the first bar checked is the one after the entry. That is correct: the entry bar's range happened before the entry price existed.

## Defects and deviations found

Severity: **H** means it changes results or can lose money, **M** means it misleads or behaves inconsistently, **L** is cosmetic or documentation.

| # | Sev | Where | Finding | Status |
|---|---|---|---|---|
| 1 | H | Kaggle data via `kaggle_5m` (`backtest_liquidity.py` L79) | 149 synthetic holiday sessions (flat bars, one repeated OHLC) were treated as trading days. That created fake PDH/PDL values 5-10 pts wide for the next day: 44 signals, of which 35 were taken in the workbook (+14.33R, Rs 45,250). | **Fixed**: sessions whose bars all share one OHLC are dropped. Test `test_kaggle_drops_flat_holiday_sessions`. Both 10-year workbooks predate the fix and are contaminated. |
| 2 | M | `message()` L214 (old text) | "Stop for the day after 2 losses" was never enforced and can never bind with 1 trade per side. | **Fixed (text only)**; test `test_alert_text_matches_rules`. |
| 3 | H | `ledger()` `backtest_liquidity.py` L247-305 | At 5% of capital, when one lot's stop loss exceeds 5% the trade is skipped ("1 lot risks over 5%"). In the first 10-year workbook 263 of 548 signals were skipped for this reason (265 taken), and the skips correlate with wide stops, high VIX and low capital, so the equity curve is a **selected subset**, not the strategy. | Reported. Not changed (it is a sizing rule, not a defect in code). |
| 4 | H | `backtest()` L208-241, `option_exit` L191 | Option exits are priced at the **entry** VIX with forward = spot (IV constant). The audit shows IV x0.9 / x1.1 at exit moves option expectancy from +0.087R to -0.236R / +0.411R. The workbooks' rupee figures are model outputs, not history. | Reported. |
| 5 | M | `history_data` L143-157 | The 10-year run uses index TWAP; live uses NIFTYBEES VWAP. Signals differ on 1 of 17 signal days over the overlapping 60 days. | Reported (`VWAP_AND_DATA_QUALITY_AUDIT.md`). |
| 6 | M | `expiry_for` L163-188 | Disagrees with the nearest expiry listed in NSE EOD data on 8 days (2023-06-23..27: code 06-28, NSE 06-29, because the holiday file marks 06-29 as a holiday; 2025-10-15..21 around Diwali: code 10-21, NSE 10-20). Affects the option DTE and the expiry-day skip on those days. | Reported. |
| 7 | M | `break_until` L372 vs `ledger` L282 | Live counts every signal and a loss is a close past entry **before costs**. The backtest counts only trades actually sized (qty > 0). The two cooldowns diverge whenever trades are skipped. | Reported. |
| 8 | M | `main` L427-428 | A Yahoo bar starting at now minus 5 minutes is treated as complete. Yahoo is a few minutes late, and the latest bar can be **still forming** (repaint risk live, not in the backtest). | Reported. Fix: require the next bar to exist, or use broker bars. |
| 9 | M | `main` L452 | The loop ends at the first wake after 14:30 (14:31). Entries closing at 14:25/14:30 rely on Yahoo having those bars within 1 minute; with the usual delay they surface on no later scan and are never sent. 16 of 544 baseline trades (2.9%, +2.13R in total) have entry closes at 14:25-14:30. | Reported; not verified live. |
| 10 | M | `scan` L150-155 | A setup whose risk is below 30 pts ends that side for the day (`break` after the `if`). A later valid setup the same day is never considered. | Documented as part of the specification. |
| 11 | L | `pivots` L84 | Non-strict (`==`). Equal highs both count. Strict pivots change 3 trades (+0.032R vs +0.0316R): immaterial. | Documented; test `test_equal_highs_both_pivot`. |
| 12 | M | `scan` L111 | A long's structure break can use a 15m swing high from **before** the sweep, sometimes from the opening bars. That is "break of the morning high" rather than a post-sweep change of character. | See `SWING_AND_STRUCTURE_AUDIT.md`. |
| 13 | L | `scan` L107 | The 15m close is evaluated only on the bucket's third 5m bar. If that bar is missing (data gap), the 15m close is never evaluated. Groww lacked the 15:20 bar on 24 sessions (after the entry window, so no effect). | Reported. |
| 14 | M | `scan` L130 | A missing NIFTYBEES VWAP (`None`) blocks the entry on both sides. 56 index bars in 60 days had no NIFTYBEES bar. | Reported. |
| 15 | M | Docstring L12-13 | "2016-2026: more trades fit the risk budget" justifies OTM strikes with a capital-filter artifact (finding 3). It does not show better expectancy (+0.087 ATM vs +0.087 2 OTM per option R at 2R exits). | Reported. |
| 16 | M | Live PDH/PDL | Taken from the Yahoo daily bar, which can differ from the 5m-derived high/low used in the backtest. The NSE EOD vs 5m median difference is 0, but P99 is 2-3 pts, and the sweep threshold is 1 pt. | Reported. |
| 17 | L | `estimated_plan` L253 | One IV for every strike. The 2% OTM put skew is +3.7 vol pts (measured). | Already flagged with a `ponytail:` comment in the code. |
| 18 | L | `weekly_expiry` L241 | Tuesday only. Correct for live since 2025-09-01; not usable for history (the backtest uses `expiry_for`). | OK. |

## Changes made by this audit

| File | Change | Rule impact |
|---|---|---|
| `backend/app/liquidity_signals.py` L214 | Alert line no longer claims a 2-loss daily stop | None (text) |
| `backend/scripts/backtest_liquidity.py` `kaggle_5m` | Drop synthetic flat holiday sessions | Data fix; changes 10-year backtest output |
| `backend/scripts/audit_liquidity.py` | New research harness: replica, variants, statistics, options, charts | Research only, never imported by live code |
| `backend/tests/test_liquidity_audit.py` | 6 tests for the findings above | - |

No entry, exit, risk or sizing rule was changed. Live trading was not enabled and no orders were sent.

## Tests

`uv run pytest tests/test_liquidity_audit.py tests/test_liquidity_signals.py tests/test_option_pnl.py -q`: 19 passed.
