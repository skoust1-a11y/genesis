# Genesis: Institutional Trading & Strategy Intelligence Platform

Factual product brief and system specification for the Genesis platform. Component, schema, and dependency details live in the companion documents in this folder. Operational procedures live in [backend/README.md](../backend/README.md) and the root [README.md](../README.md).

**Source of truth:** Application code (`backend/app/`, `frontend/src/`, `scripts/`, `trades/`), Alembic revisions, SQLite WAL schemas, and `.env.example`.

*Analysis & System Specification Date: 2026-10-30.*

---

## 1. Problem Statement & Evolution

A retail options strategy idea typically arrives as a video, a social media post, a web article, or a Markdown note. Transforming that unstructured concept into a mathematically backtested, forward-tested strategy with realistic Indian options charges, reviewing it trade-by-trade, and deploying it into live market conditions has historically been fragmented, manual, and prone to execution slippage.

Genesis resolves this by providing an institutional quantitative pipeline spanning **Strategy Research & Lab**, **Automated Review & Ingestion**, **Real-Time Market Structure Analytics**, and **5 Autonomous Trading Crews** running continuous live operations.

---

## 2. Core Platform Capabilities

1. **Strategy Lab & Multi-Source Ingestion**:
   - Ingests trading strategies from YouTube, Instagram, web documents (HTML), NotebookLM, or Markdown.
   - Extracts structured Markdown with an executable `rules:` block.
   - Detects specification gaps and asks clarifying questions, iteratively re-extracting with author responses.

2. **Deterministic Review Crew**:
   - Executes a 7-step LangGraph StateGraph: syntax audit, parameter scenario building, historical backtesting with Indian options charges (brokerage, STT, exchange fees, GST, SEBI, stamp duty, slippage), hard-stop capital sweeps (2% to 5%), trade-by-trade post-mortems, verdict synthesis, and improvement proposals.

3. **5 Specialized Autonomous Trading Crews**:
   - **PreMarketCrew (08:30 – 09:14 IST)**: Gap analysis, Call/Put gamma walls, Max Pain, PCR, event risk screening, and morning battleplan generation.
   - **ScannerCrew (09:15 – 15:30 IST)**: Real-time multi-timeframe delta polling (1m, 2m, 5m, 15m), VWAP proxy, dynamic level projection, ATR deadband/lunch-lull filtering, and multi-strategy pattern detection.
   - **TradeCrew (Live Market Hours)**: Macro trend slope evaluation, dynamic R:R (+1.8R to +4.5R), whale volume absorption trap veto, 3-Tier sizing, strictly OTM strike selection, Telegram ticket dispatch, and trade journaling.
   - **ExecutionCrew**: Smart routing tranches (30% market / 70% limit chase), broker bridge (Paper and Live), and zero-drift position reconciliation.
   - **PostMarketCrew (15:31+ IST)**: Intraday tape reconstruction, exit audits, walk-forward parameter sweeps, 50% Capital Safety Wall profit locking, code quality audit, and daily executive digest.

4. **Non-Repainting Market Structure Engine**:
   - Mathematical swing high/low detection (2L/2R), Market Structure Break (`BOS`), Change of Character (`CHoCH`), Fair Value Gaps (`FVG`), dynamic trendlines, and session VWAP.

5. **Institutional Risk & Sizing Engine**:
   - **3-Tier Execution Framework**: 3 Lots 1-OTM (standard momentum), 4 Lots 2-OTM (super-trend runner), and 2 Lots ATM (scalp mode).
   - **50% Capital Safety Wall**: Permanently locks 50% of cumulative net realized profits into a protected floor, eliminating the risk of giving back gains.
   - **Daily Circuit Breakers**: Max 3 daily trades, soft pause on 2 consecutive losses, mandatory hard stop on 3 losses.

6. **Single Source of Truth (SSOT) & Excel Sync**:
   - High-throughput SQLite WAL database (`data/trades.db`) provides atomic, concurrent ACID transactions.
   - Automatically synchronizes institutional Excel workbooks under `trades/` (`live_and_forward_trades.xlsx`, `todays_live_trades.xlsx`, and `by_strategy/`).

7. **Cryptographic Provenance & Dual Audit Chains**:
   - Strategies approved for live deployment freeze `content_sha256` and `rules_sha256`, verified on every tick.
   - Dual SHA-256 hash chains (`audit_events` and `live_audit_events`) guarantee mathematical proof of tamper resistance via `GET /api/audit/verify`.

