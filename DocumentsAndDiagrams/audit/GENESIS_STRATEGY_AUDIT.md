# Genesis — Strategy Audit (Phases 1 and 6)

## 1. What a "strategy" is here

There is no hard-coded strategy class. A strategy is a markdown document whose `rules:` block is validated
into `StrategyRules` (`app/strategies/rules.py`). The app database currently holds **0 strategies**; the
bundled example (`app/extract/extractor.py: FIXTURE_MARKDOWN`) and the research files
(`backend/research/strategies/*.md`, git-ignored, run by research scripts outside the app) are what exists.
So the audit covers (a) the rule engine every strategy runs on, and (b) the one example strategy run on real data.

## 2. Rule engine (applies to every strategy)

| Item | Implementation | Assessment |
|---|---|---|
| Entry fields | `iv_rank, dte, spot, vix, pct_from_prev_high/low, pct_high_from_prev_high, pct_low_from_prev_low` | PASS. Each is a plain comparison; a missing value fails the check (`_compare`) |
| Indicators | Only IV rank is computed. RSI/EMA/SMA/Bollinger/ATR/MACD/ADX/VWAP/Supertrend/Fibonacci/volume/S-R: **NOT IMPLEMENTED** (none is referenced by any rule field) | n/a |
| IV rank formula | `(v − min) / (max − min) × 100` over the trailing 252 India VIX closes including today; none below 20 observations or on a flat window | PASS. Recomputed independently on 300 random days of the real DB: **0 mismatches**. No look-ahead |
| IV rank source | India VIX (NIFTY 30-day implied vol) is used as the vol series for **every** underlying, including BANKNIFTY (G-20) | WARNING |
| Previous-day range | `MarketStore.previous_range`: last bar strictly before today with high and low | PASS |
| Entry timing | Backtest: checked and filled at the day's close. Live: checked every tick (default 60 s) from 09:15 on intraday spot, current VIX and the day's high/low **so far** (G-04) | FAIL (parity) |
| Expiry choice | Nearest expiry inside the DTE window, else nearest after today; never an expiry-day contract (`choose_expiry` requires ≥ 1 day) | PASS |
| Strikes | ATM = spot rounded to the strike step; shorts ± `short_distance`, wings ± `wing_width` | PASS (hand-checked on a real trade, below) |
| Exits | Expiry first (settles at intrinsic), then target/stop as % of premium or % of capital with R target, DTE exit | PASS for logic; stops fill at the close, not at the stop (documented, G-08) |
| Stop-loss | Present by validation: every rule set must have a target and a stop (`_target_and_stop`) | PASS |
| Position sizing | Fixed `max_lots`, optional risk-% sizing on `max_loss` per lot, drawdown and VIX size factors | WARNING: for naked shorts `max_loss` = the stop amount, not the true risk (G-06) |
| Risk/reward | `target / max_loss`; spreads capped at width | PASS |
| Duplicate signals | `max_open_positions` (default 1), `max_entries_per_day`; backtest enters only on days with no exit | PASS |
| Re-entry / cooldown | `max_consecutive_losses` + `cooldown_days` in trading days; `min_minutes_between_trades` | PASS (losing-streak logic covered by tests) |
| Session restrictions | Live only (`market_status` with NSE holiday file 2020–2026) | PASS |
| Event blackouts | `entry_blackout` fails **closed** when no calendar is loaded | PASS, but the market DB's `market_events` table is empty today, so any blackout strategy gets zero entries (G-13) |
| Conflicting strategies | No cross-deployment check of direction or underlying (G-21) | WARNING |

## 3. The example strategy on real NSE data

Rules: NIFTY iron condor, short strikes ATM ± 300, wings 100, enter when IV rank ≥ 40 and 7 ≤ DTE ≤ 21,
target 50 % of credit, stop 100 % of credit, 2 lots, 1 open position. Capital from `.env`
(`DEFAULT_CAPITAL_INR`, ≈ ₹50,000 implied by the results). Run with the app's own `run_window`
after the fixes in this audit (costs include the new one-tick slippage floor).

