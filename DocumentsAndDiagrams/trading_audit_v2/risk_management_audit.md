# Risk management audit

Audit date: 4 Nov 2026. Capital figures below are the contents of `trades/daily_risk_config.json` at read time: base ₹50,000, safety-wall reserve about ₹2,13,954, `daily_cumulative_r` 9.65, `daily_losses_count` 0, lock false, `default_lots` 3.

## What is enforced on the daemon

| Control | Enforced? | Independent of the signal? | Notes |
|---|---|---|---|
| New trade only if flat, or one trade already at breakeven | Yes | Yes, in `TradeManager` | Second bet is allowed (V2-12) |
| Stop distance > 55 points | Yes, in the detector and again in `RiskAgent` | Yes | `scan` can emit a wider swing stop. Those never become candidates. The 22–35 point clamp is `EntryAgent`, used for the waterfall and for a fallback the daemon does not take |
| Daily loss count ≥ 2 or cumulative R ≤ −2 or lock flag | Yes | Yes | Counters are not reset by any caller (V2-05). "Daily" is since the file was last written by a trade or a hand edit |
| Loss definition | Partial | — | A loss counts only if `pnl_rupees ≤ −500` or `realized_r ≤ −0.5`. A −0.4R loss does not count. A win does not zero the loss count |
| VIX band, stale bar, missing wall, 09:30, 14:30 | Yes | Yes | Real gates |
| Lunch 11:30–13:00 | Weak | — | Waived if the candidate name contains sweep, equal or reversal, or risk ≥ 30, or volume ≥ 1.5× (V2-19) |
| Rupee cap vs capital | No | — | `max_loss_rupees` is computed and ignored (V2-11) |
| Safety wall as a cash reserve that cannot be traded | No | — | The reserve is a number in JSON. Sizing does not shrink when the reserve grows, because `frozen_baseline` forces 3 or 4 lots. The wall does not protect capital; it inflates a headline (V2-04) |
| Kill switch that flattens a broker | Not on this path | — | `live/kill_switch.py` belongs to deployments. The daemon has no broker position to flatten |
| Max daily loss in rupees (settings default ₹10,000) | Not on this path | — | `PORTFOLIO_MAX_DAILY_LOSS_INR` is read by the research trade crew |

## Circuit breaker bug (V2-05)

`RiskAgent.reset_daily_counters` sets losses, cumulative R, and the lock back to a fresh day and saves the file. Repository search shows the definition and no call. Consequences:

- A −2R day sets `is_safety_wall_locked` true. Nothing in the morning run clears it. Trading stays off until a person edits JSON.
- A string of green days leaves `daily_cumulative_r` large and positive (the file showed +9.65). A later −2R session does not reach the −2 threshold. The breaker fails open.
- `daily_losses_count` similarly carries across calendar days.

The comment says "2 consecutive losses." The code increments on every material loss and never decrements on a win. Three losses spread across winners still trip the count, and they need not be consecutive. The comment and the code disagree; the missing reset is the serious part.

## Safety wall (V2-04)

On a win, half of `pnl_rupees` is added to `safety_wall_reserve`. `pnl_rupees` is delta-0.5 (see options audit). Half of a fictional gain is still fictional. The post-market briefing then prints the reserve as if it were equity and recommends a lot tier from it ("7 lots" on 3 Nov) while the sizer stays at 3. An operator who sizes from the briefing, not the code, over-risks.

`record_trade_outcome` is the only writer of that ratchet. If exits do not call it, the file and the briefing diverge. This session confirmed the formula and the file contents, not a line-by-line trace of every exit into `record_trade_outcome`. Treat the ₹2.14 lakh as an accounting artifact until a broker ledger says otherwise.

## Research stack (the other risk system)

`engine/sim.py` can apply a drawdown ladder, a floor, a daily target, an odds gate, and `RiskLimits`. Defaults in `config.py` turn off `max_entries_per_day`, `max_consecutive_losses`, `loss_cooldown_days`, `max_loss_pct_of_capital`, and `max_losses_per_day` (all 0). A strategy is unprotected unless its own rules or a deployment policy set the limits (V2-15).

At entry, `pnl_today` sums only trades whose **exit** date is today, and a new entry is skipped entirely when something already closed today (`if not closed_today`). Open mark-to-market is not in `pnl_today`. A daily loss limit cannot see an open loss, and it cannot block the first entry of a day. This is October finding G-05, still visible in the loop.

There is no margin check. A short-premium structure can show a return on capital that could not post SPAN margin (October G-07). Not re-simulated today. The scanner only buys options, so margin is premium paid; the research engine is the one that can sell premium.

## Scenarios the scanner does not survive safely

| Scenario | What happens |
|---|---|
| Process down while a paper "position" is open | JSON still says open. Nothing at a broker, so the financial loss is the operator following Telegram by hand. If they did, the platform will not square it off |
| Restart mid-session | In-memory idempotency set is empty. Active JSON still blocks the same key. A closed trade's key is gone, so the same setup can be journaled again |
| Spot gap through the stop at the open | Exit uses the CMP the feed returns. The journal books the point difference times 0.5, not the option's open print |
| India VIX unavailable | New entries stop. Open trades still exit on spot. Sensible |
| Groww page change | Chain is None, walls missing, no new entries, and no quote on alerts. Open trades are managed on spot only. Premium risk becomes invisible |
| Two strategies, same morning, same direction | Allowed after breakeven on the first. Both lose if the index reverses |
| Operator believes the safety wall is withdrawable cash | It is a float in a JSON file |

## Independence

The scanner risk check does not import the strategy's private stop math from `scan()`, which is good. It still trusts the stop the detector just invented, then clamps or rejects it. A detector that emits a 25-point stop by default (`CandidateSetup.risk_points = 25`) will pass the 55-point cap even when the swing that would invalidate the idea is 80 points away. The cap creates a false sense that risk was measured.

## Required shape of a real limit

One function, called for every new order, that receives premium at risk (ask × quantity, or a measured worst case), realized losses today in rupees, open mark in rupees, and a hard stop in rupees. It must reset on the IST date. It must not reset because a JSON load failed. The signal code must not be able to skip it. That function does not exist on the daemon path.
