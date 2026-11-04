# Project maturity scorecard

Audit date: 4 Nov 2026. Scores are for **trading use**, not for how many files exist. 0 means absent or actively misleading. 5 means a personal research tool that knows its limits. 8 means you could defend it to a risk committee for a small live book. Nothing here is an 8.

Evidence is the companion files in this folder. October statistical numbers are prior measurements, not a new run.

| Category | Score | Why this number |
|---|---|---|
| Overall architecture | 4 | Two real stacks. The research one is coherent. The daemon one journals signals and narrates an execution crew it does not call. |
| Market data infrastructure | 4 | Spot history and a lot-size table exist. Option history does not. Live quotes are a scraped last trade. Gaps are not policed. |
| Quantitative research | 5 | Costs, a daily simulator, a skeptical October study, and a metric function with a confidence interval. Undone by a hardcoded catalog and a fake walk-forward that writes config. |
| Strategy engine | 4 | `scan()` is a real state machine and the detector calls it. Live then caps risk, changes targets, and adds timeframes the study did not run. |
| Backtesting accuracy | 3 | Index path for `scan()` was checked for look-ahead in October. Option fills are a constant-IV model. Same-bar daily fills remain in `sim.py`. No spread on the liquidity ledger. |
| Statistical validation | 4 | One honest negative-to-inconclusive study, filed and dated. It does not cover the code that runs now. No experiment registry. |
| Options analytics | 3 | Black-76, implied vol, put-call basis, and a written bias comment. Live P&L throws that away and uses delta 0.5. |
| Risk management | 3 | Several gates are real (VIX, time, stale bar, walls, 55-point cap). The daily breaker does not reset, the rupee cap is not applied, and the safety wall accounts fictional profits. Research defaults for streak and loss percent are off. |
| Paper trading | 3 | A journal and Telegram exist. Fills, charges, and partials do not. Three stores can disagree. |
| Execution infrastructure | 2 | Sandbox order manager exists and is not what the daemon uses. The live branch returns `FILLED` with no I/O. Score is not 1 only because the research order manager and the "enabled" flag default are real. |
| Portfolio management | 2 | At most two scanner bets, the second only after breakeven. No net delta, no vega, no shared limit with the research book. |
| Performance analytics | 4 | `engine/metrics.py` covers the usual ratios for a daily curve. The numbers an operator sees each evening do not. |
| Reliability | 3 | Loop survives exceptions. Journal and risk files can be destroyed by a bad read followed by a save. No lock, no IST clock. |
| Security | 4 | Hashed tokens and authenticated broker routes. Research and paper routes are open. Acceptable on localhost, not on a LAN. |
| Testing | 5 | Dozens of tests, including order plumbing and signal cases. No CI. No test that the live broker branch must fail. |
| Documentation | 4 | A lot of it, including a prior audit that argues against an edge. Module banners and the October architecture note describe a system the process does not run. |
| Monitoring | 3 | Logs, Telegram, a daily markdown file. The file can be healthy with an empty price frame. |

## Strongest pieces

- `engine/costs.py`: named charges, schedules, tick floor, side-aware STT and stamp.
- `engine/metrics.py`: ratios that become `None` instead of infinity, plus a bootstrap interval.
- October liquidity audit: tried to break the result, and said the edge was not shown.
- Live vetoes for missing VIX, stale bars, and missing walls.
- `groww.py` telling the reader it is not a broker.
- Research kill switch and order manager, as a design, behind `LIVE_TRADING_ENABLED=false`.

## Weakest pieces

- Scanner P&L and the safety wall (V2-03, V2-04).
- Broker bridge live branch (V2-01).
- Parameter agent (V2-06).
- Live/research argument split on the same signal functions (V2-08).
- Active-trade file recovery (V2-13).

## Critical trading risks

1. A person treats the daily audit rupees as option P&L and sizes up.
2. Someone sets the live broker flag and believes the `FILLED` status.
3. The daily loss lock never clears, or never trips, because the counters are not daily.
4. Hand-traded alerts are unmanaged when the process is down.

## Biggest research limitations

No option tape. One statistical study, on rules the daemon no longer trades. Trial count not attached to later ideas. Catalog numbers that do not come from the engine.

## Production readiness

As a **localhost research notebook with a UI**: usable, with the catalog and the daily audit treated as untrusted.

As an **unattended paper process that must not lie**: not ready, until V2-03, V2-06, and V2-07 are fixed so the reports match the data.

## Live trading readiness

**Not ready.** Score for "may send orders": 0 of the conditions in the executive summary are met. Software completeness is not the reason. The fill model and the evidence are the reason.
