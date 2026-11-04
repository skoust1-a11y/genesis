# Implementation task register

Date: 4 Nov 2026. Do not start these until the blueprint is approved. No task retunes a strategy parameter.

Complexity: S under a day, M a few days, L longer. Sequence is the dependency order. Parallel means safe beside another task in the same band.

## P0

### T-01 Refuse a fill that was not received

- **Finding:** F-01
- **Now:** `BrokerBridgeAgent.place_order` returns `FILLED` when `is_live_broker` is true.
- **Change:** return `success=False`, `status="REJECTED"`, message `no broker adapter`. Do not allocate an id that looks filled.
- **Files:** `backend/app/trading/execution_crew/broker_bridge_agent.py`, `backend/tests/test_pre_market_and_execution_crews.py`
- **Depends:** none
- **Sequence:** first
- **Complexity:** S
- **Parallel with:** T-02, T-04, T-05
- **Regression:** any test that expects `FILLED` in live mode. Daemon passes `False`, so the paper branch stays.
- **Tests:** A-01
- **Accept:** flag true yields rejected. Flag false still returns `SIMULATED_FILLED` and the test name says simulated.

### T-02 Stop writing 0.5-delta rupees and stop the ratchet

- **Finding:** F-02, F-03, F-04
- **Now:** `record_exit` sets `pnl_rupees` from points × 0.5 × qty. `record_trade_outcome` adds half of that to the reserve. Manager passes that number through.
- **Change:** write `realized_points` and `realized_r`. Set `pnl_rupees` null. Set `pnl_method=index_points_not_priced`. Do not call the reserve addition unless `pnl_method` is a premium method. Add `reserve_basis` on the next save without changing the numeric reserve.
- **Files:** `backend/app/trading/crew/journal_agent.py`, `backend/app/trading/crew/risk_agent.py`, `backend/app/trading/crew/manager.py`, briefing template that prints rupees
- **Depends:** none for the stop. Premium fills are T-08
- **Sequence:** first, with T-01
- **Complexity:** M
- **Parallel with:** T-01, T-04
- **Regression:** UI and Excel that assume `pnl_rupees` is always a float. They must show blank, not zero. Daily audit text changes.
- **Tests:** A-02, A-03
- **Accept:** exit with no premium leaves reserve unchanged. Worked example in `financial_correctness_spec.md` fails if someone puts the 0.5 formula back.

### T-03 IST reset of risk counters

- **Finding:** F-07
- **Now:** `reset_daily_counters` has no caller. Loss count is not consecutive.
- **Change:** `session_date` in the risk file. Reset when the Kolkata date changes, before entries. A win zeros the consecutive loss count. Lock from those counters clears on the new date. Do not clear a separate manual lock.
- **Files:** `backend/app/trading/crew/risk_agent.py`, call site in `TradeManager.on_market_tick` or daemon startup
- **Depends:** none
- **Sequence:** after T-02 so the reset test does not also fight the ratchet
- **Complexity:** S
- **Parallel with:** T-05
- **Regression:** a lock from yesterday opens the next session. Intended.
- **Tests:** A-04
- **Accept:** date rollover zeros R and the loss count. Same date does not. Two losses block the third entry.

### T-04 Parameter agent cannot write the live file

- **Finding:** F-09
- **Now:** `PostMarketManager.run_daily_review` calls `save_calibrated_parameters` onto `active_strategy_params.json`. Daemon calls that review with an empty frame.
- **Change:** remove that call from the daemon and from `scripts/run_post_market_audit.py`. Proposals, if kept, append to a log and cannot set `approved`.
- **Files:** `backend/app/trading/post_market_crew/post_market_manager.py`, `backend/app/trading/post_market_crew/parameter_agent.py`, `scripts/live_scanner.py`, `scripts/run_post_market_audit.py`
- **Depends:** none
- **Sequence:** first
- **Complexity:** S
- **Parallel with:** T-01, T-02
- **Regression:** the buffer already on disk stays until a person edits it. Waterfall stops stop changing every afternoon.
- **Tests:** A-05
- **Accept:** a review run leaves `active_strategy_params.json` byte-identical.

### T-05 Do not overwrite a journal that failed to load

- **Finding:** F-13
- **Now:** load exception returns `{}`, and `record_entry` saves.
- **Change:** on load error, set a flag, refuse entries, do not save.
- **Files:** `backend/app/trading/crew/journal_agent.py`, `backend/app/trading/crew/manager.py`
- **Depends:** none
- **Sequence:** first
- **Complexity:** S
- **Parallel with:** T-01, T-03
- **Regression:** a missing file is still an empty book. Only exceptions change.
- **Tests:** A-06
- **Accept:** invalid JSON, file bytes unchanged, no entry.

### T-06 Daemon does not pretend to execute

- **Finding:** F-01, F-11
- **Now:** `ExecutionManager` is constructed and unused. Logs say "Trade Action Executed".
- **Change:** remove the construction and the import. Log `SIGNAL`.
- **Files:** `scripts/live_scanner.py`
- **Depends:** T-01 so the dangerous method is rejected even if something else calls it
- **Sequence:** immediately after T-01
- **Complexity:** S
- **Parallel with:** T-04
- **Regression:** none on orders, because no order was sent
- **Tests:** import check or a one-line test that the script source does not need to be executed; a unit test is enough if the runner is extracted. Accept if `grep execute_entry scripts/live_scanner.py` is empty and the process is not required to boot in CI.
- **Accept:** `live_scanner.py` does not reference `ExecutionManager`.

