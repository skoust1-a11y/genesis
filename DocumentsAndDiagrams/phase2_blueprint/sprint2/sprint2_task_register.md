# Sprint 2 task register

Date: 5 Nov 2026. Corrected the same day for reserve ownership, the loss-count rule, the S2-V3 checklist, and the `insufficient_data` token. Corrected again the same day for loss-count lifetime and atomic reserve posting. Do not start until this register is approved. No task places an order, edits `active_strategy_params.json`, rewrites a historical trade, changes a PDC Zone or 15 EMA rule, or enables `LIVE_TRADING_ENABLED`.

Detail is in `sprint2_financial_integrity_design.md` and `recency_intelligence_architecture.md`. Acceptance ids are in `sprint2_acceptance_matrix.md`.

P1 and P2 must not merge in a way that weakens a P0 check. Recency code does not import the broker bridge. The risk gate does not read conviction.

Complexity: S under a day, M a few days.

## P0 Financial integrity

### S2-04 Risk file fail-closed

- **Now:** A risk file that does not parse is replaced on the next `begin_session`. `save_config` has no `config_unreadable` guard. `on_market_tick` ignores the boolean from `begin_session`. `reset_daily_counters` is unused and would save without `session_date`.
- **Change:** On a load exception, set `config_unreadable` and `session_blocked`. `save_config` returns without writing. `begin_session` returns false and does not write. `on_market_tick` does not let an exit call `record_trade_outcome` while the flag is set. Delete `reset_daily_counters`. `save_config` replaces the risk file atomically (temp file, fsync, `os.replace`) and does not decide a credit or a loss count. `begin_session` resets `daily_losses_count` only when that replace successfully establishes a new `session_date`. A failed save restores the count. The same `session_date` does not reset it. Neither path clears `loss_counted_trade_ids` or `reserve_posted_for`. `begin_session` and `record_trade_outcome` share the exclusive lock on `trades/daily_risk_config.json.lock`.
- **Files:** `backend/app/trading/crew/risk_agent.py` (`_load_config`, `save_config`, `begin_session`, `reset_daily_counters`). `backend/app/trading/crew/manager.py` (`on_market_tick` session branch).
- **Depends:** none.
- **Sequence:** first.
- **Failure:** truncated file bytes unchanged. A save error on a readable rollover restores counters, including `daily_losses_count`, and stays blocked. Marker lists stay.
- **Tests:** B-01, B-02, B-03, B-04, B-26g, B-26h, B-26i.
- **Accept:** unreadable risk file is still unreadable after a tick. Reserve number identical. Entries blocked.

### S2-06 Journal fail-closed on every save path

- **Now:** Active-file load is fail-closed. `load_failed` is one flag. `record_exit` replaces a corrupt `live_executed_trades.json`. A failed `record_exit` still reaches `record_trade_outcome`. SQLite upsert runs after a swallowed JSON error.
- **Change:** `load_failed` is per path. Do not rewrite the executed log on parse failure. If `record_exit` returns `None`, do not update risk counters and do not delete the open row. Upsert SQLite only after the JSON write succeeds.
- **Files:** `journal_agent.py` (`load_active_trades`, `save_active_trades`, `record_entry`, `record_exit`). `manager.py` (full-close branch around `record_exit`).
- **Depends:** none. Parallel with S2-04.
- **Sequence:** first.
- **Failure:** corrupt executed log bytes unchanged. Active trade remains. Reserve unchanged.
- **Tests:** B-06, B-07, B-08.
- **Accept:** no save path replaces a file it could not read.

### S2-07 Capital model

