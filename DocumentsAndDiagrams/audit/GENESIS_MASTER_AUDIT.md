# Genesis — Master Audit (Phase 11)

Audit date 13 Oct 2026. Status: **Open**, **Fixed** (code changed), **Verified** (fixed and proven by a test
that failed before the change or by a hand calculation). "Exposure today" says whether the issue can affect
money now, given that no real broker adapter exists.

## Critical

| ID | Module | File | Function | Evidence | Problem | Consequence | Proposed fix | Test | Status |
|---|---|---|---|---|---|---|---|---|---|
| G-01 | Execution | `backend/app/trading/orders.py` | `OrderManager._place_and_wait`, `_fill_leg` | Old code: cancel sent, then a re-priced order sent without checking the old one was terminal; new test sent 3 orders on the old code | Re-price while the cancelled order can still fill | Double position at a real exchange (async cancels). Exposure today: none (sandbox cancels synchronously) | Wait for a terminal status after cancel; never re-send otherwise; error → unwind/halt | `test_no_reprice_while_the_cancelled_order_can_still_fill` | **Verified** |
| G-02 | Review | `backend/app/agents/review_crew/graph.py` | `_verdict_node` | Verdict ignored `data_source`; `approvals.py:119` skips the acknowledgement for "Profitable" | "Profitable" from synthetic Bachelier prices | Approval of a strategy with no market evidence, without a warning | Label "Synthetic data only" when not `nse_eod` | `test_synthetic_data_is_never_called_profitable` | **Verified** |
| G-03 | Review | `backend/app/config.py` | `verdict_min_*_trades` | Defaults 3 backtest / 2 forward | "Profitable" on five trades | Materially misleading verdict | Defaults 30 / 10 | `test_a_handful_of_winning_trades_is_too_few_to_judge` | **Verified** |
| G-04 | Strategy/live parity | `agents/trade_crew/graph.py`, `market/feed.py`, `engine/sim.py` | `_entry`, `BrokerFeed.snapshot`, `simulate` | Backtest decides and fills at the close; the crew ticks every 60 s from 09:15 on intraday spot, current VIX and the day's high/low so far | Live trades are not the trades the backtest measured | Backtest evidence does not apply to live results. Exposure today: paper only | An entry window setting (e.g. 15:15–15:25) for EOD rules, or an intraday engine | Crew test: no entry outside the window | Open |

## High

| ID | Module | File | Function | Evidence | Problem | Consequence | Proposed fix | Test | Status |
|---|---|---|---|---|---|---|---|---|---|
| G-05 | Risk | `engine/sim.py`; `agents/trade_crew/graph.py` | `simulate` (AccountState); `_risk_checks` | Sim enters only on days with no exit, so `pnl_today` = 0 at entry; live sums closed trades only | Daily loss limits ignore open losses and never bind in backtests | A large open loss does not stop new entries elsewhere | Realized today + change in open MTM since day start, in both | Sim and crew test with an open loss | Open |
| G-06 | Risk | `agents/trade_crew/legs.py` | `entry_economics` | Undefined-risk structures: `max_loss = stop` | True risk is unbounded; stops fill at the close (hand check: 25 % overshoot) | Sizing and `max_loss_pct_of_capital` understate risk for strangles/straddles. Live: excluded by default | Stress-based max loss for undefined risk | Unit test per structure | Open |
| G-07 | Backtest | `engine/sim.py` | `simulate` | No margin model; real run: drawdown 46–51 % of ₹50k capital on a 2-lot condor | Returns on capital that could not hold the position | Inflated return %, meaningless drawdown % | Approximate margin per structure; refuse entries above capital | Sim test: entry refused | Open |
| G-08 | Backtest | `engine/sim.py` | `simulate` | Decide on the close, fill on the same close | Same-bar fill; NSE close is a 30-min average | Optimistic fills | Document; stress with extra ticks; next-day option open (needs OPEN column import) | Sensitivity run | Open (documented in reports) |
| G-09 | Costs | `backend/app/engine/costs.py` | `cost_breakdown`, `leg_costs` | 5 bps of a ₹20 premium = ₹0.01, under one tick | Slippage understated | Optimistic net P&L for low-premium legs | Floor of `SLIPPAGE_MIN_TICKS` (1) ticks per unit per fill | `test_one_sell_fill_charges_by_hand` | **Verified** |
| G-10 | Execution | `trading/orders.py` | `OrderManager._send` | `place_order` called before the order row is written | Unknown outcome not recorded | Audit gap; safety relies on halt + reconciliation (both verified) | Write SUBMIT_PENDING first; UNKNOWN state; never retry | Adapter that raises after accepting | Open |
| G-24 | Backtest | `engine/sim.py`, review | `_metrics` | Reports had no PF, expectancy, Sharpe, Sortino, Calmar, streaks, exposure | Could not judge edge vs luck | Decisions on net P&L alone | `engine/metrics.performance` + bootstrap CI | `test_performance_matches_hand_calculation` | **Verified** |
| G-27 | Review | `agents/review_crew/hard_stop.py` | `pick_stop` | Picks by backtest + forward net | Forward window used for selection | The "out-of-sample" check of the recommended stop is in-sample | Select on backtest only; report forward as a check | Unit test on `pick_stop` | Open |
| G-12 | Validation | `engine/sim.py`, review graph | `split_dates` | Single 70/30 split, no walk-forward | Weak evidence | Overfit strategies pass | Rolling walk-forward windows | Known-overfit fixture fails | Open |

