# Sprint 2 financial integrity design

Date: 4 Nov 2026. Design only. Nothing in this file is implemented. Automated live trading stays off. No second order manager. No new framework. Strategy thresholds (sweep, stop clamp, target R, 15:10 flat, the 55-point cap, frozen 3/4 lot ceiling) stay as they are. Historical rows are not rewritten. `trades/daily_risk_config.json` is not edited by this design pass.

Sprint 2 is the smallest change that makes a rupee mean a premium difference minus `engine/costs.py`, and that stops every other number from pretending to be that rupee.

## What already exists and must be reused

| Need | Use this | Do not add |
|---|---|---|
| Charge math | `app.engine.costs.leg_costs` / `cost_breakdown` | A third fee formula. `app.paper.order_charges` already calls `cost_breakdown` with `slippage_bps=0` for typed paper fills. Scanner marks are not typed fills. They use `leg_costs` with the settings profile, slippage included, one order per fill. |
| Scanner diary | `JournalAgent` row in `.active_trades_{date}.json` and `live_executed_trades.json` | A new trade table as the source of truth. `trade_store.upsert_trade` is the SQLite copy and must learn the same fields. |
| Research book | `engine/sim.py` `simulate` already stores leg prices and `net = gross - costs` | Do not point the daemon at `simulate`. Do not merge the two capital numbers. |
| Research orders | `trading/orders.py` and `LiveStore` | The daemon does not call them in Sprint 2. Do not construct `ExecutionManager`. |
| Risk file | `RiskAgent` in `backend/app/trading/crew/risk_agent.py` | A new risk service. `SafetyWallAgent` stops being a second writer of `daily_risk_config.json`. |
| Quote | `contract_quote` in `manager.py` | Black-76, the 0.5 delta, and the `78.67` default in `get_forward_tested_trades`. |

`PREMIUM_PNL_METHODS` is duplicated and empty in `risk_agent.py` and `safety_wall_agent.py`. One frozenset, defined next to the journal method string, imported by both. The only member Sprint 2 adds is `premium_leg_costs`.

## Invariants the design has to keep

1. `pnl_rupees` is null, or it is `(exit_premium - entry_premium) * quantity - leg_costs(...).total` for a long option. It is never `points * 0.5 * quantity`.
2. `safety_wall_reserve` changes only inside `RiskAgent.record_trade_outcome`, and only when `pnl_method == "premium_leg_costs"`. The number 213954.1 is not recomputed and not deleted.
3. `max_loss_rupees` is either null or `entry_premium * quantity` for a long option (the whole premium, which is the real max loss of a debit). The 0.5-delta estimate is removed from the approval object. It is not a sizing input under another name.
4. `effective_capital` is not `base + reserve`. The field stops being an input to lot selection.
5. A failed read of the risk file or the active journal does not write a replacement.
6. Old rows keep their bytes. A missing `pnl_method` with a numeric `pnl_rupees` means `index_points_x_0.5` and is not added to the reserve again.

## S2-01 Canonical option row on the existing journal

### Files and functions

- `JournalAgent.record_entry` and `JournalAgent.record_exit` (`backend/app/trading/crew/journal_agent.py`).
- `TradeManager.on_market_tick`, the block that already calls `contract_quote` after a successful `record_entry`, and the fully-closed branch that calls `record_exit`.
- `trade_store.upsert_trade` (`backend/app/db/trade_store.py`): read `entry_price`, `realized_points`, `realized_r`, `quantity`, `direction`, and the new premium keys. Today it looks up `entry`, `points`, `r_multiple`, `qty`, and `side`, so a crew row lands as zeros.

### Current behaviour

The open row stores the spot signal: prices, stop, target, lots, `quantity`, `suggested_strike`. It does not store a premium. The exit always sets `pnl_rupees` null and `pnl_method` to `index_points_not_priced`, even when `contract_quote` found an LTP a moment earlier. The quote is used only to decide whether Telegram fires.

### Proposed behaviour

On entry, if `contract_quote` returns an LTP, lot, and strike, write onto that same dict before the second `save_active_trades`:

- `entry_premium` (the LTP, not a model)
- `fill_kind` = `last`
- `quantity` from the chain lot times the already approved lots, when the chain lot is present. If the chain lot disagrees with `RiskAgent.lot_qty`, keep the signal's quantity and set `lot_size_conflict` true. Do not silently switch lot size in this sprint. The chain lot is recorded as `quoted_lot_size`.
- `option_type` CE or PE from the quote
- `strike` from the quote
- `premium_missing` false

