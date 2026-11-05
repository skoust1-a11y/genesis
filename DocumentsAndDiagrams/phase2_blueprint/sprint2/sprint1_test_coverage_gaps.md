# Sprint 1 test coverage gaps

Date: 5 Nov 2026. Corrected the same day so the named gaps match the Sprint 2 acceptance ids. Corrected again the same day for the loss-count test conflict. Commit `33706ebad1f6d07921803e559953a70d68ad1194`. The tests named here were not executed in this review. A source reading is not a green suite.

## What was timed, and what was not run

From `backend/` with `backend/.venv`, `MPLCONFIGDIR=/tmp/genesis-mpl-audit`:

| Import | Time | What printed |
|---|---|---|
| `import app.trading.crew.manager` in a fresh process | 13.07s | `Matplotlib is building the font cache; this may take a moment.` |
| The same import in a second process with that cache already built | 1.37s | nothing |

`app.main` was not re-timed in this pass. The 4 Nov note reported about 0.92s for `app.main` and about 50s for `tests.conftest` after Alembic. Those two figures were not reproduced here. Do not quote them as this review's measurements.

The four-file run cited on 4 Nov (`test_sprint1_guards.py`, `test_pre_market_and_execution_crews.py`, `test_trading_and_post_market_crews.py`, `test_live_suggestion_fixes.py`, 47 tests, `--noconftest`) was not repeated. This review does not treat 47 as a pass and does not treat it as the backend suite.

No test in this review constructed `RiskAgent` or `JournalAgent` on the repo `trades/` directory.

## Why a normal pytest run looks stalled

This is import coupling plus pytest capture. It is not an infinite loop in the order router, and it is not evidence of a trading defect.

Chain, confirmed in source:

1. `backend/tests/test_sprint1_guards.py` imports `TradeManager` at module level.
2. `backend/app/trading/crew/manager.py` imports `DynamicLookoutWindow` and `detect_dual_trendline_squeeze` from `app.liquidity_signals`.
3. `backend/app/liquidity_signals.py` line 28 executes `from matplotlib.figure import Figure` at import time. The same module imports `httpx`, `GrowwQuotes`, and `load_dotenv`. None of that places an order. The chart import is what builds the font cache.
4. Pytest captures stdout during collection. The font-cache sentence never reaches the terminal. A killed run with no summary is not a pass.

`--noconftest` does not avoid this chain. It skips `backend/tests/conftest.py`. It does not skip a test module that imports `TradeManager`.

`backend/pyproject.toml` sets `testpaths = ["tests"]` and `pythonpath = ["."]`. It sets no `addopts`. There is no pytest timeout. A quiet collection is indistinguishable from a hang until the cache finishes or the process is killed.

Classification: application import coupling, amplified by a cold Matplotlib font cache and by pytest capture. A warm cache imported in 1.37s here. The first process paid 13.07s and printed the cache line because stdout was not captured. A cold cache on a machine that cannot write `~/.matplotlib` can take longer than 13s. That longer figure was not re-measured.

## What `--noconftest` omits

`backend/tests/conftest.py` is the API harness. Skipping it omits:

- The autouse fixture `_code_default_settings`, which clears LLM keys, Telegram tokens, and capital-related env vars to code defaults and clears the `get_settings` cache. Without it, tests see the developer environment.
- Database fixtures `market_db_url`, `live_db_url`, `db_url`, `engine`, `live_engine`, `session`, `live_session`. They point SQLite at `tmp_path` and upgrade Alembic. `db_url` also forces `LIVE_TRADING_ENABLED` and `SANDBOX_ALLOW_LIVE` to false.
- HTTP fixtures `client`, `demo`, `approvable`, `paper_account`, `live_env`, `live`. `live_env` is the fixture that turns `LIVE_TRADING_ENABLED` on, and it does so for the sandbox broker inside the test.

`--noconftest` on the four crew files drops no test inside those files. They use `tmp_path` and `monkeypatch`. `--noconftest` on the whole `tests/` directory fails every test that requests a conftest fixture, and it drops the autouse env lock.

## Files outside the four-file command

Every other file under `backend/tests/` was outside that command. Counted in this review: 49 `test_*.py` files. The four above were the only ones in the earlier command. The other 45:

