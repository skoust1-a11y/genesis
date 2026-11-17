# Prompt 02 — Implement safe winner/thesis management

Implement a pure, versioned research management policy and a separately identified account adapter. The goal is to let valid trends run while managing losses and invalidation. No silent promotion into the current candidate.

Keep an unchanged20%-after1R control. Test a thesis-managed variant without a fixed percentage-of-profit exit and without an automatic fixed3R full exit. Retain initial risk and EOD squareoff. Evaluate additional5m-structure and1m-structure variants separately: original verified structure is5m;1m is a new requested hypothesis. Do not rename1m research as historical original behavior.

Use the audit matrix to adapt healthy HOLD/retst/consolidation states, actual invalidation/exhaustion exits, and progressive stop protection. A stop can only tighten; never widen the initial or already protected loss. Preserve separate stop/exit-intent state. Fresh quotes for the EXACT held contract determine fills; index levels and monetary thresholds are triggers only. Missing/stale held quotes retain exposure and retry intent. Risk-reducing exits must work even if news becomes unavailable.

Risk/exit priority must be explicit and tested: committed exit intent, protective stop and EOD must not be cancelled by a healthy HOLD or new entry. Timestamp all reference bars/levels; use only completed bars. Opposing-signal exits require fresh, directionally unambiguous confirmed signals. No candle or level confirmed later may justify an earlier action.

Historical gamma-wall/order-flow branches stay unavailable unless backed by contemporaneous evidence. Do not synthesize them from later prices. A true VWAP-dependent rule must fail unavailable if valid volume is absent; an explicitly named price-only alternative can be a separate hypothesis.

If partial booking/liquidity hopping is retained, use whole lots, book every fee and reserve extra exit costs. One-lot trades cannot be split. Clarify whether testing no-fixed-target runners versus the original one-hop target-managed variant; do not mix them under one label. No extra orders that invalidate the full-premium/daily reservation guarantees.

Meaningful tests: pullback HOLD versus genuine invalidation; monotonic stops; future/forming bars; missing factors; exact contract identity; repeated/restarted decisions; partial-lot constraints and fees; stop/EOD priority; pending/unpriced exits; fresh opposite-signal behavior. Add a regression for the9:38/9:43 scenario without hardcoding today's winner into generic rules. Report actual causal exit time and net P&L; do not force a10:30 exit to imitate the chart.