If the quote is missing, leave `entry_premium` null and `premium_missing` true. Do not substitute 78.67, 40, or `risk_points * 4.5`.

On exit, call `contract_quote` once for the same strike. Store `exit_premium` and `exit_fill_kind=last` when the LTP is there. Pricing then follows S2-02. No quote means the exit stays `index_points_not_priced` and `pnl_rupees` null.

This is one row per round trip, which is what the journal already is. A fill ledger of two rows is not required until a partial close books a real premium. Liquidity-hop partials stay index points and stay unpriced. Do not invent a premium for the booked lots.

### Data model

New optional keys on the JSON object. No new file. SQLite `live_and_forward_trades` already has `entry_premium`, `exit_premium`, `est_pnl_inr`, `qty`, `lots`. `upsert_trade` must fill those from the journal keys. Add no column in this sprint. `est_pnl_inr` holds the net from S2-02 or NULL. The column default of 0 must not apply to an explicit NULL.

### Historical compatibility

Existing JSON is not rewritten. Rows without the new keys stay as they are. Readers treat a missing premium as unknown, not zero.

### Failure scenarios

- Quote disappears between entry and exit: `pnl_rupees` stays null. Reserve unchanged.
- Quote present but non-positive: `contract_quote` already returns null. Same as missing.
- Process dies after the active-file save and before SQLite: the JSON row is the source. The next upsert may retry from the executed log. Do not let SQLite failure delete the JSON row.
- Chain lot differs from 65: recorded, not applied. A later sprint can refuse the entry. This sprint must not change filled quantity out from under the signal.

### Regression tests

Temp journal. Feed a one-contract chain with LTP 100 and lot 65. Entry row has `entry_premium==100` and `fill_kind=="last"`. Same path with `nifty_chain=None` leaves `entry_premium` null and does not write 78.67.

### Acceptance

A journal entry either has a last-trade premium taken from `contract_quote`, or the premium field is null. The active file and the SQLite row show the same premium or the same null.

## S2-02 Premium net via `leg_costs`

### Files and functions

- `JournalAgent.record_exit`.
- `app.engine.costs.leg_costs` and `CostProfile` built from `get_settings()` the same way `paper.order_charges` builds a profile, except `slippage_bps` stays the settings value (paper types zero because a person typed the price; a scraped LTP did not).
- `TradeManager` passes `entry_premium`, `exit_premium`, `quantity`, and the session date into `record_exit`.

### Current behaviour

Exit rupees are always null. Charges are not computed. Index points and R are stored and are what the risk counters use.

### Proposed behaviour

When both premiums are positive and quantity is a positive int:

```text
gross = (exit_premium - entry_premium) * quantity
charges = leg_costs(
    [("BUY", entry_premium, quantity), ("SELL", exit_premium, quantity)],
    profile,
    trade_date=session_date,
)
pnl_rupees = round(gross - charges.total, 2)
pnl_method = "premium_leg_costs"
```

Store the `CostBreakdown` fields on the row (`brokerage`, `stt`, `exchange_txn`, `sebi`, `stamp_duty`, `gst`, `slippage`, `charges_total`). Index `realized_points` and `realized_r` stay beside them.

Otherwise `pnl_rupees` is null, `pnl_method` is `index_points_not_priced`, and no charge fields are written as zeros.

Long options only. The crew buys calls and puts. Do not apply the short-option sign in this sprint.

Worked check, to be asserted rather than hand-waved: buy 65 at 100, sell 65 at 110, same `trade_date`. Net equals `10 * 65 - leg_costs(...).total` within one paisa. A test that hard-codes a charge total without calling `leg_costs` does not count.

### Data model

Same JSON row. `pnl_rupees` becomes a float only for `premium_leg_costs`. Charge keys are absent when unpriced.

### Historical compatibility

Rows that already have a numeric `pnl_rupees` and no `pnl_method` are left untouched. New code that displays them labels the method `index_points_x_0.5` in memory, not by rewriting the file. They are excluded from reserve math because the method is not `premium_leg_costs`.

### Failure scenarios

- `leg_costs` raises: do not catch it and store the gross. Leave `pnl_rupees` null, log the error, still store points and R. A missing charge is not zero charges.
- Quantity 0 or premium 0: treated as unpriced.
- Settings rates change later: the row keeps the charge figures written at exit. Do not recompute history when settings change.

### Regression tests

