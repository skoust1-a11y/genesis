# Sprint 2: independent strategy onboarding and research

Baseline inspected: `0f502c60bf9a9ffb42a82f0352d94ac71d465bab`. This extends the existing causal adapter and walk-forward engine. No frozen production sources, operational state, delivery adapters, or broker integrations are changed.

## Capability matrix

| Capability | At inspection | Result / exact boundary |
| --- | --- | --- |
| Structured markdown extraction, normalization, ambiguity quarantine | IMPLEMENTED | Existing ingest → schema → normalizer; unverified prose/LLM rule coverage stays quarantined |
| Mandatory configuration declarations | PARTIAL | Configuration v2 checks submitted fields before schema defaults, including predicate direction/timeframe and capital/sizing/session declarations; conflicting stop/target parameter declarations reject |
| Immutable baselines, variants, persistence and API | IMPLEMENTED | Existing hashes, parent bindings, conflict/corruption checks reused; v1 snapshots require explicit re-onboarding under v2 |
| Exact independent 5m rules | PARTIAL | Existing EMA relation, candle body, VWAP and daily sweep adapter reused; two different entry/target fixtures demonstrated |
| Contextual timeframes, arbitrary data feeds and optional risk rules | DEFECTIVE | Previously accepted without execution; now rejected before reading candles |
| Dynamic risk-budget lot sizing | DEFECTIVE | Previously behaved as a capped fixed request; now explicitly unsupported; declared fixed lots execute subject to existing account admission gates |
| Strategy capital and daily-loss declaration | DEFECTIVE | Capital bound to replay capital; daily realized gross loss budget enforced independently of account exposure ceilings |
| Costs, adverse spread/slippage, synthetic IV stress | DEFECTIVE | Existing accounting reused; corrected low-IV stress floor that could improve exits; invalid cost assumptions rejected |
| Historical causal replay, stop-first ambiguous intrabar resolution | IMPLEMENTED | Existing engine retained |
| Bull/bear/range research segmentation | PARTIAL | Existing regime-v2 retained; separate closed-bar research labels add transitional and realized-range volatility segments |
| Regime trading authority / regime-conditioned execution | UNSUPPORTED | Research labels grant no eligibility, order, approval, or delivery authority |
| Training, purge/embargo, OOS and holdout separation | IMPLEMENTED | Existing IS-only selection and full-coordinator/development-snapshot boundary retained |
| Final independent holdout evaluation | UNSUPPORTED | Holdout reserved, never scored by candidate runs; promotion blocked |
| Verified historical demonstration | UNSUPPORTED | Approved calendar registry is empty; timestamp-valid verified options/Greeks have not been supplied for this fixture |
| Promotion dossier / positive-backtest safety | IMPLEMENTED | Development scope, synthetic fidelity and untouched holdout all block paper authorization |
| Recency / four-arm ablation | IMPLEMENTED | Existing shadow research path inspected; not made authoritative |
| EOD sim | IMPLEMENTED | Existing separate adapter reviewed; configuration causal workflow remains 5m only |

## Trace and acceptance fixtures

`POST /api/ingest/extract` (markdown) → stored original source/version → `create_configuration` → `parse_strategy_markdown` → `normalize_markdown` → exact causal capability preflight → immutable `StrategyConfiguration` → existing research endpoint / `run_development_research` → approved dataset validation → development snapshot → `run_configuration` → existing causal walk-forward generator, cost model and account gates → metrics and research report.

The executable acceptance sources are `backend/tests/fixtures/independent_strategies/ema-cross.md` and `body-momentum.md`. A uses a 15-period EMA cross-above with a 2R target. B contains no EMA parameter or indicator: it uses a bullish candle body ratio of at least 0.4 and a fixed 40-index-point target. Both explicitly declare NIFTY long calls, 5m closed bars, a 25-point protective stop, fixed one-lot requests, ₹50,000 capital, 2% daily realized loss limit, session times, empty extra restrictions and data dependencies. No trailing management, additional confirmation, exit indicator or contextual timeframe is requested. Stops, target and square-off are the full exit policy.

Historical contract metadata determines the actual dated contract lot size; the underlying candle fixture does not prove listed contracts or executable option quotes. Synthetic pricing explicitly uses IV 0.02, Black-76, 75bps spread per side, two slippage ticks and 5% adverse exit-IV stress, with the existing date-effective fee policy. Full execution assumptions are content-bound in the research plan's walk-forward configuration and the configuration snapshot; historical brokerage assumptions remain identified as assumptions.

The test executes each strategy separately through the same workflow, repeats each full research run and asserts identical artifacts, costs, OOS evidence and immutable identities. It blocks outbound socket connections. The existing variant tests plus the regime-hypothesis test establish parent/version isolation. Bullish-target and sideways-target variants are independently versioned target hypotheses; no automatic regime switching is implemented.

