# Prioritized improvement roadmap

Audit date: 4 Nov 2026. No item below authorizes a parameter search. Fix the measurement first.

Effort is for one developer who already knows this repo: S a day or less, M a few days, L more than a week.

## P0 — can lose money or invent a result

### V2-01 Refuse fake live fills

- **Current:** `BrokerBridgeAgent.place_order` returns `FILLED` when `is_live_broker` is true, with no adapter call.
- **Weakness:** A flag flip reports a position the exchange does not have.
- **Change:** If there is no broker adapter, return `REJECTED` and do not invent an order id that looks filled. Keep the simulated status only when the flag is false.
- **Modules:** `backend/app/trading/execution_crew/broker_bridge_agent.py`, `backend/tests/test_pre_market_and_execution_crews.py`.
- **Benefit:** Live mode cannot succeed by accident.
- **Effort:** S.
- **Depends on:** nothing.
- **Regression:** tests that expect `FILLED` in live mode must change. The daemon passes `False`, so the paper path stays.
- **Accept:** `is_live_broker=True` yields `success=False` and status `REJECTED`.
- **Test:** one unit test with the flag on.

### V2-03 and V2-04 Stop reporting model rupees as P&L

- **Current:** `record_exit` sets `pnl_rupees = realized_points * 0.5 * quantity`, and the risk agent adds half of that to `safety_wall_reserve`.
- **Weakness:** Daily audits, the safety wall, and lot advice are not option results. The file already shows a reserve above ₹2 lakh on a ₹50,000 base.
- **Change:** Persist `realized_points` and `realized_r` as the result. Set rupees only from a stored premium fill (entry LTP, exit LTP, charges). If either LTP is missing, store null and print "not priced". Stop calling the safety-wall ratchet on the 0.5-delta number. Do not delete the historical JSON; add a field `pnl_method` so old rows stay identifiable.
- **Modules:** `crew/journal_agent.py`, `crew/risk_agent.py`, `post_market_crew/briefing_agent.py`, `db/trade_store.py` if it sums `est_pnl_inr`.
- **Benefit:** The evening report stops training the wrong decision.
- **Effort:** M.
- **Depends on:** a place to put entry and exit LTP. `contract_quote` already reads LTP at entry and then drops it on the floor when the quote is missing.
- **Regression:** UI and Excel that assume `est_pnl_inr` is always a float. They must show a blank, not zero.
- **Accept:** a trade with no exit LTP has null rupees; the safety-wall reserve does not move; a hand example with a known premium matches charges from `order_charges`.
- **Test:** journal exit with and without LTP. Risk agent does not increase the reserve on a points-only exit.

### V2-06 Stop the parameter writer

- **Current:** any losing trade recommends buffer 10 and "+25 points"; two large winners recommend 4R and "+45 points"; the writer stores the buffer where the daemon reads it.
- **Weakness:** The live stop buffer moves because a template matched, not because a sample said so. On the current daemon that buffer is read only by `EntryAgent` (waterfall and the unused sweep fallback). `scan()` stops do not use it. `dynamic_rr_ceiling` is written and not read. `otm_steps` is read by the detector and is not what this template changes. The writer is still a landmine: the next person who wires the ceiling or the buffer into `scan` inherits a fitted value, and the waterfall path is already affected.
- **Change:** `save_calibrated_parameters` must not run from the daemon. Printing the text in the markdown file is optional and must be labelled "not a backtest".
- **Modules:** `post_market_crew/parameter_agent.py`, `post_market_crew/post_market_manager.py`, `scripts/live_scanner.py`.
- **Benefit:** Tomorrow's rules equal today's rules.
- **Effort:** S.
- **Depends on:** nothing.
- **Regression:** `active_strategy_params.json` stops changing. The buffer already on disk (10) remains until you edit it by hand.
- **Accept:** a post-market run does not modify the JSON.
- **Test:** manager test with a losing trade, assert the file bytes are unchanged.

### V2-13 Do not overwrite a book you failed to read

- **Current:** load errors become `{}`; the next save writes that; the idempotency set lives on the object.
- **Weakness:** a corrupt or half-written active file disappears, and a restart can re-journal a closed setup.
- **Change:** on load failure, keep the previous memory if any, refuse new entries, and do not save. Write the idempotency key into the day file when the trade is **opened**, and keep the key after close (a small list on disk, not only on the open row).
- **Modules:** `crew/journal_agent.py`, `crew/manager.py`.
- **Benefit:** a crash does not delete the only copy of an open alert you might be trading by hand.
- **Effort:** S.
- **Depends on:** nothing.
- **Regression:** a genuinely empty day still loads as empty. Only exceptions change.
- **Accept:** unreadable JSON → no `ENTRY_OPENED` and the file is not replaced.
- **Test:** pass a directory with invalid JSON; assert the file is unchanged and no trade is added.

### V2-05 Make the daily breaker daily

- **Current:** counters persist; `reset_daily_counters` is unused; the comment says consecutive losses.
- **Weakness:** lock sticks forever, or a fat positive sum hides a bad day.
- **Change:** store `session_date`. When the IST date differs, reset count, R, and the lock before the first tick. Increment the loss count only for losses; set it to zero on a win if you truly want "consecutive". Write the rule in one comment that matches the code.
- **Modules:** `crew/risk_agent.py`, `scripts/live_scanner.py` or `TradeManager.__init__` path.
- **Benefit:** the halt means "this session".
- **Effort:** S.
- **Depends on:** a single clock helper. Do not add a scheduler.
- **Regression:** a lock set yesterday will clear this morning. That is the point. If you wanted a manual lock, use a different flag.
- **Accept:** date rollover resets; same date does not; two losses halt; the third tick does not enter.
- **Test:** two evaluations with the same date, then one with the next date.

