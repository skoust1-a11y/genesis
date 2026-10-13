# Genesis — Improvement Roadmap

Finding IDs refer to `GENESIS_MASTER_AUDIT.md`. Do not start a lower tier while an item above it is open,
except where marked "parallel".

## Done in this audit

| Item | Finding | Files |
|---|---|---|
| No re-price while a cancelled order can still fill | G-01 | `backend/app/trading/orders.py`, `backend/tests/test_live_plumbing.py` |
| Synthetic-data verdict cannot be "Profitable"/"Conditional" | G-02 | `backend/app/agents/review_crew/graph.py`, `frontend/src/app/lab/page.tsx`, `backend/tests/test_reviews_improvements.py` |
| Verdict needs 30 backtest + 10 forward trades | G-03 | `backend/app/config.py`, `backend/tests/test_trades.py` |
| Slippage floor of 1 tick per unit per fill | G-09 | `backend/app/engine/costs.py`, `backend/app/config.py`, `.env.example` |
| Performance statistics + bootstrap CI in every scenario | G-24 | `backend/app/engine/metrics.py` (new), `backend/app/engine/sim.py`, review graph, `backend/tests/test_metrics.py` (new) |

## P0 — Financial safety and correctness

1. **G-04 Entry window for EOD strategies.** Add an `entry_window` (e.g. 15:15–15:25 IST) so live entries happen
   when the backtest's assumptions roughly hold. Must land before any paper track record is used for approval.
2. **G-10 Write-ahead orders.** Persist the order (SUBMIT_PENDING) before calling the broker; on exception mark
   UNKNOWN, never retry, halt. Required before any real adapter.
3. **G-05 Daily loss with open MTM**, in the shared `AccountState` so sim and crew agree.
4. **G-06 Max loss for undefined-risk structures** from a stress move; keep them off `LIVE_ALLOWED_STRUCTURES`.
5. **G-28** Re-run portfolio checks inside `_execute` after any lock release.

## P1 — Data and backtesting reliability

1. **G-27** Hard-stop sweep: select on the backtest window only.
2. **G-07** Approximate margin per structure; refuse entries over capital; report return on margin.
3. **G-13** Import the event calendar; warn when it ends before today. **G-14** verify and date the 2026 STT rates.
4. **G-12** Walk-forward: rolling 2-year train / 6-month test windows; report each fold's stats and the pooled
   out-of-sample result. Add trade-sequence Monte Carlo for the drawdown distribution.
5. **G-11** Per-quote age live; flag low-volume closes historically; data-completeness report per review.
6. **G-08** Sensitivity: rerun each verdict at +2 ticks slippage; a strategy that flips sign is "fragile".

## P2 — Strategy quality and quantitative research (after P0/P1)

1. Realized-vs-implied volatility filter; trend/range filter. Each validated walk-forward, one at a time.
2. Strike distance as a multiple of the expected move instead of fixed points.
3. **G-20** BANKNIFTY IV from its own options (or restrict IV-rank rules to NIFTY).
4. **G-21** Per-underlying and net short-vega caps across deployments.

## P3 — Architecture and engineering (parallel to P1/P2)

1. **G-26** CI: pytest, ruff, pyright (ratcheted), frontend test/tsc/eslint, pip-audit.
2. **G-16** Pyright errors in `trading/` first.
3. Split `trade_crew/graph.py` (booking, stops, opinion) when next touched.
4. **G-15** Bind to 127.0.0.1 by default. **G-17, G-22, G-23, G-25, G-29** small fixes.

## P4 — Advanced analytics (only once a strategy survives walk-forward)

1. Portfolio backtest of several deployments with shared limits.
2. Real broker adapter behind the gate list in `GENESIS_EXECUTION_AUDIT.md` §5.
3. Intraday engine (opening range, VWAP) if the research strategies justify it.
4. Dashboard "why" view (see below).

## Dashboard (Phase 10)

What exists: `/lab` (verdict badge, scenarios), `/deploy` (shows window and `data_source`), `/deployments`
(mode column, kill switch), `/markets` (candles, paper orders).

Problems found:
- `/lab` shows the verdict without the data source or assumptions; fixed for the synthetic case (amber
  "Synthetic data only"), but the window, trade count and the new `stats` (expectancy CI) are not displayed.
- Backtest, paper and live are distinguished only by a text `mode` column; nothing visually separates paper
  P&L from live P&L.
- No data-freshness indicator (last imported date, event calendar end).

Recommended: per decision, show the entry checks with actual vs threshold (already stored in
`entry_conditions`/`risk_checks`), the failed checks for blocked intents, and the expectancy CI next to every
verdict. Colour-band or badge every P&L by source: BACKTEST / PAPER / LIVE.
