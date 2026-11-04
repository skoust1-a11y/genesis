# Sprint 2 task register

Date: 4 Nov 2026. Do not start until this design is approved. No task places an order, edits `active_strategy_params.json`, rewrites a historical trade, or changes a strategy threshold. Detail is in `sprint2_financial_integrity_design.md`. This list is the order and the stop conditions.

Complexity: S under a day, M a few days.

## S2-04 Risk file fail-closed, one reserve writer

- **Now:** A risk file that does not parse is replaced on the next `begin_session`. `SafetyWallAgent.process_daily_pnl` can rewrite the same file and drop `session_date` once any premium method exists. `effective_capital` is `base + reserve`.
- **Change:** `save_config` does nothing when the load failed. `begin_session` does not clear that block by writing defaults. Remove the risk-file write from `SafetyWallAgent.process_daily_pnl`. Stop writing `effective_capital`. Add `cash_inr: null`, `margin_inr: null`, `daily_loss_fraction: 0.02` only on a save of a file that loaded. Copy `safety_wall_reserve` through unchanged.
- **Files:** `backend/app/trading/crew/risk_agent.py`, `backend/app/trading/post_market_crew/safety_wall_agent.py`, `backend/app/trading/crew/manager.py` (`begin_session` false returns before an exit calls `record_trade_outcome`).
- **Depends:** none.
- **Sequence:** first.
- **Complexity:** S.
- **Data:** same JSON. No rewrite of a file that failed to load. First good save drops `effective_capital` and adds the null cash and margin keys.
- **History:** 213954.1 is not recomputed.
- **Failure:** truncated file bytes unchanged. Disk error on a good file restores counters and stays blocked.
- **Tests:** A-12 fixture. Reserve fixture unchanged by an unpriced save. Safety-wall call does not touch the risk file.
- **Accept:** unreadable risk file is still unreadable after a tick. Reserve number identical. Entries blocked.

## S2-01 Quote fields on the journal row

- **Now:** `record_entry` stores the spot signal. `contract_quote` is used for Telegram only.
- **Change:** Write `entry_premium`, `fill_kind`, `option_type`, `strike`, `quoted_lot_size`, `premium_missing` from `contract_quote` onto the existing dict. Null when the quote is missing. No default premium.
- **Files:** `journal_agent.py` `record_entry`; `manager.py` block after `record_entry` that already calls `contract_quote`; `trade_store.py` `upsert_trade` key mapping (`entry_price`, `quantity`, `direction`, `realized_points`, `realized_r`).
- **Depends:** S2-04 so a bad risk file cannot be saved as a side effect of an entry.
- **Sequence:** second, with S2-02.
- **Complexity:** M.
- **Data:** optional keys on the current JSON row. No new table. SQLite columns already exist.
- **History:** old rows are not backfilled.
- **Failure:** missing quote leaves nulls. SQLite failure does not roll back the JSON row.
- **Tests:** chain LTP 100 stored; `nifty_chain=None` stores null and not 78.67; upsert round-trip keeps premium and realized R.
- **Accept:** the active file and SQLite agree. No model premium is written.

## S2-02 `pnl_rupees` from `leg_costs`

- **Now:** exit always sets `pnl_rupees` null and `index_points_not_priced`.
- **Change:** When entry and exit premiums and quantity exist, set `pnl_method` to `premium_leg_costs` and `pnl_rupees` to gross minus `leg_costs` for BUY then SELL. Store the breakdown. Otherwise keep the Sprint 1 null. Put `premium_leg_costs` in the one shared `PREMIUM_PNL_METHODS`. `record_trade_outcome` may then add half of a positive net, and a negative net may increment the loss count. The −500 rupee stand-in goes away for this method only.
- **Files:** `journal_agent.py` `record_exit`; `manager.py` exit branch; `engine/costs.py` call only; `risk_agent.py` `record_trade_outcome`.
- **Depends:** S2-01.
- **Sequence:** same change as S2-01, or immediately after.
- **Complexity:** M.
- **Data:** charge keys on the row only when priced. `est_pnl_inr` NULL or the net. Not 0.
- **History:** numeric `pnl_rupees` without a method stays on disk and is not a premium method.
- **Failure:** `leg_costs` raises means null rupees, points still stored, reserve unchanged.
- **Tests:** 65 qty, 100 to 110, net equals `650 - leg_costs(...).total` within one paisa. No premiums: reserve unchanged. Legacy 1300 without a method: reserve unchanged.
- **Accept:** the financial-correctness worked example passes. The 0.5 formula is still absent.

## S2-03 Readers treat null as unknown

- **Now:** post-market normalizer, Excel merge, API active merge, and `compute_metrics` turn a missing rupee into 0 or into 78.67. Trailing alerts use `risk_points * 4.5`.
- **Change:** Pass null through. Copy `pnl_method`. Metrics set `pnl_status` to `incomplete` if any row is unpriced, and do not raise. Trailing and hop alerts say "not priced" unless `entry_premium` is on the row. Frontend home card and forward-tested page render null as not priced.
- **Files:** `post_market_manager.py`, `safety_wall_agent.py` (no risk-file write, already in S2-04), `trade_store.py` `compute_metrics`, `app/api/routes.py` `get_forward_tested_trades`, `alert_agent.py`, `frontend/src/app/page.tsx`, `frontend/src/app/forward-tested/page.tsx`.
- **Depends:** S2-02 so the null and the premium method both exist.
- **Sequence:** after S2-02.
- **Complexity:** M.
- **Data:** `pnl_status` on the metrics payload. No migration of workbook cells.
- **History:** workbook `Est P&L` stays in the legacy bucket. Catalog literals in `analyzer.py` are out of scope.
- **Failure:** mixed priced and unpriced day shows incomplete, not a partial sum labelled as the day's P&L. The priced row may still have moved the reserve.
- **Tests:** normalizer keeps null and method. Metrics do not throw on NULL. API payload for a crew-shaped active row has null premium, not 78.67.
- **Accept:** grep of `pnl_rupees` in `backend/app` finds no `or 0.0` coercion.

## S2-05 Shrink lots from premium risk

- **Now:** lots are 3 or 4. `max_loss_rupees` is `points * 0.5 * qty` and is ignored.
- **Change:** `max_loss_rupees` is `entry_premium * quantity` or null. After the frozen lot is chosen, step down by one lot while that product exceeds `base_capital * daily_loss_fraction`. Zero lots rejects. Missing premium does not step down. Never step above the frozen lot. Do not add the reserve to base.
- **Files:** `risk_agent.py` `evaluate_order`. Callers already honour `approved` and `lots`.
- **Depends:** S2-01 so the premium on the approval is the quote, and S2-04 so the fraction is on the file. Can be coded after S2-03.
- **Sequence:** last.
- **Complexity:** S.
- **Data:** none beyond `daily_loss_fraction` from S2-04.
- **History:** open trades keep their lots.
- **Failure:** premium 100 with 65 quantity and a ₹50,000 base at 2% rejects. That will block typical 3-lot debits. It is the specified gate, not an accident.
- **Tests:** the three cases in the design (100 rejects, 10 stays at 3, null stays at 3 with null `max_loss_rupees`). No approval returns `points * 0.5 * qty`.
- **Accept:** lot count is never above the frozen rule. The inequality uses `base_capital` only.

## Not in this register

T-07 research/live rule card, broker adapters, margin, and any edit to holding, exit, or sweep rules. Those stay out until this register is done and approved on its own.
