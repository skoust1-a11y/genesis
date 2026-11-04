# Live trading readiness gates

Date: 4 Nov 2026. Every gate is **open** (not met). Automated broker orders stay off until all of them are closed with evidence, not with a code comment.

| Gate | Evidence required | State today |
|---|---|---|
| G1 No false fill | A-01 passing. Daemon does not construct a bridge that can return `FILLED` | Open. F-01 confirmed. Daemon does not call the method, which is not the same as the method being safe |
| G2 Rupees are premiums | A-02 and A-03 passing. Safety wall not fed by points × 0.5. Reserve labelled | Open. F-02, F-03 |
| G3 One account capital | Scanner reports name ₹50,000. Research reports name their own capital. They are not added | Open. File is 50000. Settings default is 500000 |
| G4 Daily risk is a day | A-04 passing. `session_date` stored. Consecutive means consecutive | Open. F-07. File shows cumulative R 9.65 |
| G5 Live file is human-written | A-05 passing | Open. F-09 |
| G6 Journal cannot be wiped by a bad read | A-06 passing | Open. F-13 |
| G7 Research call equals live call | A-07 and A-08 passing on the card that will trade. Version id on the report | Open. F-05 |
| G8 Option result is not the index result | A published run with index R and, separately, either null option P&L or priced fills. Model column not used to choose parameters | Open. October study not rerun. Model still in the ledger |
| G9 Forward sample | At least 30 priced round trips is the research verdict floor for **not** calling a sample an edge. It is not a graduation. Use it to show the plumbing and to compare with the twin backtest. A large gap stops the project. A small sample does not prove an edge. October's 544-trade interval still crossed zero on the old rule | Open. Current forward rupees are F-02 |
| G10 Crash | Kill the process after submit and before the fill is saved. Restart. One order at the broker, or zero if it was never sent. Not two | Open. No broker path. Research `_send` still writes the row after `place_order` |
| G11 Kill switch | In broker mode, switch sends reduce-only orders and waits for `COMPLETE`. A JSON flag is not enough | Open for the scanner |
| G12 CI | The acceptance tests run on every change | Open. No workflow |
| G13 Secrets | Broker keys only in the environment. Live routes stay on `require_auth`. Research routes authenticated or bound to loopback | Partial. Credential routes require auth. Research routes do not. Bind not rechecked |

## Decision

**Automated live trading: no.**

Paper Telegram may continue after G2, G4, G5, and G6, and only if the message says the idea is unpriced. It is not live trading.

Research simulation may continue if the report says snapshot or model. It is not a live edge.

Closing G7 and G8 is what makes the next backtest worth reading. Closing G1 and G10 is what makes a broker call permissible. Closing G9 does not, by itself, make the strategy true.