## Medium

| ID | Module | File | Function | Evidence | Problem | Consequence | Proposed fix | Test | Status |
|---|---|---|---|---|---|---|---|---|---|
| G-11 | Data | `market/feed.py`, `market/historical.py` | `BrokerFeed.snapshot`, `option_price` | Only the index quote is aged; option close is last trade of the day | Stale/asynchronous option prices | Wrong marks, bad fills | Per-quote age; prefer settlement or flag low-volume closes | Feed tests | Open |
| G-13 | Data/ops | `market_eod.db` | `event_lookup` | `market_events` = 0 rows | Calendar not imported | Blackout strategies never enter; LLM context lacks events | Run `scripts/import_events.py`; warn when calendar ends before today | — | Open |
| G-14 | Costs | `config.py` | `stt_schedule` | Schedule ends 2024-10-01 | 2026 STT change not in schedule | Costs understated if rates rose | Verify Finance Act 2026 rates; add a dated entry | Cost test per date | Open, **UNVERIFIED** |
| G-15 | Security | `README.md`, `audit/middleware.py` | — | `--host 0.0.0.0`; open research/paper endpoints without tokens | LAN exposure | Anyone on the network can run reviews, paper orders, ingest | Bind 127.0.0.1 by default | — | Open |
| G-16 | Code | `backend/app/**` | — | Pyright: 264 errors | Latent `None` paths in money code | Runtime errors in rare paths | Fix orders/policy/decisions first; add to CI | Pyright in CI | Open |
| G-19 | Paper | `brokers/paper.py` | `PaperBroker.place_order` | Fills every LIMIT at once | Optimistic paper results | Paper ≠ live | Use sandbox broker for paper track records | — | Open (documented) |
| G-20 | Data | `market/store.py` | `import_vix` | India VIX used for BANKNIFTY IV rank | Wrong vol series for BANKNIFTY | Mis-timed BANKNIFTY entries | Bank Nifty IV from ATM options, or document | — | Open |
| G-21 | Portfolio | `agents/trade_crew/graph.py` | `_risk_checks` | Limits are counts only | Stacked/opposite exposure across deployments | Correlated tail losses | Per-underlying and net vega caps | Crew test | Open |
| G-26 | Engineering | repo | — | No CI config | Tests not enforced | Regressions | CI workflow | — | Open |
| G-28 | Concurrency | `agents/trade_crew/graph.py` | `_opinion`, `_reason`, `_execute` | `rollback()` before LLM calls, no re-check before writing the intent | Portfolio limit race with LLM steps on | Exceed `PORTFOLIO_MAX_OPEN_POSITIONS` | Re-run portfolio checks in `_execute` | Two-crew test | Open |

## Low

| ID | Module | File | Problem | Proposed fix | Status |
|---|---|---|---|---|---|
| G-17 | Code | `agents/trade_crew/graph.py:171` | Freeze-quantity lookup failure swallowed (`except: pass`) | Log it | Open |
| G-18 | Security | `market/news.py` | stdlib XML on RSS | `defusedxml` if a dependency is acceptable | Open |
| G-22 | Data | `market/hours.py` | Unlisted holiday = trading day | Warn when the holiday file has no entries for the current year | Open |
| G-23 | Live | `graph._risk_checks` | `now.date()` in UTC for "today" (correct only inside session hours) | Use IST date | Open |
| G-25 | Execution | `trading/orders.split_quantity` | Freeze below lot size gives non-lot chunks | Refuse | Open |
| G-29 | Dead code | `engine/costs.apply_trade_costs`, `src/trading_strategy_lab_backend` | Unused | Delete | Open |

## Verified facts vs assumptions

**Verified by this audit:** test results; tool outputs; DB integrity numbers; IV rank correctness (300 samples);
one real trade's prices, P&L and charges by hand; example-strategy results on real data; every "Open" item's
evidence (read in code).

**Assumptions / UNVERIFIED:** 2026 STT change (G-14); whether Indian brokers deduplicate client order tags;
NSE rate limits; the real SPAN margin of the example condor (not looked up; the claim that ₹50k is too little is
an estimate); research-folder results for intraday strategies.
