# Sprint 1 independent verification

Date: 5 Nov 2026. Authority is the worktree at commit `33706ebad1f6d07921803e559953a70d68ad1194` (`main`, 2026-11-04 23:35:22 +0530, `feat(trading): supervise open positions and fail closed when live inputs are missing`). No application file was edited. No broker order was sent. `trades/daily_risk_config.json` was read and not written.

Dirty state at review: untracked `backend/genesis.db`, `backend/live.db`, and `trades/trades.db` only. Those files were not opened. They are not the crew journal. Sprint 1 source and the five earlier Sprint 2 notes are inside that commit, not an uncommitted overlay.

Tests named below exist. This review did not execute them. A PASS below is a source verdict. Test execution status is in `sprint1_test_coverage_gaps.md`.

## Verdict

| Task | Verdict | Confidence |
|---|---|---|
| T-01 Refuse a fill that was not received | PASS | High on source. Tests not re-run. |
| T-02 Stop the 0.5-delta rupee ratchet | PARTIAL | High. Journal and both reserve writers stopped. The formula still sits on `RiskApproval`, and several readers still turn a missing rupee into a number. |
| T-03 IST reset of risk counters | PARTIAL | High. A readable file, including a missing `session_date`, matches the task. An unreadable file is replaced with defaults. |
| T-04 Parameter agent cannot write the live file | PASS | High on source. |
| T-05 Do not overwrite a journal that failed to load | PARTIAL | High. Active-file load is fail-closed. The executed log, SQLite, and the risk save on a failed exit are not. |
| T-06 Daemon does not pretend to execute | PASS | High for `scripts/live_scanner.py`. The API order router is a different process and stays behind `LIVE_TRADING_ENABLED` defaulting to false. |

`max_loss_rupees` does not affect approve or reject. `effective_capital` is `base_capital + safety_wall_reserve` and does not change the daemon's lot count while `frozen_baseline` and `default_lots` are set. Automated live trading through the scanner daemon is off.

## Call path the daemon actually runs

`scripts/live_scanner.py` `main` constructs `TradeManager(..., is_paper_trading=True)` and calls `on_market_tick` with the Kolkata date. That method:

1. `JournalAgent.load_active_trades`. A failed load returns `JOURNAL_UNREADABLE` and does not call `begin_session`.
2. `RiskAgent.begin_session` (return value ignored).
3. For each open trade, `HoldingAgent.evaluate_holding`, then either a synthetic full exit or `ExitAgent.evaluate_exit`.
4. On a full close, `JournalAgent.record_exit`, then `RiskAgent.record_trade_outcome`, which always calls `save_config`.
5. On a new signal, `RiskAgent.evaluate_order`, then `JournalAgent.record_entry`. `contract_quote` reads an LTP for the alert. It is not stored as a fill.

`GrowwQuotes` is imported to read a chain. `backend/app/brokers/groww.py` states it does not place orders. `ExecutionManager` and `trading/orders.py` are not referenced by `live_scanner.py`.

The API path is separate. `app/live/gates.py` `live_blockers` refuses a live deployment when `settings.live_trading_enabled` is false. The default in `app/config.py` is false. `trading/orders.py` `OrderRouter._send` calls `adapter.place_order` only after that gate. `SandboxBroker` and `PaperBroker` implement `place_order`. The scanner bridge does not call them.

## T-01 Refuse a fill that was not received

**PASS.**

`BrokerBridgeAgent.place_order` in `backend/app/trading/execution_crew/broker_bridge_agent.py`:

- `is_live_broker=False` returns `success=True`, `status="SIMULATED_FILLED"`, and an `ORD_…` id. No adapter is called.
- `is_live_broker=True` returns `success=False`, `status="REJECTED"`, `order_id="UNSENT"`, `error_message="no broker adapter"`. The id is not a fill id.

`test_broker_bridge_live_flag_is_rejected`, `test_broker_bridge_simulated_fill`, and `test_execution_manager_live_flag_does_not_fill` encode this. They were not re-run.

