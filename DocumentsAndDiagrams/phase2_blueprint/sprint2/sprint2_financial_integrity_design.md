# Sprint 2 financial integrity design

Date: 5 Nov 2026. Design only. Nothing here is implemented. Automated live trading stays off. No second order manager. No new broker adapter. Historical journal rows are not rewritten. `trades/daily_risk_config.json` is not edited by this design pass.

The 4 Nov draft of this file treated an LTP as a fill and kept three lots of a ₹10 option under a ₹1,000 premium budget. Both are wrong. This file replaces that draft.

The frozen PDC Zone + 15 EMA entry, stop, and exit rules are not retuned here. `HoldingAgent` preemptive exits already in commit `33706eb` are not extended. Sprint 2 does not add another reason to close a trade.

## What already exists and must be reused

| Need | Use this | Do not add |
|---|---|---|
| Charge math | `app.engine.costs.leg_costs` and `cost_breakdown` | A third fee formula. `app.paper.order_charges` already calls `cost_breakdown` with `slippage_bps=0` when the price is a typed fill. |
| Scanner diary | `JournalAgent` row in `.active_trades_{date}.json` and `live_executed_trades.json` | A second order manager. `ExecutionManager` stays unwired. |
| Research book | `engine/sim.py` already stores leg prices and `net = gross - costs` | Do not point the daemon at `simulate`. Do not merge research capital with the risk file. |
| API orders | `trading/orders.py` and `LiveStore` fills | The daemon does not call them. Sprint 2 does not route the scanner through them. |
| Risk file | `RiskAgent` | `SafetyWallAgent` stops being a second writer of `daily_risk_config.json`. |
| Quote | `contract_quote` in `manager.py` | Black-76, the 0.5 delta, and the `78.67` default. An LTP is a market reference, not a fill. |

`PREMIUM_PNL_METHODS` is duplicated and empty in `risk_agent.py` and `safety_wall_agent.py`. One frozenset, next to the method strings. Sprint 2 adds one member: `broker_premium_net`. Paper results use `paper_premium_net` and are not in the set. Estimated results have no method that the reserve accepts.

## How `leg_costs` actually behaves

`leg_costs` sums `cost_breakdown` once per `(side, price, quantity)` tuple. Each tuple is one order. Brokerage is `brokerage_per_order` (code default ₹20) per tuple, not per lot. STT applies to sells. Stamp duty applies to buys. GST is 18% of brokerage + exchange + SEBI. Slippage is `max(turnover * slippage_bps / 10_000, quantity * slippage_min_ticks * 0.05)`.

When `trade_date` is set, STT and exchange rates come from the schedules on the profile, not from the flat defaults. Code-default schedules (`app/config.py`):

- STT: `0.000625` until 2024-10-01, then `0.001`.
- Exchange: `0.000503` until 2024-10-01, then `0.0003503`.

The flat `stt_pct` of `0.0005` applies only when no date is passed. A dated test and an undated test are different numbers. The config comment says to verify the schedule against NSE and the Finance Act before relying on it. This design does not confirm the statutory rate. Tests must call `leg_costs`. A hard-coded charge total does not count.

Recomputed from those formulas with code-default rates, `trade_date=2026-11-05`, one order per side, quantity 65, buy 100, sell 110. This is an illustration, not a fixture to copy:

| Book | Slippage inputs | Charges | Gross | Net |
|---|---|---|---|---|
| Settings slippage (5 bps, 1 tick floor) | as `CostProfile` defaults | 67.0284 | 650 | 582.9716 |
| No `trade_date` (flat STT 0.05%) | same slippage | 63.4534 | 650 | 586.5466 |
| Broker fill (`slippage_bps=0`, `slippage_min_ticks=0`) | price is the fill | 60.2034 | 650 | 589.7966 |

Broker-confirmed fills use the paper-order convention: slippage 0, because the fill price is the price. Slippage for a paper or estimate book is a separate assumption and is stored on its own key. It is not added again on top of a broker fill.

Three lots in one order are not three times one lot. Buy 10, sell 8, quantity 195, same date, slippage 0: premium exposure 1,950, planned gross −390, charges 50.2735, planned net −440.2735. One lot (quantity 65) on the same prices: exposure 650, planned gross −130, charges 48.2245, planned net −178.2245. Brokerage stays ₹40 for the round trip. Multiplying the one-lot charge by three overstates the cost.

A `.env` override of any rate changes the result. The test builds the profile the same way production will, then asserts equality with `leg_costs`.

## Invariants

