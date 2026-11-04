# Phase 2 executive summary

Date: 4 Nov 2026. This pass re-read the current source. It did not change application code, strategy rules, databases, or `trades/*.json`. It did not place an order or open `.env`.

Prior write-ups in `DocumentsAndDiagrams/trading_audit_v2/` and `DocumentsAndDiagrams/audit/` were treated as claims. Each claim in the brief was checked again. The classification is in `forensic_findings_revalidation.md`.

## Verdict

**Automated live trading is not allowed.** Research simulation can stay on. The scanner may keep sending Telegram only if every rupee figure is labelled as a 0.5-delta model, which it is not today.

## What was reconfirmed in this session

| Claim | Result |
|---|---|
| Live `place_order` can return `FILLED` with no broker call | Confirmed in `BrokerBridgeAgent.place_order`. Not on today's daemon path: `live_scanner.py` constructs `ExecutionManager(is_live_broker=False)` and never calls it |
| Journal rupees = index points × 0.5 × quantity | Confirmed, `JournalAgent.record_exit` |
| Safety wall takes 50% of that rupee number | Confirmed. `TradeManager` passes `pnl_rupees` into `RiskAgent.record_trade_outcome` |
| Reserve vs ₹50,000 base | Confirmed in `trades/daily_risk_config.json`: base 50000, reserve 213954.1, cumulative R 9.65. This session did not replay the trade log to prove every rupee of that reserve |
| Research and live calls differ | Confirmed. `backtest()` calls `scan` / `scan_retest` without dynamic targets, opening-range retests, or re-entry. The detector passes those. `TradeManager` then replaces the target |
| No historical option tape | Confirmed as an absence. Black-76 remains the historical premium. Not re-measured against NSE closes today |
| Daily counters do not reset | Confirmed. `reset_daily_counters` has no caller |
| Lots frozen at 3 or 4 | Confirmed. `TradeManager` builds `RiskAgent(frozen_baseline=True, default_lots=3)` |
| Parameter agent writes config | Confirmed. `PostMarketManager.run_daily_review` calls `save_calibrated_parameters` |
| Catalog hardcodes performance | Confirmed. `RESEARCH_CATALOG` literals in `strategies/analyzer.py` |
| Docs over-claim execution | Confirmed for the execution crew and the backtest docstring |
| No CI | Confirmed. No `.github` workflow |

A local pytest of `tests/test_pre_market_and_execution_crews.py` and `tests/test_money_rules.py` finished **24 passed in 2.78s**. Those tests build `ExecutionManager(is_live_broker=False)`. They do not fail the live `FILLED` branch.

## What to build, in order

1. Make a missing broker adapter return `REJECTED`, never `FILLED`.
2. Split index R from option rupees. Rupees only from premiums and `engine/costs.py`. Stop the safety-wall ratchet until that exists.
3. Reset risk counters on the IST date, and store that date.
4. Stop `save_calibrated_parameters` from the daemon. Proposals may be written. The live JSON may not.
5. One call signature for `scan`, `scan_retest`, and `scan_trendline_reversal` in the backtest and the detector. Stop overwriting the signal's target unless that overwrite is in the backtest too.
6. Reuse `trading/orders.py` when a broker is ever added. Do not finish `BrokerBridgeAgent`.

The designs are in the other files in this folder. Implementation waits for approval.

## Answers

1. **Good.** `engine/costs.py` itemises Indian charges and a tick floor. `trading/orders.py` is a real poll-and-persist order manager for the research deployment stack, with terminal statuses `COMPLETE`, `REJECTED`, `CANCELLED`. `scan()` is a causal state machine the detector actually imports. Missing VIX, a stale 5-minute bar, and missing walls block new scanner entries.
2. **Fundamental weaknesses.** Two capitals (scanner file ₹50,000, `Settings.default_capital_inr` default ₹5,00,000). Two P&L definitions. Two order stories, and the one the daemon constructs does not send orders. The studied call is not the live call.
3. **Unresolved previous findings.** All twelve items in the brief are still present. None is fixed. Details and the two that are latent rather than hot-path are in the revalidation file.
4. **Trust backtests?** The October index-point study of `scan()` was not rerun. Trust it only as that dated study, with its own interval crossing zero. Do not trust option-rupee columns. Do not treat it as the live scanner.
5. **Are research and live signals identical?** No.
6. **Trust options P&L?** No. The journal multiplies spot points by 0.5.
7. **Is risk financially sound?** No. The daily breaker is not daily. Size ignores the rupee loss it computes. The reserve is not a cash account.
8. **Is execution safe?** The research order manager is a reasonable simulator and defaults live trading off. The scanner bridge is not safe if `is_live_broker` is set, because it reports a fill it did not receive. Today the daemon does not call it. Safe only while nobody flips the flag or wires the call.
9. **What blocks production.** Items 1–5 above, plus no option premium ledger, no broker reconciliation on the scanner, and no CI.
10. **Stop developing.** More crew agents, more parameter auto-writes, catalog marketing numbers, smart-router banners, and any live adapter until the fill status cannot lie.
11. **First improvements.** Fake-fill refusal, P&L split, safety-wall freeze, IST reset, parameter-write ban.
12. **Target.** Closed bars → the same signal function → one rupee risk gate → `trading/orders.py` or an explicit human fill → charges from `engine/costs.py` → two reports (index R, option net). Diagrams in `target_architecture.md`.
13. **Before automated live orders.** The gates in `live_trading_readiness_gates.md`. None are met.
