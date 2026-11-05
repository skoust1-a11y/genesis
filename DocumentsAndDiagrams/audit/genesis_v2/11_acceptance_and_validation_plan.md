# 11 — Acceptance and validation plan

A row is accepted only when the named test exists and a pytest summary says it passed. This audit did not run pytest. Sprint 1’s earlier 47-test figure is not a pass for this plan. The acceptance matrix says the same.

Live trading stays off. No test calls a real broker. Risk and journal fixtures use `tmp_path`. No test opens `trades/daily_risk_config.json`, `trades/live_executed_trades.json`, `backend/genesis.db`, `backend/live.db`, or `trades/trades.db`.

## Close-out order

1. Decision A-00 and D-01 recorded.
2. P0 tests B-01 through B-26, with B-26 closed only when B-26a through B-26w pass.
3. Sprint 1 tests that must still pass, listed below.
4. Recency tests R-01 through R-14 after P0, without weakening a P0 assertion.
5. One full `backend/tests` run under the command in the coverage note. Quote the summary only if the process finishes. Record risk-file and executed-log mtimes before and after. They must match.

## P0 map

The pass text is the acceptance matrix. This plan does not restate every subcase. Implementers use `sprint2_acceptance_matrix.md` as the assertion source. If this plan and that matrix disagree, the matrix wins, and this audit should be corrected. They were compared on 5 Nov 2026 and agree on the following:

- B-01 through B-04: unreadable and rollover behavior of the risk file.
- B-05 and B-26t: `process_daily_pnl` writes nothing and reports `added_to_reserve` 0 for unpriced, paper, and `broker_premium_net`.
- B-06 through B-08: executed log and a `None` exit.
- B-09 through B-15: executions, FIFO, reference LTP, paper net, broker net, missing broker id, `leg_costs` raising.
- B-16: null readers.
- B-17 through B-21 and B-23: sizing. Three lots of a ₹10 option are not inside a ₹1,000 premium budget. One lot is.
- B-22 and B-24: estimated and paper P&L do not increase cash or the reserve. `effective_capital` is absent.
- B-25: daemon source has no `ExecutionManager`.
- B-26a through B-26w: one loss count, one reserve credit, markers in the same document, no win-clear, session reset once, failed replace returns false.

Charge tests call `leg_costs`. They do not paste the design’s illustration as the expected rupee.

## Sprint 1 tests that remain

| Test | Must remain true |
|---|---|
| `test_broker_bridge_live_flag_is_rejected` | `REJECTED` / `UNSENT` |
| `test_broker_bridge_simulated_fill` | `SIMULATED_FILLED` |
| `test_unpriced_exit_leaves_rupees_null_and_reserve_unchanged` | Still true when no executions are stored |
| `test_daily_review_does_not_rewrite_active_parameters` | Parameter file bytes unchanged |
| `test_journal_malformed_file_is_not_replaced` | Active journal bytes unchanged |
| `test_live_scanner_does_not_construct_an_execution_manager` | Script source has no `ExecutionManager` |

`test_ist_session_resets_on_date_change_and_blocks_when_save_fails` keeps date-change reset, same-date keep, and failed-save restore. The win-clear and the unpriced R increments in that test are updated in S2-02. Updating them is part of acceptance, not a silent edit during design.

## Recency and validation

R-01 through R-14 as written in the matrix. R-14 checks that the checklist names B-26a through B-26w and that no path reads the hypothetical label into the journal, the size, or an order.

Half-life priors are not an acceptance of profitability. A higher win rate with worse expectancy after `leg_costs` is not a pass (S2-V1).

## PDC regression, when a V0 function exists

Not a Sprint 2 row. When someone later touches the benchmark, the tests are the frozen predicates, not the script’s extra buffers:

- Zone is the previous session’s 15:25 high and low, with no ±3 widen.
- Window 09:20–15:15.
- Stop invalidation is a candle close, not a wick.
- 50% exit at 2R, remainder toward PDH or PDL.
- Exit on an opposite 15 EMA close.
- No second same-direction position while the first is open.
- A future bar is invisible.
- Recency output is absent from the fills.

Until those tests exist, do not claim the script is V0.

## Explicit non-acceptance

Copied from the matrix, still in force:

- A killed pytest process.
- A dashboard that shows ₹0 for null.
- A reserve that moved because paper or estimated P&L was positive.
- A second credit for the same `trade_id`.
- A loss that changed `safety_wall_reserve`.
- A loss count moved by a non-broker, zero, or positive net, or cleared by a win.
- A rollover that deletes marker ids.
- An acknowledged post whose file lacks the marker.
- An LTP stored as a broker fill.
- A recency score that changed an entry, a stop, or a lot count.
- A claim that the decay priors are statistically validated.

## Safe run

From `backend/`, as the coverage note specifies:

```text
cd backend
export MPLCONFIGDIR="$PWD/.mplconfig"
export LIVE_TRADING_ENABLED=false
export SANDBOX_ALLOW_LIVE=false
mkdir -p "$MPLCONFIGDIR"
.venv/bin/python -m pytest -p no:cacheprovider --durations=30
```

Do not export a production database URL. Do not point the run at the untracked database files. If a risk or journal mtime changes, stop. Do not repair the file inside the test run.

## Verdict check

Implementation of this plan, with A-00 and D-01 accepted and B-01 through B-26 passing, is what “ready” means for money and shadow recency. It is not approval to turn on live orders. S2-V3 does not grant that approval.