Residual, not on the daemon path: `ExecutionManager.execute_entry` adds `1.0` to `total_slippage_points_saved` after the bridge returns, including when the status is `REJECTED`. The receipt status stays `REJECTED`. The live-flag test does not assert the slippage counter. `scripts/live_scanner.py` does not construct `ExecutionManager`.

## T-02 Stop writing 0.5-delta rupees and stop the ratchet

**PARTIAL.**

`JournalAgent.record_exit` writes `realized_points`, `realized_r`, `pnl_rupees=None`, and `pnl_method="index_points_not_priced"`. `lot_qty` is an unused argument. The assignment `points * 0.5 * quantity` is gone from this function.

`RiskAgent.PREMIUM_PNL_METHODS` and the copy in `SafetyWallAgent` are empty frozensets. `record_trade_outcome` adds 50% of `pnl_rupees` to `safety_wall_reserve` only when the method is in that set and the rupee value is not null. An unpriced win leaves the reserve number alone and still calls `save_config`, which writes `reserve_basis` (`index_points_x_0.5_frozen` unless the file already has one) and rewrites `effective_capital`.

`TradeManager` passes the closed row's `pnl_rupees` and `pnl_method` through. Exit alert text says the option premium is not priced (`alert_agent.py`). `SafetyWallAgent.process_daily_pnl` returns before reserve math when no row has a premium method, and in that branch it does not write `daily_risk_config.json`.

The formula is still computed. `evaluate_order` sets `max_loss_rupees` to `round(risk_points * 0.5 * quantity, 2)` on approval and to `0.0` on rejection. Nothing compares that field with capital.

### Readers that still turn a missing rupee into a number

These do not move the safety-wall number today, because the premium set is empty. They still present or store a rupee as if it were known.

| Location | Behavior |
|---|---|
| `PostMarketManager.run_daily_review` normalizer | `float(Est P&L or pnl_rupees or 0.0)`. `None` becomes `0.0`. The copy drops `pnl_method`. |
| `app/api/routes.py` `get_forward_tested_trades` | Excel fallback uses `Est P&L or 0`. The active-file merge, when the row has a `plan`, defaults premium `78.67` and quantity `260`. Crew rows do not have that `plan` shape, so this branch does not price them. It is still a live default. |
| `app/db/trade_store.py` `upsert_trade` | Reads `entry`, `points`, `r_multiple`, `qty`, and `side`, not the crew keys `entry_price`, `realized_points`, `realized_r`, `quantity`, and `direction`. A crew exit lands as zeros. `entry_premium` uses `or 0.0`, so a missing premium is stored as 0. `est_pnl_inr` is SQL NULL when `pnl_method` is `index_points_not_priced`. |
| `compute_metrics` | `sum(t["est_pnl_inr"])`. A NULL raises. The route's `except` then falls through to the Excel path. |
| `manager.py` trailing block | Still builds `opt_entry = round(max(40.0, risk_p * 4.5), 1)` for the trailing alert. The alert labels it estimated. It is not the reserve. |
| `scripts/diagnose_trades.py`, `resimulate_since_oct15.py`, `maintain_trades_excel.py`, `simulate_two_stage_veto_pipeline.py` | Research or workbook scripts. Not on the daemon. They are not a Sprint 1 pass. |

`test_unpriced_exit_leaves_rupees_null_and_reserve_unchanged` covers the journal and the reserve. It does not cover the readers above.

## T-03 IST reset of risk counters

**PARTIAL.**

`session_day` uses the tick's `date_str` when present, otherwise `Asia/Kolkata`. `live_scanner.py` passes `datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")`. `begin_session` runs before entries when the journal loaded.

On a date change, or when `session_date` is missing:

- `daily_losses_count` and `daily_cumulative_r` become 0.
- `is_safety_wall_locked` clears unless `manual_lock` is true.
- `safety_wall_reserve` is not recalculated.
- The next successful `save_config` writes `session_date` and `reserve_basis`.

