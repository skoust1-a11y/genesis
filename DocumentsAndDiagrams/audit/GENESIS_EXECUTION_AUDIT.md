# Genesis — Execution and Broker Integration Audit (Phase 5)

No live trade was placed. All tests use `PaperBroker` or `SandboxBroker`.

## 1. What exists

- Adapters: `PaperBroker` (fills every LIMIT at once at quote ± `SLIPPAGE_BPS`), `SandboxBroker` (asynchronous
  and partial fills, rejections, freeze limit, SL orders, margin, persistent state). **No real broker adapter.**
- `BrokerAdapter` / `LiveBrokerAdapter` protocols require `get_order`, `margin_required`, `available_margin`,
  `positions`, `reconciles` for live.
- `OrderManager` (`app/trading/orders.py`): hedge-first ordering (BUY legs before SELL legs, opens and closes),
  freeze-quantity splitting in whole lots, `MAX_ORDER_VALUE_INR` cap, `MAX_ORDERS_PER_MINUTE` rate limiter,
  LIMIT orders at the quote, poll to terminal or timeout, cancel, re-price toward the market up to
  `ORDER_REPRICE_STEPS` within `MAX_SLIPPAGE_BPS`.
- Every request, status change, fill and charge is written to append-only tables through `LiveStore`.

## 2. Checks

| Item | Finding | Status |
|---|---|---|
| Authentication / token expiry | Broker credentials Fernet-encrypted in the live DB or from env; no real broker so no token refresh exists | UNVERIFIED (no adapter) |
| Request validation | Sandbox validates side/qty/type/price/freeze; `OrderManager` caps value | PASS |
| Placement / cancel | Implemented; modification not (cancel + new order instead) | PASS |
| Partial fills | Tracked per fill; remainder re-sent | PASS |
| Rejected orders | Entry: error → unwind filled legs → halt. Exit: retried `ORDER_EXIT_RETRIES` then halt with position open + alert | PASS |
| **Cancel not yet confirmed, then re-price** | Before: after a timeout the manager sent a cancel and immediately re-sent the remainder at a new price without confirming the old order was dead. With a real exchange (cancel is asynchronous) both orders can fill: **double position**. Hidden in tests because the sandbox cancels synchronously | **FIXED (G-01)** |
| Placed at broker, app timed out | `adapter.place_order` is called **before** the order row is written. If it raises, nothing is recorded, the crew halts (runner catches every exception) and the next start's reconciliation halts on the unexplained position. Fail-closed, but the audit trail does not show the order (G-10) | WARNING |
| Duplicate order prevention / idempotency | `client_order_id = tc-<intent>-<n>` is deterministic per attempt. Indian brokers generally do not deduplicate on client tags (UNVERIFIED per broker), so the only real guard is "never auto-retry an unknown outcome", which holds today because exceptions halt | PASS (by halting) |
| Retries | Only on explicit rejections, never on exceptions | PASS |
| Position reconciliation | Every tick and at startup, by tradingsymbol net quantity, across all deployments on the account; mismatch → halt + alert | PASS |
| Order status reconciliation | Working protective stops synced each tick | PASS |
| Latency / slippage measurement | `slippage_vs_expected` per live trade (fill vs quote) | PASS |
| Symbol mapping | `tradingsymbol()` builds `NIFTY26JAN2223000CE`-style symbols; must be mapped per broker in its adapter | UNVERIFIED |
| Tick / lot validation | Prices rounded to ₹0.05; quantities are lots × lot size; freeze split keeps whole lots | PASS (LOW: freeze below lot size gives odd chunks, G-25) |
| Rate limiting | Sliding 60 s window | PASS |
| Stale option quotes | Not aged individually (G-11) | WARNING |

## 3. The fix in detail (G-01)

`OrderManager._place_and_wait` now waits for the cancelled order to reach a terminal status (same poll budget
as the fill wait). `_fill_leg` refuses to re-send while the previous order is not terminal and returns an
error; the caller then unwinds (entry) or halts (exit), and reconciliation catches a late fill.

Test: `tests/test_live_plumbing.py::test_no_reprice_while_the_cancelled_order_can_still_fill` uses a broker
whose cancel is acknowledged but not yet done. It **fails on the old code** (3 orders sent) and passes now
(1 order, error raised). All 50 live-plumbing tests pass.

## 4. Recommended execution state machine

Separate the states explicitly (today they are implicit across `TradeIntent`, `Order`, `OrderEvent`, `Fill`):

```
SIGNAL ─► APPROVED (risk passed, intent written)
        ─► SUBMIT_PENDING  (order row written BEFORE calling the broker)   ◄─ add (G-10)
        ─► ACKNOWLEDGED (broker order id)  | REJECTED
        ─► WORKING ─► PARTIAL ─► FILLED
                 └─► CANCEL_PENDING ─► CANCELLED | FILLED (late)          ◄─ now enforced (G-01)
        ─► UNKNOWN (exception/timeout on submit or cancel)
              → look up by client id / order book; never re-send; halt until resolved
        ─► RECONCILED (broker positions == DB)
```

Rules: write before you send; never retry UNKNOWN; re-price only from CANCELLED; reconcile before every decision.

## 5. Before adding a real broker adapter (gate list)

1. Implement SUBMIT_PENDING/UNKNOWN (G-10) and an order-book lookup by client tag.
2. Map tradingsymbols and instrument tokens from the broker's instrument dump; refuse unknown symbols.
3. Token expiry: detect auth errors, halt, alert; never trade on a half-authenticated session.
4. Run the full sandbox suite against a recorded-replay of the real API (no live orders).
5. Paper-trade on the real broker's quotes for `LIVE_MIN_PAPER_DAYS` (set it above 0).
