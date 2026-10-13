# Genesis — Multi-Strategy and Portfolio Audit (Phase 7)

## 1. Current state

- Each deployment runs its own crew process with its own rules and limits.
- Portfolio-level controls that exist: `PORTFOLIO_MAX_OPEN_POSITIONS` (default 3), `PORTFOLIO_MAX_DAILY_LOSS_INR`
  (default ₹10,000, realized only), global `MAX_DRAWDOWN_PCT` (20 %) pause, account-wide reconciliation.
- Capital per deployment is set at approval (`capital_inr`), with deposits/withdrawals as capital events.
- Backtests run one strategy at a time; there is no portfolio backtest.

## 2. Checks

| Concern | Finding | Status |
|---|---|---|
| Duplicate exposure | Two deployments can open the same structure on the same underlying and expiry | Not controlled (G-21) |
| Opposite-direction trades | A long call and a bear put spread can run at once | Not controlled |
| Correlated strategies | All current strategies are NIFTY/BANKNIFTY options: highly correlated; short-premium ones share the same tail | Not measured |
| Capital allocation | Fixed per deployment; sum of capitals is not checked against the account | Not controlled |
| Strategy-level drawdown | Per-deployment ladder | Present |
| Regime performance | Not computed | Missing |
| Prioritisation / simultaneous execution | Crews are independent processes on one SQLite live DB with `BEGIN IMMEDIATE`, so a tick's risk read and intent write are normally serialized. But when the LLM opinion or explain step is on (both off by default), the crew releases the lock (`rollback()`) between the risk read and the intent write and does not re-check, so two crews can both see "2 of 3" and both enter | Race when LLM steps are on (MEDIUM, G-28) |

## 3. Should there be an allocation engine?

Not yet. With one strategy whose edge is not demonstrated (see the strategy audit), allocation logic would
optimise noise. The cheaper, safer steps first:

1. Per-underlying cap and a net short-vega cap across deployments (blocks stacking the same tail).
2. Sum of deployment capitals ≤ broker account equity, checked at approval.
3. A portfolio backtest that replays several approved deployments on the same days with the shared limits.

Then, if two or more strategies pass walk-forward validation: equal **risk** allocation (by max loss per lot), and
only after that, test volatility- or correlation-based weights **out of sample**. Do not raise allocation on past
performance without an out-of-sample test; performance chasing is the default failure here.

## 4. Verdict

Portfolio risk is controlled by **counts**, not by **exposure**. Acceptable for a single deployment; not for several.
