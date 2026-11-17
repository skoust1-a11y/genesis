# Reconciliation of the earlier 27 Genesis trades

**The saved INR87,637.22 reconciles exactly under its original index-points × delta-0.5 formula. It is not an arithmetic error. It is not verified historical option net profit.**

## Comparable dates and what could be reproduced

The old artifact covers 15 October–2 November 2026, not the full period through16 November used in the recent studies. All comparisons below stop at2 November. Each constrained account starts at INR50,000.

Original artifact: reports/last_sim_trades.json in the read-only archive, hash d96dbe57525f5128efbce68d9e2f5d8be5a43dbf4f03fc246329f0cd88069482. It was first committed in33706eb. Trade-id generation dates point to3 November after efde368; this is the latest committed revision before generation, but not proof of the exact generating working tree. Saved rationale strings include “V2 Dynamic R:R”, which is absent from the efde368 manager. The original Yahoo response was not preserved. These source/data gaps prevent an exact reproduction of all27 trades.

This investigation runs the actual efde368 scanner, EntryAgent, TrendAgent, RiskAgent, GammaAgent fallback and TradeManager under a separate legacy_app namespace. Only imports, alert delivery and the unused global SQLite mirror are adapted; source bytes are preserved. Networking is blocked, archived helper writes are restricted to this audit directory, and operational state is untouched.

| Evidence / replay | Entries / closed | P&L INR | Meaning |
| --- | ---: | ---: | --- |
| Saved original artifact | 27 /27 | 87,637.22 | Fee-free index/delta proxy; old sizing and saved prices |
| Closest-source legacy-script emulation on frozen provider data | 24 /24 | 65,153.39 | Same old proxy; noncausal replay assumptions retained for diagnosis |
| Same committed engine with causal inputs | 24 /24 | 47,534.49 | Still old sizing and stale-price accounting; diagnostic, not an executable return |
| Same committed engine with current execution/risk gates | 5 /5 | 1,366.92 | Synthetic bid/ask net after charges; fresh entries, actual quote sizing and chronological cash |
| Current zone entries + fixed3R, same dates | 10 /10 | 4,219.71 | Existing matched model control, cut at2 November |
| Current selected trail, same dates | 10 /10 | 1,282.68 | Existing matched model control, cut at2 November |

**INR1,366.92 is the constrained replay of the closest committed engine, not a claim that the exact saved27 mechanically reduce to that amount.** Different source/data, admission and portfolio paths prevent that interpretation.

## Quantified differences

- **Sizing and risk:**19 original positions started at4 lots,8 at3. Under the old risk model, initial estimated stop exposure was INR3,402.75–7,078.50 per trade. The current stop-risk budget starts at INR1,000 and the full-premium limit is INR5,000. Merely imposing a3-lot ceiling on the same old index-price path gives INR72,778.91; this is an arithmetic exposure diagnostic, not an executable strategy return.
- **Affordability:**at the saved timestamps,15 of27 selected current two-OTM contracts exceed the INR5,000 full-premium cap even at one lot;1 fails the spread gate and1 fails current stop geometry. Ten pass a flat-account sizing diagnostic. This does not mean all ten are valid causal or chronological account entries.
- **Causal signals:**20 saved native strategy/time/side/price signatures appear in flat closed-bar probes at their saved close (price tolerance2 index points for provider differences). Two trendline signatures appear only five minutes later. Four native signatures do not reproduce in the tested closed-bar probes. One trade is the manager-only waterfall fallback, not a native scanner signal. See signal-probes.json; a raw pattern is not the same as a fully admitted trade.
- **Admission versus signal timestamps:**the emulated portfolio matches20 saved entry signatures. Six of these are admitted more than90 seconds after the corrected source confirmation, retaining an earlier signal price; one matched15m signal is admitted before its declared close. The waterfall’s own5m bar-start stamp is treated separately from genuine staleness. Six saved rows overlap in timestamp intervals despite the manager’s one-position intent, consistent with retrospective signal timestamps; this is not proof of simultaneous funded fills.
- **Option accounting and missing fills:**the old file has no option expiry, bid/ask, charges or partial timestamps. Five trades have booked partial gains but no time-stamped option fills. Exact after-cost option P&L for those27 cannot be recovered from this artifact. Breakeven at the underlying entry is not net option breakeven.