A win, or any `realized_r > 0`, zeros the loss count. A loss increments the count only when `realized_r <= -0.5`, or when a priced rupee loss is at or below −500. Two such losses reject the next `evaluate_order`. The same date does not reset. `reset_daily_counters` still has no caller. It would reset without setting `session_date`, and it still calls `save_config`.

`trades/daily_risk_config.json` as read on 5 Nov 2026 has no `session_date` and no `manual_lock`. It has `base_capital` 50000, `safety_wall_reserve` 213954.1, `effective_capital` 263954.1, `daily_losses_count` 0, `daily_cumulative_r` 9.65, `is_safety_wall_locked` false, `default_lots` 3. The next daemon tick that reaches `begin_session` will zero 9.65, write today's Kolkata date, and leave 213954.1 in place. The file cannot say whether 9.65 belongs to today. This review did not start the daemon.

### Unreadable file

`_load_config` on any exception sets `session_blocked` and leaves the in-memory reserve at 0, because the parse never finished. `begin_session` skips the reset only when `session_date` already equals today and the session is not blocked. After a failed load, `session_date` is null, so the next tick calls `save_config` and then sets `session_blocked` back to false. The unreadable bytes are replaced with a default document whose reserve is 0. Fail-closed is not met.

A save that raises inside `begin_session` restores the in-memory counters and stays blocked. `evaluate_order` honours that block. An exit later in the same tick can still call `record_trade_outcome`, whose `save_config` is not wrapped in that restore. `on_market_tick` does not check the boolean `begin_session` returns.

`is_market_hours` in `live_scanner.py` uses a naive `datetime.now()`. The session date uses Kolkata. On a host whose zone is Asia/Kolkata they agree. They do not agree if the host zone is not.

Tests cover a readable rollover, a failed save, a manual lock, and two losses. No test loads a `{` risk file and checks the bytes.

## T-04 Parameter agent cannot write the live file

**PASS.**

`PostMarketManager.run_daily_review` calls `ParameterAgent.append_proposals` onto `trades/parameter_proposals.jsonl`. It does not write `active_strategy_params.json`. `scripts/live_scanner.py` and `scripts/run_post_market_audit.py` only call `run_daily_review`.

`append_proposals` writes `approval_status: "pending"`. If the caller passes a path whose name is `active_strategy_params.json`, the write is redirected to `parameter_proposals.jsonl`. `save_calibrated_parameters` is the same function.

The daemon still reads `active_strategy_params.json` at startup. A person can change the live file. The afternoon review cannot.

## T-05 Do not overwrite a journal that failed to load

**PARTIAL.**

`load_active_trades`: a missing file is an empty book and `load_failed` is false. A non-object, bad JSON, or an `OSError` sets `load_failed` and returns `{}`. `save_active_trades` returns without opening the file when `load_failed` is true. `record_entry` and `record_exit` return `None` in that case. `on_market_tick` returns `JOURNAL_UNREADABLE` and does not open a new trade. Tests cover a missing file, a `{` file, and a read that raises `PermissionError`.

Gaps, all read in `journal_agent.py` and `manager.py`:

- `load_failed` is one flag. A later successful load of another date clears it. A save of the date that failed can then proceed if the caller still holds the empty dict.
- `record_exit`, after a successful active-file load, replaces `live_executed_trades.json` even when that file does not parse (`executed = []`, then write). A corrupt executed log is not fail-closed.
- The executed-log write is not inside the SQLite `try`. If it raises after the active file was saved without the trade, the trade is gone from the active file and missing from the executed log. `record_trade_outcome` does not run, because the exception leaves `on_market_tick` first. That is data loss, not a double count.
- `save_active_trades` logs and swallows a write error. `record_entry` still upserts SQLite and may still rewrite `pre_market_plan.json`.
- If `record_exit` returns `None`, the manager still calls `record_trade_outcome` for a fully closed decision, then deletes the id from its in-memory dict and calls `save_active_trades`. The active file stays put only while `load_failed` is still true. The day's R still moves, and that path calls `save_config`.

