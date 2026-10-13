# Genesis — Backtesting Engine Audit (Phase 3)

Engine: `app/engine/sim.py` (`simulate`, `run_window`), costs `app/engine/costs.py`, windows and verdict
`app/agents/review_crew/graph.py`.

## 1. Bias and realism checks

| Check | Finding | Status |
|---|---|---|
| Look-ahead in signals | IV rank and previous-day range use only past and same-day data (verified). The odds gate counts only trades closed before the signal day | PASS |
| Same-bar decision and fill | Rules are checked on the close and filled at the same close. The close, VIX close and day high/low are known only after 15:30 (G-08) | WARNING |
| Intrabar stop/target ordering | Daily marks only: a stop or target is seen at the close, never intraday. Losses overshoot stops (hand-checked trade: −₹4,462.50 against a −₹3,577.50 stop) and intraday stop-outs that recovered by the close are missed | WARNING (documented in every report) |
| Entry and exit prices | Bhavcopy close if the contract traded, else settlement (entries on settlement prices are refused) | PASS, but asynchronous to the index close (G-11) |
| Survivorship | Not applicable to index options | PASS |
| Data leakage between windows | 70/30 date split. **The hard-stop sweep picks its recommended stop by backtest + forward net**, so the forward window is used for selection (G-27) | FAIL |
| Overfitting | Up to 24 scenario variants (±5 IV rank, ±1 strike step, stop/target ×0.5–1.5); the verdict needs all variants positive for "Profitable", which is conservative. No multiple-testing adjustment; no walk-forward | WARNING |
| Transaction costs | Brokerage per order, STT on sells (dated), exchange (dated), GST, SEBI, stamp on buys, exercise STT at expiry | PASS (hand-checked to the paisa) |
| Slippage | Was 5 bps of premium = under one ₹0.05 tick below ₹100 premium. **Now** at least 1 tick per unit per fill (`SLIPPAGE_MIN_TICKS`) | FIXED (G-09) |
| Bid/ask spread | Not modelled separately (no EOD bid/ask); the tick floor is the proxy | WARNING |
| Liquidity / partial fills | Entries refused on untraded (settlement-priced) legs; no volume cap on size; fills always complete | WARNING |
| Margin | **None.** Returns and drawdowns are on `DEFAULT_CAPITAL_INR`, even when the position's margin exceeds it (G-07) | FAIL |
| Daily loss limit | Entries only happen on days with no exit, so `pnl_today` is always 0 at entry: `risk.max_daily_loss_pct` can never bind in a backtest (G-05) | FAIL |
| Concurrency | `max_open_positions` per strategy; one strategy per run | PASS |
| Compounding | Optional policy (`sizing_capital`), capped | PASS |
| Overnight exposure | All positions are overnight by design (EOD) | Note |
| Window end | Open positions closed at the last day's quotes and labelled `window_end` | PASS |

## 2. Metrics

Before this audit a report had net/gross P&L, costs, win rate, closed-trade and MTM drawdown, drawdown
duration, per-year P&L and skipped-signal counts. Added in `app/engine/metrics.py` and carried in every
scenario's `stats`:

gross profit, gross loss, profit factor, expectancy (+ 95 % seeded bootstrap CI), average win, average loss,
max consecutive losses, return %, annualised return %, Sharpe, Sortino, max drawdown % of capital, Calmar,
recovery factor, exposure % (days with an open position).

Definitions: daily return = change in mark-to-market P&L ÷ initial capital (no compounding);
Sharpe = mean/sample stdev × √252; Sortino = mean/downside deviation × √252; Calmar = annualised return ÷ max
drawdown %. Ratios with nothing to divide by are `None`, never infinite.

**Not calculated, and why:**
- Average drawdown, turnover, monthly table: not added (low value at EOD frequency; easy follow-ups).
- Performance by market regime: needs a regime definition first (Phase 6 proposal).
- Benchmark comparison: a short-premium strategy has no natural benchmark; NIFTY buy-and-hold is not comparable.
- Sharpe/Sortino with < ~30 trades are noise; the CI on expectancy is the number to read.

## 3. Validation methods

| Method | Status |
|---|---|
| In-sample / out-of-sample | Present: chronological 70/30 split |
| Walk-forward / rolling windows | Not implemented (roadmap P1-4) |
| Parameter sensitivity | Present in a limited form (scenario variants) |
| Monte Carlo trade resampling | Partial: bootstrap CI of mean trade P&L (added). Sequence resampling for drawdown distribution: roadmap |
| Minimum sample | Was 3 backtest + 2 forward trades for "Profitable". **Now 30 + 10** (`VERDICT_MIN_*`) (G-03, fixed) |

## 4. Independent validation

- Hand-calculated real trade (2020-02-26 → 2020-02-28): strikes, prices, gross P&L and ₹356.32 of charges
  all match the engine exactly (see the strategy audit).
- Existing tests already hand-check: stop through a gap fills at the close, expiry settles at intrinsic with
  exercise STT only, MTM drawdown sees what closed-trade drawdown hides, and the simulator and Trade Crew
  take the same trades on the same data.
- New tests: `tests/test_metrics.py` (statistics against the `statistics` module and hand values; one fill's
  full charge breakdown; the tick floor vs bps).

## 5. Is a backtest trustworthy today?

**Partly.** The data and the arithmetic are right. The results are still optimistic because of close fills,
no margin, daily-close stops and realized-only daily loss, and the hard-stop recommendation leaks the forward
window. Treat any verdict as "worth paper trading", never as "worth capital".