- **Now:** `effective_capital = base + reserve`. `evaluate_order` walks milestones with that sum when not frozen. `SafetyWallAgent.process_daily_pnl` rewrites the risk file once a premium method exists, and can drain the reserve on a loss.
- **Change:** Stop writing `effective_capital`. Ignore it on old files. Add `cash_inr: null`, `margin_inr: null`, `risk_fraction: 0.02` only on a save of a file that loaded. Copy `safety_wall_reserve` through. Copy `loss_counted_trade_ids` and `reserve_posted_for` through. A file that lacks them gains empty lists. Do not replay historical trades into them. Delete the milestone walk. `process_daily_pnl` may report daily P&L and must not credit, debit, or rewrite reserve ownership. It does not write `daily_risk_config.json` or `trades/safety_wall_state.json` for unpriced rows, paper nets, simulated outcomes, or a `broker_premium_net` row. It does not write either marker list. Reserve updates occur only in `RiskAgent.record_trade_outcome`. Deployable cash is `base_capital` until `cash_inr` exists. Estimated P&L is not added.
- **Files:** `risk_agent.py` `save_config`, `evaluate_order`. `safety_wall_agent.py` `process_daily_pnl`.
- **Depends:** S2-04.
- **Sequence:** after S2-04.
- **History:** 213954.1 is not recomputed. Open trades keep their lots.
- **Failure:** an unpriced day, a paper net, and a `broker_premium_net` row passed only through `process_daily_pnl` leave the state-file reserve and the risk-file reserve unchanged. A loss on that path does not reduce or increase either reserve.
- **Tests:** B-02, B-05, B-22, B-24, B-26t.
- **Accept:** reserve is not in the sizing inequality. `effective_capital` is absent from a new save.

### S2-01 Canonical execution ledger

- **Now:** `record_entry` stores the spot signal. `contract_quote` is used for alerts. `record_exit` always closes the whole trade and sets rupees null. `upsert_trade` reads the wrong keys and coerces a missing premium to 0.
- **Change:** Append-only `executions` on the existing journal object. Fields in the financial design. Duplicate `(price_kind, id)` ignored. FIFO derivation. Partial sell leaves status `PARTIAL`. An LTP may be stored only as `reference_premium` with `reference_kind="ltp"`. It is not an execution. `upsert_trade` reads crew keys and writes NULL for a missing premium. Make `entry_premium` nullable. Do not backfill.
- **Files:** `journal_agent.py` `record_entry`, `record_exit`. `manager.py` only to pass a reference quote through, not to synthesize a fill. `trade_store.py` `upsert_trade` and the `entry_premium` column.
- **Depends:** S2-04 and S2-06.
- **Sequence:** after the fail-closed saves.
- **History:** rows without `executions` stay as they are.
- **Failure:** duplicate execution does not change derived gross. Restart recomputes from the list and does not post the reserve or the loss count. Those posts are the risk-file markers, not the execution list.
- **Tests:** B-09, B-10, B-11.
- **Accept:** active JSON and the SQLite summary agree on null versus a real execution. No model premium is written.

### S2-02 Five P&L books via `leg_costs`