`test_api.py`, `test_approvals.py`, `test_audit.py`, `test_chart_analytics.py`, `test_drawdown.py`, `test_equal_signals.py`, `test_events.py`, `test_expiry_and_liquidity.py`, `test_hard_stop.py`, `test_import.py`, `test_immutability.py`, `test_intraday_liquidity.py`, `test_journal.py`, `test_langfuse.py`, `test_liquidity_audit.py`, `test_liquidity_signals.py`, `test_live.py`, `test_live_plumbing.py`, `test_llm_and_ingest.py`, `test_logging.py`, `test_login.py`, `test_loss_analyzer.py`, `test_market_data.py`, `test_markets_and_clarify.py`, `test_metrics.py`, `test_money_rules.py`, `test_news_context.py`, `test_notifications.py`, `test_odds_gate.py`, `test_opinion_eval.py`, `test_opinions.py`, `test_option_buying.py`, `test_option_pnl.py`, `test_order_flow_agent.py`, `test_policy.py`, `test_reviews_improvements.py`, `test_risk_rules.py`, `test_rules.py`, `test_scaling_law.py`, `test_scanner_crew.py`, `test_simulator.py`, `test_strategies.py`, `test_trade_crew.py`, `test_trades.py`, `test_veto.py`.

`test_login.py` was missing from the 4 Nov list. It is included here. None of these 45 were executed for Sprint 1.

## Can the complete backend suite run safely?

It can be started without sending a broker order, if it is started the way the fixtures are written. This review did not start it. That is not a pass.

Safe properties already in the tree:

- `db_url` uses a temporary SQLite file and sets `LIVE_TRADING_ENABLED=false` and `SANDBOX_ALLOW_LIVE=false`.
- `live_env` sets the flag true only with `SANDBOX_ALLOW_LIVE` and the in-test sandbox broker. Those `place_order` calls are on the fake or sandbox adapter.
- The scanner bridge is not on this path. `BrokerBridgeAgent` with `is_live_broker=True` returns `REJECTED` and does not call an adapter.

Do not treat a full run as harmless to local state:

- `RiskAgent` and `JournalAgent` default to `trades/daily_risk_config.json` and `trades/` relative to the process cwd. Sprint 1 tests pass `tmp_path`. A test that constructs `RiskAgent()` or `EntryAgent()` with no path loads whatever `trades/daily_risk_config.json` is in the cwd. Run pytest from `backend/`, not from the repo root, so that relative path is not the production file. Even from `backend/`, a test that uses the default path and the process cwd is the repo root will hit production. Prefer tests that always pass `tmp_path`.
- `liquidity_signals` import builds a Matplotlib cache and reads `.env` via `load_dotenv`. It does not place an order.
- `test_liquidity_audit.py` and anything that calls `path_check` with the default 10,000 paths will be slow. Slowness is not a hang in the order router.
- Untracked `backend/genesis.db`, `backend/live.db`, and `trades/trades.db` must not be the databases under test. The fixtures use `tmp_path`. A test that opens a relative `data/trades.db` or `genesis.db` is not isolated. Do not point pytest at those untracked files.

## Safe command

Run from `backend/`. Do not export `LIVE_TRADING_ENABLED`. Do not pass a production database URL.

```text
cd backend
export MPLCONFIGDIR="$PWD/.mplconfig"
export LIVE_TRADING_ENABLED=false
export SANDBOX_ALLOW_LIVE=false
mkdir -p "$MPLCONFIGDIR"
.venv/bin/python -m pytest -p no:cacheprovider --durations=30
```

Expected first collection pays the font cache once into `backend/.mplconfig` (gitignored or left untracked; do not commit it). Later processes should be near the 1.4s import, plus Alembic upgrades per test that uses `db_url`. The full directory was not timed. Quote a result only if the process prints a pytest summary. A killed run is not a pass.

Add `--ignore` only for a file that is shown to open `trades/daily_risk_config.json` or a repo database. Do not ignore the Sprint 1 files.

The production risk file and journals stay untouched if every risk and journal test keeps using `tmp_path`. Before the run, record the mtime of `trades/daily_risk_config.json` and `trades/live_executed_trades.json`. After the run, those mtimes must be unchanged. If they change, stop and do not "fix" the files.

## Sprint 1 assertions present in source, not re-executed

