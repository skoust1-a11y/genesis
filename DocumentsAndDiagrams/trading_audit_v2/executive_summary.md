# Genesis trading audit v2 — executive summary

Audit date: 4 Nov 2026. Scope: current source under `backend/app`, `scripts/live_scanner.py`, `trades/`, and the prior written audits. No strategy rules were changed. No orders were sent. No 10-year backtest was re-run in this session.

Load assumed: one operator, one process, Indian index options (NIFTY first). Not a multi-user fund.

## Verdict

**Do not turn on automated live trading.**

Genesis has two trading stacks that do not share an order, a fill, or a P&L definition.

1. **Research stack** (`backend/app/engine/sim.py`, review crew, sandbox broker, `trading/orders.py`). Daily option snapshots, a real Indian cost schedule, and a kill switch. This is the only path with a broker-shaped order lifecycle, and it is still a simulator.
2. **Scanner stack** (`scripts/live_scanner.py` and `backend/app/trading/**`). This is what the daemon runs. It journals a signal, sends Telegram, and records rupees as index points times 0.5 times quantity. `ExecutionManager` is constructed and never called. The "live broker" branch reports a fill without calling a broker.

The October 2026 liquidity audit (kept at `DocumentsAndDiagrams/liquidity_strategy_audit/`) found **no significant edge** on the researched PDH/PDL sweep: about +0.032R per trade, 95% interval crossing zero, out-of-sample negative. Those numbers were **not** recomputed today. The daemon's detector does call `scan`, `scan_retest`, and `scan_trendline_reversal`, but not with that study's arguments, and `TradeManager` then replaces the target. The October expectancy is not a measurement of the live book (V2-08).

The safety-wall balance in `trades/daily_risk_config.json` (reserve about ₹2.14 lakh on a ₹50,000 base, as of this reading) is 50% of that delta-0.5 rupee series. It is not broker cash.

## Twelve answers

1. **What is genuinely good?** A cost model that names brokerage, STT, exchange fee, GST, SEBI and stamp duty, with a one-tick slippage floor (`engine/costs.py`). A daily simulator that can refuse a verdict on too few trades. A prior audit that tried to falsify the sweep instead of celebrating a workbook. Live gates that block a missing VIX, a stale 5-minute bar, and a missing call/put wall. Groww is correctly documented as quotes, not a broker.
2. **Most serious architectural weakness?** Two stacks. The one that can talk to an order manager is not the one the daemon runs. The one the daemon runs accounts P&L as if the option's delta were always 0.5 and then locks "profits" into a safety wall.
3. **Trust the backtests?** Trust the October index-point study only as a study of `scan()` on the Kaggle/Groww spot tape, with the caveats in that folder (holiday sessions removed, constant-IV option rupees rejected). Do not trust option-rupee workbooks. Do not treat that study as the live scanner: the live call uses dynamic targets, a 55-point risk cap, opening-range retests, re-entry, and 1-minute bars for retest and trendline, and the manager overwrites the target.
4. **Trust forward tests?** No, not as option results. Forward rows are spot excursions converted at delta 0.5 (`journal_agent.record_exit`). There is no broker fill, no spread, and no charge on that path.
5. **Are strategies implemented correctly?** The researched sweep's state machine was checked in October and matched its own spec. The detector imports that function, then keeps only risks from 10 to 55 points, prices VWAP from the index bars in hand, and runs retest and trendline on 1-minute bars as well as 5-minute. `EntryAgent.evaluate_sweep` (the 22–35 point micro-wick) is a fallback when no candidate list is passed. The daemon always passes a list, so that fallback is not the normal entry. The sniper's 1-minute rewrite does not run, because the daemon does not pass `df_1m` into `on_market_tick`. The sentence "the backtest runs the code the live alerts run" is only half true.
6. **Is the options execution model realistic?** No. Historical premiums are Black-76 at the previous day's India VIX, IV held constant, forward equal to spot in the estimate path, and the liquidity backtest docstring states no slippage. Live marks, when present, are Groww last-traded prices with no bid or ask. The journal does not even use those marks for P&L.
7. **Are risk controls sufficient?** No. Scanner lot size is frozen at 3, or 4 if ADX is high. The rupee loss figure is `stop_distance * 0.5 * quantity` and is not compared with capital. `reset_daily_counters` has no caller, so the "daily" R sum in the risk file is not a day. Research-stack caps for entries, streaks and loss percent default to 0 (off). A second scanner position is allowed once the first is breakeven-locked.
8. **What blocks production?** Fake fills if live mode is flipped on. P&L and the safety wall are not money. The traded rules are not the tested rules. No continuous integration. Post-market "review" can run on an empty frame and hardcoded levels. Parameter updates are canned sentences, not a walk-forward.
9. **What is easy to overlook?** The daemon prints "Trade Action Executed" for a journal write. `BrokerBridgeAgent` returns `FILLED` on the live branch with a random id. The research catalog hardcodes win rate and profit factor. Lunch "hard freeze" is waived for any candidate whose name contains sweep, equal or reversal.
10. **What to do first?** See P0 in `prioritized_improvement_roadmap.md`: make live mode incapable of reporting a fill it did not receive; stop ratcheting the safety wall on delta-0.5 rupees; stop the parameter agent from writing the live config; stop a bad active-trade file from being overwritten with an empty book.
11. **Future architecture?** One path: closed bar → one signal function → one risk gate in rupees → one order object → one fill from a quote with a spread → one position → one P&L. Research and live call that same function. Details in `recommended_future_architecture.md`.
12. **What must be proven before automated live trading?** A written rule card that the live code and the backtest both call. Out-of-sample index results that survive costs and multiple testing. A forward sample of real option fills (bid/ask or traded price, charges, partial fills) whose distribution is not wildly worse than that backtest. A kill switch that flattens a real broker position. A restart test that does not double an order. None of this is met.

