# Sprint 1 test coverage gaps

Date: 4 Nov 2026. The 47 passing tests from the earlier session are not full validation. This file says what those tests cover, why the default pytest run looked stuck, and why the rest of `backend/tests` was not proven.

## What actually passed

The run that finished used:

```text
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
pytest --noconftest --assert=plain -p no:cacheprovider
  tests/test_sprint1_guards.py
  tests/test_pre_market_and_execution_crews.py
  tests/test_trading_and_post_market_crews.py
  tests/test_live_suggestion_fixes.py
```

That is four files. `test_sprint1_guards.py` has 9 tests. Together with the other three files the count reported earlier was 47. This review did not re-run them and does not treat 47 as the backend suite.

Those four files do not import `tests/conftest.py` fixtures (`client`, `db_url`, `demo`, `live`, `live_env`). They use `tmp_path` and `monkeypatch`. `--noconftest` does not drop any test inside those four files.

## Why the normal run stalled

This is a test-startup cost, not a trading defect, and it is not an infinite loop. It is long enough, and quiet enough, that the earlier runs were killed with no pytest line printed.

Measured in this review, from `backend/` with `backend/.venv`:

| Import | Time | What printed |
|---|---|---|
| `app.main` | 0.92s | nothing unusual |
| `tests.conftest` in a process that had just imported `app.main` | 50.12s | Alembic autogenerate plugin lines, then silence, then success |
| `from app.trading.crew.manager import TradeManager` in a fresh process | 59.62s | `Matplotlib is building the font cache; this may take a moment.` |

The chain that takes the minute is:

1. `backend/app/trading/crew/manager.py` imports `DynamicLookoutWindow` and `detect_dual_trendline_squeeze` from `app.liquidity_signals`.
2. `backend/app/liquidity_signals.py` line 28 executes `from matplotlib.figure import Figure` at import time. The same module also imports `httpx`, `GrowwQuotes`, and `load_dotenv`. None of that places an order. The chart class is what builds the font cache.
3. Matplotlib reported that `/Users/skoust/.matplotlib` is not writable and created a new temporary cache directory. A new process can pay the font-cache build again.
4. Pytest captures stdout during collection. The font-cache sentence never reaches the terminal. The run looks hung until the cache finishes or the process is killed.

The earlier terminals `842207` through `842211` match this: pytest on the crew files, no captured output, killed after about 78s to 473s. Plugin autoload was not required for the stall. `--noconftest` still collects `test_sprint1_guards.py`, which imports `TradeManager` at module level, so it still walks into `liquidity_signals`.

`app.main` itself was under a second. The API application object is not the slow import. The slow import is the crew manager pulling in the liquidity-alert module so it can use two helpers.

Classification: application import coupling, amplified by an unwritable matplotlib config and by pytest capture. A warm cache imports quickly. Do not describe a killed run as a green suite.

## What `--noconftest` omits

`backend/tests/conftest.py` is the API harness. Skipping it omits:

- The autouse fixture `_code_default_settings`, which clears LLM keys, Telegram tokens, and capital-related env vars to code defaults and clears `get_settings` cache. Without it, tests see the developer environment.
- Database fixtures `market_db_url`, `live_db_url`, `db_url`, `engine`, `live_engine`, `session`, `live_session`. They point SQLite at `tmp_path` and upgrade Alembic. `db_url` also forces `LIVE_TRADING_ENABLED` and `SANDBOX_ALLOW_LIVE` to false.
- HTTP fixtures `client`, `demo`, `approvable`, `paper_account`, `live_env`, `live`. `live_env` is the one fixture that turns `LIVE_TRADING_ENABLED` on, and it does so for the sandbox broker inside the test.

Every other file under `backend/tests/` was outside the 47-test command. That includes the API and research suites:

`test_api.py`, `test_approvals.py`, `test_audit.py`, `test_chart_analytics.py`, `test_drawdown.py`, `test_equal_signals.py`, `test_events.py`, `test_expiry_and_liquidity.py`, `test_hard_stop.py`, `test_import.py`, `test_immutability.py`, `test_intraday_liquidity.py`, `test_journal.py`, `test_langfuse.py`, `test_liquidity_audit.py`, `test_liquidity_signals.py`, `test_live.py`, `test_live_plumbing.py`, `test_llm_and_ingest.py`, `test_logging.py`, `test_loss_analyzer.py`, `test_market_data.py`, `test_markets_and_clarify.py`, `test_metrics.py`, `test_money_rules.py`, `test_news_context.py`, `test_notifications.py`, `test_odds_gate.py`, `test_opinion_eval.py`, `test_opinions.py`, `test_option_buying.py`, `test_option_pnl.py`, `test_order_flow_agent.py`, `test_policy.py`, `test_reviews_improvements.py`, `test_risk_rules.py`, `test_rules.py`, `test_scaling_law.py`, `test_scanner_crew.py`, `test_simulator.py`, `test_strategies.py`, `test_trade_crew.py`, `test_trades.py`, `test_veto.py`.