1. An LTP, bid, or ask is a market reference. It never creates an execution, never sets `pnl_method` to a realized method, and never moves `safety_wall_reserve` or deployable cash.
2. Broker-realized gross and net exist only when every contributing execution has `price_kind="broker"`, a broker execution id, a positive quantity, and a positive price. If any of those is missing, broker-realized fields stay null. That is fail-closed.
3. Paper-realized fields exist only for executions with `price_kind="paper"` and `simulated=true`. They do not move the reserve.
4. Estimated unrealized P&L uses a market reference on open quantity. It is not realized. It does not move the reserve or `base_capital`.
5. `safety_wall_reserve` changes only inside `RiskAgent.record_trade_outcome`, and only when `pnl_method=="broker_premium_net"` and the trade id has not already been applied. The number 213954.1 is not recomputed and not deleted.
6. `effective_capital` is not an input. New saves omit it. Readers that still find it on an old file ignore it. Nothing replaces it with `base + reserve` under another name.
7. A failed read of the risk file, the active journal, or the executed log does not write a replacement.
8. Old rows keep their bytes. A missing `pnl_method` with a numeric `pnl_rupees` is labeled `index_points_x_0.5` in memory only and is not added to the reserve.
9. Derived P&L is a pure function of the immutable execution list. It is not a second source of truth. Recomputing it must not post the reserve twice.

## Price categories

| Kind | What it is | Allowed use | Forbidden use |
|---|---|---|---|
| `broker` | Confirmed fill: price, quantity, broker execution id, timestamp | Broker-realized gross, charges, net, reserve | Invented when the broker id is missing |
| `paper` | Simulated fill, `simulated=true` | Paper gross and paper net only | Reserve, deployable cash, broker net |
| `reference` | LTP, bid, or ask from `contract_quote` or a bar | Estimated unrealized P&L and monitoring | Execution, realized P&L, charges-as-if-filled, reserve |

`contract_quote` returns `ltp`, `lot`, `delta`, `strike`, and `side`. Sprint 2 may copy `ltp` onto `reference_premium` with `reference_kind="ltp"`. It must not copy it onto `entry_premium` as if it were a fill. Today's journal does not store a premium at all. That stays true until a real execution exists.

## Canonical option ledger

One journal object per trade, the object `JournalAgent` already writes. Executions are an append-only list on that object. No new order manager. No new database as the source of truth. `trade_store.upsert_trade` copies summary fields and must not invent a premium.

### Identity and contract

Required on the trade object when known. Null when not known. Never a silent default.

- `trade_id` (already assigned in `record_entry`)
- `symbol` (underlying; the signal already stores `symbol`)
- `option_symbol` (trading symbol when a chain row has one; else null)
- `option_type` `CE` or `PE`
- `strike`, `expiry`
- `lot_size` from the chain `marketLot` when present, else null. Do not assume 65 if the chain omitted it. The signal's `quantity` stays the quantity the risk gate approved. A disagreement sets `lot_size_conflict=true` and does not resize the open trade.
- `status`: `OPEN`, `PARTIAL`, `CLOSED`, `REJECTED`
- `reconciliation_status`: `unreconciled`, `paper_only`, `broker_matched`, `broken`

### Execution record

Append-only. A stored execution is not edited. A correction is a new execution with `corrects` set to the prior id. Fields:

- `execution_id` unique per source
- `trade_id`
- `side` `BUY` or `SELL`
- `price_kind` `broker` or `paper`
- `simulated` true only for paper
- `price`, `quantity`
- `broker_execution_id` required for `broker`, null for `paper`
- `timestamp` and `received_at`
- `fees` absent until `leg_costs` has been applied to a closed match; charges are derived, not typed by the caller

Duplicate rule: if `(price_kind, broker_execution_id or execution_id)` is already on the list, ignore the new event. Do not add quantity. Do not add P&L.

### Position and P&L are derived

Sort executions by `(timestamp, execution_id)`. Match sells to open buys FIFO. Do not use average cost. A later buy must not change the P&L of a quantity already matched.

Worked partial. One order book, slippage ignored in the gross line:

- Buy 65 at 10 (`execution_id` E1).
- Buy 65 at 12 (E2). Open quantity 130. No realized P&L.
- Sell 65 at 11 (E3). FIFO matches E1. Closed gross = `(11-10)*65 = 65`. Open remainder: 65 at 12. Status `PARTIAL`.
- A second delivery of E3 is ignored. Closed gross stays 65.

Charges for the matched pair are one `leg_costs` call on the matched slices, not on the still-open quantity. The open slice has entry charges only, and those stay in the cost of the open lot until it closes. Estimated unrealized on the remainder uses a reference price and is not added to the closed gross.

