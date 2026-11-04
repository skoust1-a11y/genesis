# Sprint 2 acceptance matrix

Date: 5 Nov 2026. A row is accepted only when the test exists and passes. A design sentence is not a pass. Sprint 1 tests were not re-run for this document and close none of these rows.

Live trading stays disabled in every fixture. No test calls a real broker. Risk and journal fixtures use `tmp_path`. None of these tests open `trades/daily_risk_config.json` or `trades/live_executed_trades.json` from the repo.

Charge rows call `leg_costs`. They do not paste a rupee total as the expected value. The illustrations in the financial design are checks on the formula, not substitutes for the call.

## P0

| ID | Task | Setup | Pass |
|---|---|---|---|
| B-01 | S2-04 | Risk file bytes are `{`. Construct `RiskAgent` on that path and call `begin_session`, then the manager tick path that would exit. | File bytes unchanged. `begin_session` is false. `evaluate_order` is not approved. `record_trade_outcome` is not called. In-memory reserve is not written back. |
| B-02 | S2-04, S2-07 | Readable file: reserve 213954.1, `daily_cumulative_r` 9.65, no `session_date`, no `manual_lock`. `begin_session` for a later date. | Reserve is 213954.1. R is 0. `session_date` is the new date. `effective_capital` is absent. `cash_inr` and `margin_inr` are null. `risk_fraction` is 0.02. `premium_exposure_budget_inr` is absent. `reserve_basis` still says the reserve is frozen index-points. |
| B-03 | S2-04 | Same file with `manual_lock` true and `is_safety_wall_locked` true. | Lock stays. Entry rejected. Reserve unchanged. |
| B-04 | S2-04 | `begin_session` save raises. | Counters restored. Later `evaluate_order` rejected. File from the last good save is unchanged. |
| B-05 | S2-07 | `SafetyWallAgent.process_daily_pnl` with unpriced rows, with one paper net, and with one `broker_premium_net` row, against a temp risk file that has `session_date`. | Risk file bytes unchanged in all three cases. State-file reserve unchanged on the unpriced and paper calls. |
| B-06 | S2-06 | `live_executed_trades.json` bytes are `{`. Close a temp active trade through `record_exit`. | Executed-file bytes unchanged. Function returns `None`. Active trade still present. |
| B-07 | S2-06 | Load a malformed active file for date A, then a valid file for date B, then save date A. | Date A bytes unchanged. |
| B-08 | S2-06 | Manager full-close path where `record_exit` returns `None`. | `record_trade_outcome` not called. Open row remains. Reserve unchanged. |
| B-09 | S2-01 | Append a broker execution, then append it again with the same broker execution id. | One execution stored. Derived gross unchanged by the duplicate. |
| B-10 | S2-01 | Buy 65 at 10, buy 65 at 12, sell 65 at 11, all `price_kind=broker` with ids. | FIFO closed gross is `(11-10)*65 = 65`. Remainder 65 at 12. Status `PARTIAL`. A replay of the sell id does not change the gross. |
| B-11 | S2-01, S2-02 | Store `contract_quote` LTP 100 as a reference on an open trade. No execution. | `reference_kind` is `ltp`. No execution is created. Estimated unrealized may be computed. Broker gross, broker net, and reserve are unchanged. `pnl_method` is not `broker_premium_net`. |
| B-12 | S2-02 | Paper executions, `simulated=true`, buy 65 at 100, sell 65 at 110, dated. | Paper gross is 650. Paper net equals `650 - leg_costs` with settings slippage. `pnl_method` is `paper_premium_net`. Reserve unchanged. |
| B-13 | S2-02 | Same prices as B-12 with `price_kind=broker`, slippage 0, broker execution ids, same date. | Broker gross is 650. Broker net equals `650 - leg_costs` at slippage 0 within 0.01. Charge keys match that `CostBreakdown`. `realized_points` still stored. |
| B-14 | S2-02 | Broker-shaped execution missing `broker_execution_id`. | Broker gross and broker net are null. Reserve unchanged. |
| B-15 | S2-02 | `leg_costs` patched to raise on a complete broker round trip. | Net is null. Points kept. Reserve unchanged. |
| B-16 | S2-03 | Normalizer given null `pnl_rupees` and a method. `compute_metrics` given one legacy float and one null broker net. `get_forward_tested_trades` given a crew-shaped active row with no `plan`. Trailing text with no reference premium. | Null stays null and the method is copied. Metrics do not raise. `pnl_status` is `incomplete`. `net_pnl_inr` is null. Response has no 78.67. Trailing body says not priced and has no `risk_points * 4.5` premium. Active parameter file bytes unchanged. |
| B-17 | S2-05 | `evaluate_order` with explicit `premium_exposure_budget_inr=1000`, entry premium 10, stop premium 8, lot 65, base 50000, `risk_fraction` 0.02, `default_lots` 3, not super-trend. | Approved lots are 1, not 3 and not 0. Quantity 65. Premium exposure 650. `max_loss_rupees` is 650. `planned_loss_rupees` equals 130 plus `leg_costs` of one buy at 10 and one sell at 8 for quantity 65, using the settings profile (slippage included and reported as its own component). Two lots are not returned. |
| B-18 | S2-05 | Same as B-17 but the candidate is forced to 2 lots before the step-down (budget check on quantity 130). | Not approved at 2 lots. Exposure 1300 is above 1000. |
| B-19 | S2-05 | Premium budget unset, so available funds are base 50000. Entry 10, stop 8, lot 65, frozen 3, one order. | Approved lots 3. Exposure 1950. Planned gross 390. Planned charges are one `leg_costs` call on quantity 195, not three times the one-lot charge. Planned loss is under 1000. `max_loss_rupees` is 1950 and is not compared with 1000 as if it were the stop. Reserve is not an input. |
| B-20 | S2-05 | Entry premium 100, stop premium 80, lot 65, base 50000, risk budget 1000, premium budget unset. | Not approved. One lot exposure is 6500, which fits in 50000, and planned gross is `(100-80)*65 = 1300`, which is above 1000 before charges. The rejection reason is the risk budget, not a missing quote. |
| B-21 | S2-05 | Premium omitted, or stop premium omitted, stop distance 30 index points, `default_lots` 3, reserve large. | Not approved. Lots are not 3. `max_loss_rupees` is null. Lots are not increased by the reserve. |
| B-22 | S2-02, S2-07 | Estimated unrealized positive, and a paper net positive, start reserve 213954.1. | Reserve unchanged. `assumed_cash` used by the gate is still `base_capital`. It does not rise. |
| B-23 | S2-05 | Search the sizing expression. | `safety_wall_reserve` and `effective_capital` do not appear. `risk_points * 0.5` does not appear in `RiskApproval` construction. |
| B-24 | S2-07 | Readable save after S2-04. | Document has no `effective_capital`. Lots with a huge reserve and frozen `default_lots` 3 stay 3 before the premium step-down. |
| B-25 | S2-05 | Source check plus the existing bridge tests. | `live_scanner.py` has no `ExecutionManager`. Live bridge flag is `REJECTED` / `UNSENT`. Shadow module is not required for this row. |
| B-26 | S2-02 | Positive broker net posted once for a `trade_id`, then `record_trade_outcome` called again for the same id. A second trade id with a negative broker net. | Reserve increases by half the net once. The second call does not add again. The loss increments `daily_losses_count` and does not reduce `safety_wall_reserve`. |