## T-06 Daemon does not pretend to execute

**PASS** for the scanner process.

`scripts/live_scanner.py` does not mention `ExecutionManager`, `execute_entry`, `is_live_broker`, or "Trade Action Executed". Tick actions are printed as `SIGNAL:`. `TradeManager` is constructed with `is_paper_trading=True`. The startup banner still says the architecture includes `ExecutionCrew`. That string does not construct the class.

Removing the unused manager did not drop a call. The previous daemon did not call `execution_manager`. `scanner_crew/` has no `ExecutionManager` reference.

`test_live_scanner_does_not_construct_an_execution_manager` reads the script source. It was not re-run.

## Estimated `max_loss_rupees`

It does not influence a risk decision.

`evaluate_order` rejects on `session_blocked`, `manual_lock`, the counter lock, loss count, cumulative R at or below −2.0, stop distance above `max_stop_points` (55), and an absorption flag. It never compares `max_loss_rupees` with capital or with a loss limit. Callers check `approved` and `lots` only.

## `effective_capital` consumers

The number in the file is `base_capital + safety_wall_reserve` (263954.1 on the file read for this review). It is not broker cash.

| Consumer | Effect |
|---|---|
| `RiskAgent.save_config` | Writes the sum on every outcome and session save. |
| `RiskAgent.evaluate_order` | Uses the sum to walk `LOT_CAPITAL_MILESTONES` only when `frozen_baseline` is false or `default_lots` is null. |
| `SafetyWallAgent.process_daily_pnl` | Uses its own `base_capital + reserve_balance` the same way and rewrites `daily_risk_config.json`, dropping `session_date`, `manual_lock`, `reserve_basis`, and `daily_cumulative_r`. This write does not run while the premium set is empty. |
| `PreMarketManager` | Does not read `effective_capital`. It reads `default_lots` and `tomorrow_approved_lots`. |

The daemon's `RiskAgent` is `frozen_baseline=True` with `default_lots=3` and `max_super_lots=4`. Lot count on that object is 3, or 4 when the caller passes `is_super_trend`. `effective_capital` does not change that count. A `RiskAgent` built without `default_lots`, and without a file that supplies it, still sizes from the sum. The daemon injects the frozen agent. `EntryAgent`'s fallback `RiskAgent()` is the unfrozen shape if nobody injects the manager's agent.

`eff_safety = self.safety_wall_reserve or safety_wall_balance` treats a reserve of 0 as missing and substitutes the argument. The stored reserve is not 0.

## Strategy behavior in this commit

T-01 through T-06 do not retune a sweep threshold. The same commit does change what the daemon will take and how it will exit. These are not Sprint 1 tasks. They are in the audited SHA. This review confirmed two of them in source:

- `TradeManager.on_market_tick` sets `nifty_open_allowed` from `gamma_info.walls_live`. Missing live walls block a new Nifty entry (`VETO_MISSING_WALLS`). `GammaAgent` can fill synthetic walls and mark them not live.
- `HoldingAgent.evaluate_holding` runs before `ExitAgent`. The manager turns `PREEMPTIVE_EXIT`, `PREEMPTIVE_PROFIT`, and `FLIP_AND_REVERSE` into a full close. That is an exit path the frozen point-stop rules do not own.

Other numeric thresholds in `EntryAgent`, `TrendAgent`, and `ExitAgent` were not re-audited line by line in this pass. Do not treat the daemon as a byte-faithful frozen PDC Zone + 15 EMA card until that diff is accepted or reverted. Sprint 2 must not silently extend those changes.

`active_strategy_params.json` was not edited by this review.

## What this review did not do

The backend pytest suite was not run. The daemon was not started. Historical JSON and the risk file were not rewritten. `leg_costs` was not imported; the charge arithmetic in the financial design was recomputed from the formulas in `engine/costs.py` using code-default rates. Charge math is not a Sprint 1 booking path.