## Data and reproduction

This is **synthetic software acceptance evidence**, not market-performance evidence. Data is generated by `_make_session_candles` and `_trading_calendar` in `backend/tests/test_causal_walk_forward.py`: 90 synthetic weekday sessions beginning 2026-05-04, 75 bars per day, with up/down/range patterns rotating by day. The weekday calendar is a pytest-only authority, explicitly labeled `SYNTHETIC-NOT-HISTORICAL`; it is never added to the production registry. The last five sessions are untouched final holdout. Development uses the first 85 sessions, 60-day IS, 5-day purge, 5-day embargo and 20-day OOS windows. This provides one OOS fold; it cannot establish stability across independent folds. The acceptance run retains the production minimums of three OOS folds and 30 trades; completing the pipeline does not pass those validation requirements.

Run from `backend` with the locked dependencies:

```sh
PYTHON_DOTENV_DISABLED=true SPRINT2_REPORT_PATH=/tmp/sprint2-full-report.json python -m pytest tests/test_sprint2_onboarding.py -q
```

`docs/sprint2-synthetic-results.json` retains the compact metric/provenance evidence, configuration/plan/report/fold hashes, development/full integrity receipts and holdout blockers. The optional test output contains the complete reports. The full coordinator receipt can change if holdout input changes; candidate results, development receipts and plan remain independent of valid holdout value changes (existing negative test).


## Synthetic OOS results

| Strategy | Trades | Win rate | Avg win / loss (₹) | Net expectancy (R) | Profit factor | Max drawdown (₹) | Costs (₹) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| body-momentum | 38 | 81.58% | 1556.55 / -83.19 | 2.8612 | 82.8605 | 168.81 | 4690.37 |
| ema-cross | 5 | 100.00% | 2576.36 / undefined | 3.7474 | undefined (no losses) | 0.0 | 579.74 |

Only one OOS fold exists, so multi-fold stability is unestablished. Small samples (5 and 38 trades) and synthetic prices prevent confidence in a market edge. EMA has undefined profit factor with no losses and fails that criterion. Both reports fail verified quote coverage, synthetic-tier eligibility and final-holdout requirements. Holdout eligibility and paper approval remain false. Detailed observed regime metrics and skipped-event counts are in the JSON.

## Research regimes and limitations

`research-regime-v1` uses only 5m bars closed by the decision time. Over 14 close changes, directional efficiency ≥0.6 is bullish/bearish by displacement, ≤0.3 is sideways, and intermediate values are transitional. Mean 14-bar true range divided by last close is low volatility at ≤0.05%, high at ≥0.15%, otherwise normal. Less than 15 observations stays insufficient/unknown. This realized underlying range is explicitly separate from intraday VIX; no future or EOD VIX proxy is invented. Tests cover every requested structure and low/high volatility, including future-candle perturbation.

Reports segment the exact OOS execution stream by structure, volatility, direction and timeframe. Rejected executions remain unpriced/skipped; their P&L is excluded by existing metrics. Only observed segments are reported. Group drawdowns use isolated segment sequences and are not additive portfolio drawdowns. This fixture supplies only CALL/5m executions; it cannot establish PUT or other-timeframe performance. No trade sample in a segment means no performance conclusion.

ATR/ADX entry rules, arbitrary indicator exits, trailing/partial exits, multi-timeframe execution, active hedging and HFT have no acceptance claim. Unsupported declarations fail explicitly. Existing production/paper paths are not expanded by this research workflow. New configurations that formerly relied on defaults or ignored rules may now reject and require a complete explicit specification. No final-holdout implementation or live authorization is implied by a positive synthetic result.

## Validation and publication

Local validation: the broad backend run completed with 2,352 passed, seven existing skips and two failures. One was sandbox-denied localhost binding; all seven delivery-isolation tests passed outside that restriction. The other was an acceptance-fixture dataclass error loaded before its correction; the final onboarding/configuration/research batch passed all 56 tests, including the corrected risk test and all 26 new acceptance cases. The broader strategy/fidelity/causal batch passed 85 tests before the final additional cases, and the stricter daily-loss regression passed separately. No assertion was relaxed to permit trading: the existing daily-exposure assertion now requires the stronger strategy-specific rejection.

Separate provisioning: 13 passed. Frontend TypeScript, three tests and production build passed (existing lint warnings). Release security passed after staging all new files; 18/18 frozen production hashes verified before and after testing. The local runner used Python 3.12.13 and an existing isolated environment matching all applicable locked backend versions (only platform-inapplicable colorama/httpx2-jsfetch are absent); the repository venv lacked pytest. Final full-suite confirmation and publication status are supplied by the commit's GitHub Actions run and the delivery message. No verified market-data or final-holdout claim is made.