Those files are where `client`, `db_url`, `live_env`, and `demo` are used. They were not executed for Sprint 1. `--noconftest` on the full directory would error on every test that requests those fixtures, and it would drop the autouse env lock.

## Can the complete backend suite run safely?

It can be started without sending a broker order, if it is started the way the fixtures are written. This review did not start it, so this is not a pass.

Safe properties already in the tree:

- `db_url` uses a temporary SQLite file and sets `LIVE_TRADING_ENABLED=false`.
- `live_env` sets the flag true only with `SANDBOX_ALLOW_LIVE` and the in-test sandbox broker (`test_live.py`, `test_trade_crew.py`, `test_live_plumbing.py`). Those `place_order` calls are on the fake or sandbox adapter.
- The scanner bridge is not on this path.

Do not treat a full run as harmless to local state:

- `RiskAgent` and `JournalAgent` default to `trades/daily_risk_config.json` and `trades/` relative to the process cwd. The Sprint 1 tests pass `tmp_path`. A test that constructs `EntryAgent()` with no injected risk agent loads whatever `trades/daily_risk_config.json` is in the cwd. Run pytest from `backend/`, not from the repo root, so that relative path is not the production file.
- `liquidity_signals` import builds a matplotlib cache and reads `.env` via `load_dotenv`. It does not place an order. The first collection can sit for about a minute with no output.
- `test_liquidity_audit.py` and anything that calls `path_check` with the default 10,000 paths will be slow. Slowness is not a hang in the order router.
- Set `MPLCONFIGDIR` to a writable directory before the run so the font cache is not rebuilt into a fresh temp folder every process.

A full run is the right check before anyone claims the API suite is green. It is not evidence about T-01–T-06 until those assertions are in it.

## Sprint 1 assertions that are missing

Present and useful:

- Live bridge flag is `REJECTED` / `UNSENT`, paper flag stays `SIMULATED_FILLED`.
- Unpriced exit stores null rupees and `index_points_not_priced`, and a 213954.1 reserve does not move.
- Date rollover clears counters and keeps the reserve. The same date does not. Two −0.8R losses block. A rupee figure without a premium method does not increment the loss count. A failed `save_config` on rollover blocks. `manual_lock` survives the new date.
- `run_daily_review` leaves `active_strategy_params.json` byte-identical and writes `approval_status` pending.
- Missing journal is empty. Malformed and unreadable journals are not replaced. The manager will not open a trade on a bad journal.
- `live_scanner.py` source does not contain `ExecutionManager`.

Not covered, and required before calling Sprint 1 closed:

| Gap | Why it matters |
|---|---|
| A-12, truncated `daily_risk_config.json` | `begin_session` overwrites it and clears `session_blocked`. No test loads a `{` risk file and checks the bytes. |
| Production-shaped file with no `session_date` and `daily_cumulative_r` 9.65 | Rollover behaviour is inferred from code, not from a fixture with that document. The real file was not used as a fixture, on purpose. |
| `live_executed_trades.json` that does not parse | `record_exit` replaces it. No test. |
| `load_failed` cleared by a load of a different date | One flag covers every date. No test. |
| Exit when `record_exit` returns `None` | Manager still calls `record_trade_outcome`. No test. |
| `max_loss_rupees` is not a reject | No test asserts that a huge `0.5 * points * qty` still approves when the point stop is legal. |
| Daemon `frozen_baseline` ignores `effective_capital` | No test constructs the manager's risk agent and checks lots stay 3 when the reserve would buy a higher milestone. |
| Post-market normalizer turns null `pnl_rupees` into 0 | No test. The safety wall happens to ignore that 0 because `pnl_method` was dropped. |
| Trailing alert model premium | No test that the Telegram body does not contain a computed rupee stop. |
| SQLite upsert of a crew exit | No test that `entry_price` / `realized_r` survive `upsert_trade`, or that `compute_metrics` tolerates a null `est_pnl_inr`. |
| A-03, premium 100 to 110 | Not implemented. T-08 was not in this sprint. The Sprint 1 test only checks the unpriced case. |
| `ExecutionManager` slippage increment on a rejected live entry | The live test checks status only. |
| Full API suite, including tests that enable `LIVE_TRADING_ENABLED` for the sandbox | Not run. |

`test_trading_and_post_market_crews.py` still calls `record_trade_outcome` with raw rupee numbers and no premium method, and expects the reserve to stay still. That locks the empty `PREMIUM_PNL_METHODS` behaviour. It does not prove a real premium would be booked correctly. When Sprint 2 names a premium method, those tests must be updated in the same change or they will forbid the only legal reserve movement.
