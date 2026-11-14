# Sprint 1 — current-main test stabilization (14 November 2026)

Baseline: `04057fd634dd8ac67eff6d376fce27e55a1bd427`, verified against fetched `origin/main` before edits. Tests use isolated source-only temporary checkouts, CPython 3.12.13, uv 0.11.2, and the existing `backend/uv.lock`. `uv sync --locked --python 3.12.13 --no-install-project` succeeds without changing either dependency manifest or lockfile. No strategy/runtime production source or frozen manifest is changed.

## Reproduction and classification

The current baseline produced **2,232 passed, 25 failed, 4 skipped** (2,261 collected) in the socket-restricted sandbox. The unchanged localhost-only test passes separately with loopback access (**1 passed**), yielding **2,233 passed, 24 failed, 4 skipped** under the applicable execution conditions. Thus the review's historical count of 25 unresolved backend failures is not the current actionable count. In particular, `test_market_provenance.py::test_session_records_code_revision_strategy_and_data_sources` passes with this repository's actual Git metadata; an archive without Git metadata is not an equivalent provenance environment. Frozen-source permission checks also pass with Git file modes preserved.

All test names below are relative to `backend/tests/`. Parameter instances are listed explicitly where they failed. Each failing group was reproduced before editing; fixes retain the existing assertions except where the test incorrectly assumed excluded callers. No fail-closed production validation was relaxed.

| Root cause | Exact failing tests | Affected functions and decision |
| --- | --- | --- |
| Synthetic calendar/lot expectation mismatch (10) | `test_backtest_integrity.py::test_expiry_follows_the_schedule_in_force_on_the_trade_date`; `test_expiry_on_a_holiday_moves_to_the_trading_day_before`; `test_lot_size_follows_the_trade_date`; `test_backtest_records_both_staged_legs`; `test_lot_size_transition_boundaries`; `test_liquidity_signals.py::test_estimated_option_plan_without_a_live_chain`; `test_market_provenance.py::test_broker_lot_agrees_with_the_dated_reference_table[2025-06-02-75]`, `[2022-06-01-50]`, `[2024-06-03-25]`, `[2024-12-02-25]` | `resolve_expiry`, `contract_lot_size`, `RuleBasedCausalSignalGenerator.generate_signals`, liquidity `backtest`, frozen `weekly_expiry`/`estimated_plan`, and `paper_ledger.lot_verified` correctly use the supplied metadata. Tests wrongly assumed historical tables in the synthetic release. Inject explicitly invented closure and dated-lot scenarios, preserving expected rollover, boundary, pricing and staged-exit results. The paper-ledger transition tests use a temporary reference file, while runtime admission tests continue using the shipped flat 65-unit table. A new test independently asserts that shipped table accepts 65 and rejects 25/75. |
| Historical golden/unavailable fixture (2) | `test_live_replay.py::test_golden_replay_is_deterministic`; `test_superseded_goldens_are_preserved_byte_for_byte` | Historical JSON goldens are deliberately absent. Keep their digest/registry/source-hash/equality assertions and conditionally skip when required artifacts are missing. Do not recreate historical evidence from current output. Existing `test_synthetic_release.py::test_full_synthetic_day_expected_outcomes_and_repeatability` actively checks independently specified candidates, admission reasons/times, exits, accounting and full replay repeatability. |
| Historical Git SHA assumption (1) | `test_candle_integrity_runner.py::test_P03_baseline_signal_fill_fee_risk_and_report_hash_parity` | The pre-V3 `af8d9f79...` source object is excluded from the independent-root repository. Skip that historical comparison only when `git cat-file -e SHA:path` confirms it unavailable. Add current-source raw-candle versus certified-snapshot parity across the entire report and report hash, asserting nonempty OOS execution. This proves current input-mode parity, not historical equivalence. |
| Excluded legacy caller expectation (1) | `test_live_suggestion_fixes.py::test_every_entry_capable_caller_supplies_checked_vix` | Static caller inventory wrongly required excluded `diagnose_trades.py` and `resimulate_since_oct15.py`. Require the exact current inventory `{scripts/live_scanner.py}`, retain its checked-VIX assertion and the frozen-manager fallback check. Any new entry-capable caller still fails the allowlist. |
| Runtime/fixture mismatch — journal (1) | `test_journal.py::test_journal_filters` | `JournalStore` filtering is correct; synthetic demo returns no `high_cost_ratio` review. Explicitly create that review through the API and assert creation before testing the tag filter. Keep outcome, date, live tag and pagination assertions. Cost-ratio classification remains independently covered in `test_reviews_improvements.py`. |
| Runtime/fixture mismatch — contract identity (9) | `test_market_provenance.py::test_a_stale_broker_lot_rejects_priced_paper_admission`; `test_a_missing_instrument_token_rejects_priced_paper_admission`; `test_a_stale_quote_is_rejected_at_admission_and_never_reaches_the_ledger`; `test_unverifiable_delta_fails_closed_without_a_paper_fill[feed_kw0-trusted0]`, `[feed_kw1-trusted1]`, `[feed_kw2-trusted2]`, `[feed_kw3-trusted3]`; `test_a_verified_contract_opens_exactly_one_lot_of_the_reference_size`; `test_signal_only_session_records_vetoes_and_local_messages_without_delivery` | `option_quote.from_chain` correctly rejects rows without underlying identity. Complete the invented chain row so tests reach their intended lot, freshness, delta and ledger checks. An absent provider token still reaches the ledger's identity rejection; an explicit null token is malformed and rejected earlier. Add a separate explicit-null test asserting `QUOTE_MISSING`, no paper decisions, no trades and no events. All original downstream safety assertions remain. |
| Genuine application defect (0) | None established | No application change is justified by these reproductions. |
| Other: execution environment (1) | `test_delivery_isolation.py::test_loopback_server_remains_available` | Socket sandbox denies localhost bind. Unchanged test passes outside that restriction. CI runs on a hosted runner; outbound guards remain enabled. |