Restart: load the execution list and recompute. The reserve moves only on a transition into `broker_matched` for that `trade_id`, recorded as `reserve_posted_for`. A second boot sees the marker and does not add the net again.

`record_exit` today always marks the whole trade `CLOSED` and never stores a partial premium. Liquidity-hop partials stay index points and stay unpriced. Do not invent a premium for booked lots. A hop that has no execution remains `index_points_not_priced`.

### SQLite

`live_and_forward_trades` is a copy. `upsert_trade` must read the crew keys it currently ignores (`entry_price`, `realized_points`, `realized_r`, `quantity`, `direction`) and the new summary fields. It must not apply `or 0.0` to a missing premium. Missing premium stays NULL. The column `entry_premium REAL NOT NULL` cannot hold that NULL until the schema allows it. Sprint 2 changes that column to nullable. It does not backfill old zeros into premiums. A stored 0 that came from the old `or 0.0` coercion is not a fill. Do not treat it as one.

`est_pnl_inr` is the wrong name for three different numbers. Stop writing realized, paper, and estimated into that one column. Write:

- `estimated_unrealized_inr` NULL or the reference mark
- `paper_realized_net_inr` NULL or the paper net
- `broker_realized_net_inr` NULL or the broker net

If a migration cannot add columns in the same change, leave `est_pnl_inr` NULL for every crew row and put the three figures on the JSON row only. Do not overload `est_pnl_inr` with an LTP estimate. `compute_metrics` must not sum NULL as zero and must not raise.

### Historical compatibility

Existing JSON is not rewritten. Rows without `executions` have no fill. Readers treat a missing premium as unknown. A numeric `pnl_rupees` with no `pnl_method` is historical index-points times 0.5, in memory only.

## Five P&L figures

All five can be null. Null is not zero.

1. **Estimated unrealized.** Open broker or paper quantity marked at the latest reference price. `(reference - average open entry) * open_qty` minus no realized charge. Long options only. Sign is premium up minus premium down. This number is display-only.
2. **Simulated realized gross.** FIFO gross on `price_kind="paper"` matches. `(sell - buy) * matched_qty`.
3. **Broker-confirmed realized gross.** FIFO gross on `price_kind="broker"` matches. Null if any matched slice lacks a broker execution id.
4. **Charges.** `leg_costs` on the matched slices of that book only. Broker book: slippage 0. Paper book: settings slippage, stored as `slippage` inside the breakdown, not mixed into the gross. A raise from `leg_costs` leaves net null. Do not store gross as net.
5. **Broker-confirmed realized net.** Broker gross minus broker charges, rounded to paise. This is the only figure that may enter `record_trade_outcome` as `pnl_method="broker_premium_net"`.

Paper net is paper gross minus paper charges. It is stored and never passed to the reserve.

Index `realized_points` and `realized_r` stay beside the option figures. They remain the inputs to the existing daily R circuit breaker. They are not rupees.

Worked broker check, to be asserted by calling `leg_costs`: buy 65 at 100, sell 65 at 110, `trade_date` set, slippage 0. Gross 650. Net equals `650 - leg_costs(...).total` within one paisa. With code-default schedules on 2026-11-05 that illustration is 589.7966 before paise rounding. The test compares to the function, not to 589.80 typed by hand.

## Capital model

One model. Conflicting definitions today:

| Name | Today | After Sprint 2 |
|---|---|---|
| `base_capital` | 50000 in the risk file. Also the `RiskAgent` default. | Operator capital assumption. Not broker cash. |
| `safety_wall_reserve` | 213954.1, built from the old unpriced ratchet. | Frozen historical balance. Not cash. Not deployable. Not a lot-tier input. Moves only on a new `broker_premium_net` win, by the existing 50% of that net, once per `trade_id`. Losses do not reduce it in this sprint. The safety-wall loss-absorption branch is removed from the risk-file writer so a paper or estimated number cannot drain it. |
| `effective_capital` | `base + reserve` (263954.1). | Omitted on the next legitimate save. Ignored if still present. |
| `cash_inr` | Absent. | Null until a broker cash figure exists. Broker-realized sizing refuses to run while it is null. |
| `margin_inr` | Absent. | Null. Long option buying has premium exposure, not SPAN, until a broker margin figure exists. |
| `assumed_cash_inr` | Absent. The code quietly uses `base + reserve`. | Equals `base_capital` for the paper and signal diary only. Never includes the reserve. Never includes estimated P&L. |
| Available funds for a new buy | Not computed. | `assumed_cash_inr` minus open premium exposure minus entry charges, when `cash_inr` is null. When `cash_inr` is present, use that instead and do not use the assumption. |
| Deployable capital | The milestone walk treats reserve as deployable. | `assumed_cash_inr` or `cash_inr`. Estimated P&L is not added. Closed historical `pnl_rupees` is not added. |