## P1 and P2

| ID | Task | Setup | Pass |
|---|---|---|---|
| R-01 | S2-R1, S2-R3 | Age \(t\) at one prior half-life. | \(W(t) = 0.5\) within 1e-9. Age 0 gives 1. Negative age is excluded, not weighted. |
| R-02 | S2-R1 | Observation with no timestamp. | Freshness 0. It does not enter the mass as fresh. |
| R-03 | S2-R2 | Two headlines, same normalized title, same source, 10 minutes apart. | One event. Bullish mass does not double. |
| R-04 | S2-R3 | Previous-day high older than several short half-lives. | Structural weight is at least the configured floor. It is not 0. |
| R-05 | S2-R3 | Two observations with the same `cluster_id`. | Only the larger absolute piece enters the mass. |
| R-06 | S2-R4 | A case with agreement and a case with both sides present. | Bands are `low`, `medium`, or `high` only. The payload has no percentage field. |
| R-07 | S2-R5 | Manager candidate the current rules would journal. Shadow label `WOULD_NO_TRADE`. | Journal row still follows the strategy and the rupee gate. Signal entry, stop, and target bytes unchanged by recency. |
| R-08 | S2-R5, S2-R7 | Import graph of `recency.py`, and a tick with the hook installed. | Module does not import `BrokerBridgeAgent`, `ExecutionManager`, or `trading.orders`. Tick result `orders_sent` is false. |
| R-09 | S2-R6 | One new opposing observation on an open trade whose stop is not hit. | Exit action is the `ExitAgent` or `HoldingAgent` action already chosen. Recency class does not call `record_exit`. |
| R-10 | S2-R6 | Fixtures for each class. | Labels are only `strengthening`, `stable`, `weakening`, `invalidated`, `insufficient_data`. `invalidated` does not set `is_fully_closed`. |
| R-11 | S2-R7 | `assess_entry` raises. | Tick does not raise. Strategy action unchanged. Data quality on the shadow note is `insufficient`. Risk gate result unchanged. |
| R-12 | S2-R2, S2-V1 | Decision time T. One bar closing after T. One event with `first_observed_at` after T. | Both excluded. A duplicate of an included event adds no weight. |
| R-13 | S2-V2 | Temp shadow jsonl row and an execution list. Run the outcome job twice. | Outcome fields filled. Second run does not duplicate them. Execution list and reserve file bytes unchanged. |
| R-14 | S2-V3 | Default settings and the daemon source after the checklist task. | `LIVE_TRADING_ENABLED` default is false. No code path reads the hypothetical label to journal, size, or send an order. |

