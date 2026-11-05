# S1-P6 remediation: strategy separation, stop monotonicity, crash integrity, gate equivalence

Date: 6 Nov 2026. Baseline: P5 commit `9846288`. P7 is not started.

Scope: the research-only runtime `backend/app/trading/research_runtime.py` and its journal. No production database, market feed, scanner, Telegram or broker was touched. No frozen rule source changed: `manager.py`, `exit_agent.py`, `detector_agent.py`, `liquidity_signals.py` and `backtest_pdc_15ema.py` keep their pinned SHA-256 values (test 19a).

## 1. Strategy separation

### Identity

| Strategy | Identity | Where it runs | Status |
|---|---|---|---|
| Scanner card | `strategy_id` = the scanner's own setup name (for example `Liquidity Sweep [5M]`), `strategy_version` = `scanner-v0` | `DetectorAgent` setups admitted by the research runtime | The P6 research subject |
| PDC Zone + 15 EMA V0 | Frozen card, rule table in `audit/genesis_v2/08_genesis_v2_target_architecture.md` | Only `backend/scripts/backtest_pdc_15ema.py`, which does not match the card | Independent benchmark. Not implemented, not changed |

Scanner signals are never labelled V0. Test 20 checks that admitted strategy ids are scanner names with version `scanner-v0`, that no trade label contains `pdc`, `15 ema` or `v0`, that the runtime does not reference the V0 script, and that the V0 script hash is unchanged. Each model trade also records `execution_policy` on its `MODEL_ENTRY` (section 2). The policy is not part of the signal id.

### V0 script against the frozen card

Read from `scan_pdc_trades` and `find_pdc_zone` at hash `62230eec…050d6e`. Line numbers refer to that file.

| Frozen card | Script | Lines |
|---|---|---|
| 5-minute NIFTY and BANKNIFTY | NIFTY only (strike step and data path) | 114 |
| Zone = previous day 15:25 bar high and low | Takes 15:25 or 15:20, else the last bar. A zone narrower than 6 points is replaced by midpoint ±3 | 65–85 |
| 15 EMA | EMA seeded from the first bar of the same day, with no prior-day warm-up | 54–62, 107–109 |
| CE condition | Touch with a 4-point band above the zone high and 8 points below the zone low, a bullish candle (close > open) and a close above the EMA | 130–135 |
| PE condition | Close below the zone low and the EMA, plus an extra 3-point body filter and "one of the two previous closes was at or above the zone low" | 215–218 |
| No risk filter on the card | Trades only if risk is 10–65 points | 139, 222 |
| Candle-close stop | Wick stop (`low <= stop`, `high >= stop`) at zone edge or bar extreme ±4 | 137, 155, 220, 236 |
| 50% at 2R, then PDH/PDL | One full exit at `max(2.5R, PDH)` (`min(2.5R, PDL)` short). No partial | 141, 223 |
| Opposite close of the 15 EMA | Present, but ignored on the first bar after entry (`j > i + 1`) | 169, 250 |
| Session 09:20–15:15 | Entries 09:25–14:45. The loop also skips the first 2 and last 4 bars. Square-off is the day's last bar close, labelled "15:15" | 116, 122, 145–147 |
| No overlapping same-direction position | Blocks every new signal, in either direction, until the prior exit | 112, 118, 177 |
| Not specified | Stop is checked before target on the same bar | 155–167 |

Conclusion: the script is not the benchmark. V0 still needs a pure function that matches the card. Building it is outside P6.

## 2. Stop-loss monotonicity

### The reported movement

Fixture model trade: CALL (long) at 24000, stop 23970, target 24060, risk 30 points, 3 lots. For a long, any fall in the stop loosens it.

Sequence in the original P6 test 11:

| Step | Stop | Function | Effect |
|---|---|---|---|
| Entry | 23970 | scanner setup `tentative_stop` | — |
| Bar closes 24065 (target hit, lots ≥ 2) | 24057 | `ExitAgent.evaluate_exit` (`exit_agent.py` 104–137): `ratcheted_sl = target − 3`; written by `TradeManager.on_market_tick` 313–326 | Tightens. 1 lot booked, target promoted to 24105 |
| Next tick, r = 2.17 | 24000 | `on_market_tick` 420–423, 0.7R milestone: `stop_loss = entry_price` | Loosens 57 points |
| Next tick | 24024 | `on_market_tick` 444–451, 2R milestone: `entry + 0.8 × risk` | Still 33 points below 24057 |