---

## 3. Documents in This Directory

| Document | Role & Description |
| :--- | :--- |
| [CURRENT_ARCHITECTURE_AND_AGENT_FLOWS.md](CURRENT_ARCHITECTURE_AND_AGENT_FLOWS.md) | Exhaustive specification of the 5 Autonomous Crews, operational lifecycles, and Mermaid flowcharts. |
| [CURRENT_SYSTEM_ARCHITECTURE_ANALYSIS.md](CURRENT_SYSTEM_ARCHITECTURE_ANALYSIS.md) | Component-by-component architectural audit, endpoint mapping, and middleware stacks. |
| [CURRENT_DATABASE_ARCHITECTURE.md](CURRENT_DATABASE_ARCHITECTURE.md) | Schema definitions, table columns, indices, and constraints across all databases. |
| [DATABASE_TABLE_REFERENCE.md](DATABASE_TABLE_REFERENCE.md) | Detailed table reference, field descriptions, and read/write access patterns. |
| [INSTITUTIONAL_SCALING_LAW_AND_SAFETY_WALL.md](INSTITUTIONAL_SCALING_LAW_AND_SAFETY_WALL.md) | Mathematical proof and implementation of the 50% Safety Wall and 3-Tier sizing model. |
| [LIBRARIES_AND_DEPENDENCIES.md](LIBRARIES_AND_DEPENDENCIES.md) | Exhaustive dependency analysis of libraries imported across backend and frontend. |
| [FINAL_REPORT.md](FINAL_REPORT.md) | Comprehensive engineering and academic report conforming to NIIT guidelines. |

---

## 4. Visual Architectural Diagrams

All architectural diagrams are maintained as vector SVGs and rendered into high-resolution PNGs in [diagrams/](diagrams/):

| Diagram | Description | Links |
| :--- | :--- | :--- |
| **Full Platform Architecture** | High-level system architecture showing Next.js UI, FastAPI, Daemons, Multi-Tier Storage, and Externals. | [SVG](diagrams/architecture.svg) · [PNG](diagrams/architecture.png) |
| **5 Autonomous Crews Ecosystem** | The 5 specialized crews (PreMarket, Scanner, Trade, Execution, PostMarket) and their 21 agents. | [SVG](diagrams/crew-architecture.svg) · [PNG](diagrams/crew-architecture.png) |
| **Live Scanner & Execution Flow** | Multi-timeframe polling, dynamic levels, pattern detection, sizing, safety wall, and post-market loop. | [SVG](diagrams/live-scanner-flow.svg) · [PNG](diagrams/live-scanner-flow.png) |
| **Review Crew & Trade Crew Workflow** | Workflow comparison between Research Lab Review Crew and live deployment Trade Crew. | [SVG](diagrams/workflow.svg) · [PNG](diagrams/workflow.png) |
| **Multi-Tier Database Architecture** | Schema relationships between Research DB, Live DB, TradeStore SSOT, and Market DB. | [SVG](diagrams/database.svg) · [PNG](diagrams/database.png) |
| **Lab Ingest Flow** | Ingestion, gap clarification, backtest replay, and report generation sequence. | [SVG](diagrams/flow-lab.svg) · [PNG](diagrams/flow-lab.png) |
| **Live Trading Gate Flow** | Policy validation, snapshot hashing, live execution gates, and position reconciliation. | [SVG](diagrams/flow-live.svg) · [PNG](diagrams/flow-live.png) |
| **Paper Trading Flow** | Interactive chart execution, delayed quotes, Indian charge deduction, and paper position tracking. | [SVG](diagrams/flow-paper.svg) · [PNG](diagrams/flow-paper.png) |

To regenerate PNGs, HTML, DOCX, and PDF exports from the Markdown sources, run:
```bash
python3 DocumentsAndDiagrams/export_docs.py
```

---

## 5. Getting Started & Operations

- Complete setup, port bindings, CLI commands, and environment variables are documented in the root [README.md](../README.md).
- Quick service management:
  ```bash
  ./scripts/genesis.sh start    # Starts Backend (8841), Frontend (3000), and Live Scanner Daemon
  ./scripts/genesis.sh status   # Inspect health and running process PIDs
  ./scripts/genesis.sh logs     # Tail unified system logs
  ```