Of the **24 actionable baseline failures**, **21 become passing tests** and **3 become explicitly unavailable historical-evidence skips**. The three historical equivalence claims remain unverified; they are not presented as repaired application regressions.

## CI contract

`.github/workflows/ci.yml` runs on pushes to main and pull requests with read-only contents permission, immutable action SHAs, no persisted checkout credential and bounded job timeouts. Backend and provisioning use Python 3.12.13 and uv 0.11.2 with `uv sync --locked`; no dependency upgrade is included.

- Backend: frozen-source verification, broad pytest excluding provisioning, then frozen-source verification even after test failure.
- Provisioning: separate job/process running unittest discovery for `test_provisioning.py`; its permanent audit hooks are retained.
- Frontend: Node 26.0.0, `npm ci --ignore-scripts`, TypeScript `--noEmit`, unit tests, production build; existing npm lockfile is unchanged.
- Release security: inspect tracked paths first and reject protected names without opening their contents; scan allowed source for common private-key, AWS, GitHub and Telegram credential signatures without printing matching values. This is a bounded source exclusion/signature check, not a claim to detect every possible secret. Eleven regression cases cover protected-path refusal, token redaction and allowed synthetic/lockfile source. Existing ignore-boundary tests remain in the backend suite.

The four pre-existing protected-state hash tests remain skipped because `--verify-protected-state` would open operational contents, which this task prohibits. No such flag was used. Offline, SQLite, collection, broker-order and Telegram guards remain intact. No production operational database, journal or risk-state contents were opened. No broker order, Telegram delivery or live trading was performed.

## Validation and delivery

Final isolated broad backend regression: **2,265 passed, 0 failed, 7 skipped**, 2,272 collected, in 470.18 seconds. Three additional protected-filename parameter cases were added while that run was underway; the complete final security test file then passed separately (**11 passed, 0 failed, 0 skipped**). Counting distinct tests across those runs, the final source has **2,268 passing cases and 7 skips**. The CI run will collect all 2,275 backend cases together.

Separate provisioning: **13 passed, 0 failed, 0 skipped**. Frontend: **3 passed, 0 failed, 0 skipped**; TypeScript and production build both exit 0. Frozen-source verification: **18/18**, before and after testing. Secret/protected-path scanner and `git diff --check` pass. No historical goldens or historical Git objects are restored, regenerated or imported.

Focused red/green evidence: metadata/security **50 passed** after the metadata corrections; runtime reproduction **16 failed, 13 passed**, then the corrected provenance module **27 passed**; replay/current input-mode parity **34 passed, 3 explicitly skipped**. The final broad run includes the additional shipped-table assertion. The full current-main baseline, rather than the focused subsets, determines the 24-failure classification above.

GitHub Actions is validated after the authorized main push; its exact job results and run URL are supplied in the delivery response. The workflow is not described as remotely passing before that run completes.

Changed files: `.github/workflows/ci.yml`; `scripts/check_release_security.py`; this report; and `backend/tests/{test_backtest_integrity,test_candle_integrity_runner,test_journal,test_liquidity_signals,test_live_replay,test_live_suggestion_fixes,test_market_provenance,test_release_security}.py`.

Reproduce locally from a source-only checkout:

```sh
cd backend
uv python install 3.12.13
uv sync --locked --python 3.12.13 --no-install-project
uv run --no-sync python frozen_source_guard.py
uv run --no-sync python -m pytest --ignore=tests/state_authority/test_provisioning.py -q -ra
uv run --no-sync python -m unittest discover -s tests/state_authority -p test_provisioning.py -v
uv run --no-sync python frozen_source_guard.py
cd ../frontend
npm ci --ignore-scripts
npx --no-install tsc --noEmit
npm test
npm run build
cd ..
python3 scripts/check_release_security.py
```
