# Project maturity scorecard

Date: 4 Nov 2026. Scores are for trading use after this revalidation. 0 is absent or misleading. 5 is a personal research tool that knows its limits. Nothing here is ready for unattended orders.

Profitability is separate. The October index study did not show a significant edge and was not rerun. A higher engineering score would not change that.

| Category | Score | Evidence |
|---|---|---|
| Overall architecture | 4 | Research stack is coherent. Daemon path is a journal. They do not share orders or P&L |
| Market data | 4 | Spot series and a lot table exist. No option tape. Three VWAP definitions. Gaps are not reported |
| Quantitative research | 5 | Costs, a daily simulator, a filed skeptical study. Undercut by catalog literals and a parameter writer |
| Strategy engine | 4 | `scan` is real and is called live, with different arguments and a target rewrite |
| Backtesting | 3 | Index path for the old `scan` call was audited in October. Option fills are a constant-IV model. Same-bar daily fills remain. Not rerun today |
| Statistical validation | 4 | One dated study with an interval through zero. It is not the live call. No experiment registry |
| Options analytics | 3 | Black-76 and a written bias comment. Live P&L does not use it |
| Financial accounting | 2 | `engine/costs.py` is real and not on the journal path. Journal rupees are points × 0.5. Reserve 213954.1 on a 50000 base |
| Risk management | 3 | VIX, stale bar, walls, 55-point cap are real. Daily counters never reset. Lots frozen. Second position allowed after breakeven |
| Paper trading | 3 | JSON journal and Telegram. No fill, no charge. Log text says the trade was executed |
| Live execution | 2 | `trading/orders.py` exists and is not the daemon. Live bridge returns `FILLED` with no call. Daemon does not invoke it today |
| Portfolio management | 2 | At most two scanner bets. No net greek. Two capital defaults |
| Reliability | 3 | Loop survives exceptions. Bad JSON can be overwritten. No IST clock. No lock |
| Security | 4 | Hashed tokens on broker routes. Research and paper routes unauthenticated. Fine only on localhost |
| Testing | 5 | 48 test modules exist. This session: 24 passed in two files. Those tests miss F-01's live branch. No CI |
| Documentation | 4 | Prior audits are careful. Module banners describe an execution system the process does not run |
| Monitoring | 3 | Telegram and a daily markdown file that can be produced from an empty frame |

## Readiness

| Question | Answer |
|---|---|
| Research, as a simulator of daily option snapshots | Usable if reports say "snapshot fill" and catalog numbers are ignored |
| Backtest credibility for the live scanner | Not credible until the parity card matches and option rupees are not the score |
| Paper trading | A signal diary. Not a paper fill record |
| Live execution | Not ready. Latent false fill |
| Automated trading | Not allowed. Gates in `live_trading_readiness_gates.md` are open |

Strongest code: `engine/costs.py`, `trading/orders.py` on the research stack, the October study's refusal to bless a weak R. Weakest: journal rupees, the safety wall, the bridge's live branch, the parameter writer.
