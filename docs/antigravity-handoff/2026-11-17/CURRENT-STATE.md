# Verified state and user intent

Date/time zone: 17 November 2026, Asia/Calcutta (IST). The workspace is Genesis. The owner will use Antigravity to continue and will push the current branch when done.

## Active paper policy — do not confuse with research

Read the actual active-run manifest at `logs/paper-forward/active-run.json`; last verified run ID was `forward-20261116-172047`. Two separate 50,000 accounts (baseline and candidate), never summed. Live orders remain disabled.

Candidate `zone10-5m-1m-v1`: confirmed 5m equal-level pivots within20 points, four-bar separation,15-point pullback, midpoint±10 zone. Completed1m sweep/reclaim followed by a later completed1m breakout confirmation. Initial stop2 points outside rejection; underlying risk10–55. Fixed2-strike OTM, max3 whole lots (65 units/lot). Estimated stop-risk cap min(1,000,2% cash); full-premium and gross-daily-loss-plus-reservation caps5,000. One position. No entries from15:00; square-off15:10.

Exit currently:20% giveback from observed peak NET liquidation profit, armed at1R monetary entry risk; initial stop retained; no fixed3R profit target. Fresh held-contract bid minus one tick and actual fee policy; threshold is a trigger, never a guaranteed fill. Quote-only monitor runs about1second; main evidence snapshots about11seconds. Missing quotes retain exposure/exit intent. Earlier user explicitly chose this trail; later requested original thesis-based winner management. Research the alternative and make promotion explicit.

Mac supervision is installed: forward runner, awake guard and watchdog. AC, lid open, login and network are still necessary. Never force-restart with OPEN/EXIT_PENDING or unknown holdings. Host supervision cannot run while the Mac sleeps. Do not restart the running service merely because code was committed.

## What was verified about the original

The repository already includes `backend/app/trading/crew/holding_agent.py`, `exit_agent.py`, and `manager.py`. Do not duplicate these blindly. The later archived manager calls HoldingAgent before normal ExitAgent processing. The older frozen commit (`efde368`) used in the27-trade audit differs and does NOT establish all later HoldingAgent behaviors. Distinguish both versions.

Original documented tiers: ATM2lots for scalps/countertrend;1-OTM3lots standard;2-OTM4lots supertrend. Later archived resolver actually chose2-OTM for all setups, preferred3/4lots,2 for wide stops, default7.5% risk (3,750 on50,000) and28,000 margin/premium budget. Thus documentation and execution differed. The old delta0.5 accounting showed initial exposure3,402.75–7,078.50 in the27 saved trades;3,000 was not a hard uniform cap. Do not restore its forced-minimum-one-lot affordability bug.

Original ExitAgent: initial/ratcheted stops; target booking and one liquidity hop when at least2lots; structure exit from1.5R using completed5m close beyond prior5m low/high; opposing signal; stagnation;15:10 exit. Manager milestones: breakeven around0.7R and lock0.8R around2R. HoldingAgent adds profitable gamma-wall rejection, opposing structural-level rejection, order-flow absorption,15m invalidation, VWAP failure/traps and healthy-retest/consolidation HOLD states. These are mostly5m/15m, not proof of an original1m-only exit. Some old code can widen a protected stop; a new adapter must not widen risk.

Historical order flow/gamma/OI and true volume VWAP are not available in the saved spot candles. Index volume is often zero; a typical-price fallback is NOT verified volume VWAP. Keep unavailable factors explicitly unavailable/disabled.

## Today's facts and important counterexamples

Actual candidate paper: only13:52 CALL,25550CE,3lots, closed net -1,082.46. Earlier signals12:38 PUT,13:08 PUT,13:14 CALL were discovered stale after sleep. Current zone detector's first zone was known only10:40, so it could not detect the opening reversal even if awake.

9:37 candle local-low sweep and reclaim closed25,304.00; action time is9:38. Prior local low25,300.85; swept low25,292.85; stop25,290.85. Never stamp a9:37 candle-close entry before9:38. This is a LOCAL sweep, not a sweep of the opening/session low. 1m confirmation and5m context are separate contracts.

In the immediate-reversal-only MODEL,9:38 one-lot ITM CALL exited9:43 for+1,031.17. Peak+1,546.71 at9:41;1R718.23;80% trigger1,237.37; next observed fill net1,031.17. Profit trail, not original stop, closed it. Later PUT losses made that whole-day variant -616.86. In broader morning variants,9:34 PUT remained open until9:41 and blocked9:38 CALL. A9:30 start filter alone does not solve that conflict.

A1:38 CALL continuation broke confirmed local high25,398.50. A1:52 CALL was the actual candidate entry. Near1:38 the MODEL quoted25500CE about0.95/1.00 while a recorded broker quote was8.70/8.75. Model-based rejection and P&L can therefore be materially wrong.

With higher RESEARCH budgets2,500/3,000, ATM/ITM cap2 and OTM cap4, independent recorded-book shadows:
-13:38:ITM1lot +2,523.64;ATM1lot +2,198.10;OTM4lots +1,569.13.
-13:52:ITM1lot +951.23;ATM2lots +1,484.93;OTM4lots -1,427.24.
These overlap and start with independent flat capital; never sum as one account. They use ~11second snapshots, not the full live tick path. Those budgets/caps are NOT active.

## Principal results and caveats

See copied reports. Historical primary period15Oct–16Nov has21 sessions, with16Nov partial but beyond squareoff. Today is separate/exploratory:16Nov VIX is missing, so models carry13Nov VIX. Four past expiry days are19Oct,27Oct,3Nov,9Nov;17Nov is fifth.

Morning-window study (all using same2,500 research ceiling and exits), historical net/drawdown:
- Original expanded all-day:12,278.72 /5,489.42.
- Existing confirmation09:30–11:00:8,633.48 /3,749.64.
- Immediate reclaim+continuations09:30–10:30:6,910.70 /5,207.00.
- Immediate reclaim+continuations09:30–11:00:8,770.66 /5,207.00.
- Immediate reversals only09:30–11:00:-2,850.56 /4,627.53.
- Morning09:30–11:00 +original afternoon12:00–15:00:13,730.87 /5,772.38.
Five causal/window tests and564 modeled-fill reconciliations passed. More signals or earlier entry did not establish an improved policy by themselves.

Old27 saved result87,637.22 reconciles under an index-points×0.5delta formula, not verified option P&L. Exact old working tree/quotes are not preserved; audited engine had stale/forming-data issues. Do not present it as a trustworthy performance target. A separate fixed3R model on current-zone entries produced14,679.73 through16Nov, while the selected20%-after1R model produced1,499.52. Different exits/risks/data scopes must always be labeled.

The recently reported studies did NOT fully restore the later HoldingAgent. The next work is the matched thesis-management study, not a declaration that original management is already active.