The same defect also appears in another order (test 19d): breakeven first (23970 → 24000, a real tightening), then the hop (24057), then 2R pulls the stop back to 24024.

### Root cause

The milestone block writes an absolute stop computed from the entry price. It never compares that stop with the current one, and its milestone keys (`0.7R`, `2R`) ignore `HOP_PROMOTED`. `ExitAgent` only partly hides this: with `be_locked` it uses `max(stop, entry)` for a long, so the stop never drops below entry. It can still drop from 24057 to 24000.

### Legacy bug or intended behavior

It is a legacy defect. The milestone's own alert text says "MOVE STOP LOSS TO COST", "100% RISK-FREE" and "LOCK +1R ON RUNNERS". Each is a protective instruction, and none intends to give back profit the hop already locked. `ExitAgent`'s `be_locked` floor points the same way. None of this is part of the frozen V0 card, which has no hop and no breakeven milestone.

### Did P6 reproduce it?

Yes, exactly. The original P6 test 19c drove `TradeManager` and the runtime with the same bars. Both produced 24057 → 24024 and a 24-point exit.

### Decision

- `manager.py` is unchanged and still pinned. The daemon keeps the defect, and test 19d records the legacy sequence.
- The research runtime now runs under `EXECUTION_POLICY = "research-exec-v1-monotonic-stop"`, which is journaled on every `MODEL_ENTRY`. Every stop the runtime writes, from the hop ratchet and from both milestones, goes through `_tighter`: up only for a CALL, down only for a PUT. Milestones are still recorded with the stop held, so `be_locked`, the stagnation allowance and the milestone order stay in step with legacy.
- Result difference: it applies only when a milestone fires after a tighter stop, which today means after a hop. In the fixture, the runner exits at 24057 for 57 points (1.9R) instead of 24024 for 24 points (0.8R). With no hop, the runtime still matches `TradeManager` step for step (test 19c).
- `finalized_results` counts only scanner-v0 trades of the current policy. Trades journaled without it are listed as `OTHER_EXECUTION_POLICY`; trades of any other strategy version as `OTHER_STRATEGY_VERSION`.

## 3. Crash recovery integrity

Every journal write is one SQLite `IMMEDIATE` transaction under the session fence. Startup recovery runs the consistency check inside the write lock and refuses an inconsistent journal before writing (tests 02, 02b).

| Scenario | Behavior | Test |
|---|---|---|
| Crash after admission, before or inside `open_trade` | The trade row and its `MODEL_ENTRY` roll back together. The admission stays with its original session id. At restart, `missing_model_entries` lists it. A rescan returns `MISSING_ENTRY` and opens nothing. A surviving session does not open it late either | 23 |
| Crash after entry | Current-day orphan, reported with its owner session. Never adopted, modified or closed, even when price crosses its stop. No duplicate admission or trade | 12 |
| Crash after a partial exit | Orphan stays `PARTIALLY_EXITED` and untouched | 24 (`partial_exit`) |
| Crash mid-hop (levels written, partial not) | Orphan with hop levels and no partial, untouched. A same-session failure is completed on the next tick (11b), but `run()` stops on any tick error, so in practice this only happens through a crash | 24 (`hop_levels`), 11b |
| Crash during a journal mutation | Atomic rollback, verified with the existing event failpoint | 23 |
| Restart on the same market day | New generation session. Prior-day open trades become `EXPIRED_UNRESOLVED` with no price. Current-day orphans and UNKNOWN publications are reported, never resent or adopted | 01, 12, 15, 24 |
| Duplicate prevention | `signal_id` is unique per day for admissions and globally for trades. The runtime checks the trade before any open and returns `EXISTS`, `PRIOR_SESSION` or `MISSING_ENTRY` | 06, 12, 23, 24 |

`finalized_results(path)` counts only `CLOSED` trades whose `MODEL_EXIT` has `model_points` and `model_r` and whose entry carries `strategy_version = scanner-v0` and the current policy. Its output states the strategy version, policy, eligibility rule and units. Open, orphaned, expired, quarantined, other-strategy-version and other-policy trades are listed under `excluded` with their state or reason. Admissions with no entry are listed under `missing_model_entries`. Totals are index points and R only, with no premium or rupee figures (test 25).