The 100 to 110 case against `leg_costs`. The no-premium case stays null and the reserve does not move. A row that already contains `pnl_rupees: 1300` and no method is not rewritten by `record_exit` of a different id, and is not passed to the reserve adder.

### Acceptance

The worked example matches `leg_costs` by hand. Unpriced exits still match Sprint 1. Points and R are still present on both kinds of exit.

## S2-03 Stop reading null as zero

### Files and functions

- `PostMarketManager.run_daily_review` normalizer. Keep `pnl_rupees` as null. Copy `pnl_method` onto the normalized dict.
- `SafetyWallAgent.process_daily_pnl`. Delete the lot-tier write of `daily_risk_config.json` from this function. It may read the risk file. It must not write it. Reserve movement lives only in `RiskAgent.record_trade_outcome`.
- `trade_store.compute_metrics`. Sum `est_pnl_inr` only for rows whose method is `premium_leg_costs` or, for legacy rows, whose method is absent and the value is a float that came from the old workbook. If any closed row in the requested set is unpriced, the options total is null and the payload includes `pnl_status: "incomplete"`. Do not add null as zero. A null must not raise and must not dump the caller into the Excel fallback.
- `app/api/routes.py` `get_forward_tested_trades`. Remove the `78.67` / `260` defaults. If the active row has no `entry_premium`, omit rupee fields or send null. The Excel fallback must not coerce a missing `Est P&L` to 0 when the cell is blank; leave it null.
- `BriefingAgent._pnl_text` already handles null. Keep it. Do not feed it the coerced 0.
- `AlertAgent.send_trailing_update` and the hop alert. Stop computing `risk_points * 4.5`. If `entry_premium` is on the trade, show that. If not, say the premium is not priced. Do not instruct a rupee stop from a model.
- Scripts `maintain_trades_excel.py`, `diagnose_trades.py`, `resimulate_since_oct15.py`: same null rule if they import the journal. They are not on the daemon. Fix them only where they would rewrite `trades/` or the workbook from a null. A research script that still prints `0.5 * points` must say "model" in the label. `simulate_two_stage_veto_pipeline.py` is that script. Label only. Do not change its strategy math in this sprint.

### Current behaviour

See the verification note. The reserve is safe because the premium set is empty. The post-market copy, the API active merge, the metrics sum, and the trailing ticket still speak in rupees.

### Proposed behaviour

One display rule: null is "not priced", a legacy number is "index points × 0.5, historical", and `premium_leg_costs` is the only "net P&L".

### Data model

No new store. `compute_metrics` gains `pnl_status` on the metrics dict. Old clients that only read `net_pnl_inr` will see null when the set is incomplete. The forward-tested page already prints the number. Null must render as a blank or "not priced", not as ₹0. That is a one-branch change in `frontend/src/app/forward-tested/page.tsx` and the home card in `frontend/src/app/page.tsx`. The strategies catalog literals in `app/strategies/analyzer.py` are not journal P&L. Leave them. Do not "correct" them into a new invented total.

### Historical compatibility

Workbook rows with a real `Est P&L` stay in the legacy bucket. They are not relabelled `premium_leg_costs`. They are not fed to the reserve.

### Failure scenarios

- A mixed day of one priced and one unpriced trade: options total is incomplete. Points and R still sum. The priced trade may still move the reserve (S2-04). The unpriced one must not.
- `compute_metrics` used to throw on NULL and the API silently showed the workbook. After the change the API returns the SQLite rows with nulls. Confirm the page does not crash on null.

### Regression tests

Normalizer output for a null `pnl_rupees` is null, and `pnl_method` is still on the dict. `process_daily_pnl` on an unpriced list does not write `daily_risk_config.json` (bytes unchanged, including a file that has `session_date`). `compute_metrics` on one null and one legacy float returns `pnl_status=="incomplete"` and does not raise.

### Acceptance

No production reader of the crew journal uses `or 0.0` on `pnl_rupees`. The safety-wall agent does not open `daily_risk_config.json` for writing.

## S2-04 One rupee gate, and a risk file that cannot be wiped

### Files and functions

- `RiskAgent._load_config`, `begin_session`, `save_config`, `evaluate_order`, `record_trade_outcome`.
- `TradeManager.on_market_tick`: already calls `begin_session` and `evaluate_order`. Pass the quote premium into `evaluate_order`. If `begin_session` returns false, return immediately after the session-blocked action, before the exit loop calls `record_trade_outcome`. Exits of an already-open trade can still be journaled. They must not be the write that replaces a corrupt risk file.
- Delete the dead `reset_daily_counters` or make it call `begin_session` only. A second reset that does not set `session_date` is how a lock comes back the wrong way. Prefer deleting it. It has no caller.