## P1 — evidence and forward test

### V2-08 Make the live call match the studied call

- **Current:** `DetectorAgent` already imports `scan`, `scan_retest`, and `scan_trendline_reversal`. It passes `dynamic_targets`, `include_orh_orl`, `allow_reentry`, a 10–55 point band, and 1-minute bars for retest and trendline. `backtest()` does not. `TradeManager` then sets the target to `risk * regime.recommended_target_r`, discarding the signal's target. VWAP in the detector is computed on the bars in hand; the Yahoo backtest uses NIFTYBEES.
- **Change:** one argument set, written on a rule card, used by both the backtest and the detector. Stop replacing the target after the signal returns. Do not retune thresholds in the same change. `evaluate_sweep` can stay unused or be deleted later; it is not the daemon path.
- **Modules:** `scanner_crew/detector_agent.py`, `crew/manager.py`, `scripts/backtest_liquidity.py`.
- **Benefit:** the next statistic refers to the program that will trade.
- **Effort:** M to align the call; L to re-validate the aligned rule.
- **Depends on:** P0 accounting, so you do not "validate" with delta-0.5 rupees.
- **Regression:** dropping 1-minute retests, opening-range retests, or the target rewrite will change which Telegram alerts fire. That is a strategy change. Do it on paper and say so.
- **Accept:** a test where the same bars produce the same entry, stop, and target from the backtest helper and from the detector, before manager gates that are also in the backtest.
- **Test:** known bars, equal signals. A second test that the manager does not overwrite `target` when the card says the signal owns it.

### V2-07 Feed the post-market job or skip it

- **Current:** empty frame, PDH 23150, PDL 22950.
- **Change:** pass the 5-minute bars the scanner already stored (`odds.save_nifty_5m` writes them) and that day's PDH/PDL. If the store is empty, write "review skipped".
- **Modules:** `scripts/live_scanner.py`, `post_market_crew/post_market_manager.py`.
- **Effort:** S.
- **Accept:** empty store does not produce "zero missed setups".
- **Test:** empty input → skipped status; two bars → those bars are what the autopsy sees.

### V2-09 Label model premiums

- **Current:** Black-76 at lagged VIX, IV constant, liquidity ledger has charges and no slippage.
- **Change:** no new pricer. Column names and the summary sheet must say "constant IV model". Rank on index R only until a premium archive exists. Optionally add the same one-tick slippage floor `costs.py` already has, as a stress column, not as a new truth.
- **Modules:** `backend/scripts/backtest_liquidity.py`, report text.
- **Effort:** S for labels; M for a stress column.
- **Accept:** a reader cannot mistake the column for NSE prints.
- **Test:** existing ledger tests still pass; a string assert on the header if one exists.

### V2-02 and the order path

- **Current:** execution manager constructed and unused.
- **Change:** remove the construction. When you are ready for a broker, call `trading/orders.py` with a real adapter. Do not finish `BrokerBridgeAgent`.
- **Effort:** S to remove; L to connect a broker, and that project does not start in this roadmap's P0.
- **Accept:** `live_scanner.py` does not import `ExecutionManager`.

### V2-11 and V2-12 Position and size

- **Change:** one open position. Sizing waits until premium risk is real (after V2-03). Until then, keep 1 lot on any new paper experiment, not 3, if the operator agrees. This audit does not change the lot by itself.
- **Effort:** S for the one-position gate.
- **Test:** breakeven flag does not allow a second `ENTRY_OPENED`.

### Forward-test protocol

Not a code framework. A written rule: frozen callable, git revision, 0 parameter writes, store LTP bid/ask if you ever get them, else last and a warning, compare to the twin backtest on stored bars. Graduation is an out-of-sample interval on **option** P&L above costs, pre-declared. The October 30-trade verdict floor is necessary and not sufficient.

## P2 — research hygiene

| ID | Change | Effort | Accept |
|---|---|---|---|
| V2-10 | Catalog shows "not computed" or a path to a report, not a literal profit factor | S | API fixture has no baked `net_pnl_inr` |
| V2-15 | Deployment UI or log line when common risk caps are 0 | S | A sim report includes `caps_off: true` |
| V2-16 | `require_auth` on review, ingest, and paper orders, or document loopback-only | S | Unauthenticated POST gets 403 when tokens are required; when tokens are unset, bind stays local |
| V2-17 | Pytest in CI on the backend suite | S | A failing cost test goes red |
| V2-18 | Confirm STT and exchange rows for the dates you trade. Do not invent a rate | S | Schedule comment cites the document you checked |
| V2-19 | Remove the lunch name override or log it as an explicit exception on the alert | S | A sweep candidate inside 11:30–13:00 is rejected, unless you wrote the exception down |
| V2-14 | No code. Do not add a broker scrape | — | — |
| G-05, G-08 | Open P&L in the daily loss; say same-bar on daily reports | M | A sim with an open loss does not add risk |

## P3 — only after P0 and P1

- Archive the Groww chain you already fetch (timestamp, strike, ltp, oi, delta) so that in six months you have a premium history. Append-only. This is the cheap start of an option tape. It does not backfill 2016.
- Walk-forward on the **frozen** index rule, reported as R, with the trial counted.
- One-lot live orders through the existing order manager, after the forward protocol passes. Not before.

## Explicitly not in this roadmap

Optimizing ADX, R multiples, OTM steps, or the stop buffer. Adding Kite, Dhan, and Angel One adapters. A portfolio optimizer. Rewriting the crew into a new framework.
