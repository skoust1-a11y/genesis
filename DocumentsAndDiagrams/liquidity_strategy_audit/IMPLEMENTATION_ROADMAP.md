# Implementation roadmap

No step below enables live trading or order placement. Every step keeps the original strategy reproducible: `scan_v` defaults plus `spec_exit_of` in `backend/scripts/audit_liquidity.py`, guarded by `test_scan_v_matches_production`.

## Phase 0: done in this audit

- [x] Production replica with 0 mismatches; repaint and early-signal checks.
- [x] Fake holiday sessions removed from the Kaggle loader.
- [x] Alert text matches the rules (no phantom 2-loss stop).
- [x] Research harness, `RESULTS.md`, `results.json`, 14 charts, 6 regression tests.

## Phase 1: hygiene (no strategy change), about 1 day

1. Decide on the working-tree v3 change (3R, breakeven at 1R, 2 OTM): revert to the 2R/ATM baseline, or keep it explicitly as the forward-test candidate. Update the docstring either way (remove "more trades fit the risk budget").
2. Lower `RISK_PCT` to 1 (owner decision; the plan workbook already says 1%).
3. Regenerate the 10-year workbook with the fixed loader. Add an "every signal, net R" sheet next to the capital-sized ledger.
4. Label all Black-76 rupee columns as "modelled (constant IV)".

## Phase 2: live robustness, 2-3 days

1. Bar completeness: use a bar only when the next bar exists, or move to Groww/Kite 5m bars (`main` L427-428).
2. Run the loop until 14:40 so 14:25/14:30 entries are evaluated; `FRESH` still blocks stale ones (L452).
3. Derive PDH/PDL from the previous session's 5m bars, the same as the backtest (L349).
4. VWAP fallback to index TWAP when the NIFTYBEES bar is missing (L130/L347).
5. One cooldown definition shared by `break_until` and the backtest ledger: net index R < 0 on every signal.
6. Tests: fake-clock tests for `FRESH` and the end of the loop; one test per fix.

## Phase 3: forward test (paper only), 6+ months

1. Run alerts unchanged, with no orders. For each alert, log the alert time, the bar close time, the quoted option premium, the premium actually obtainable (bid/ask), the index exit, and the option exit premium.
2. Track A (baseline), A + 2-pt sweep, D (displacement) and v3 exits side by side from the same bars. Rules stay frozen during the test.
3. Pre-registered pass criteria in `LIVE_TRADING_READINESS.md`.

## Phase 4: data, in parallel

1. NIFTY futures 5m bars with volume, to test configuration E (futures VWAP).
2. Intraday option data (even 1-minute closes for ATM strikes) to replace constant-IV Black-76 in the backtest.
3. NSE holidays 2016-2019 for `expiry_for`.

## Phase 5: only if Phase 3 passes

Small live size (0.5% risk, 1 lot), manual execution, a monthly review against the forward-test distribution. Automatic order placement is out of scope until 12 months of live results match the forward test.
