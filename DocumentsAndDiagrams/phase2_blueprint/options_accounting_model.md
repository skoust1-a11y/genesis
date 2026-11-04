# Options accounting model

Date: 4 Nov 2026. Design only. No schema was changed.

## Rule

A rupee is the difference in option premium times filled quantity, minus charges from `engine/costs.py` (`leg_costs` / `cost_breakdown`). A fixed delta is not a rupee.

Index points and R stay on a second record. They may be shown. They may not be added into the safety wall, the daily loss in rupees, or a profit factor labelled "P&L".

## Fields per fill

One row per fill, not per idea. A round trip is two or more fills.

| Field | Required | Notes |
|---|---|---|
| account_mode | yes | `research_sim`, `paper_human`, or `broker` |
| underlying | yes | NIFTY or BANKNIFTY |
| tradingsymbol | yes for broker and paper fills | Exchange symbol. Display string `NIFTY 26100 CE` is not enough |
| option_type | yes | CE or PE |
| strike | yes | |
| expiry | yes | |
| side | yes | BUY or SELL |
| timestamp | yes | timezone-aware IST |
| underlying_price | yes | spot at the same decision |
| premium | yes for a rupee | null if unknown. Null is not zero |
| quantity | yes | contracts, not lots alone |
| lot_size | yes | from the chain or the dated table, stored on the row |
| fill_kind | yes | `model`, `last`, `bid`, `ask`, `broker`, `human` |
| brokerage, stt, exchange, gst, sebi, stamp, slippage | yes when premium is known | `CostBreakdown` fields. Do not invent a second calculator |
| order_id | yes for broker | null for a research model row |

Gross = (sell premium − buy premium) × quantity, signed by side. Net = gross − charge total. Unrealized uses the latest stored premium of the same `fill_kind`, or null if none.

Partial fills are several rows with the same order id. The position quantity is the sum.

## What happens to the current journal

`pnl_rupees` on existing JSON rows stays as a historical field. New writes add `pnl_method`. For old rows the method is `index_points_x_0.5`. New exits write `pnl_rupees` only when entry and exit premiums exist. Otherwise the field is null and the alert says "not priced".

Do not recompute history with Black-76 and overwrite the old number. That would replace one model with another and hide the change.

## Reuse

| Need | Module |
|---|---|
| Charge math | `engine/costs.py` `leg_costs` |
| Research daily mark | `engine/sim.py` already stores leg prices. Its report must keep the label "snapshot fill" |
| Live deployment orders | `LiveStore` order and `BrokerCharge` rows, once the daemon uses `trading/orders.py` |
| Scanner diary | `JournalAgent` gains premium fields. It does not grow a third cost function |

`app.paper.order_charges` is what `estimated_plan` uses today. Before implementation, check it against `cost_breakdown` on one numerical example. If they differ, the canonical one is `engine/costs.py`, and the liquidity ledger switches or documents the difference. This session did not run that numerical comparison.

## Reporting

**Underlying block:** points, R, count, drawdown in R. No rupees.

**Options block:** net rupees, charges, count of unpriced trades. If any trade is unpriced, the options total is "incomplete", not a sum that treats null as zero.

The 3 Nov daily audit mixed these. The briefing template has to stop.