## Sprint 1 tests that must still pass

| Existing test | Must remain true |
|---|---|
| `test_broker_bridge_live_flag_is_rejected` | Live flag is `REJECTED`, id `UNSENT`. |
| `test_broker_bridge_simulated_fill` | Paper flag is `SIMULATED_FILLED`. |
| `test_unpriced_exit_leaves_rupees_null_and_reserve_unchanged` | Still true when no executions are stored. |
| `test_daily_review_does_not_rewrite_active_parameters` | Parameter file bytes unchanged. |
| `test_journal_malformed_file_is_not_replaced` | Active journal bytes unchanged. |
| `test_live_scanner_does_not_construct_an_execution_manager` | Daemon source still has no `ExecutionManager`. |

`test_trading_and_post_market_crews.py` expectations that a raw `pnl_rupees` moves nothing must stay, unless the call passes `pnl_method="broker_premium_net"` and a `trade_id` that has not been posted. Updating those tests is part of S2-02, not a silent edit.

## Not acceptance

- The earlier 47-test run.
- A pytest process that was killed with no summary.
- A dashboard that shows ₹0 for a null.
- A reserve that changed because estimated or paper P&L was positive.
- Three lots of a ₹10 option described as inside a ₹1,000 premium-exposure budget.
- An LTP stored as a broker fill.
- A recency score that changed an entry, a stop, or a lot count.
- A claim that the decay priors are statistically validated.

## Stop

Sprint 2 is not approved to start until this matrix is accepted. After implementation, B-01 through B-26 are the P0 close-out. R-01 through R-14 are the recency and validation close-out. The full `backend/tests` suite is then run once, with `MPLCONFIGDIR` set, from `backend/`, under the safety rules in `sprint1_test_coverage_gaps.md`. The result is quoted only if it finishes. The production risk file mtime must be unchanged.
