# TradingAgents integration

The upstream reference lives in `vendor/TradingAgents` (TauricResearch/TradingAgents).

This project uses that tree as an architecture reference for LangGraph multi-agent flows. Nothing under `backend/app` imports it. US-equity data vendors from that package are not used. Indian options data comes from imported NSE end-of-day files and from the broker feed.

Runtime crews in this repository:

- **Review Crew** — `backend/app/agents/review_crew/`. Nodes: audit, scenarios, simulate, hard-stop sweep, trade review, verdict, improvements.
- **Trade Crew** — `backend/app/agents/trade_crew/`. The paper and live tick. It is not the TradingAgents Trader agent. Rules in code decide entries and exits. An LLM can only skip or shrink an entry the rules already approved, and only when the veto is explicitly enabled.

Role mapping against the reference package:

| TradingAgents role | In this repository |
| --- | --- |
| Analyst team | Strategy auditor (`audit` node) |
| Bull and bear researchers | Scenario planner (`scenarios` and `simulate`) |
| Trader | Trade Crew, implemented here |
| Risk and portfolio manager | Cost model, risk checks, and the verdict node |
