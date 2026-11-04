# Financial correctness specification

Date: 4 Nov 2026. The behaviour an implementation must produce. Not implemented in this pass.

## Invariants

1. `status=FILLED` is written only after a broker ack that says filled, or a `paper_human` row a person entered, or a `research_sim` row labelled model. `BrokerBridgeAgent` returning `FILLED` with no ack violates this. The fix is to return `REJECTED` until an adapter exists.
2. `pnl_rupees` is null or a premium result. It is never `points * 0.5 * qty`.
3. `safety_wall_reserve` does not change unless a premium net was just realized, and only by the fraction written in the risk policy. Until premium nets exist, the reserve is frozen. Do not delete the current 213954.1 figure; stop adding to it. A separate note in the file, `reserve_basis: index_points_x_0.5_frozen`, tells a reader what the number is.
4. One capital figure per mode. Scanner paper capital is the `base_capital` in the risk file (₹50,000 today). Research sim capital is `default_capital_inr` or the deployment capital, and the report names which. They are not added together.
5. A missing load of the active-trade file does not save an empty book over it (F-13).
6. Charges on a priced trade equal `CostBreakdown.total` for that fill's notional, side, date, and quantity.

## Worked check the tests must encode

Buy 65 NIFTY CE at premium 100, sell at 110, same day, one lot. Charges from `leg_costs` for BUY 100×65 and SELL 110×65. Net = 10×65 − those charges. A journal exit that instead books `spot_points * 0.5 * 65` fails the test.

A second case: exit with no premium stored. `pnl_rupees` is null. `safety_wall_reserve` unchanged.

A third case: `place_order` with live flag and no adapter. Status is not `FILLED`. Success is false.

## What is explicitly not corrected by rewriting history

The reserve already on disk. Old `pnl_rupees` values. Catalog literals (those are removed from the served payload, not "fixed" into a new invented number). October option workbooks.

## Daily loss in rupees

Once premiums exist, the IST day's realized net plus the open mark (last premium) is the number compared with the daily loss limit. The limit is a fraction of the mode's capital, written down, defaulting to something the ₹50,000 account can survive (the roadmap does not pick the fraction; the risk spec requires it to be explicit and on). A null mark means the open trade counts as its entry premium, the conservative full-premium loss for a long option, until a mark exists.

Index R may still halt the strategy (the −2R rule) but it is a strategy halt, not an account-rupee halt. Both can exist. They must not share one field called `daily_cumulative_r` that never resets.