| Window | Dates | Trades | Net ₹ | Win rate | Profit factor | Expectancy ₹ (95 % bootstrap CI) | Sharpe | Max DD (MTM) |
|---|---|---|---|---|---|---|---|---|
| Backtest | 2020-01-01 → 2024-09-27 | 45 | +4,236 | 62.2 % | 1.05 | +94 (−1,234 … +1,374) | 0.07 | ₹23,004 (46 % of capital) |
| Forward | 2024-09-30 → 2026-10-13 | 13 | −16,738 | 30.8 % | 0.63 | −1,288 (−4,195 … +2,259) | −0.51 | ₹25,693 (51 %) |

By year (backtest): 2020 −2,687 · 2021 +2,729 · 2022 +10,617 · 2023 +4,011 · 2024 −10,434.

**Verdict: FAIL.** The confidence interval of expectancy contains zero in both windows; the forward window
loses; the drawdown is about half the capital; and ₹50k would not cover the margin of a 2-lot condor in practice
(G-07). Costs took 72 % of the gross P&L in the backtest (₹11,012 of ₹15,247).

### Hand-checked trade (independent of the engine)

Entry 2020-02-26, NIFTY close 11,678.50 → ATM 11,700 → legs SELL 12000 CE @ 26.20, SELL 11400 PE @ 38.25,
BUY 12100 CE @ 14.85, BUY 11300 PE @ 25.75 (all equal the bhavcopy closes), expiry 2020-03-05 (DTE 8),
IV rank 42.66. Credit = 23.85 × 150 = ₹3,577.50, so the stop is −₹3,577.50. On 2020-02-28 the closes give
P&L = (23.60 − 243.00 − 12.70 + 202.35) × 150 = **−₹4,462.50**, matching the engine. Charges recomputed by hand
(8 orders, STT 0.0625 % on sells, exchange 0.0503 %, GST, SEBI, stamp, slippage) = **₹356.32**, matching to the
paisa. The realized loss overshoots the stop by 25 %: end-of-day stops are not stops.

## 4. Status per strategy

| Strategy | Status | Why |
|---|---|---|
| Example iron condor (`FIXTURE_MARKDOWN`) | FAIL | No statistically demonstrable edge; forward loss; drawdown ~50 % of capital |
| Previous-day breakout (research, intraday) | NOT IMPLEMENTED in the app | Needs intraday candles; research notes say "break-even after realistic costs" (UNVERIFIED by this audit) |
| Liquidity sweep (research) | WARNING / UNVERIFIED | Its EOD fields exist in the app; no saved run to audit |

## 5. Quantitative research: what would materially help (Phase 6)

Only items that address a measured weakness are listed. Everything else (RSI, MACD, Supertrend, Fibonacci)
would add signals without addressing a known failure, so it is not recommended.

| Proposal | Problem it solves | How | Data | Cost | Downside | Measured by |
|---|---|---|---|---|---|---|
| Realized-vs-implied filter (VIX − 20-day realized vol of NIFTY) | Short premium only pays when implied > realized; IV rank alone ignores realized | Enter only when the spread is above a threshold | Index closes (have) | trivial | One more parameter to overfit | Walk-forward expectancy with vs without |
| Trend / range regime (e.g. 20-day ADX or close vs 50-day mean) | Condors lose in trends (2024, 2026 losses) | Skip or widen short strikes in trends | Index OHLC (have) | trivial | Lag; whipsaw | Per-regime P&L table, out-of-sample |
| Volatility regime sizing by VIX level | Tail losses cluster in high VIX | Already exists (`vix_sizing`); calibrate out-of-sample | have | none | — | Max DD and CVaR per band |
| Strike by delta / expected move instead of fixed points | ±300 points means very different risk at NIFTY 11k vs 26k | `short_distance` as a multiple of spot × IV × √(DTE/365) | have | low | Needs IV per strike for precision | Hit rate of short strikes per regime |
| Event risk filter | Gap losses around RBI/Budget/elections | Already exists; **import the calendar** (G-13) | YAML in `data/events/` | none | — | P&L of trades spanning events |
| Opening range / VWAP / breadth / sector participation | Intraday signals | Needs intraday and constituent data | not in the app | high | Only useful with an intraday engine | — |

Recommended first: realized-vs-implied filter and a trend filter, each validated walk-forward (P2 roadmap).
