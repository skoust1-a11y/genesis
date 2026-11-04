# Sprint 1 independent verification

Date: 4 Nov 2026. Source of truth is the current worktree, not the implementation note from the earlier session. No application file was edited for this review. No broker order was sent. `trades/daily_risk_config.json` was read and not written.

The worktree is dirty beyond T-01–T-06. Those extra edits are called out where they change live behaviour. They were not reverted.

## Verdict

T-01, T-02, T-04, and T-06 match the task register on the paths the daemon actually runs.

T-03 matches a valid risk file, including a missing `session_date`. It does not match acceptance A-12: a risk file that fails to parse is replaced with defaults on the next tick.

T-05 protects `.active_trades_{date}.json` after a failed load. It does not protect `live_executed_trades.json`, the SQLite row, or the risk counters if the load fails between the manager's read and the exit write.

`max_loss_rupees` is still `stop_points * 0.5 * quantity`. Nothing in the approve/reject path reads it.

The daemon cannot send a broker order. Automated live trading stays off.

## T-01 Refuse a fill that was not received

**Status: met.**

`BrokerBridgeAgent.place_order` in `backend/app/trading/execution_crew/broker_bridge_agent.py`:

- `is_live_broker=False` returns `success=True`, `status="SIMULATED_FILLED"`, and an `ORD_…` id. No adapter is called.
- `is_live_broker=True` returns `success=False`, `status="REJECTED"`, `order_id="UNSENT"`, `error_message="no broker adapter"`. No adapter is called.

`test_broker_bridge_live_flag_is_rejected` and `test_execution_manager_live_flag_does_not_fill` encode A-01.

Residual, not on the daemon path: `ExecutionManager.execute_entry` still adds `1.0` to `total_slippage_points_saved` after the bridge returns, including when the bridge rejected the order. The receipt status stays `REJECTED`. `scripts/live_scanner.py` does not construct `ExecutionManager`.

## T-02 Stop writing 0.5-delta rupees and stop the ratchet

**Status: met on the journal and both reserve writers. Not met for every reader.**

`JournalAgent.record_exit` writes `realized_points`, `realized_r`, `pnl_rupees=None`, and `pnl_method="index_points_not_priced"`. The old `points * 0.5 * quantity` assignment is gone. `lot_qty` is now unused.

`RiskAgent.PREMIUM_PNL_METHODS` and `SafetyWallAgent.PREMIUM_PNL_METHODS` are empty frozensets. `record_trade_outcome` adds 50% to `safety_wall_reserve` only when the method is in that set and `pnl_rupees` is not null. An unpriced win leaves the reserve number alone and still calls `save_config`, which adds `reserve_basis` (`index_points_x_0.5_frozen` unless the file already has one).

`TradeManager` passes the closed row's `pnl_rupees` and `pnl_method` through. Exit Telegram text says option P&L is not priced. `BriefingAgent._pnl_text` prints "not priced" when `today_net_pnl` is null. `SafetyWallAgent.process_daily_pnl` returns before any reserve math when no row has a premium method.

### Readers that still turn a missing rupee into a number

These do not move the safety-wall number. They do present or store a rupee as if it were known.

| Location | What it does with an unpriced row |
|---|---|
| `PostMarketManager.run_daily_review` normalizer | `float(Est P&L or pnl_rupees or 0.0)`. `None` becomes `0.0`. The copy also drops `pnl_method`, so the safety wall then ignores it. Autopsy sums points, not this field. |
| `scripts/maintain_trades_excel.py` | Same `or 0.0` coercion. |
| `scripts/diagnose_trades.py` | `float(t.get("pnl_rupees", 0))`. A present `None` raises. A missing key becomes 0. |
| `scripts/resimulate_since_oct15.py` | Missing key becomes 0, then the script sums and win-rates that column as rupees. |
| `scripts/simulate_two_stage_veto_pipeline.py` | Still prints `total_r * 25 * 0.50 * lot_qty` as net realized rupees. Research script, not the daemon. |
| `app/api/routes.py` `get_forward_tested_trades` | The Excel fallback uses `Est P&L or 0`. The active-file merge, when the row has no `plan`, uses premium default `78.67` and quantity default `260`, then computes a rupee P&L. Crew journal rows do not have that `plan` shape. |
| `app/db/trade_store.py` `compute_metrics` | Sums `est_pnl_inr`. A SQL `NULL` from a new unpriced upsert makes that sum raise. The route's `except` then falls through to the Excel path above. |
| `AlertAgent.send_trailing_update` | Still quotes a model premium (`max(40, risk_points * 4.5)` and offsets of that). The text says "Estimated". It is not `pnl_rupees`, and it is not the reserve. |

`trade_store.upsert_trade` does keep `est_pnl_inr` null when `pnl_method` is `index_points_not_priced` or `pnl_rupees` is null. The journal row's `entry_price`, `realized_points`, and `realized_r` are not the keys the upsert reads (`entry`, `points`, `r_multiple`), so the SQLite copy of a crew exit is mostly zeros plus a null P&L. That is a ledger gap, not a revival of the 0.5 formula.