- Live bridge flag is `REJECTED` / `UNSENT`. Paper flag stays `SIMULATED_FILLED`.
- Unpriced exit stores null rupees and `index_points_not_priced`. A reserve fixture does not move. The test exists; this review did not run it.
- Date rollover clears counters and keeps the reserve. The same date does not. Two losses block. A failed `save_config` on rollover blocks. `manual_lock` survives the new date. The win-clear inside `test_ist_session_resets_on_date_change_and_blocks_when_save_fails` is Sprint 1 source and is superseded by the Sprint 2 loss-count rule.
- `run_daily_review` leaves `active_strategy_params.json` byte-identical and writes `approval_status` pending.
- Missing journal is empty. Malformed and unreadable active journals are not replaced. The manager will not open a trade on a bad journal.
- `live_scanner.py` source does not contain `ExecutionManager`.

## Assertions that do not exist

| Gap | Why it matters |
|---|---|
| Truncated `daily_risk_config.json` | `begin_session` overwrites it and clears `session_blocked`. No test loads a `{` risk file and checks the bytes. |
| Production-shaped file with no `session_date` and `daily_cumulative_r` 9.65 | Rollover is visible in `begin_session`. The real file was not used as a fixture, on purpose. |
| `live_executed_trades.json` that does not parse | `record_exit` replaces it with `[]`. No test. |
| `load_failed` cleared by a load of a different date | One flag covers every date. No test. |
| Exit when `record_exit` returns `None` | Manager still calls `record_trade_outcome`. No test. |
| Executed-log write raises after the active file dropped the trade | Trade disappears from both files. No test. |
| `max_loss_rupees` is not a reject reason | No test asserts that a huge `0.5 * points * qty` still approves when the point stop is legal. |
| Daemon `frozen_baseline` ignores `effective_capital` | No test constructs the manager's risk agent and checks lots stay 3 when the reserve would buy a higher milestone. |
| Post-market normalizer turns null `pnl_rupees` into 0 | No test. |
| Trailing alert model premium | No test that the body is labeled estimated and is not the reserve. |
| SQLite upsert of a crew exit | No test that `entry_price` / `realized_r` survive `upsert_trade`, or that `compute_metrics` tolerates a null `est_pnl_inr`. |
| Premium 100 to 110 against `leg_costs` | Not a Sprint 1 booking. No test. |
| `ExecutionManager` slippage increment on a rejected live entry | The live test checks status only. |
| Full API suite, including tests that enable `LIVE_TRADING_ENABLED` for the sandbox | Not run. |
| `process_daily_pnl` given `broker_premium_net` | No test that `trades/safety_wall_state.json` and the risk file stay unchanged. Sprint 2 acceptance is B-05. The empty premium set only hides the writer. |
| Loss count uses `realized_r <= -0.5` or `pnl_rupees <= -500`, and a later win clears it | Sprint 1 source. The Sprint 2 specification removes the ₹500 stand-in and the win-clear. No test that a negative broker net increments `daily_losses_count` once per `trade_id` with `loss_counted_trade_ids` in the same replace, or that a duplicate, a restart, a zero, a positive, a paper, a simulated, an estimated, or an unpriced row does not change it, or that a new `session_date` resets it once without dropping the marker. Sprint 2 acceptance is B-26, subcases B-26a through B-26w. |

`test_trading_and_post_market_crews.py` calls `record_trade_outcome` with raw rupee numbers and no premium method and expects the reserve to stay still. That locks the empty `PREMIUM_PNL_METHODS` behaviour. It does not prove a real premium would be booked correctly. When Sprint 2 names a premium method, those tests must be updated in the same change or they will forbid the only legal reserve movement. That movement is `round(net * 0.50, 2)` of a new positive `broker_premium_net`, once per `trade_id`, inside `record_trade_outcome` only, in the same atomic replace as `reserve_posted_for`. B-05 forbids `process_daily_pnl` from posting it or rewriting either marker. B-26, including B-26a through B-26w, is the idempotency, loss-count lifetime, and atomic-posting row. The ₹500 comparison in source is not that rule.

`backend/tests/test_sprint1_guards.py` `test_ist_session_resets_on_date_change_and_blocks_when_save_fails` asserts that a subsequent win clears `daily_losses_count`. The calls are `record_trade_outcome(is_win=False, pnl_rupees=None, realized_r=-1.0)` and then `record_trade_outcome(is_win=True, pnl_rupees=None, realized_r=0.4)`, and the test expects `daily_losses_count == 0`. The corrected specification supersedes that assertion. A later win and a positive `realized_r` do not clear the count. The same test also expects unpriced `realized_r=-0.8` calls to increment the count. That is the Sprint 1 increment, not a broker-confirmed loss. S2-02 must update those outcome expectations. This documentation pass does not modify the test. The test's successful new date, same-date keep, and failed-save restore stay.
