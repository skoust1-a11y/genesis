# Forensic revalidation

Date: 4 Nov 2026. Method: read the current function, then search the repo for callers. A finding is **Confirmed** only when this session saw the line. **Latent** means the bad line exists and the daemon does not call it today.

No finding from the brief was marked fixed.

## F-01 Live fill without a broker call — Confirmed, latent

`BrokerBridgeAgent.place_order` (`backend/app/trading/execution_crew/broker_bridge_agent.py`).

- Flag false: returns `SIMULATED_FILLED` at the requested price (lines 61–74).
- Flag true: the comment says "Direct API call to broker adapter". The next lines build a latency number and return `success=True`, `status="FILLED"`, same price, new UUID (lines 77–90). No adapter object is used. The `except` does not run unless constructing that result throws.

`scripts/live_scanner.py` line 205 constructs `ExecutionManager(is_live_broker=False)`. A search for `execution_manager.` in that file finds no call. `execute_entry` is called from `tests/test_pre_market_and_execution_crews.py` with the flag false.

`trading/orders.py` is a different stack. It calls `adapter.place_order` and writes the row after the ack (lines 216–234). That is not the scanner bridge. October note G-10 (row written after the broker call) was not re-tested.

## F-02 Rupee P&L is points × 0.5 × quantity — Confirmed

`JournalAgent.record_exit` (`backend/app/trading/crew/journal_agent.py` lines 158–166):

```text
runner_pnl = decision.realized_points * 0.5 * (lots * lot_qty)
pnl_rupees = runner_pnl + booked_prior
```

`lot_qty` defaults to 65. `decision.realized_points` is a spot distance from `ExitDecision`. No premium, no charge, no partial-fill quantity.

`TradeManager` (manager.py lines 347–358) stores that `pnl_rupees` and passes it to `record_trade_outcome`.

`RiskAgent.evaluate_order` line 210 computes `approx_option_risk = risk_points * 0.5 * qty` and returns it as `max_loss_rupees`. Nothing in that function compares it with capital. The function rejects on the circuit breaker, on `risk_points > max_stop_points` (55), and on an absorption flag.

## F-03 Safety wall uses that P&L — Confirmed

`RiskAgent.record_trade_outcome` lines 103–107: if `is_win` and `pnl_rupees > 0`, add `pnl_rupees * 0.50` to `safety_wall_reserve`, then `save_config()`.

Caller: `TradeManager` after `record_exit`, with `is_win=(exit_decision.realized_points > 0)`. A positive spot move counts as a win even when the option lost money. The option price is not an input.

## F-04 Reserve vs ₹50,000 — Confirmed as file state

`trades/daily_risk_config.json` at read time:

| Field | Value |
|---|---|
| base_capital | 50000.0 |
| safety_wall_reserve | 213954.1 |
| effective_capital | 263954.1 |
| default_lots | 3 |
| tomorrow_approved_lots | 4 |
| daily_cumulative_r | 9.65 |
| daily_losses_count | 0 |
| is_safety_wall_locked | false |

`Settings.default_capital_inr` defaults to **500000** (`config.py`). The scanner file and the research default are not the same account.

This session did not sum the trade log and show it equals 213954.1. The writer that can produce a reserve of that kind is F-03. Provenance of this exact number: **requires a ledger replay**, not done.

`frozen_baseline=True` means the reserve does not change lot size (F-08). The number is a headline, not a sizing input, on the current daemon.

## F-05 Research and live arguments differ — Confirmed

`backend/scripts/backtest_liquidity.py` `backtest()`:

- `scan(..., stop_at, require_trendline, skip_lunch)` — no `dynamic_targets`.
- `scan_retest(..., skip_lunch)` — defaults: no `include_orh_orl`, no `allow_reentry`, `bar_minutes=5`.

`DetectorAgent.scan_multi_timeframe_strategies`:

- `scan_trendline_reversal(..., allow_reentry=True, dynamic_targets=True, bar_minutes` from the frame).
- `scan_retest(..., allow_reentry, include_orh_orl=True, dynamic_targets=True)`.
- `scan(..., dynamic_targets=True)` and only when `bar_mins >= 3`.
- Then drops risks outside 10–55. `scan`'s own `MIN_RISK` is 30, so the floor does not add sweeps; the ceiling removes wide stops.

`TradeManager.on_market_tick` lines 693–697 set

```text
target = trigger ± risk_points * regime.recommended_target_r
```

before any sniper. The signal's target is discarded.

Sniper block (lines 699+) runs only if `df_1m` is passed. `live_scanner.py` does not pass `df_1m`. **Sniper is not on the daemon path.** That part of the old narrative is not a current live behaviour.

`EntryAgent.evaluate_sweep` runs only when `candidate_setups is None` (manager.py around the fallback). The daemon passes the snapshot list, so this fallback is not the normal entry. **Partially confirmed** as dead on the daemon, still present for other callers.