## Concrete temporal examples from the closest-source emulation

| Saved row | Recorded signal | Emulated admission | What changes |
| --- | --- | --- | --- |
| 10 | 2026-10-21 11:55 PUT at 22818.45 | 2026-10-21T12:00:05+05:30 | Latest closed spot 22808.8; saved-price difference -9.65 points |
| 16 | 2026-10-27 13:10 PUT at 22736.1 | 2026-10-27T13:30:05+05:30 | Latest closed spot 22684.65; saved-price difference -51.45 points |
| 21 | 2026-10-29 14:15 CALL at 22902.05 | 2026-10-29T14:05:05+05:30 | Latest closed spot 22902.05; saved-price difference 0.0 points |
| 27 | 2026-11-02 11:40 PUT at 23314.4 | 2026-11-02T11:50:05+05:30 | Latest closed spot 23295.35; saved-price difference -19.05 points |

For example, row16 is recorded as a13:10 PUT at22,736.10 but is admitted in the closest-source emulation at13:30:05, when the latest closed spot is22,684.65. Much of the apparent move has already happened before admission. Row21’s declared14:15 fifteen-minute confirmation is admitted at14:05:05 from a forming15m aggregate. These are concrete emulation findings, not proof that the missing generating working tree followed exactly the same path.

The old script also precomputed an opening range through the09:45-start bar and supplied it from the session start. None of the saved27 entry timestamps precedes09:50, so this audit does not attribute the entire profit gap to that early opening-range leak. The35-minute versus30-minute range and forming15m resampling can still change later setup/management decisions.

## Every original row

“Flat gate” assumes an otherwise empty INR50k account at the saved clock; it is not portfolio admission. “Current replay” refers only to matched entries in the fully constrained committed-engine replay.