## T-03 IST reset of risk counters

**Status: met for a readable file. Failed for an unreadable file.**

`session_day` uses the tick's `date_str` when present, otherwise `Asia/Kolkata`. `live_scanner.py` passes `datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")` into `on_market_tick`. `begin_session` runs before entries.

On a date change, or when `session_date` is missing:

- `daily_losses_count` and `daily_cumulative_r` become 0.
- `is_safety_wall_locked` clears unless `manual_lock` is true.
- `safety_wall_reserve` is not recalculated.
- The next `save_config` writes `session_date` and `reserve_basis`.

A win, or any `realized_r > 0`, zeros the loss count. A loss increments the count only when `realized_r <= -0.5`, or when a priced rupee loss is at or below −500. Two such losses reject the next `evaluate_order`. The same date does not reset. `reset_daily_counters` still has no caller.

`trades/daily_risk_config.json` as read on 4 Nov 2026 has no `session_date`. It has `base_capital` 50000, `safety_wall_reserve` 213954.1, `effective_capital` 263954.1, `daily_losses_count` 0, `daily_cumulative_r` 9.65, `is_safety_wall_locked` false, `default_lots` 3. There is no `manual_lock`. The next daemon tick that reaches `begin_session` will zero 9.65, write today's Kolkata date, and add `reserve_basis`, leaving 213954.1 in place. The file cannot say whether 9.65 belongs to today or to older sessions. This review did not start the daemon, so the file is unchanged.

### Missing `session_date` versus a broken file

A readable file with no `session_date` is treated as a new session. That matches the task.

A file that raises in `_load_config` sets `session_blocked` and leaves the in-memory reserve at 0, because the parse never finished. `begin_session` only skips the reset when `session_date` already equals today and the session is not blocked. After a failed load, `session_date` is null, so the next tick calls `save_config` and then sets `session_blocked` back to false. The unreadable bytes are replaced with a default document whose reserve is 0. A-12 ("defaults are not written back") is not met. The block the load tried to set does not survive that tick.

A save that raises inside `begin_session` restores the in-memory counters and stays blocked. `evaluate_order` and the entry section of `on_market_tick` both honour that block. Exits earlier in the same tick can still call `record_trade_outcome`, whose `save_config` is not wrapped. A disk that rejects the reset can still raise on the exit.

`is_market_hours` in `live_scanner.py` still uses a naive `datetime.now()`. The session date uses Kolkata. On this machine the zone is IST, so they agree. They will not agree if the host zone is not Asia/Kolkata.

## T-04 Parameter agent cannot write the live file

**Status: met.**

`PostMarketManager.run_daily_review` calls `ParameterAgent.append_proposals` onto `trades/parameter_proposals.jsonl`. It does not call a writer of `active_strategy_params.json`. `scripts/live_scanner.py` and `scripts/run_post_market_audit.py` only call `run_daily_review`.

`append_proposals` writes `approval_status: "pending"`. If the caller passes a path whose name is `active_strategy_params.json`, the write is redirected to `parameter_proposals.jsonl`. `save_calibrated_parameters` is the same function.

The daemon still reads `active_strategy_params.json` at startup and applies `otm_steps` and `micro_wick_stop_buffer` when those keys exist. A person can still change the live file. The afternoon review cannot.

## T-05 Do not overwrite a journal that failed to load

**Status: met for the active-trade file. Not met for every save that follows an exit.**

`load_active_trades`: a missing file is an empty book and `load_failed` is false. A non-object, bad JSON, or an `OSError` sets `load_failed` and returns `{}`. `save_active_trades` returns without opening the file when `load_failed` is true. `record_entry` and `record_exit` return `None` in that case. `on_market_tick` returns `JOURNAL_UNREADABLE` and does not open a new trade. Tests cover a missing file, a `{` file, and a read that raises `PermissionError`.

Gaps:

- `load_failed` is one flag for every date. A later successful load of another date clears it. A save of the date that failed can then proceed if the caller still holds the empty dict.
- `record_exit`, after a successful load, replaces `live_executed_trades.json` even when that file itself does not parse (`executed = []`, then write). A corrupt executed log is not fail-closed.
- The SQLite upsert and the `pre_market_plan.json` thesis write are not refused when the JSON save of the active file throws. `save_active_trades` logs and swallows that error, then `record_entry` still upserts.
- If `record_exit` returns `None` because a reload failed, the manager still calls `record_trade_outcome` for a fully closed decision. The active file stays put. The day's R still moves, and that path calls `save_config`.

## T-06 Daemon does not pretend to execute

**Status: met.**

