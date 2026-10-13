# Existing strategy specification (frozen baseline)

This document freezes the strategy **as specified in the audit brief and as implemented in `backend/app/liquidity_signals.py` `scan()` (L88-156)**. All baseline numbers in this audit refer to this specification ("A Original"). The research replica is `scan_v()` in `backend/scripts/audit_liquidity.py` (L153); on every non-expiry session from 2016-01-01 to 2026-10-13 (568 production signals) it produces exactly the same signals as production `scan()` (0 mismatching days, see `RESULTS.md` "Reproduction").

## Working-tree divergence during the audit

During this audit another session changed the uncommitted production file (not by this audit):

| Item | Specified baseline (A) | Working tree now ("v3") |
|---|---|---|
| Target | 2R (`R_MULT = 2.0`) | 3R (`R_MULT = 3.0`, L49) |
| Stop management | Fixed | Stop moves to entry from the bar after +1R is touched (`exit_of`, L352-369, reason "Breakeven") |
| Option strike | ATM | 2 strikes OTM (`OTM_STEPS = 2`, `strike_of`, L59-61) |
| Alert text | - | "Move the stop to entry ... (+1R)" (L185) |

`scan()` itself (entries, stops, filters) is unchanged. Both exit versions are reproduced bar-for-bar by `run_exit()` (0 mismatches each). v3 is reported separately as "Working tree v3" and is **not** treated as the baseline.

## Data inputs (live)

| Input | Source | Code |
|---|---|---|
| 5m NIFTY bars | Yahoo `^NSEI` 5m, few minutes delayed | `session_data` L345-349 |
| VWAP side | NIFTYBEES.NS 5m VWAP (ETF proxy, index has no volume) | L347-348 |
| PDH / PDL | Yahoo `^NSEI` 1d, previous row | L349, L429-431 |
| Day open | First 5m bar's open | L94 |
| India VIX | Yahoo `^INDIAVIX` 1d, last close | `option_plan` L234 |
| Option chain | Groww live chain | `option_plan` L221-238 |

## Long (PDL sweep reversal)

1. **Sweep**: a 5m bar with `low < PDL - 1` (L100). Stage 1 begins; the running sweep extreme `ext` tracks the lowest low thereafter (L105).
2. **Structure**: at the close of a 15m bar (evaluated on its third 5m bar, L107), `15m close > high of the last confirmed 15m swing high` (L110-114). The swing high is taken from all 15m bars of the session completed before this one, **including bars before the sweep**.
3. **Entry**: on the same or a later completed 5m bar with close time between 09:30 and 14:30 (L126-131): `close > VWAP` (proxy), `close > PDL`, `close > day open`.
4. **Stop**: the last confirmed 15m swing low formed at or after the 15m bucket of the sweep, minus 5, if it is below the entry (L137-140); otherwise the sweep low minus 5 (L142).

## Short (PDH raid reversal)

1. **Raid**: a 5m bar with `high > PDH + 1` (L100).
2. **Rejection**: a 15m close `< PDH` (L115-117).
3. **Structure**: a later 5m close below the low of the last confirmed 5m swing low (L118-122). It is never the same bar as the rejection (an `elif`).
4. **Entry**: as for the long, mirrored: `close < VWAP`, `close < PDH`, `close < open`, between 09:30 and 14:30.
5. **Stop**: the last confirmed 5m swing high at or after the raid bar, plus 5, if it is above the entry (L144-146); otherwise the raid high **plus 15** (L148). The long fallback is minus 5, so the two sides are asymmetric.

## Swings

`pivots()` (L81-85): bar j is a swing high (low) if its high (low) equals the max (min) of bars j-2 to j+2. **Equal highs both qualify (non-strict).** A pivot at j is only usable once bars j+1 and j+2 have completed, because `pivots(bars[:m])` cannot return j > m-3. There is no repainting (verified, see `SWING_AND_STRUCTURE_AUDIT.md`).

## Trade management (specified)

| Rule | Value | Code |
|---|---|---|
| Target | 2R from the entry close | L151 (now 3.0 in the working tree) |
| Exit | First stop or target touch after the entry bar; stop wins on a same-bar tie; else the close of the bar ending at 15:10 | `exit_of` L352-369 |
| Entry window | Close time 09:30-14:30 | L49, L127-130 |
| Max trades | 1 long + 1 short per day | `break` L155; `seen` set L418-439 |
| Min risk | 30 pts; a smaller setup **ends that side for the day** (it does not keep looking) | L51, L150-155 |

## Filters

| Filter | Value | Code |
|---|---|---|
| Expiry day | No trades on NIFTY weekly expiry (Tuesday; Monday or earlier if Tuesday is a holiday, which is the "day before a Tuesday holiday" rule) | `weekly_expiry` L241-246, `main` L405 |
| Losing streak | 3 losses in a row (index close past entry, **no costs**) means 3 trading days off | `break_until` L372-394 |
| Stale signals | Signals older than 15 minutes are printed as "missed", never sent | `FRESH` L50, L440 |

## Execution (alert only, no orders)

- **Option**: the ATM call (long) or put (short) in the specification; 2 strikes OTM in the working tree. Priced from the Groww chain; when the chain is unavailable, Black-76 at India VIX with forward = spot and a 0.15-day hold for the exit premiums (`estimated_plan` L249-267).
- **Size**: `RISK_PCT` = 5% of trading capital divided by the option loss per lot at the stop (L52, L211-213).
- **Alerts**: Telegram/email text plus charts (`message` L159, `charts` L319). **No order is ever sent.**

## Alert/rule inconsistency (resolved in this audit)

The alert used to say "Max 1 trade per side, stop for the day after 2 losses". `scan()` never enforces a 2-loss stop, and with at most one long and one short a day the rule could only ever bind after the day's last possible trade, so it never changed anything. The line now reads (L214):

> Max 1 trade per side (so at most 2 a day), exit 15:10 if neither stop nor target is hit.

The trading rules were not changed. `test_alert_text_matches_rules` in `backend/tests/test_liquidity_audit.py` guards the text. The 2026-10-13 plan workbook still says "stop after 2 losses" and "1% risk"; the code says 5%. That workbook was left as a historical record.

## Backtest cost and risk conventions used in this audit (assumptions)

- Net R = (index points - 4) / initial risk. The 4 index points (COST_PTS) are about Rs 2 of ATM premium at delta 0.5: charges plus about a 1 Rs spread.
- Option model: Black-76 on the index at the previous day's VIX, IV held constant through the trade, slippage Rs 0.5 per side (SLIP_RS), NSE charges from `app.paper.order_charges`.
- VWAP: the 10-year history uses the index running mean of the typical price ("TWAP", `twap_above` in `backend/scripts/backtest_liquidity.py` L132-140), because the index has no volume. Live trading uses NIFTYBEES VWAP.
