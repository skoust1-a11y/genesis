# Risk management redesign

Date: 4 Nov 2026. Design only.

## Problem in the current code

`RiskAgent` is not an account gate.

- Lots are 3 or 4 (`frozen_baseline`).
- `max_loss_rupees` is `stop_points * 0.5 * qty` and is not a reject condition.
- `reset_daily_counters` is never called. `daily_cumulative_r` in the file is 9.65.
- The safety wall grows from F-02 and does not reduce size.
- A second position is allowed when the first has `be_locked`.
- Research `Settings` defaults (`max_entries_per_day`, streak, loss percent) are 0, meaning off, and `default_capital_inr` is 500000 while the scanner file says 50000.
- The parameter writer can change `micro_wick_stop_buffer`, which changes waterfall risk, with no approval.

## One gate

A pure function, called by the simulator and by `TradeManager` before a journal entry or an order. The signal code does not catch its exception and continue.

```text
decide(account, intent, now_ist) -> allow or reject-with-reason
```

`account` is one mode: scanner paper or research deployment. It carries `capital_inr`, `realized_today_inr`, `open_premium_mark_inr`, `open_positions`, `consecutive_losses`, `session_date`.

`intent` carries instrument, tradingsymbol or an explicit "unpriced", quantity, lot size, premium at risk for a long (ask or last, else null), spot stop distance in points, and the strategy id.

## Layers

| Layer | Question | Scanner rule until premiums exist |
|---|---|---|
| Strategy | Does this signal match the frozen card? | Reject if the card hash does not match |
| Trade | Is this bet small enough? | One position. Quantity = 1 lot from the chain or the dated table, not a hardcoded 65 if the chain sent a lot. Reject if premium is null and the mode requires a rupee cap. Index-only mode may journal the signal with `rupee_risk = null` and must not size up |
| Portfolio | Net exposure | One NIFTY option. No second trade after breakeven. No BANKNIFTY add-on in this phase |
| Account | Can the account take the loss today? | IST date rollover clears `realized_today`, the R sum, the loss count, and a lock that was set by those counters. A manual lock is a different flag and does not clear |

## IST reset

Store `session_date` as `YYYY-MM-DD` in the risk file. At the start of `on_market_tick`, if `now` in `Asia/Kolkata` is a later date, call the reset and save. Do not use `datetime.now()` without a zone.

Loss count: increment on a closed trade with R ≤ −0.5 or, once priced, on a negative net. A win sets the consecutive count to zero. The comment and the code use the word consecutive only if that is the behaviour. The current "any material loss, never cleared" behaviour goes away.

A −2R strategy halt may remain, on **this session's** R, not on a lifetime sum.

## Capital

The scanner account capital is `base_capital` (₹50,000). The reserve is not added to capital and is not a lot-sizing input until F-03 is retired and the reserve has a ledger. `tomorrow_approved_lots` is display from the post-market agent; the gate ignores it.

Research deployments keep their own capital on the deployment row. A report that mixes ₹50,000 and ₹5,00,000 without naming the mode is a defect.

## What the parameter agent may not touch

`base_capital`, loss limits, lot cap, the lock, and the live stop buffer. Proposals go to a different file (`parameter_governance_spec.md`).

## Scenario results the gate must produce

| Situation | Result |
|---|---|
| Second signal while one position is open, even if breakeven | Reject |
| Session date moved forward | Counters zero, then the new signal is judged on the fresh day |
| Same date, two losses already | Reject |
| Premium missing, mode is rupee-capped | Reject the order. A signal diary row is allowed only if it cannot move the reserve |
| Chain lot 65 vs config 75 | Use the chain lot stored on the intent |
| Process restart | Read `session_date` and counters from the file. Do not trust RAM |

`engine/sim.py` should call the same function for the limits that are actually on. Turning the zero defaults into silent non-zero defaults would change every old research run. The spec requires the report to show `caps_off: true` when the deployment left them at zero, and the scanner path does not get a zero daily loss once it is pricing rupees.
