# Prompt 01 — Audit the complete management path

Read CURRENT-STATE.md and the source-hash reference. Trace `crew/manager.py`, `holding_agent.py`, `exit_agent.py`, risk/accounting functions and the live candidate path (`forward_candidate_account.py`). Identify every condition that HOLDs, changes a stop, partially books, fully exits, blocks re-entry or proposes reversal. Do not limit the audit to ExitAgent.

Create an implementation matrix: actual condition and threshold; candle timeframe; earliest knowable timestamp; required inputs and provenance; action; priority; original call-site actually wired or not; current candidate equivalent; safe proposed adapter behavior. Separate the old frozen efde368 engine, later current/archive implementation and README intentions.

Specifically audit: gamma-wall rejection; order-flow absorption; opposing key-level/EQH/EQL rejection;15m invalidation; VWAP/trap logic; healthy retest cushion; consolidation runway;0.7R/2R milestones;5m structure; liquidity hopping; stagnation and squareoff. Confirm that emitted reversal setups are actually admitted through account gates; do not assume an action-name annotation proves auto-flipping. The RC quick-cut marker is diagnostic in the archive, not automatically a close.

Inputs absent historically must be unavailable, not neutral evidence. Index zero volume must not become invented volume/CVD or true VWAP. Trace possible forming5m/15m usage and stale opposite signals. Identify paths where a retest cushion loosens a locked stop, and whether be_locked defeats the purported cushion. Separate plausible design from actual effective behavior.

Deliver matrix, dependency/data gaps and a versioned protocol for the next phase. Reuse existing modules where appropriate, but preserve active journals and strategy identities. Do not claim full historical restoration when factor inputs are absent.