Limitation, fail closed: a lost entry cannot be recovered deterministically, because opening it later would invent the entry time and skip the bars in between. Orphans and missing entries therefore stay unresolved. No reconciliation tool exists, so they stay excluded from finalized results permanently.

## 4. Entry-gate equivalence

Legacy means `TradeManager.on_market_tick` as called by `scripts/live_scanner.py` (no `india_vix`, no `df_1m`).

| Gate or exit | Legacy daemon | scanner-v0 research runtime |
|---|---|---|
| VIX corridor 11.5–22 (missing VIX vetoes) | Applied. Fetches `^INDIAVIX` itself | Bypassed |
| Gamma walls (missing walls veto, 15-point and runway vetoes) | Applied | Bypassed |
| Opening blackout 09:15–09:30, terminal zone ≥ 14:30 | Applied | Bypassed |
| Lunch gate 11:30–13:00 (volume or high-R override) | Applied | Bypassed |
| 13:00–13:30 transition blackout | Applied | Bypassed |
| EQH/EQL magnet gate, VWAP regime gate, consolidation lockout | Applied | Bypassed |
| TrendAgent breakout veto | Applied | Bypassed |
| Target recomputation (`risk × recommended_target_r`) | Applied. Replaces the scanner target | Bypassed. Uses the scanner `tentative_target` |
| Sniper entry (`SniperAgent`) | Code present but inactive: the daemon passes no `df_1m` | Bypassed (matches the daemon in effect) |
| Risk approval (`RiskAgent.evaluate_order`, circuit breaker) | Applied | Bypassed. P3 admission applies quote, freshness and contract checks instead |
| Delta ≥ 0.35 strike selection | Applied (2 OTM) | Bypassed. Uses the scanner's suggested strike |
| Overlap rule (second entry only if the open trade is `be_locked`) | Applied | Bypassed. Every RESEARCH_ONLY admission opens a model trade |
| Dynamic lookout window (4× the bar length) | Applied | Applied |
| `is_valid_for_trade_crew` | Applied | Applied |
| `HoldingAgent` (cushion stop, pre-emptive exits, flip and reverse) | Applied | Disabled. Depends on process state and unjournaled fields |
| Opposing-signal reversal exit | Applied | Disabled. Goes through the legacy risk file |
| `ExitAgent` stop, target, hop, stagnation, 15:10 cutoff | Applied | Applied |
| 0.7R and 2R milestones | Applied, can loosen the stop | Applied under the monotonic policy |
| ADX lot upgrade (4 lots at ADX ≥ 25) | Applied | Bypassed. Fixed 3 model lots |

scanner-v0 results are not equivalent to legacy daemon trading results. They measure scanner signals with the daemon's exit engine and should only be reported that way.

## 5. Changed files

| File | Change |
|---|---|
| `backend/app/trading/research_runtime.py` (new in P6) | `EXECUTION_POLICY`, `_tighter` on every stop write, once-only milestone keys, `missing_model_entries`, `finalized_results`, `MISSING_ENTRY` instead of a late open |
| `backend/app/db/crew_journal.py` | P6 consistency check under the write lock (`_consistency_findings`). Optional `execution_policy` on the `MODEL_ENTRY` payload |
| `backend/app/db/crew_session.py` | Recovery runs the consistency check inside its transaction |
| `backend/app/trading/admission.py` | `open_research_trade` passes `execution_policy` through |
| `backend/tests/test_research_runtime.py` (new in P6) | 32 tests, including 11, 12, 19c and 19d (updated or added) and 20–25 (new) |

## 6. Remaining blockers

1. The PDC V0 benchmark function does not exist. The script differs from the card (section 1).
2. scanner-v0 bypasses most daemon entry gates and two exits (section 4). It is a signal study, not a daemon replica.
3. The daemon's stop-loosening defect is still live in `manager.py`. Fixing it there is a strategy change that needs its own approval and version.
4. Orphans and missing entries have no operator reconciliation path, so they stay excluded permanently.
5. Carried over from P5 and P6: `resolve_unknown` has no authenticated operator interface. `quarantine_legacy_reference` is kept out of the runtime by a test, not a runtime guard. There is no market-hours gate. Lease expiry depends on each writer's clock. The live feed has only been exercised against stubs.
