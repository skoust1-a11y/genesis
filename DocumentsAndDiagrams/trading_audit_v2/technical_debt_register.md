# Technical debt register

Audit date: 4 Nov 2026. This register does not delete or replace `DocumentsAndDiagrams/audit/` or `DocumentsAndDiagrams/liquidity_strategy_audit/`. October IDs are repeated only where this session re-saw the code.

Status: **Open** means visible in the current source. **Prior** means filed in October and not re-tested line by line today.

| ID | Status | Debt | Where | Trading effect | Smallest payoff |
|---|---|---|---|---|---|
| V2-01 | Open | Live place_order returns FILLED | `execution_crew/broker_bridge_agent.py` | False position if the flag is on | Return rejected until an adapter exists. One test |
| V2-02 | Open | ExecutionManager unused | `scripts/live_scanner.py` | Operators think an order was routed | Do not construct it. Log "signal only" |
| V2-03 | Open | P&L = points × 0.5 × qty | `crew/journal_agent.py` `record_exit` | Every rupee report is wrong as an option result | Store points and a separate premium field. Leave premium null when unknown |
| V2-04 | Open | Safety wall takes 50% of V2-03 | `crew/risk_agent.py` `record_trade_outcome` | Imaginary equity, and the briefing's lot advice | Stop the ratchet until premium P&L exists. Freeze the JSON |
| V2-05 | Open | `reset_daily_counters` has no caller | `crew/risk_agent.py` | Breaker stuck or never fires | Call it when the IST date changes, and store that date in the file |
| V2-06 | Open | Canned walk-forward writes the stop buffer | `post_market_crew/parameter_agent.py` | Next day's stop is fit to a slogan | Stop writing. Recommendations may be printed as opinions |
| V2-07 | Open | Review called with empty bars and PDH 23150 / PDL 22950 | `scripts/live_scanner.py` around the 15:31 branch | "Zero misses" and a parameter update from nothing | Pass the day's stored bars or skip the job |
| V2-08 | Open | Live calls `scan` / `scan_retest` / `scan_trendline_reversal` with other arguments (dynamic targets, OR retests, re-entry, 1m bars, risk cap 55) and `TradeManager` replaces the target | `scanner_crew/detector_agent.py`, `crew/manager.py`, `scripts/backtest_liquidity.py` | October R is not the live trade | One call signature shared by the backtest and the detector, and stop rewriting the target after the signal |
| V2-09 | Open | Constant-IV Black-76, no slippage in the liquidity ledger | `scripts/backtest_liquidity.py` | Option workbook ≠ tradable P&L | Title the column "model premium". Do not rank parameters on it |
| V2-10 | Open | Catalog performance literals | `strategies/analyzer.py` `RESEARCH_CATALOG` | UI shows an edge the engine did not just compute | Remove the numbers or load them from a dated report file |
| V2-11 | Open | Fixed 3/4 lots; loss figure unused | `RiskAgent.evaluate_order` | Size ignores capital | Reject when premium-at-risk exceeds a written percent. Until premium exists, reject on a conservative point value you document |
| V2-12 | Open | Second position after breakeven | `TradeManager` `can_take_entry` | Doubled NIFTY risk | Allow one position |
| V2-13 | Open | Empty load then save; RAM idempotency set | `JournalAgent`, `TradeManager` | Lost book; duplicate journal after restart | Refuse to save if the load failed. Persist signatures for the IST day |
| V2-14 | Open | Scraped LTP, no bid/ask | `brokers/groww.py` | Fills and walls depend on a website layout | Keep the ponytail comment. Do not trade on it |
| V2-15 | Open | Common risk defaults are 0 | `config.py` | Sim can run with no streak or loss cap | Set non-zero defaults only if you want them on every strategy. Otherwise the deployment screen must show "caps off" |
| V2-16 | Open | Research and paper routes have no token | `api/routes.py`, `api/market.py` | LAN clients can run reviews and paper orders | Depend on `require_auth`, or bind loopback |
| V2-17 | Open | No CI | repo root | Regressions | One workflow: the existing pytest suite |
| V2-18 | Open | STT schedule last row 2024-10-01 | `config.py` `stt_schedule` | Costs may be stale. **Rate for 2026 was not verified this session** | Check the Finance Act figure before the next quoted backtest |
| V2-19 | Open | Lunch freeze waived by substring | `TradeManager` Gate B | The control does not match its comment | Delete the override or name the exception in the alert |
| V2-20 | Open | Banners and architecture note over-claim | crew modules, `CURRENT_SYSTEM_ARCHITECTURE_ANALYSIS.md` | False operational picture | Edit the banners when you touch the files. Not a drive-by rewrite |
| G-05 | Prior, re-seen | Daily loss uses closed P&L only; sim skips entry on a day that already exited | `engine/sim.py` | Limit does not bind the way the name suggests | Include open P&L. Still open |
| G-07 | Prior | No margin in the daily sim | `engine/sim.py` | Short-premium returns on too little capital | Not re-simulated |
| G-08 | Prior, re-seen | Signal and fill share the day's snapshot | `engine/sim.py` | Optimistic daily fills | Document on every report. Next-bar fill is the later fix |
| G-10 | Prior | Order row after `place_order` | `trading/orders.py` | Crash window with an unknown order | Not re-read in full |
| G-12 | Prior | Single 70/30 split | review / sim | Weak OOS | Not re-tested |
| G-13 | Prior | `market_events` empty | `market_eod.db` | Blackout rules never fire | Not re-counted |
| G-14 | Prior | Same as V2-18 | costs | — | — |
| G-19 | Prior | Paper broker fills limits fully | `brokers/paper.py` | Paper track record too clean | Not re-tested |

## Ponytail comments already in the code

`groww.py` states the page can change and that there is no VIX on that feed. Those are accepted limits, not new defects, as long as nothing places orders on that client. The VIX gap is partly closed elsewhere (Yahoo VIX in the manager) and still fails closed when Yahoo fails. Keep that.

## Debt that is not worth paying now

Rewriting crew class names, adding a message bus, deleting `vendor/TradingAgents`, or rebuilding Black-76. A better normal CDF will not fix V2-03.