- **Now:** exit always sets `pnl_rupees` null and `index_points_not_priced`.
- **Change:** Derive estimated unrealized, paper gross, broker gross, charges, and broker net as specified. Broker charges use slippage 0. Paper charges use the settings slippage and store it separately. `broker_premium_net` is the only new member of `PREMIUM_PNL_METHODS`. `record_trade_outcome` is the logical owner. It credits `round(net * 0.50, 2)` of a positive broker net once per `trade_id`, and it increments `daily_losses_count` once per unique negative broker-confirmed net whose journal `date_str` equals the current `session_date`. The balance and `reserve_posted_for`, or the count and `loss_counted_trade_ids`, commit together in one `save_config` replace under the lock from S2-04. A failed replace returns false, restores memory from the last complete document, and is not a successful post. A retry of an uncommitted same-session outcome commits once. A duplicate, a restart, an overlapping call, or a replay after rollover does not apply a committed id again. A win, `is_win`, and `realized_r > 0` do not clear the count. A zero net does not change it. Paper, simulated, estimated, and unpriced outcomes do not change the count or the reserve. A loss does not reduce or increase `safety_wall_reserve`. The ₹500 loss stand-in is removed. `realized_r <= -0.5` does not increment the count. `leg_costs` raising leaves net null and does not move the reserve. Points and R stay on the R circuit breaker, which is not the loss count and is not `max_daily_losses`. The manager passes the existing `trade_id` and journal `date_str`. It does not treat a false return as posted.
- **Files:** `journal_agent.py` derivation next to `record_exit`. `engine/costs.py` call only. `risk_agent.py` `record_trade_outcome`. `manager.py` passes the method, `trade_id`, and `date_str` through and skips the call when the exit did not persist.
- **Test conflict:** `backend/tests/test_sprint1_guards.py` `test_ist_session_resets_on_date_change_and_blocks_when_save_fails` expects a later win (`is_win=True`, `realized_r=0.4`) to clear `daily_losses_count`, and expects unpriced `realized_r=-0.8` calls to increment it. Those outcome assertions are superseded. Update them in this task so a broker-confirmed loss stays counted after a later win, and so an unpriced R does not increment the count. Do not edit that test in the documentation pass. Do not weaken its date-change, same-date, or failed-save assertions.
- **Depends:** S2-01.
- **Sequence:** same change as S2-01, or the next commit.
- **History:** numeric `pnl_rupees` without a method stays on disk and is not a broker method.
- **Tests:** B-11, B-12, B-13, B-14, B-15, B-22, B-26, B-26a, B-26b, B-26c, B-26d, B-26e, B-26f, B-26j, B-26k, B-26l, B-26m, B-26n, B-26o, B-26p, B-26q, B-26r, B-26s, B-26u, B-26v, B-26w.
- **Accept:** the 100 to 110 broker example matches `leg_costs` with slippage 0. An LTP does not. A negative broker net increments `daily_losses_count` once, with its marker in the same replace, and does not move the reserve. A later win does not clear that count.

### S2-03 Readers treat null as unknown

- **Now:** post-market normalizer, API active merge, and `compute_metrics` turn a missing rupee into 0, into 78.67, or into an exception. Trailing alerts use `risk_points * 4.5`.
- **Change:** Pass null through. Copy `pnl_method`. Metrics set `pnl_status` incomplete and do not raise. Trailing text says estimated only when a reference premium exists, and not priced otherwise. Frontend home and forward-tested pages render null as not priced.
- **Files:** `post_market_manager.py`, `trade_store.py` `compute_metrics`, `app/api/routes.py` `get_forward_tested_trades`, `alert_agent.py`, `manager.py` trailing premium, `frontend/src/app/page.tsx`, `frontend/src/app/forward-tested/page.tsx`.
- **Depends:** S2-02.
- **Sequence:** after S2-02.
- **History:** workbook `Est P&L` stays legacy. `analyzer.py` catalog literals are out of scope.
- **Failure:** a mixed day is incomplete, not a partial sum labeled as the day.
- **Tests:** B-16.
- **Accept:** no production reader of the crew journal uses `or 0.0` on `pnl_rupees`.

### S2-05 Rupee gate and position size

- **Now:** lots are 3 or 4. `max_loss_rupees` is `points * 0.5 * qty` and is ignored. Missing premium still approves.
- **Change:** Implement the two-constraint gate in the financial design. Ceiling stays the frozen lots. Missing premium, missing premium stop, or missing lot size rejects. Premium-exposure budget is optional and is not defaulted to 1,000. Risk budget is `base_capital * risk_fraction` and caps planned loss including one `leg_costs` round trip. Available funds are `base_capital` or `cash_inr`, never the reserve. `max_loss_rupees` is maximum premium loss. `planned_loss_rupees` is the stop path including charges.
- **Files:** `risk_agent.py` `evaluate_order`. Callers already honour `approved` and `lots`.
- **Depends:** S2-07 and S2-02 so charges are the real function. Can be coded after S2-03.
- **Sequence:** last among P0.
- **History:** open trades keep their lots.
- **Failure:** explicit premium budget 1,000, premium 10, lot 65, frozen 3, stop premium 8: size becomes 1 lot, not 3 and not 0. Two lots are illegal under that budget. Planned gross on one lot is 130. Maximum loss on one lot is 650.
- **Tests:** B-17, B-18, B-19, B-20, B-21, B-23, B-25.
- **Accept:** lot count is never above the frozen rule. The 0.5 formula is gone from `RiskApproval`. The reserve is not in the inequality.

