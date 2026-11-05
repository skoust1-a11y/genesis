# 10 — Architecture risk register

Severity is what happens if the item is ignored while someone starts the proposed V2 diagram.

| ID | Severity | Item | Evidence | If ignored |
|---|---|---|---|---|
| RSK-01 | Blocker | Unreadable risk file is overwritten with reserve 0 | `risk_agent.py` `_load_config` L99–101 and `begin_session` L148–170 | The next daemon tick destroys the only reserve document |
| RSK-02 | Blocker | `process_daily_pnl` priced branch replaces `daily_risk_config.json` and sets `daily_losses_count` to 0 | `safety_wall_agent.py` L100–139 | The first real premium method wipes session state and can drain the reserve |
| RSK-03 | Blocker | Loss count is R and ₹500, then a win clears it. No `trade_id` marker | `risk_agent.py` L135–138 | Sprint 2 tests will fail, and a restart can count a loss twice |
| RSK-04 | Blocker | Failed exit still posts an outcome and drops the memory book | `manager.py` L356–382 | Disk and memory disagree. Counters move without a durable close |
| RSK-05 | Blocker | PDC V0 is not the daemon and is not the script’s predicates | `backtest_pdc_15ema.py` `scan_pdc_trades` versus the frozen card in report 08 | “Preserving V0” while editing `TradeManager` does not preserve V0 |
| RSK-06 | Blocker | Two order stacks | Daemon journal vs `run_tick` / `OrderManager`. `ExecutionManager` is a third, unwired | Wiring the banner’s execution crew creates a second sender beside the live flag |
| RSK-07 | High | `save_config` is not an atomic replace and has no lock | `risk_agent.py` L120–121 | A crash leaves a truncated file, which RSK-01 then replaces |
| RSK-08 | High | `record_exit` replaces a corrupt executed log | `journal_agent.py` L195–205 | History disappears |
| RSK-09 | High | `load_failed` is one flag | `journal_agent.py` L46–57 | A good load of another date re-opens a bad date for writing |
| RSK-10 | High | Post-market review uses an empty frame and constant PDH/PDL | `live_scanner.py` L280–284 | The daily report is not the session |
| RSK-11 | High | Missing chain invents walls and OI | `gamma_agent.py` L89–97 | Holding and the pre-market plan display fake levels. Entries are blocked only by `walls_live` |
| RSK-12 | High | Gift price is invented | `gap_agent.py` L47–49, daemon omits the argument | The battleplan states a gap that was not observed |
| RSK-13 | High | Three books | JSON journal, `trade_store` with `or 0.0`, live DB | A reader can show ₹0 or a fill that never happened |
| RSK-14 | Medium | `begin_session` boolean ignored | `manager.py` L192 | Entries can proceed in the same tick after a failed story that still falls through. The entry section does check `session_blocked` later (L459). Exits in that tick can still save |
| RSK-15 | Medium | In-memory duplicate signatures | `manager.py` L156, L889 | Restart can journal the same signal again |
| RSK-16 | Medium | Second entry while `be_locked` | `manager.py` L450–456 | Overlapping positions. Illegal for V0. Legal for the live card until that card is rewritten, which this migration must not do |
| RSK-17 | Medium | Holding preemptive exits | `manager.py` L282–296 | Live exits are not the exit agent’s rules and not V0 |
| RSK-18 | Medium | Host zone vs Kolkata | `is_market_hours` uses naive `now`. Session date uses Kolkata | Post-market can run at the wrong time off-zone |
| RSK-19 | Medium | `fcntl` lock is host-local | Stated in the financial design | Two hosts sharing the JSON file are unsafe. This repo assumes one host. Unresolved if a second host is ever added |
| RSK-20 | Medium | Matplotlib import on the trade-manager import | `sprint1_test_coverage_gaps.md` | A killed pytest with no summary gets quoted as a pass |
| RSK-21 | Low | Execution slippage counter on a reject | `execution_manager.py` L83–85 | Meaningless if someone wires the class |
| RSK-22 | Low | Vendor TradingAgents | `vendor/TradingAgents` | Dead weight. Not on the daemon. Do not “integrate” it |
| RSK-23 | Low | `trade_store` creates `data/` at import | `trade_store.py` L18–20 | Import side effect. Not the untracked DB files |

## Unresolved decisions

These are not answered by the six Sprint 2 documents. Implementation of P0 does not need them. A later live order does.

1. Whether the scanner product and the deployment product will ever share one rule card. Until that decision, they stay apart.
2. Whether PDC V0 will be reimplemented as a pure function that matches the frozen card, or whether the script’s buffers and window are accepted as the benchmark. This audit does not accept the script as the card.
3. The statutory STT and exchange schedules. The design says to call `leg_costs` and not to treat the illustration as a fixture. It does not confirm the statute.
4. A multi-process lock across the daemon and a second writer. `fcntl` on one host is the specified answer. A second host is out of scope and unsafe.
5. What happens to a negative broker net whose replace fails and whose session date then rolls. The design says it does not count later and does not add an outbox. That is a known miss, not an oversight to “fix” with a queue in this migration.
6. Whether `HoldingAgent` preemptive exits remain part of the live card. Sprint 2 says not to extend them. It does not remove them. Removing them would change live behavior and is out of scope.
7. Bank Nifty for V0. The frozen card names it. The script that was read is NIFTY. No daemon path implements either.

## Blockers that must close before implementation

Close means “accepted in writing,” not “coded,” except RSK-01 through RSK-04, which close only when their tests pass.

| Before any S2 commit | Why |
|---|---|
| A-00 accepted | Stops a CrewAI or execution-crew rewrite |
| Tests will use `tmp_path` and will not open `trades/daily_risk_config.json` | The real file has no `session_date`. A test on the default path would reset it |
| Pytest is run from `backend/` with `MPLCONFIGDIR` set, and a result is quoted only if a summary prints | Coverage note |
| Production risk file and executed-log mtimes recorded and unchanged by the test run | Coverage note |
| D-01 acknowledged | Stops a PDC “integration” inside the tick |

P0 code may start after that. It is not approved to enable a broker.

## What is not a blocker

Missing recency. It is P1 and shadow-only. Missing walk-forward harness. It is P2. The empty post-market frame. It is P-01 and must not precede the fail-closed risk file. The lack of CrewAI. That absence is correct.