`scripts/live_scanner.py` does not mention `ExecutionManager`, `execute_entry`, `is_live_broker`, or "Trade Action Executed". Tick actions are printed as `SIGNAL:`. `TradeManager` is constructed with `is_paper_trading=True`. The only broker import on this process is `GrowwQuotes`, used to read an option chain. `groww.py` states it does not place orders. `trading/orders.py` is not imported by the daemon.

The startup banner still says the architecture includes `ExecutionCrew`. That string does not construct the class.

Removing the unused manager does not drop a call, because the previous daemon never called `execution_manager.`. Orders do not change because of the deletion. What does change in the same dirty `live_scanner.py` and `manager.py` is listed under strategy rules below.

The research stack can still place a sandbox or paper order if the API is run with `LIVE_TRADING_ENABLED` and a deployment is started. That is not the scanner daemon. Default `live_trading_enabled` in `app/config.py` is false.

## Estimated `max_loss_rupees`

It does not influence a risk decision.

`evaluate_order` sets `max_loss_rupees` to `round(risk_points * 0.5 * quantity, 2)` on an approval, and to 0 on a rejection. The function rejects on `session_blocked`, `manual_lock`, the counter lock, loss count, cumulative R, stop distance above `max_stop_points` (55), and an absorption flag. It never compares `max_loss_rupees` with capital or with a loss limit.

Callers check `approved` and `lots` only: `TradeManager.on_market_tick` and `EntryAgent` sweep, long, and waterfall paths.

## `effective_capital` consumers

The number in the file is `base_capital + safety_wall_reserve` (263954.1 today). It is not broker cash.

| Consumer | Effect today |
|---|---|
| `RiskAgent.save_config` | Writes the sum on every outcome and session save. |
| `RiskAgent.evaluate_order` | Uses the sum to walk `LOT_CAPITAL_MILESTONES` only when `frozen_baseline` is false or `default_lots` is null. |
| `SafetyWallAgent.process_daily_pnl` | Uses its own `base_capital + reserve_balance` the same way, and would rewrite `daily_risk_config.json`, only after a priced row. The premium set is empty, so this write does not run. The early return also leaves the safety-wall state file unchanged. |
| `trades/daily_risk_config.json` | Stores the last written sum. `PreMarketManager` does not read this key. It reads `default_lots` and `tomorrow_approved_lots`. |

The daemon's `RiskAgent` is `frozen_baseline=True` with `default_lots=3` and `max_super_lots=4`. Lot count on that object is 3, or 4 when the caller passes `is_super_trend`. `effective_capital` does not change that count. A `RiskAgent` built without `default_lots`, and without a file that supplies it, still sizes from the sum. `EntryAgent`'s fallback `RiskAgent()` is that shape if nobody injects the manager's agent. The daemon injects it.

`eff_safety = self.safety_wall_reserve or safety_wall_balance` treats a reserve of 0 as missing and substitutes the argument. The stored reserve is not 0, so the daemon does not hit this.

## Strategy rules in the same dirty tree

T-01–T-06 do not retune a sweep threshold. The uncommitted tree around them does change what the daemon will take and how it will exit. These are not Sprint 1 tasks. They are in the worktree now.

- `TradeManager.on_market_tick` sets `nifty_open_allowed` from `gamma_info.walls_live`. Missing live open interest blocks a new Nifty entry (`VETO_MISSING_WALLS`). `GammaAgent` fills synthetic walls when OI is absent and marks them not live.
- A new `HoldingAgent` runs before `ExitAgent` on every open trade. It can move the stop, emit `PREEMPTIVE_EXIT`, `PREEMPTIVE_PROFIT`, or `FLIP_AND_REVERSE`.
- `ExitAgent` time-stop: a breakeven trade with `current_r >= 0.2` now gets 10 bars instead of being exempt at `current_r >= 0.5`. Below +1.8R it still exits.
- `TrendAgent`: a short ADX sample returns 0 instead of 22. Level clearance uses 8 points instead of 2.
- `EntryAgent`: zero volume is no longer treated as a surge. Super-trend sizing requires at least 28 bars and ADX at least 25. A gamma reversal no longer counts as super-trend by itself. A live chain can reject a strike whose delta is under 0.35.
- `detector_agent.py` no longer passes `is_super_trend=True` into sweep, retest, and trendline candidates. Super-trend lots now depend on the manager's ADX check.
- `OrderFlowAgent` reads Groww `liveData.oi`. Zero volume is not a surge.
- Manager VWAP filter vetoes a non-sweep PUT more than 8 points above session VWAP, and a non-sweep CALL more than 8 points below it.
- `path_check` (10,000 paths) is stored on the signal and does not approve or block. A throw there aborts the tick. It is extra work on the entry path, not a new threshold.

`active_strategy_params.json` was not edited by this review.

## What this review did not do

The full backend pytest suite was not run. The daemon was not started. Historical JSON and the risk file were not rewritten. Charge math for the 100-to-110 example was not executed, because Sprint 1 does not book premiums (that is T-08).