### Current behaviour

`evaluate_order` ignores premium and ignores `max_loss_rupees`. `begin_session` rewrites a file that failed to parse. `record_trade_outcome` will not move the reserve until a method is added to the frozenset. Index R still gates the day. `SafetyWallAgent`, if a method were added without care, would overwrite the risk file and drop `session_date`, `manual_lock`, `reserve_basis`, and `daily_cumulative_r`.

### Proposed behaviour

`_load_config` on any exception sets `session_blocked` and sets `config_unreadable`. `save_config` returns immediately when `config_unreadable` is set. `begin_session` returns false without writing. Entries stay blocked until a person replaces the file. Do not invent a reserve of 0.

`record_trade_outcome` for `pnl_method=="premium_leg_costs"`:

- Win and net > 0: add `round(net * 0.50, 2)` to `safety_wall_reserve`. Same fraction as today. Do not change the fraction in this sprint.
- Loss count: keep the index rule (`realized_r <= -0.5`) and also increment when the priced net is negative. Do not require the net to be ≤ −500 once the method is real. The −500 figure was a stand-in for the fake 0.5-delta rupees.
- Anything else, including legacy numbers: reserve unchanged.

`evaluate_order` gains the premium and the quantity it would actually use:

- Keep the existing point cap, circuit breaker, manual lock, and session block.
- `max_loss_rupees` is null when `entry_premium` is null. It is `entry_premium * quantity` when the premium is known. It is not `points * 0.5 * quantity`.
- The gate does not reject a journal signal solely because the premium is missing. The daemon is still a signal diary. It rejects the idea of a rupee budget being "met" when the premium is missing: `rupee_risk_known` is false, lots stay on the frozen 3/4 rule, and the approval's `max_loss_rupees` is null.
- When the premium is known, reject the entry if `entry_premium * quantity` is greater than `base_capital * daily_loss_fraction`. `daily_loss_fraction` is a new key written once, default `0.02`, on the next successful `save_config`, without touching the reserve number. Two percent of 50,000 is 1,000 rupees. A 100-rupee premium on 3 lots of 65 is 19,500, which this gate would reject. That is the point of a rupee gate. It can only reduce size or reject. It cannot raise the frozen ceiling. See S2-05 for the size step that happens before this check.
- `effective_capital` is removed from the lot walk. The non-frozen branch that walks `LOT_CAPITAL_MILESTONES` with `base + reserve` is deleted. The daemon is frozen, so this deletes a path the daemon does not use. Tests that built a `RiskAgent` without `default_lots` and expected milestone lots must be updated to pass `default_lots` or to expect the frozen default, not the reserve-inflated tier.

`save_config` writes:

- `base_capital` (unchanged)
- `safety_wall_reserve` (unchanged number, unless a priced net just moved it)
- `reserve_basis` (kept)
- `session_date`, counters, locks (as today)
- `cash_inr`: null until a broker statement exists
- `margin_inr`: null
- `daily_loss_fraction`: 0.02 if the key was absent
- It does not write `effective_capital`. Leave the old key out of new writes. Readers that still find it on an old file ignore it.

Do not add `base + reserve` under a new name.

### Data model

Same JSON file. New keys are null or a fraction. Removing `effective_capital` on the next legitimate save is a shape change. The reserve number stays. A corrupt file is not saved, so this shape change cannot be the thing that destroys a bad file. The first good save after a readable load will drop `effective_capital`. That is acceptable because the verification showed no reader uses it for the daemon's lot count. `PreMarketManager` does not read it.

### Historical compatibility

213954.1 stays. 9.65 is still cleared by the Sprint 1 date rollover the first time a readable file with no `session_date` is opened. Sprint 2 does not add a second clearing. If Sprint 2 ships before that tick, the rollover and the new keys happen on the same save. The reserve write and the counter reset stay two different assignments so a reviewer can see the reserve was copied through.

### Failure scenarios

- Truncated risk file: bytes unchanged, entries blocked, exits may journal, `record_trade_outcome` does not call `save_config` while `config_unreadable`.
- Save fails on a real rollover: Sprint 1 restore behaviour stays. Entries blocked.
- Premium known and loss would exceed 2% of base: entry rejected, no journal row.
- Premium missing: entry still allowed under the existing point rules, `max_loss_rupees` null, reserve cannot move on that trade later unless an exit quote completes S2-02.
- Someone passes a legacy `pnl_rupees` of 10,000 with no method: reserve unchanged.