`save_config` writes `base_capital`, `safety_wall_reserve`, `reserve_basis`, `session_date`, the counters, the locks, `cash_inr: null`, `margin_inr: null`, `risk_fraction: 0.02` if absent, and `premium_exposure_budget_inr` only when the file already has it or the operator set it. It does not write `effective_capital`. It does not default `premium_exposure_budget_inr` to 1000. The ₹1,000 figure in the sizing example is an explicit budget, not a hidden default.

The first good save after a readable load drops `effective_capital`. The reserve number is copied through. A corrupt file is not saved, so this shape change cannot be the thing that destroys a bad file.

Migration: do not recompute 213954.1. Do not replay October trades into it. The missing `session_date` still clears `daily_cumulative_r` 9.65 on the first readable `begin_session`, which is Sprint 1 behaviour, not a second clearing.

## Centralized rupee gate

The gate is `RiskAgent.evaluate_order`. No new service. `TradeManager` already uses `approved` and `lots`.

### Separate quantities

For a long option:

- Premium exposure = `entry_price * quantity`. This is also the maximum possible premium loss if the option expires at zero, before charges already paid.
- Planned gross stop-loss = `(entry_premium - stop_premium) * quantity`, and only when `stop_premium` is positive and strictly below `entry_premium`. A stop that is not a premium is not this number. An index-point stop is not a rupee stop.
- Planned charges = `leg_costs` on one buy at the entry and one sell at the stop, for the quantity of that candidate, slippage recorded separately.
- Planned loss = planned gross stop-loss + planned charges. The gross line and the charge line are both stored. They are not blended into the premium.
- Risk budget = `base_capital * risk_fraction`. At 50,000 and 0.02 that is 1,000. This caps planned loss. It does not cap premium exposure.
- Premium-exposure budget, when set, caps `entry_premium * quantity` plus premium already open. It is not the risk budget.
- Available funds cap premium exposure plus entry charges. The reserve is not part of available funds.

### Worked size

Capital 50,000. Risk fraction 2%. Risk budget 1,000. Premium 10. Lot size 65. Stop premium 8.

| Lots | Quantity | Premium exposure | Planned gross stop | Max premium loss |
|---|---|---|---|---|
| 1 | 65 | 650 | 130 | 650 |
| 2 | 130 | 1,300 | 260 | 1,300 |
| 3 | 195 | 1,950 | 390 | 1,950 |

Under a premium-exposure budget of 1,000, one lot fits (650 ≤ 1,000) and two lots do not (1,300 > 1,000). The frozen start of 3 steps down to 1. It does not stay at 3 because "one lot is 650 and 650 ≤ 1,000".

That 1,000 is not the stop-loss test. One lot's planned gross stop is 130, not 650. With code-default charges, slippage 0, one order, dated 2026-11-05, planned charges are 48.2245 and planned loss is 178.2245, which is under the 1,000 risk budget. Three lots in one order: planned gross 390, charges 50.2735, planned loss 440.2735, also under 1,000. Three lots fail the 1,000 premium-exposure budget and pass the 1,000 stop-loss budget. The binding constraint is the premium budget. They are not the same gate.

If the premium-exposure budget is left unset, available funds are `base_capital` 50,000. Exposure of 1,950 plus entry charges is under 50,000, so cash does not reject three lots of a ₹10 option. The stop-loss budget still has to pass. A realistic premium does not behave like ₹10. One lot at ₹100 is exposure 6,500. That fits in 50,000 cash and does not fit in a 1,000 premium budget. A planned stop of ₹20 on that lot is a gross loss of 1,300, which fails the 1,000 risk budget even for one lot. A planned stop of ₹8 is a gross loss of 520, which passes the risk budget and still fails a 1,000 premium budget. The gate reports which constraint failed.

Charges and slippage stay on their own keys. They are added to planned loss. They are not subtracted from the premium to make the exposure look smaller.

### Decision order

After the existing point cap, circuit breaker, manual lock, and session block:

