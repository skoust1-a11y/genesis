# Sprint 2 acceptance matrix

Date: 4 Nov 2026. A row is accepted only when the test exists and passes. A design sentence is not a pass. Sprint 1's 47 tests do not close any row here except where a Sprint 1 test is reused on purpose.

Live trading stays disabled in every fixture. No test calls a real broker. Risk fixtures use `tmp_path`. None of these tests open `trades/daily_risk_config.json` from the repo.

## Gate

| ID | Task | Setup | Pass |
|---|---|---|---|
| B-01 | S2-04 | Risk file bytes are `{`. Construct `RiskAgent` on that path and call `begin_session` for the next date. | File bytes unchanged. `begin_session` is false. `evaluate_order` is not approved. `safety_wall_reserve` in memory is not written back. |
| B-02 | S2-04 | Readable file: reserve 213954.1, `daily_cumulative_r` 9.65, no `session_date`, no `manual_lock`. `begin_session` for a later date. | Reserve is 213954.1. R is 0. `session_date` is the new date. `effective_capital` is absent. `cash_inr` and `margin_inr` are null. `daily_loss_fraction` is 0.02. `reserve_basis` still says the reserve is frozen index-points. |
| B-03 | S2-04 | Same file with `manual_lock` true and `is_safety_wall_locked` true. | Lock stays. Entry rejected. Reserve unchanged. |
| B-04 | S2-04 | `begin_session` save raises. | Counters restored. Later `evaluate_order` rejected. File from the last good save is unchanged. |
| B-05 | S2-04 | `SafetyWallAgent.process_daily_pnl` with unpriced rows, and with one `premium_leg_costs` row, against a temp risk file that has `session_date`. | Risk file bytes unchanged in both cases. State file reserve unchanged on the unpriced call. |
| B-06 | S2-01 | `on_market_tick` with a one-leg chain, LTP 100, lot 65, and a signal the current rules would take. Use a temp trades dir. | Open row `entry_premium` is 100, `fill_kind` is `last`, `premium_missing` is false. SQLite `entry_premium` is 100. |
| B-07 | S2-01 | Same tick with `nifty_chain` null. | `entry_premium` is null. The JSON does not contain 78.67 or `risk_points * 4.5`. |
| B-08 | S2-02 | Exit a row that has entry premium 100, exit quote 110, quantity 65, same session date. | `pnl_method` is `premium_leg_costs`. `pnl_rupees` equals `(110-100)*65 - leg_costs([BUY 100×65, SELL 110×65], profile, date).total` within 0.01. `realized_points` still stored. Charge keys match `CostBreakdown`. |
| B-09 | S2-02 | Exit with no exit quote. | `pnl_rupees` is null. `pnl_method` is `index_points_not_priced`. Reserve unchanged. |
| B-10 | S2-02 | `record_trade_outcome` for the B-08 net, win. Start reserve 213954.1. | Reserve increases by exactly half the net, rounded to paise. A second call with `pnl_method` omitted and `pnl_rupees` 10000 does not move it. |
| B-11 | S2-02 | `leg_costs` patched to raise. | Exit row has null rupees, points kept, reserve unchanged. |
| B-12 | S2-03 | `run_daily_review` loads an executed row with `pnl_rupees` null and `pnl_method` set. | The dict passed onward still has null rupees and the method. Active parameter file bytes unchanged (Sprint 1 A-05, still required). |
| B-13 | S2-03 | `compute_metrics` on one legacy float and one null `est_pnl_inr`. | Does not raise. `pnl_status` is `incomplete`. `net_pnl_inr` is null. |
| B-14 | S2-03 | `get_forward_tested_trades` with a crew-shaped active file and no `plan` key. | Response premium is null. Body does not contain 78.67. |
| B-15 | S2-03 | Trailing-update message builder for a trade with no `entry_premium`. | Body says the premium is not priced and does not contain a computed rupee premium. |
| B-16 | S2-05 | `evaluate_order` with premium 100, qty basis 65, base 50000, fraction 0.02, `default_lots` 3, not super-trend. | Not approved. |
| B-17 | S2-05 | Premium 10, same base and fraction. | Approved, lots 3, `max_loss_rupees` equals `10 * 3 * 65`. |
| B-18 | S2-05 | Premium omitted, stop 30 points, `default_lots` 3, reserve large enough to clear a capital milestone. | Approved, lots 3, `max_loss_rupees` is null. Lots are not 4 from the reserve. |
| B-19 | S2-05 | Super-trend flag, premium 10, fraction wide enough for 4 lots. | Lots are 4, not 5. |
| B-20 | S2-05 | Search `RiskApproval` construction. | No remaining `risk_points * 0.5`. |

## Sprint 1 tests that must still pass

| Existing test | Must remain true |
|---|---|
| `test_broker_bridge_live_flag_is_rejected` | Live flag is `REJECTED`, id `UNSENT`. |
| `test_broker_bridge_simulated_fill` | Paper flag is `SIMULATED_FILLED`. |
| `test_unpriced_exit_leaves_rupees_null_and_reserve_unchanged` | Still true when no premiums are stored. |
| `test_daily_review_does_not_rewrite_active_parameters` | Parameter file bytes unchanged. |
| `test_journal_malformed_file_is_not_replaced` | Active journal bytes unchanged. |
| `test_live_scanner_does_not_construct_an_execution_manager` | Daemon source still has no `ExecutionManager`. |

`test_trading_and_post_market_crews.py` expectations that a raw `pnl_rupees` moves nothing must stay, unless the call also passes `pnl_method="premium_leg_costs"`. Updating those tests to pass the method is part of S2-02, not a silent edit.

## Not acceptance

- The earlier 47-test run.
- A pytest process that was killed with no summary.
- A dashboard that shows ₹0 for a null.
- A reserve that changed because `effective_capital` or the 0.5 formula was renamed.

## Stop

Sprint 2 is not approved to start until this matrix is accepted. After implementation, B-01 through B-20 are the close-out. The full `backend/tests` suite is then run once, with `MPLCONFIGDIR` set, from `backend/`, and the result is quoted only if it finishes.