### Regression tests

Copy of a `{` risk file is byte-identical after `RiskAgent` init and `begin_session`. Readable file without `session_date` keeps the reserve and zeros R. Priced win of a known net adds exactly half. Unpriced win adds nothing. `entry_premium * qty` above `base * 0.02` is rejected. Missing premium is approved when the point stop is legal, and `max_loss_rupees` is null. `effective_capital` is absent from the saved document. `safety_wall_reserve` in a fixture of 213954.1 is unchanged by an unpriced day.

### Acceptance

A-12 passes. The reserve moves only on `premium_leg_costs`. The 0.5-delta field is gone from `RiskApproval`. The daemon still cannot place an order.

## S2-05 Position size from premium risk, under the current lot ceiling

### Files and functions

- `RiskAgent.evaluate_order` only. `TradeManager` already uses `risk_appr.lots`.
- Do not change `EntryAgent` stop buffers, ADX cuts, or strike rules in this change.

### Current behaviour

Daemon lots are 3, or 4 if `is_super_trend` (28 bars and ADX ≥ 25 on the manager path). `max_loss_rupees` is informational. Capital does not reduce lots.

### Proposed behaviour

After the existing frozen lot is chosen:

```text
lots = frozen lots   # 3, or 4 when is_super_trend
if entry_premium is not null:
    while lots > 0 and entry_premium * (lots * lot_qty) > base_capital * daily_loss_fraction:
        lots -= 1
if lots == 0:
    reject
```

`lot_qty` stays the agent's 65 unless S2-01 recorded a conflict, in which case the size check uses the quantity already on the signal and does not invent a second lot size. The ceiling does not go above 4. The reserve is not added to `base_capital` for this division.

This will reject or shrink almost every current 3-lot Nifty debit, because one lot of premium is often already more than ₹1,000. That is an honest gate, and it is a behaviour change on entries. It is not a change to the signal rules. Ship it only with the acceptance test below, and with the operator told that Telegram entries will stop until the fraction or the lot ceiling is a conscious choice. The default fraction 0.02 is written in the risk file so it is visible. Do not hide it in code only.

If that rejection rate is not acceptable, the alternative of equal size is to keep lots at 3/4 and only set `max_loss_rupees` to the true premium, without rejecting. That alternative does not meet the requested "sizing based on actual premium risk". The design picks the shrink-or-reject rule and names the consequence. Do not do both.

### Data model

No new store. `RiskApproval.lots` can be lower than 3. `quantity` follows lots.

### Historical compatibility

Open trades keep the lots they were opened with. The gate applies to new entries only.

### Failure scenarios

- Premium missing: do not shrink. The frozen lots stand, and `rupee_risk_known` is false. Shrinking on a null premium would be a hidden zero.
- Premium dust (0.05) that makes 4 lots legal: lots stay at the frozen number, not at a new larger number.
- Super-trend 4 lots reduced to 1: still one position. The second-entry-after-breakeven rule in the manager is unchanged.

### Regression tests

Premium 100, lot 65, base 50,000, fraction 0.02, frozen 3: `100*65=6500 > 1000`, so lots fall 3 → 2 → 1 → 0 and the order is rejected. Premium 10, same inputs: `10*65=650 ≤ 1000`, lots stay 3. Premium null: lots stay 3 and `max_loss_rupees` is null. Super-trend does not produce 5 lots.

### Acceptance

No path returns more lots than the frozen rule. A known premium that cannot fit in one lot at the written fraction is a reject. The reserve balance does not appear in the inequality.

## What Sprint 2 deliberately skips

- A fill table, a new order manager, and wiring `ExecutionManager` or `trading/orders.py` into the daemon.
- Broker cash, margin, and SPAN. Those fields stay null.
- Rewriting October workbooks, the 213954.1 reserve, or catalog literals in `analyzer.py`.
- Changing sweep, waterfall, holding, or exit thresholds. The dirty-tree strategy edits listed in the verification note are not part of this sprint. Do not "finish" them here.
- Black-76 backfill of old `pnl_rupees`.
- Turning on `LIVE_TRADING_ENABLED`.

## Order of work

S2-04's "do not overwrite a bad risk file" goes first, because every later save touches that file. Then S2-01 and S2-02 together (a premium that is not priced is worse than no field). Then S2-03 so readers do not zero the new nulls. Then S2-05 last, because it changes which signals become journal rows, and it should land on a book that can actually store the premium.
