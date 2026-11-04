# Architectural gap analysis

Audit date: 4 Nov 2026. Compared with a small systematic book (one person, one underlying, listed options), not with a multi-strategy hedge fund. Anything a fund would build for hundreds of symbols is marked optional.

| Capability | Now | Why it matters | Problem it solves | Must or optional | Effort | Priority |
|---|---|---|---|---|---|---|
| One call signature shared by research and live | Detector already imports `scan`, `scan_retest`, and `scan_trendline_reversal`, then changes targets and filters | Otherwise the October number is about a different trade | V2-08 | Must | Medium. Pass the same arguments. Stop replacing the target in the manager. Quarantine `evaluate_sweep` if nothing calls it with `candidate_setups is None` | P1 |
| Closed-bar event loop | 10-second poll, bar drop is ad hoc | Duplicate work and clock skew | Missed and double signals | Must for live. Optional while Telegram-only | Small | P1 |
| Option premium store | Absent | Spot R cannot certify an option book | False profitability | Must before option-rupee claims. You may trade spot research without it if you stop publishing rupees | High (data vendor or a daily chain archive you start now) | P1 |
| Spread-aware fill | Last or model mid | Round trip can exceed the edge | Optimistic backtests | Must before live. One tick plus a fixed spread is enough at this size | Small once premiums exist | P1 |
| Single risk gate in rupees, IST day | Two gates, one of them not daily | A strategy cannot be trusted to size itself | V2-05, V2-11, V2-12 | Must | Small | P0 |
| Order object with unknown/acked/filled/rejected | Exists only on the research stack | Restart and duplicates | V2-01, V2-02, V2-13 | Must before any broker. Not required for a signal diary if the diary says so | Medium. Reuse `trading/orders.py`. Do not grow the execution crew | P0 |
| Kill switch on the same order object | Research only | Process must flatten what it opened | Unmanaged residual | Must before live | Small once there is one order path | P1 |
| Experiment record | Markdown and xlsx | You cannot reproduce a number | Catalog and parameter drift | Must for any new claim. A JSON file with git hash, data hash, params, and metrics is enough | Small | P2 |
| Walk-forward | Claimed, not implemented | Stops the parameter agent from fitting yesterday | V2-06 | Must as a **ban** on auto-writes now. A real walk-forward is optional until the engine is clean | Ban is small. A real one is medium | P0 for the ban |
| Portfolio greeks | Absent | Two NIFTY longs are one bet | Hidden concentration | Optional until two concurrent positions are even allowed. Prefer one position | Small to cap at 1. Medium to compute greeks | P2 |
| Performance attribution | Daily audit text | You cannot see if P&L was spot, vol, or theta | V2-03 | Must as labelling. Full attribution is optional | Small | P0 |
| Independent monitoring | Telegram repeats the strategy | The report cannot audit itself | V2-07 | Must: the daily note should be generated from stored trades and stored bars, or say "not run" | Small | P1 |
| CI | None | A stub fill can ship | V2-17 | Must at the moment a broker is considered. Optional this week if nobody sends orders | Small | P2 |
| Data lineage | Paths in docstrings | Holiday-session bug already happened once | Silent contamination | Must when a backtest is quoted | Small | P2 |
| Tick-level matching engine | Absent | Overkill for 5-minute holds with one lot-block | — | Optional. Do not build it | — | P3 |
| Multi-broker smart order router | Banner only | One person, one symbol | — | Optional. A single limit order is the whole requirement | — | Do not build |
| Ingest from YouTube into live rules | Implemented for research | A video is not a tested strategy | Accidental deployment of an untested rule | Optional feature. Must stay off the daemon | — | P3 |

## What not to add

A message bus, a portfolio optimizer, a second agent framework, or an "institutional" OMS beside `trading/orders.py`. The gap is that the daemon does not use the order code you already have, and it reports money the fill model did not earn.