VWAP: the detector builds `above` from the bars it was given (volume-weighted typical price, or cumulative typical price when volume is 0). `backtest()`'s Yahoo path uses NIFTYBEES. **Confirmed different inputs.** Not a look-ahead finding.

## F-06 Historical option prices are modelled — Confirmed as a gap

`liquidity_signals.estimated_plan` and `backtest_liquidity.option_exit` price with `black76` at a VIX input, IV held for the exit. `option_pnl.py` documents that constant IV flatters calls on a rally and understates puts on a sell-off. There is no stored historical bid/ask chain in the modules read. The October comparison of Black-76 to NSE closes was **not rerun**. Do not quote its error band as a new measurement.

The live journal does not use Black-76 either. It uses F-02. So "historical options are unrealistic" and "live rupees are not option prices" are both true and they are different defects.

## F-07 Daily risk counters do not reset — Confirmed

`RiskAgent.reset_daily_counters` is defined at line 121. Repository search finds that definition and no call. `record_trade_outcome` adds `realized_r` into `daily_cumulative_r` forever and sets the lock when the sum is ≤ −2 or the loss count is ≥ 2. A win does not zero the loss count. The comment says "consecutive". The code is not consecutive and not daily.

The file's +9.65 R with a zero loss count matches a counter that survives across sessions. It does not by itself prove no manual edit occurred.

## F-08 Frozen lots — Confirmed

`TradeManager.__init__` passes `default_lots=3`, `max_super_lots=4`, `frozen_baseline=True`. `evaluate_order` then sets lots to 4 if `is_super_trend` else 3, and skips the capital milestone loop. `is_super_trend` on the manager path is `len(df_5m) >= 28 and regime.adx >= 25`.

## F-09 Parameter agent writes canned values — Confirmed

`ParameterAgent.evaluate_recent_parameters`: a negative `realized_r` recommends buffer 10 and `expected_point_gain=+25`; two trades with R ≥ 2 recommend a 4R ceiling and `+45` points; the ADX row always says the threshold filtered 100% of false breakouts. No window is recomputed.

`save_calibrated_parameters` writes `trades/active_strategy_params.json` (or the path passed in). `PostMarketManager.run_daily_review` calls it (line 105) after the autopsy.

`live_scanner.py` reads `otm_steps` and `micro_wick_stop_buffer` from that file into the detector and `EntryAgent`. It does not read `dynamic_rr_ceiling`. The buffer is used in `EntryAgent` (sweep fallback and waterfall), not in `scan()`. So the auto-write **does** change the waterfall stop and **does not** change a normal `scan()` stop or the trend-agent target. The ceiling in the file is inert.

The daemon calls `run_daily_review` with `pd.DataFrame()`, PDH 23150, PDL 22950 (`live_scanner.py` lines 283–287). `scripts/run_post_market_audit.py` uses the same constants. The parameter write still runs on whatever trades the journal loaded, even when the price frame is empty.

## F-10 Catalog metrics are literals — Confirmed

`backend/app/strategies/analyzer.py` `RESEARCH_CATALOG`, first entry: `win_rate` 0.41, `net_pnl_inr` 192554, `profit_factor` 1.28, `max_drawdown_inr` 48500, `trades_count` 224. These are not computed in that function from `engine/sim.py`. Further entries in the same list are the same kind of literal. This session did not hit the HTTP route to see the JSON response.

## F-11 Docs do not match the process — Confirmed, examples

| Text | Code |
|---|---|
| `BrokerBridgeAgent` module doc: Zerodha, Groww, Dhan, Angel One, under 15 ms | `place_order` as in F-01. `groww.py` says it does not place orders |
| `ExecutionManager` doc: master controller for live broker execution | Not called from the daemon |
| `backtest_liquidity.py` doc: the code the live alerts run | Same functions, different arguments, then a target rewrite (F-05) |
| `ParameterAgent` doc: walk-forward and an overfitting defence | F-09 |
| `RiskAgent` comment: 2 consecutive losses | F-07 |

## F-12 No CI — Confirmed

No `.github` directory at the repo root.

## Additional confirmed item, not in the twelve

**F-13.** `JournalAgent.load_active_trades` returns `{}` on any exception (lines 54–56). `record_entry` loads, adds one trade, and saves. A corrupt file can be replaced by a one-trade file. Not executed against a corrupt file in this session. The branch is visible.

## Not reproducible / not claimed

- That 213954.1 equals the sum of historical 0.5-delta wins. Not replayed.
- October's +0.032R and its option-error percentiles. Not rerun.
- That `trading/orders.py` still double-fills on a slow cancel (October G-01 was marked fixed then). Not retested.
- 2026 STT versus `stt_schedule`'s last row `2024-10-01:0.001`. The row is confirmed. The statute was not checked.
- Whether the safety-wall file was hand-edited after the last ratchet.

## Tests this session

```text
backend/.venv/bin/pytest tests/test_pre_market_and_execution_crews.py tests/test_money_rules.py
24 passed in 2.78s
```

They do not cover F-01's true branch, F-02, F-07, or F-09.
