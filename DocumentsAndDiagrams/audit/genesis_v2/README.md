# Genesis V2 architecture audit

Date: 5 Nov 2026. Read-only. No application code, tests, configuration, databases, or Git history were changed. `backend/genesis.db`, `backend/live.db`, and `trades/trades.db` were not opened. No broker order was sent. No test suite was executed. A source reading is not a green test.

The six Sprint 2 notes under `DocumentsAndDiagrams/phase2_blueprint/sprint2/` are design requirements. They are not evidence that the code does what they say.

## Verdict

**B. Architecture ready after specified corrections.**

The Sprint 2 financial and recency notes agree with each other. They do not agree with `RiskAgent.record_trade_outcome` or `SafetyWallAgent.process_daily_pnl`. Those functions are the implementation target, and they should change in place.

The crew diagram in the audit request is not the implementation target. Genesis has no CrewAI. The live daemon is four Python managers called from `scripts/live_scanner.py`. A second, separate LangGraph trade graph serves API deployments. PDC Zone + 15 EMA V0 is a research script, and that script already differs from the frozen card. Folding V0 into the live sweep stack would change the benchmark.

Do not start a multi-crew rewrite. Implement the Sprint 2 register on the existing managers after the corrections in `09_migration_roadmap.md` and the blockers in `10_architecture_risk_register.md`.

## Reports

| File | Contents |
|---|---|
| [01_current_architecture_audit.md](01_current_architecture_audit.md) | What actually runs, with file and line evidence |
| [02_complete_agent_inventory.md](02_complete_agent_inventory.md) | Every agent class, overlap, and reachability |
| [03_complete_crew_inventory.md](03_complete_crew_inventory.md) | Manager “crews” versus the two LangGraph graphs |
| [04_agent_dependency_graph.md](04_agent_dependency_graph.md) | Who calls whom |
| [05_crew_orchestration_design.md](05_crew_orchestration_design.md) | Session flow, failure, and why not CrewAI |
| [06_financial_integrity_assessment.md](06_financial_integrity_assessment.md) | Loss count and reserve against the Sprint 2 rules |
| [07_recency_intelligence_integration.md](07_recency_intelligence_integration.md) | Shadow design versus code that can be reused |
| [08_genesis_v2_target_architecture.md](08_genesis_v2_target_architecture.md) | Proposed layers classified against source |
| [09_migration_roadmap.md](09_migration_roadmap.md) | Ordered tasks and acceptance |
| [10_architecture_risk_register.md](10_architecture_risk_register.md) | Blockers and unresolved decisions |
| [11_acceptance_and_validation_plan.md](11_acceptance_and_validation_plan.md) | What must pass, and what was not run |

## Scores

Weighted overall **3.5 / 10**. Weights and evidence are in `01_current_architecture_audit.md`. The score describes the running system, not the Sprint 2 design.

## What this audit did not do

Pytest was not run. The daemon was not started. Charge schedules were not checked against the statute. `EntryAgent`, `TrendAgent`, and `HoldingAgent` numeric thresholds were not re-derived bar by bar. Vendor `TradingAgents` was identified and not line-audited. The next daemon tick can still rewrite `trades/daily_risk_config.json`; this audit did not start that process.