1. Frozen lots are the ceiling: 3, or 4 when `is_super_trend`. Never above that. The milestone walk that uses `base + reserve` is deleted.
2. If entry premium, stop premium, lot size, or quantity cannot be calculated, reject. `rupee_risk_known=false`. Lots 0. Do not approve "because the point stop is legal". A missing quote is a missing financial input.
3. Step lots down by one while premium exposure exceeds the premium-exposure budget (if set) or available funds, or while planned loss exceeds the risk budget. Planned loss for this comparison uses `leg_costs` with the settings profile, so the settings slippage assumption is inside the charge total and is also stored as the `slippage` component. Broker-realized net, later, uses slippage 0. The gate does not use the broker convention, because the stop has not been filled.
4. If lots reach 0, reject.
5. `max_loss_rupees` on the approval is the maximum possible premium loss (`entry_premium * quantity`) for the accepted size. `planned_loss_rupees` is the planned loss including charges. Neither field is `risk_points * 0.5 * quantity`.

Open trades keep the lots they were opened with. The gate applies to new entries only.

This rejects entries the daemon records today whenever the chain has no usable premium or no premium stop. That is intentional. It does not change the signal conditions. It refuses to journal a trade whose rupee risk is unknown. Live orders stay unwired either way.

Index-point R circuit breakers stay. They are not a substitute for this gate.

## Fail-closed files

`_load_config` on any exception sets `session_blocked` and `config_unreadable`. `save_config` returns immediately when `config_unreadable` is set. `begin_session` returns false without writing. Entries stay blocked until a person replaces the file. Do not invent a reserve of 0.

`on_market_tick` must observe a false `begin_session`. Exits of an already-open trade can still be journaled. They must not be the write that replaces a corrupt risk file. `record_trade_outcome` does not call `save_config` while `config_unreadable`.

`reset_daily_counters` has no caller. Delete it. A second reset that does not set `session_date` is how a lock comes back the wrong way.

`JournalAgent.load_failed` becomes a set of paths, not one flag. A good load of another date does not clear a failed path. `save_active_trades` refuses a path that failed.

`record_exit` does not replace `live_executed_trades.json` when that file fails to parse. It leaves the bytes, returns `None`, and does not drop the active trade. `on_market_tick` does not call `record_trade_outcome` when `record_exit` returns `None`, and it does not delete the trade from the in-memory book on that path.

SQLite upsert runs only after the JSON write that owns the row has succeeded. A SQLite failure does not roll back a JSON row that was written. The JSON row remains the source. The next upsert may retry. A JSON failure does not leave SQLite as the only copy of a new trade.

`SafetyWallAgent.process_daily_pnl` may read. It must not write `daily_risk_config.json`. Reserve movement lives only in `RiskAgent.record_trade_outcome`.

## Readers

One display rule: null is "not priced". A legacy number is "index points × 0.5, historical". `broker_premium_net` is the only "broker net P&L". Paper net is labeled simulated. A reference mark is labeled estimated unrealized.

- `PostMarketManager.run_daily_review` keeps null and copies `pnl_method`. It does not write `active_strategy_params.json` (Sprint 1).
- `compute_metrics` returns `pnl_status: "incomplete"` when any closed row in the set has no broker net. `net_pnl_inr` is null in that case. It does not raise and does not fall through to the Excel workbook because of a NULL.
- `get_forward_tested_trades` stops defaulting premium to 78.67 and quantity to 260. No premium means null rupee fields.
- Trailing and hop alerts use `reference_premium` if a quote was stored as a reference, and they say estimated. If there is no reference, they say the premium is not priced. They do not compute `risk_points * 4.5`.
- Frontend home card and forward-tested page render null as not priced, not ₹0. That is `frontend/src/app/page.tsx` and `frontend/src/app/forward-tested/page.tsx`.
- `scripts/diagnose_trades.py`, `maintain_trades_excel.py`, and `resimulate_since_oct15.py` follow the null rule where they would rewrite `trades/` or a workbook. `simulate_two_stage_veto_pipeline.py` may keep its research math if the label says model. It must not say net realized rupees for `points * 0.5`.

Catalog literals in `app/strategies/analyzer.py` are not journal P&L. Leave them.

## What this sprint does not do

- Wire `ExecutionManager` or `trading/orders.py` into the daemon.
- Enable `LIVE_TRADING_ENABLED`.
- Treat the 213954.1 reserve as cash or recompute it.
- Backfill old rows with Black-76 or with today's LTP.
- Change sweep, waterfall, holding, or exit thresholds.
- Let recency evidence change this gate. The gate does not read conviction.

## Order of work

S2-04 first, so a bad risk file cannot be saved by a later step. S2-06 with it, so a bad journal cannot be saved either. Then S2-07, so the capital fields exist before sizing reads them. Then S2-01 and S2-02 together, so a premium that is only an LTP cannot be stored as a fill. Then S2-03, so readers do not zero the new nulls. S2-05 last, because it changes which signals become journal rows and it needs a book that can store an execution.