## Finding index

| ID | Severity | One line |
|---|---|---|
| V2-01 | P0 | Live broker branch reports `FILLED` and never calls an API |
| V2-02 | P0 | Scanner builds `ExecutionManager` and never uses it |
| V2-03 | P0 | Journal rupees = index points × 0.5 × quantity |
| V2-04 | P0 | Safety wall compounds V2-03 |
| V2-05 | P0 | Daily loss counters are never reset |
| V2-06 | P0 | Parameter agent writes fixed "expected points" into the live config |
| V2-07 | P1 | Post-market review is called with an empty frame and fixed PDH/PDL |
| V2-08 | P1 | Live calls `scan` / `scan_retest` / `scan_trendline_reversal` with different arguments, then overwrites the target |
| V2-09 | P1 | Option backtests are constant-IV Black-76, no slippage on that path |
| V2-10 | P1 | Strategy catalog ships baked-in performance numbers |
| V2-11 | P1 | Sizing is a fixed lot count; the loss estimate is not a capital cap |
| V2-12 | P1 | Second position allowed after breakeven; no book-level greek cap on the scanner |
| V2-13 | P1 | Idempotency is in-memory; a failed JSON load can be saved over the book |
| V2-14 | P1 | Groww quotes are last trade from a scraped page |
| V2-15 | P2 | Research common-risk defaults are off |
| V2-16 | P2 | Research and paper HTTP routes do not require a token |
| V2-17 | P2 | No CI |
| V2-18 | P2 | STT schedule's last dated row is 2024-10-01 |
| V2-19 | P2 | Lunch lull is waived by strategy name |
| V2-20 | P1 | Docs and module banners describe components the process does not run |

Prior October master-audit items (G-01 through G-29 in `DocumentsAndDiagrams/audit/`) were not all re-tested. Where this audit re-read the code, that is said in the relevant file. G-05 (daily loss ignores open P&L, and the sim only enters on days with no exit) still matches `engine/sim.py` around the `pnl_today` / `closed_today` logic.
