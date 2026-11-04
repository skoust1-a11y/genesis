# Test and acceptance matrix

Date: 4 Nov 2026. Tests to add when implementation is approved. None of these were added in this pass.

This session ran:

```text
backend/.venv/bin/pytest tests/test_pre_market_and_execution_crews.py tests/test_money_rules.py
24 passed in 2.78s
```

That run does not satisfy A-01. It never sets `is_live_broker=True`.

| ID | Task | Test | Pass |
|---|---|---|---|
| A-01 | T-01 | `BrokerBridgeAgent(is_live_broker=True).place_order(...)` | `success` is false, status is `REJECTED`, status is not `FILLED` |
| A-02 | T-02, T-08 | Exit with no premiums | `pnl_rupees` is null. Reserve unchanged |
| A-03 | T-02 | Exit with premiums 100 and 110, qty 65, one day | Net equals `(110-100)*65 - leg_costs(...)` within one paisa |
| A-04 | T-03 | Two losses on date D, then a tick on D+1 | Counters are zero before the new decision. Third loss on D is rejected |
| A-05 | T-04 | `run_daily_review` on a temp directory | `active_strategy_params.json` hash unchanged |
| A-06 | T-05 | Active file is not valid JSON | File bytes unchanged. No new trade id |
| A-07 | T-07 | Fixed bars, card with signal-owned target | Detector and backtest helper return the same entry, stop, target |
| A-08 | T-07 | Truncate the same bars one bar at a time | No signal appears earlier than on the full prefix |
| A-09 | T-09 | One open trade with `be_locked` | Second entry rejected |
| A-10 | T-10 | Empty bar frame | Review status skipped. Text does not say zero missed setups |
| A-11 | T-12 | Catalog payload | No `net_pnl_inr` literal from `RESEARCH_CATALOG` |
| A-12 | T-03, T-05 | Risk file is truncated JSON | Defaults are not written back |

## Hand checks that are not unit tests

| Check | When |
|---|---|
| STT schedule versus the statute you cite | Before the next published cost number (T-16) |
| One premium row versus a broker contract note | Before any `broker` mode gate |
| Restart after a `CREATED` order | Before any `broker` mode gate. Not required for the signal diary |

## What a green suite will still not prove

That `scan()` has an edge. That Black-76 matches the market. That the October CI is still the live number. Those need the result file in `backtesting_validation_spec.md`, not a unit test.