## P1 Recency intelligence

Shadow only. These tasks do not change `evaluate_order`, `record_entry` conditions, or exit rules.

### S2-R1 Price-action evidence

- **Now:** bars and `compute_session_vwap` exist. Nothing scores their age.
- **Change:** `recency.py` builds observation records from closed 5-minute and 15-minute bars. Future bars are dropped.
- **Files:** new `backend/app/trading/crew/recency.py`. Read-only use of frames already passed to `on_market_tick`.
- **Depends:** none in P0. Do not start until S2-05 is merged if the same manager lines would conflict. Otherwise parallel after S2-04.
- **Tests:** R-01, R-02, R-04.
- **Accept:** a bar timestamp after the decision is absent. A missing timestamp scores freshness 0.

### S2-R2 Event evidence

- **Now:** `NewsAgent.analyze_headlines` scores keywords. Repeats add. `load_event_yaml` loads the calendar. No first-observed time, no dedup.
- **Change:** Event records as specified. Dedup key on normalized title, source, and 15-minute bucket. Do not change `check_event_risk_guard` or detector blackout behaviour.
- **Files:** `news_agent.py` (a function that returns records; the guard stays). `app/market/news.py` only if a timestamp is already parsed and not surfaced. `recency.py` consumes the records.
- **Depends:** S2-R1 for the shared evidence type.
- **Tests:** R-03, R-12.
- **Accept:** a second copy of the same headline does not increase mass. An event first observed after the decision is excluded.

### S2-R3 Decay and independence

- **Now:** no decay.
- **Change:** \(W(t)=e^{-\lambda t}\) with the documented priors. Structural floor for previous-day levels. Cluster independence keeps one member.
- **Files:** `recency.py`.
- **Depends:** S2-R1, S2-R2.
- **Tests:** R-01, R-04, R-05.
- **Accept:** half-life math matches \(\ln 2 / \lambda\). Priors are labeled priors. Correlated pieces are not double counted.

### S2-R4 Conviction

- **Now:** `NewsSentiment.sentiment_score` is a single float.
- **Change:** Bull and bear masses, conflict, bands `low|medium|high`. No percentage.
- **Files:** `recency.py`.
- **Depends:** S2-R3.
- **Tests:** R-06.
- **Accept:** output has no confidence percentage. Missing inputs use directional classification `insufficient_data`.

### S2-R5 Entry shadow

- **Now:** `on_market_tick` journals after the risk approval.
- **Change:** After the action is chosen, append a shadow record. Do not read it. Hypothetical `WOULD_WAIT` or `WOULD_NO_TRADE` does not block `record_entry`.
- **Files:** `manager.py` hook. `recency.py` `assess_entry`.
- **Depends:** S2-R4. Must run after the P0 gate exists so the recorded `strategy_action` includes a rupee reject.
- **Tests:** R-07, R-08.
- **Accept:** strategy fields on the signal are unchanged. No broker import in `recency.py`.

### S2-R6 Position shadow

- **Now:** `HoldingAgent.evaluate_holding` can already force a full exit. That path stays. Recency does not join it.
- **Change:** After `evaluate_holding` returns, classify `strengthening|stable|weakening|invalidated|insufficient_data`. Do not call `record_exit` from that classification.
- **Files:** `manager.py` next to the holding call. `recency.py` `assess_position`.
- **Depends:** S2-R4.
- **Tests:** R-09, R-10.
- **Accept:** one opposing observation does not change the exit action.

### S2-R7 Shadow log and failures

