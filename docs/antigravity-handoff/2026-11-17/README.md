# Antigravity handoff — Genesis, 17 November 2026

This folder is inside the Genesis repository. Work in this checkout and current branch. The owner will push when ready; do not push or change branches on their behalf.

## Start here

Paste `START-HERE.md` into Antigravity. It directs the agent through the numbered prompts. Read `CURRENT-STATE.md` before implementing anything. Use the numbered prompts in order; finish implementation, tests and reviewable evidence rather than only writing a plan.

1. `01-audit-original-management.md` — establish exact original behaviors and data dependencies.
2. `02-implement-thesis-management.md` — restore useful holding/exit behavior in a versioned research policy.
3. `03-morning-entry-and-reentry.md` — test completed 1m entries and context filters after 09:30.
4. `04-strike-selection-and-sizing.md` — risk-aware ATM/ITM/OTM selection and time hypotheses.
5. `05-matched-backtest-and-report.md` — matched account comparison with independent reconciliation.
6. `06-paper-rollout-and-operations.md` — staged paper-only integration without corrupting the existing run.

`reports/` contains portable research conclusions; `research-summary.json` contains compact account statistics. `research-snapshots/` preserves exploratory source files otherwise excluded by the logs ignore rule. These snapshots retain original relative-path assumptions: read and normalize paths before using them from this folder. `reference/` points to the original code and documents the archived lot tiers.

Full historical candles, broker quotes, fee receipts and SQLite journals remain local under ignored `logs/`. They are deliberately not bundled with credentials or raw runtime state. A fresh clone cannot reproduce a market-data replay without those inputs. Missing evidence must be reported, never fabricated.

Everything in this folder is a handoff, not proof of profitable future execution. Current active trading settings have not been promoted to the experimental policies.
