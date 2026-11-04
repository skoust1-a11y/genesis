# Options trading audit

Audit date: 4 Nov 2026.

## How an option is chosen

| Path | Strike | Expiry | Premium used to decide |
|---|---|---|---|
| `estimated_plan` / liquidity backtest | `round(spot/step) ± otm_steps` (step 50 for NIFTY) | Tuesday weekly, or the session before if Tuesday is a holiday. History path also knows the Thursday-weekly and monthly eras | Black-76 at prior VIX. `quote` is None |
| `plan_pnl` | Caller passes the strike | Snapshot expiry | Live quote inverted for IV, then re-priced. Constant IV afterwards |
| Live `select_strike_with_delta_floor` | Start 2 OTM, walk toward ATM until \|delta\| ≥ 0.35 | Whatever contract is on the Groww page (`currentExpiry`) | LTP required only to send Telegram. Missing LTP still journals the trade (`NO_QUOTE_NO_ALERT`) |
| Spread block inside `option_plan` | Short strike 2 steps further OTM. Short premium set to `0.45 * long premium` when a chain did not price it | Same | A decorative max-risk. The live crew buys a single option, it does not trade this spread |

Delta floor without a chain uses `0.50 - 0.065 * steps`. That is a label, not a greek. Live drops the trade if a chain was supplied and no strike cleared 0.35. Good. The estimate must not be quoted as the model's delta.

## How P&L is marked

**Research liquidity ledger.** Entry premium from Black-76. Exit premium from Black-76 at the **spot** exit, same IV, slightly less time. Charges on both. This misses:

- IV crush on a call after a rally, and IV expansion on a put after a drop (the code comment states the direction of the bias)
- The bid-ask. A buy at last and a sell at last is already optimistic; a buy at ask and a sell at bid is the real round trip. On a ₹20–40 premium, one tick is material and a 1-point spread is a large fraction of a 0.03R spot edge
- Gap through the spot stop: the ledger fills the stop at the stop price if the bar's extreme trades through it (`exit_of`). The option gap can be wider
- Lot-size changes are handled in the ledger when the YAML has the date. Fallback 75 is a silent error on dates the table misses

**Scanner journal** (`journal_agent.record_exit`):

```text
pnl_rupees = realized_points * 0.5 * (lots * lot_qty) + booked_prior
```

`lot_qty` defaults to 65. No premium, no IV, no theta, no charges, no spread. A +13 point spot scalp on a 2-OTM option near the close can be a loss after decay and the spread. The journal will record a profit (V2-03).

**Risk approval** uses the same 0.5:

```text
approx_option_risk = risk_points * 0.5 * qty
```

It stores the number and does not reject the trade when that number exceeds a fraction of capital (V2-11). At 3 lots, a 35-point stop is `35 * 0.5 * 195 ≈ ₹3,400`, about 7% of a ₹50,000 book, before the spread. A real 2-OTM delta is often below 0.5, so this can overstate loss; a fast move that lifts delta, or a stop that gaps, can understate it. Either way it is not a measured premium loss.

## Greeks

Groww may attach a delta. The selector uses it as an absolute value and ignores gamma, vega, and theta for the decision. There is no book vega. Two positions after breakeven (V2-12) can both be long gamma or both long the same direction.

Walls (`GammaAgent`) are the strikes with the largest OI on the scraped chain, treated as targets and vetoes. OI walls are a hypothesis. They are not a hedge, and a failed scrape vetoes the whole entry (`VETO_MISSING_WALLS`), which couples trading to a website.

## Expiry and assignment

Liquidity backtest skips expiry day, so it never holds into the pin and never pays exercise STT. Live blocks new entries from 14:30 and the exit agent is built with a 15:10 square-off. Holding into the close on expiry is still possible if the exit logic does not fire (stale CMP, process down). Assignment is not modelled. The research cost engine has `exercise_costs`; the scanner does not call it.

## Contract identity

`suggested_strike` is a string like `NIFTY 26100 CE`. `BrokerBridgeAgent`, if it were called, would send that string as `symbol`. A broker needs the exchange tradingsymbol (`NIFTY25N0426100CE` or whatever the current scheme is), lot multiple, and product type. The stub never discovers this because it does not call an API (V2-01). `GrowwQuotes.instruments` does parse `growwContractId` and lot size. The scanner does not pass that id into an order.

## Bottom line

The platform can **illustrate** an option premium with Black-76. It cannot **prove** option profitability. The live book does not even illustrate it: it scales the index by one half.