- **Now:** no log.
- **Change:** Append `trades/recency_shadow.jsonl` with the fields in the architecture, including `orders_sent: false`. On an exception, data quality, directional classification, and the exception log are `insufficient_data`. The tick does not fail.
- **Files:** `manager.py`, `recency.py`.
- **Depends:** S2-R5, S2-R6.
- **Tests:** R-08, R-11.
- **Accept:** a thrown scorer leaves the strategy action list intact apart from a log-failure note. The risk gate still runs.

## P2 Validation and activation

### S2-V1 Four-config research harness

- **Now:** `engine/sim.py` and research scripts score the strategy without this layer.
- **Change:** A research entry point runs the four configurations in the architecture on the same bars and the same `leg_costs`. It writes a report under `reports/`. It does not write `trades/daily_risk_config.json` or `active_strategy_params.json`.
- **Depends:** S2-R3 and S2-02 so costs match production.
- **Tests:** R-12 and a fixture that a future bar and a future event are excluded.
- **Accept:** the report states net expectancy after costs. A win-rate-only improvement is not a pass.

### S2-V2 Shadow outcome scoring

- **Now:** no outcome column.
- **Change:** A offline job fills excursion and strategy result on existing shadow rows. It does not append executions and does not move the reserve.
- **Depends:** S2-R7.
- **Tests:** R-13.
- **Accept:** scoring is idempotent.

### S2-V3 Activation checklist

Non-code checklist. It does not create a journal veto, read shadow recommendations to alter journal records, block trade entry, change position sizing, or send or suppress broker orders. Recency outputs stay observational and shadow-only. The checklist names the tests required before any later implementation or activation approval. It does not grant that approval.

- **Now:** nothing reads the hypothetical label.
- **Change:** No application change. Record the checklist below. Do not stop at B-25.
- **Checklist:**
  - P0: B-01, B-02, B-03, B-04, B-05, B-06, B-07, B-08, B-09, B-10, B-11, B-12, B-13, B-14, B-15, B-16, B-17, B-18, B-19, B-20, B-21, B-22, B-23, B-24, B-25, B-26.
  - Reserve idempotency, each required by id: B-05 (state-file reserve unchanged for unpriced, paper, and `broker_premium_net` through `process_daily_pnl`; that function does not credit, debit, or rewrite reserve ownership), B-08, B-09, B-11, B-12, B-14, B-15, B-22, B-26 (once per `trade_id`; subcases B-26a through B-26w; a duplicate does not credit the reserve again; a loss does not reduce or increase the reserve; a win does not clear `daily_losses_count`; the count and the reserve each commit with their marker), R-13.
  - Recency and validation: R-01, R-02, R-03, R-04, R-05, R-06, R-07, R-08, R-09, R-10, R-11, R-12, R-13, R-14.
  - Sprint 1 tests that must still pass: `test_broker_bridge_live_flag_is_rejected`, `test_broker_bridge_simulated_fill`, `test_unpriced_exit_leaves_rupees_null_and_reserve_unchanged`, `test_daily_review_does_not_rewrite_active_parameters`, `test_journal_malformed_file_is_not_replaced`, `test_live_scanner_does_not_construct_an_execution_manager`.
  - `test_ist_session_resets_on_date_change_and_blocks_when_save_fails` keeps its date-change reset, same-date keep, and failed-save block. Its win-clear assertion (`daily_losses_count == 0` after `is_win=True, realized_r=0.4`) is superseded and is updated in S2-02, not by this checklist. This checklist does not edit the test.
  - Shadow log produced no orders. Out-of-sample expectancy after costs improved. Drawdown not worse. Half-life stable.
- **Depends:** S2-V1, S2-V2, all P0 tests.
- **Tests:** R-14.
- **Accept:** the daemon still cannot send an order. Recency still cannot override the rupee gate, alter a journal record, block an entry, change size, or suppress an order.

## Not in this register

T-07 research/live rule card, broker adapters, margin from a broker, and any edit to holding, exit, or sweep rules. Those stay out until this register is done and approved on its own.
