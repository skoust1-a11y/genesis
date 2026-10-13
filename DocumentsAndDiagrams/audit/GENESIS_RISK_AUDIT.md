# Genesis — Risk Management Audit (Phase 4)

## 1. Where risk is enforced

All entry risk goes through one function pair used by both the simulator and the live crew:
`app/trading/decisions.entry_risk_checks` → `app/strategies/rules.check_risk`, plus policy, ladder, floor,
odds and margin checks added in `graph._risk_checks` (live) and `sim.simulate` (backtest). A strategy cannot
skip them: `_execute` sends orders only when every check passed, and the LLM opinion runs after risk and can
only reduce lots or veto (`_clamp` re-runs the risk checks on a reduced proposal).

Common limits from `.env` are merged with the strategy's own using "the stricter wins" (`with_common_risk`).

## 2. Limits

| Limit | Implemented | Fail-closed? | Notes |
|---|---|---|---|
| Max risk per trade | `max_loss_pct_of_capital`, risk-% sizing | Yes | Understated for naked shorts: `max_loss` = stop amount (G-06) |
| Max daily loss (strategy) | `max_daily_loss_pct` | Yes | **Realized only**; never binds in backtests (G-05) |
| Max daily loss (portfolio) | `PORTFOLIO_MAX_DAILY_LOSS_INR` | Yes | Realized only (G-05) |
| Max weekly loss | — | n/a | Not implemented |
| Max drawdown | Per-deployment ladder + global `MAX_DRAWDOWN_PCT` (20) pause; capital floor with optional close-all | Yes | Good |
| Max open positions | Per strategy and portfolio (`PORTFOLIO_MAX_OPEN_POSITIONS`, 3) | Yes | Counts positions, not exposure |
| Exposure / concentration | — | n/a | No aggregate delta/vega, no per-underlying cap (G-21) |
| Stop-loss present | Schema requires target + stop | Yes | Stops are checked at tick (live) or close (backtest); broker-side protective stop-limits on short legs live |
| Min risk/reward | `min_risk_reward` | Yes | |
| Margin | Live: `available ≥ required × MARGIN_BUFFER`; broker unable to answer → fail | Yes | Backtest: none (G-07) |
| Gap risk | Protective stop-limits can be gapped through (tested: crew exits next tick) | Partly | Naked shorts excluded from live by default |
| Volatility sizing | `vix_sizing`; no VIX reading → most careful band | Yes | |
| Consecutive losses | Streak + cooldown in trading days | Yes | |
| Session restrictions | Market hours + holidays (broker feed) | Yes | |
| Emergency shutdown | Kill switch (stop, cancel, optional flatten), halt on any crew error, supervisor stops crews of non-started deployments | Yes | |

## 3. Scenario tests (can a trade still go out?)

| Scenario | Result | Evidence |
|---|---|---|
| Market data stale | Blocked: tick stops with `stale_market` | `graph._observe`; existing tests |
| Stop-loss missing | Impossible: rules fail validation | `StrategyRules._target_and_stop` |
| Invalid position size | Blocked (`risk.max_lots`, sizing min lots; 0 lots → blocked intent) | `check_risk`, `size_lots` |
| Daily loss breached | Blocked **only if the loss is realized** | G-05 |
| Broker unavailable | `place_order` raising → crew error → deployment halted → supervisor does not restart a halted deployment; margin query failing → `risk.margin` fails | `runner.run`, `_margin_check` |
| Position already open | Blocked by `max_open_positions` | `_entry` |
| Conflicting strategies | **Allowed**: two deployments may hold opposite or stacked positions on the same underlying, limited only by the portfolio position count | G-21 |
| Missing quotes / illiquid leg | Blocked (`market.quotes`, `market.liquidity`) | `entry_risk_checks` |
| No event calendar with a blackout rule | Blocked | `check_blackout` |

## 4. Assessment

The design is sound: one central set of pure checks, fail-closed defaults, defined-risk-only live, stricter-wins
merging. The gaps are in **what the checks measure** (realized-only daily loss, stop-as-max-loss for naked
shorts, no exposure aggregation, no margin in backtests), not in whether they are enforced.

## 5. Recommendations (in roadmap order)

1. Daily loss = realized today + change in open MTM since the day's start, in both sim and live (P0-3).
2. `max_loss` for undefined-risk structures from a stress move (e.g. 3 × expected daily move or a fixed % gap)
   rather than the stop; keep them off live (P0-4).
3. Approximate SPAN margin per structure in the backtest; refuse entries whose margin exceeds capital (P1-2).
4. Portfolio cap per underlying and on net short vega/delta across deployments (P2).
5. Weekly loss limit, only if the daily limit plus ladder proves insufficient in paper trading.
