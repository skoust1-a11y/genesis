# TradingAgents integration

The upstream reference lives in `vendor/TradingAgents` (TauricResearch/TradingAgents).

This project uses TradingAgents as an **architecture reference** for LangGraph multi-agent flows. Runtime code for strategy review is implemented in `backend/app/agents/review_crew/`.

We do **not** use TradingAgents US-equity data vendors for Indian options simulations.

Role mapping:

- Analyst team → Strategy Auditor
- Bull/Bear researchers → Scenario Planner stress variants
- Trader → deferred (paper/live phase)
- Risk + Portfolio Manager → Cost Analyst + Verdict Synthesizer