| # | Saved entry / setup | Initial lots | Old proxy INR | Causal pattern probe | Flat gate | Current replay matched net INR |
| ---: | --- | ---: | ---: | --- | --- | ---: |
| 1 | 2026-10-15 09:50 Retest (PD) [5M] CALL | 4 | 15,356.90 | at saved close | premium cap | — |
| 2 | 2026-10-16 11:00 Retest (OR-30) [5M] CALL | 4 | -4,855.50 | at saved close | premium cap | — |
| 3 | 2026-10-16 11:15 Retest (PD) [5M] CALL | 4 | 4,517.50 | at saved close | 1 lot(s) | 252.48 |
| 4 | 2026-10-19 10:15 Trendline (5M) PUT | 3 | -3,865.88 | at saved close | 3 lot(s) | — |
| 5 | 2026-10-19 12:15 Retest (OR-30) [5M] PUT | 4 | 247.00 | at saved close | 3 lot(s) | 29.47 |
| 6 | 2026-10-19 13:20 Retest (PD) [5M] PUT | 4 | 0.00 | at saved close | spread | — |
| 7 | 2026-10-21 10:20 Retest (OR-30) [5M] PUT | 4 | -7,026.50 | at saved close | premium cap | — |
| 8 | 2026-10-21 11:00 Retest (OR-30) [5M] PUT | 4 | 13,084.50 | at saved close | premium cap | — |
| 9 | 2026-10-21 11:30 MIDDAY_WATERFALL PUT | 3 | 10,188.75 | manager fallback | premium cap | — |
| 10 | 2026-10-21 11:55 Retest (PD) [5M] PUT | 4 | 10,237.50 | at saved close | premium cap | — |
| 11 | 2026-10-22 10:55 Retest (OR-30) [5M] CALL | 4 | 338.00 | not reproduced | premium cap | — |
| 12 | 2026-10-23 12:00 Retest (OR-30) [15M] PUT | 4 | 0.00 | not reproduced | stop distance | — |
| 13 | 2026-10-26 10:55 Trendline (5M) PUT | 3 | 8,518.58 | at saved close | 1 lot(s) | 1,104.33 |
| 14 | 2026-10-26 11:30 Trendline (5M) PUT | 4 | 0.00 | only +5min | 1 lot(s) | — |
| 15 | 2026-10-27 10:55 Retest (PD) [5M] CALL | 4 | -4,218.50 | at saved close | 3 lot(s) | -554.65 |
| 16 | 2026-10-27 13:10 Trendline (5M) PUT | 3 | 3,568.50 | at saved close | 3 lot(s) | — |
| 17 | 2026-10-28 10:40 Trendline (5M) CALL | 3 | 6,147.38 | at saved close | premium cap | — |
| 18 | 2026-10-28 11:20 Trendline (5M) CALL | 3 | 0.00 | only +5min | premium cap | — |
| 19 | 2026-10-29 11:15 Retest (PD) [15M] CALL | 4 | 1,482.00 | at saved close | premium cap | — |
| 20 | 2026-10-29 14:15 Retest (OR-30) [15M] CALL | 4 | 6,383.00 | not reproduced | premium cap | — |
| 21 | 2026-10-29 14:15 Retest (OR-30) [15M] CALL | 3 | 3,124.87 | not reproduced | premium cap | — |
| 22 | 2026-10-30 11:15 Retest (OR-30) [5M] CALL | 4 | 4,673.50 | at saved close | premium cap | — |
| 23 | 2026-10-30 11:45 Retest (OR-30) [15M] CALL | 4 | 7,033.00 | at saved close | premium cap | — |
| 24 | 2026-10-30 13:30 Trendline (5M) PUT | 4 | 6,909.50 | at saved close | premium cap | — |
| 25 | 2026-11-02 10:15 Retest (OR-30) [5M] CALL | 4 | 487.50 | at saved close | 1 lot(s) | 535.29 |
| 26 | 2026-11-02 11:10 Trendline (5M) PUT | 3 | 1,106.62 | at saved close | 1 lot(s) | — |
| 27 | 2026-11-02 11:40 Trendline (5M) PUT | 4 | 4,199.00 | at saved close | 1 lot(s) | — |

## Price-only sensitivity, not an account return

For completeness, rows.json reprices the saved strikes at nominal saved clocks and, separately, at completion of the recorded exit5m bar. It keeps original3–4lot exposures and ignores whether the trade could have been admitted/funded, so these totals are not usable strategy returns. For the five missing partial times, it samples minute closes that touch the saved conquered index level. The resulting min/max is a conditional timing sensitivity, not recovered fills or rigorous historical P&L bounds.

At saved exit clocks, three partial histories cannot even be completed from an observed target touch. With the exit bar allowed to complete, the unconstrained timing sensitivity sums to INR62,193.24–67,507.50. This cannot be combined additively with the sizing, causality or constrained-engine differences.

## Checks and practical conclusion

All27 saved P&L rows reproduce exactly using the original floating-point operation order and final rounding. Current execution checks independently regenerate books, recompute fee components, cash and quantities, reserve the extra possible partial-exit fee, and reconcile freshness, entry geometry, premium/stop/daily limits. All three source lanes finish without held model or paper positions. See checks.json, rows.json, signal-probes.json, engine-results.json and source-manifest.json.

The earlier rupee headline combines a different exposure policy and an index/delta profit proxy with timing/provenance limitations. This audit finds genuine recurring entry patterns, but does not validate the reported rupee return under today’s account constraints. Matching that headline by loosening caps or retaining stale entry prices would not establish an improvement. Operational paper policy and Telegram delivery remain unchanged.