## P1

### T-07 One rule card for research and live

- **Finding:** F-05
- **Now:** different arguments; manager replaces the target; sniper is unwired.
- **Change:** implement `research_live_parity_spec.md`. Do not change numeric thresholds in the same commit as the wiring. Default card matches **current daemon** behaviour or **current backtest** behaviour, chosen explicitly in the commit message. The other remains version 0.
- **Files:** `backend/app/trading/scanner_crew/detector_agent.py`, `backend/app/trading/crew/manager.py`, `backend/scripts/backtest_liquidity.py`, new card JSON under `trades/` or `research/` (a new file is a config addition; do not edit `active_strategy_params.json` by hand in the same change without approval)
- **Depends:** T-02 so a parity run is not scored in fake rupees
- **Sequence:** after P0
- **Complexity:** M
- **Regression:** which alerts fire will change if the card picks the backtest arguments. That is a strategy change and must be stated.
- **Tests:** A-07, A-08
- **Accept:** same bars, same entry, stop, and target from both callers when the card says the signal owns the target.

### T-08 Premium fields on the journal, charges from `engine/costs.py`

- **Finding:** F-02, F-06
- **Now:** no premium on the exit row.
- **Change:** store entry and exit premium when a quote exists. Net via `leg_costs`. Null otherwise. Do not backfill with Black-76.
- **Files:** `journal_agent.py`, `manager.py` where `contract_quote` already returns LTP, `engine/costs.py` call site
- **Depends:** T-02
- **Sequence:** after T-02
- **Complexity:** M
- **Regression:** Telegram text. Old rows keep `pnl_method` unset or `index_points_x_0.5`.
- **Tests:** A-02 with a known premium
- **Accept:** the worked 100 to 110 example matches `leg_costs` by hand.

### T-09 One position, lot from the chain

- **Finding:** F-08 and the breakeven second entry
- **Now:** 3 or 4 lots; second trade if `be_locked`.
- **Change:** risk gate rejects a second position. Quantity is one lot times the chain `marketLot` when present, else the dated table, else reject rather than assume 65. This **does** change size versus today's 3 lots. It is a risk fix, not a signal change. Call it out.
- **Files:** `risk_agent.py`, `manager.py`
- **Depends:** T-03
- **Sequence:** after T-03
- **Complexity:** S
- **Regression:** fewer contracts per alert. Operators who hand-trade 3 lots will notice.
- **Tests:** A-09
- **Accept:** breakeven flag does not open a second trade. Missing lot does not default silently.

### T-10 Post-market uses stored bars or skips

- **Finding:** F-09 input
- **Now:** empty frame, PDH 23150, PDL 22950.
- **Change:** pass bars from the store the daemon already writes (`odds.save_nifty_5m`) and that day's levels. If empty, write "review skipped" and do not emit "zero missed".
- **Files:** `scripts/live_scanner.py`, `post_market_manager.py`
- **Depends:** T-04
- **Sequence:** after T-04
- **Complexity:** S
- **Tests:** A-10
- **Accept:** empty input does not produce a missed-setup count of zero.

### T-11 Report caps when research limits are off

- **Finding:** research defaults at 0
- **Now:** `max_loss_pct_of_capital` and streak caps default to 0.
- **Change:** sim result includes `caps_off: true` when those are 0. Do not change the defaults in the same task (that would rewrite history).
- **Files:** `engine/sim.py` or the review report builder
- **Depends:** none
- **Complexity:** S
- **Parallel with:** T-07
- **Tests:** a sim fixture with default settings asserts the flag
- **Accept:** a reader can see the limits were off

## P2

| ID | Finding | Change | Complexity | Depends |
|---|---|---|---|---|
| T-12 | F-10 | Catalog omits hardcoded performance numbers | S | none |
| T-13 | F-12 | Pytest in CI for the backend suite plus A-01–A-06 | S | T-01–T-05 so the new tests exist |
| T-14 | security | `require_auth` on review, ingest, and paper orders, or document and bind loopback | S | none |
| T-15 | F-11 | Rewrite the execution-crew and backtest docstrings that over-claim | S | T-06, T-07 |
| T-16 | STT row | Confirm the 2026 rate before the next quoted cost. Do not invent one | S | none |
| T-17 | G-05 | Open mark in the research daily-loss figure, behind a flag so old runs can be reproduced | M | T-11 |

## P3

| ID | Change | Why it waits |
|---|---|---|
| T-18 | Append-only chain archive from the Groww fetch | Builds a future premium history. Does not fix past P&L |
| T-19 | Walk-forward on a frozen card, scored in index R | Only after T-07 and the validation spec |
| T-20 | Broker mode through `trading/orders.py` | Only after every gate in `live_trading_readiness_gates.md` |

## Explicitly not tasks

Optimizing ADX, R, OTM, or the stop buffer. Adding Kite, Dhan, and Angel One. A portfolio optimizer. Rewriting crews into a new package.
