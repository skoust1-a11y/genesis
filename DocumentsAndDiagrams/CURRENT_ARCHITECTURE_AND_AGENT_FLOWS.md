# Genesis: System Architecture & Autonomous Agent Flows Specification

*Source of truth: Active repository code (`backend/app/`, `frontend/src/`, `scripts/`, `trades/`)*  
*Last updated: 2026-10-30*

---

## 1. System Overview & Core Autonomous Crews Matrix

Genesis is an institutional quantitative trading, strategy research, and automated execution platform engineered for the Indian equity derivatives markets (NSE NIFTY and BANKNIFTY index options). 

The platform is organized around **5 Autonomous Specialized Crews** operating continuously across the trading day, accompanied by a deterministic **Review Crew**, a research **Strategy Lab**, and an immutable multi-tier persistence tier.

| Crew / Component | Core Manager & Agents | Implementation Files | Execution Window | Role & Responsibilities |
| :--- | :--- | :--- | :--- | :--- |
| **1. PreMarketCrew** | `PreMarketManager`<br/>- `GapAgent`<br/>- `GammaAgent`<br/>- `EventRiskAgent` | `backend/app/trading/pre_market_crew/` | 08:30 – 09:14 IST | Ingests GIFT NIFTY vs PDC gap bias, computes Call/Put dealer gamma walls, calculates Max Pain & PCR, checks macro event blackouts (RBI MPC, CPI, FED), formats `trades/pre_market_plan.json`, and dispatches the 09:05 IST Battleplan to Telegram. |
| **2. ScannerCrew** | `ScannerManager`<br/>- `FeedAgent`<br/>- `LevelsAgent`<br/>- `ConsolidationAgent`<br/>- `NewsAgent`<br/>- `DetectorAgent` | `backend/app/trading/scanner_crew/` | 09:15 – 15:30 IST<br/>(1m, 2m, 5m, 15m) | Multi-timeframe delta polling & ETF VWAP proxy, dynamic level projection (PDH, PDL, PDC, OR-30, round strikes), compression box & ATR deadband filtering (Lunch Lull 11:30-13:00), macro sentiment screening, and pattern detection across Breakout & Retest, Liquidity Sweep, and Trendline Reversal. Emits real-time `ScanSnapshot`. |
| **3. TradeCrew** | `TradeManager`<br/>- `TrendAgent`<br/>- `RiskAgent`<br/>- `OrderFlowAgent`<br/>- `EntryAgent`<br/>- `ExitAgent`<br/>- `AlertAgent`<br/>- `JournalAgent` | `backend/app/trading/crew/` | Live Market Hours (Continuous) | Evaluates macro trend slopes and dynamic R:R targets (+1.8R to +4.5R), enforces daily loss circuit breakers and the 50% Capital Safety Wall, checks whale volume absorption traps, triggers micro-wick entries with strictly OTM option strikes, manages Target 1/2/3 partial book & trail, dispatches Telegram tickets, and commits ACID transactions to the `trades.db` SQLite SSOT. |
| **4. ExecutionCrew** | `ExecutionManager`<br/>- `SmartRoutingAgent`<br/>- `BrokerBridgeAgent`<br/>- `ReconciliationAgent` | `backend/app/trading/execution_crew/` | Order Dispatch & Fills | Splits orders into 30% market fill + 70% limit-chasing tranches to minimize slippage, bridges to live broker APIs or zero-slippage paper emulator, and performs real-time zero-drift position reconciliation between broker account and local database. |
| **5. PostMarketCrew** | `PostMarketManager`<br/>- `AutopsyAgent`<br/>- `ParameterAgent`<br/>- `SafetyWallAgent`<br/>- `CodeQualityAgent`<br/>- `BriefingAgent` | `backend/app/trading/post_market_crew/` | 15:31+ IST Daily | Reconstructs the day's intraday tape, audits execution against hypothetical perfection, flags missed trades, runs walk-forward parameter sweeps, locks 50% of net profits into the permanent Capital Safety Wall, checks system logs & latency bottlenecks, writes `reports/daily_audit_YYYY-MM-DD.md`, and dispatches the Executive Digest to Telegram. |
| **Review Crew** | LangGraph StateGraph | `backend/app/agents/review_crew/` | On-Demand (Lab) | 7-step deterministic backtest & forward-test engine with Indian cost models (STT, exchange fees, GST, SEBI, stamp duty, slippage), hard-stop capital sweeps (2-5%), trade-by-trade post-mortems, and evidence-backed improvement proposals. |
| **Strategy Analyzer** | `InstitutionalStrategyAnalyzer` | `backend/app/strategies/analyzer.py` | On-Demand (API) | Evaluates strategies against the 5 Institutional Pillars (Edge, Risk-Reward, Feasibility, Capital Floor, Execution Risk) and generates catalog scorecards. |
| **Loss Forensic Agent**| `LossAnalyzer` | `backend/app/loss_analyzer.py` | Event-Triggered | Automated autopsy on any losing trade; escalates to an LLM Senior Trading Partner upon encountering 3 consecutive losses. |
| **Market Analytics** | `chart_analytics.py` | `backend/app/market/chart_analytics.py` | Real-Time / API | Computes non-repainting swing pivots (HH, HL, LH, LL, EQH, EQL), Market Structure Break (`BOS`), Change of Character (`CHoCH`), Fair Value Gaps (`FVG`), and Dynamic Trendlines. |
| **Supervisor Daemon**| `Supervisor` | `backend/app/live/supervisor.py` | Background Daemon | Manages worker subprocesses for active deployments with exponential backoff crash recovery (5s to 5m) and capital floor monitoring. |

---

## 2. End-to-End Daily Operational Lifecycle

```mermaid
flowchart TD
    subgraph PreSession["Phase 1: Pre-Market Preparation (08:30 - 09:14 IST)"]
        direction TB
        P0[08:30 IST: PreMarketManager Wakes] --> P1[GapAgent: Ingest GIFT NIFTY & PDC]
        P1 --> P2[GammaAgent: Option Chain Call/Put Walls & Max Pain]
        P2 --> P3[EventRiskAgent: Calendar & High-Impact Catalysts]
        P3 --> P4[Compile trades/pre_market_plan.json]
        P4 --> P5[09:05 IST: Dispatch Morning Battleplan to Telegram]
    end

    subgraph LiveSession["Phase 2: Live Scanning & Execution (09:15 - 15:30 IST)"]
        direction TB
        L0[09:15 IST: Live Scanner Daemon Active] --> L1[FeedAgent: Multi-Timeframe Delta Polling 1m, 5m, 15m]
        L1 --> L2[LevelsAgent: Map PDH, PDL, OR-30 & Psychological Strikes]
        L2 --> L3[ConsolidationAgent & NewsAgent: Lunch Lull & Event Blackout Filter]
        L3 --> L4[DetectorAgent: Multi-Strategy Pattern Recognition]
        
        L4 -->|CandidateSetup| L5{TradeManager Evaluation}
        L5 --> L6[TrendAgent: Macro Regime & Dynamic R:R]
        L6 --> L7[RiskAgent: 3-Tier Sizing & Safety Wall Check]
        L7 --> L8[OrderFlowAgent: Whale Volume Absorption Trap Veto]
        
        L8 -->|Approved Decision| L9[ExecutionCrew: Smart Routing Tranches & Broker Bridge]
        L9 --> L10[AlertAgent: Dispatch Telegram Ticket & CMP Threads]
        L10 --> L11[JournalAgent: ACID Commit to trades.db SQLite WAL & Excel Sync]
        
        L11 --> L12[ExitAgent: Target 1 / 2 / 3 Execution & 5M Trail]
    end

    subgraph PostSession["Phase 3: Post-Market Forensic Audit (15:31+ IST)"]
        direction TB
        A0[15:31 IST: PostMarketManager Wakes] --> A1[AutopsyAgent: Tape Reconstruction & Exit Audit]
        A1 --> A2[ParameterAgent: Walk-Forward R:R Optimization]
        A2 --> A3[SafetyWallAgent: Lock 50% Realized Profit into Capital Reserve]
        A3 --> A4[CodeQualityAgent: Latency & Memory Audit]
        A4 --> A5[BriefingAgent: Generate reports/daily_audit_*.md]
        A5 --> A6[Dispatch Daily Executive Digest to Telegram]
        A6 --> A7[Persist daily_risk_config.json for Tomorrow]
    end

    PreSession --> LiveSession
    LiveSession --> PostSession

    classDef pre fill:#eff6ff,stroke:#3b82f6,stroke-width:1.5px,color:#1e3a8a;
    classDef live fill:#f0fdf4,stroke:#22c55e,stroke-width:1.5px,color:#14532d;
    classDef post fill:#fff1f2,stroke:#f43f5e,stroke-width:1.5px,color:#9f1239;
    class P0,P1,P2,P3,P4,P5 pre;
    class L0,L1,L2,L3,L4,L5,L6,L7,L8,L9,L10,L11,L12 live;
    class A0,A1,A2,A3,A4,A5,A6,A7 post;
```

---

## 3. Detailed Agent Flow Diagrams

### A. Live Scanner & Multi-Timeframe Pattern Detection Loop

The daemon (`scripts/live_scanner.py`) executes continuously across configured timeframes (default: `1m, 5m, 15m`).

```mermaid
flowchart TD
    subgraph ScannerLoop["ScannerCrew Multi-Timeframe Loop"]
        direction TB
        tick_start([Market Tick Arrival]) --> feed[FeedAgent: Fetch 1m/5m/15m OHLCV & VWAP]
        feed --> hours_check{09:15 <= Time <= 15:30?}
        hours_check -->|No| sleep_out[Sleep Interval & Continue]
        
        hours_check -->|Yes| levels[LevelsAgent: Compute PDH, PDL, OR-30, Strikes]
        levels --> struct[chart_analytics: Compute Swings, BOS, CHoCH, FVGs]
        
        struct --> filter_deadband{Consolidation / Lunch Lull?}
        filter_deadband -->|Chop & Deadband| sleep_out
        filter_deadband -->|Clear Trend / Volatility| news_check{News Event Risk Blackout?}
        news_check -->|Catalyst Blackout| sleep_out
        
        news_check -->|Normal Conditions| detector[DetectorAgent: Evaluate Candidate Setups]
        
        detector --> strat_a[Strategy 1: Breakout & Retest OR-30/PD]
        detector --> strat_b[Strategy 2: Trendline 3rd Point Reversal]
        detector --> strat_c[Strategy 3: Liquidity Sweep PDH/PDL Raid]
        
        strat_a & strat_b & strat_c --> setup_found{Setup Triggered?}
        setup_found -->|No| sleep_out
        setup_found -->|Yes| emit_snap[Emit ScanSnapshot to TradeManager]
    end

    classDef loopClass fill:#f0fdf4,stroke:#16a34a,stroke-width:1.4px,color:#14532d;
    classDef checkClass fill:#fffbeb,stroke:#d97706,stroke-width:1.4px,color:#92400e;
    class feed,levels,struct,detector,strat_a,strat_b,strat_c,emit_snap loopClass;
    class hours_check,filter_deadband,news_check,setup_found checkClass;
```

---

### B. Trade Crew Decision & Execution Lifecycle

When `TradeManager` receives an actionable candidate setup, it coordinates sizing, validation, and order routing:

```mermaid
flowchart TD
    subgraph TradeCrewDecision["Trade Execution Lifecycle"]
        direction TB
        in_setup([CandidateSetup Received]) --> trend_eval[TrendAgent: Evaluate 15m Trend & Dynamic R:R]
        trend_eval --> order_flow{OrderFlowAgent: Absorption Trap?}
        
        order_flow -->|Whale Volume Climax Opposing Signal| veto_trap[Veto: Institutional Trap Detected]
        order_flow -->|Clear Order Flow| risk_eval[RiskAgent: Check Daily Limits & Circuit Breaker]
        
        risk_eval --> daily_loss{Consecutive Losses >= 3 or Daily Max?}
        daily_loss -->|Limit Exceeded| halt_daily[Halt: Daily Risk Circuit Breaker Active]
        
        daily_loss -->|Within Risk Profile| sizing[Scaling Law: 3-Tier Sizing & Safety Wall Check]
        sizing --> strike_picker[EntryAgent: Strictly OTM Contract Picker]
        
        strike_picker --> exec_route[ExecutionCrew: Smart Routing 30% Mkt / 70% Limit]
        exec_route --> broker[BrokerBridgeAgent: Place Paper or Live Orders]
        
        broker --> recon[ReconciliationAgent: Verify Fill Count & Slippage Saved]
        recon --> alert[AlertAgent: Dispatch Telegram Ticket & Thread CMP]
        alert --> journal[JournalAgent: Commit to trades.db SQLite WAL & Sync Excel]
        
        journal --> exit_mgr[ExitAgent: Monitor Target 1, 2, 3 & 5M Trail]
        exit_mgr -->|Target 1 Hit| book_half[Book 50% & Move SL to Cost]
        exit_mgr -->|Target 2 Hit| trail_swing[Book 25% & Trail 5M Swings]
        exit_mgr -->|Target 3 Hit| full_exit[Unconditional 100% Exit]
        exit_mgr -->|Opposing Sweep Formed| auto_flip[Auto-Flip: Immediate Market Exit]
    end

    classDef execClass fill:#eff6ff,stroke:#2563eb,stroke-width:1.4px,color:#1e3a8a;
    classDef haltClass fill:#fef2f2,stroke:#ef4444,stroke-width:1.4px,color:#991b1b;
    class trend_eval,risk_eval,sizing,strike_picker,exec_route,broker,recon,alert,journal,exit_mgr,book_half,trail_swing,full_exit,auto_flip execClass;
    class veto_trap,halt_daily haltClass;
```

---

### C. Review Crew Research Engine (LangGraph StateGraph)

Invoked via the Strategy Lab (`POST /api/review/run`) to backtest and forward-test strategies with Indian options charges.

```mermaid
flowchart TD
    subgraph ReviewCrewGraph["Review Crew (LangGraph 7-Step Engine)"]
        direction TB
        rc_start([Start Strategy Review]) --> audit[1. audit: Validate Markdown & rules: block syntax]
        audit --> scenarios[2. scenarios: Generate baseline & stress-test parameter combos]
        scenarios --> simulate[3. simulate: Replay on NSE historical EOD data with Indian charges]
        simulate --> hard_stop[4. hard_stop: Sweep 2%, 3%, 4%, 5% capital stop levels]
        hard_stop --> trade_review[5. trade_review: Per-trade forensic post-mortem]
        trade_review --> verdict[6. verdict: Profitable / Conditional / Too few trades / Not profitable]
        verdict --> improve[7. improve: Evidence-backed improvement proposals + LLM lessons]
        improve --> rc_end([Persist Report & Hash in Research DB])
    end

    classDef rcClass fill:#eff6ff,stroke:#3b82f6,stroke-width:1.4px,color:#1e3a8a;
    class audit,scenarios,simulate,hard_stop,trade_review,verdict,improve rcClass;
```

---

## 4. Quantitative Risk & Sizing Architecture

### A. The 3-Tier Execution Framework
Genesis eliminates static lot sizing in favor of a mathematically rigorous 3-Tier sizing model configured in `trades/daily_risk_config.json`:

1. **Tier 1: Standard Momentum (3 Lots 1-OTM)**
   - Contract: Strictly 1 strike Out-of-the-Money (50 to 100 points away from spot).
   - Lot Size: 3 lots (195 index shares).
   - Purpose: Captures high delta expansion during verified momentum breaks while minimizing theta decay.
2. **Tier 2: Super-Trend Expansion (4 Lots 2-OTM)**
   - Contract: 2 strikes Out-of-the-Money.
   - Lot Size: 4 lots (260 index shares).
   - Purpose: Deployed during multi-timeframe aligned institutional trends; delivers convex gamma upside.
3. **Tier 3: Scalp / Counter-Trend Mode (2 Lots ATM)**
   - Contract: At-the-Money (highest delta sensitivity).
   - Lot Size: 2 lots (130 index shares).
   - Purpose: Fast absorption trades with tight 20-30 pt spot invalidation.

### B. Institutional 50% Capital Safety Wall
To prevent give-back of trading profits, Genesis enforces the **Capital Safety Wall**:

$$\text{Safety Wall Reserve} = 0.50 \times \max(0, \text{Cumulative Net Realized Profits})$$
$$\text{Tradeable Risk Capital} = \text{Base Capital} + 0.50 \times \text{Cumulative Net Realized Profits}$$

- 50% of all accumulated profits are permanently locked into the capital floor.
- Drawdowns are calculated strictly against the tradeable surplus. If the floor is touched, trading is automatically halted.

---

## 5. Storage Tier & Single Source of Truth (SSOT)

```mermaid
flowchart TB
    subgraph ApplicationWorkers["Application & Worker Layer"]
        FastAPI["FastAPI Backend (Port 8841)"]
        LiveScanner["Live Scanner Daemon (live_scanner.py)"]
        SupervisorDaemon["Supervisor (supervisor.py)"]
        ExcelMaintainer["maintain_trades_excel.py"]
    end

    subgraph DataStorage["Persistence & Ledger Layer"]
        ResearchDB[("Research DB (genesis.db)<br/>Alembic 0006<br/>strategies, reports, sim_trades,<br/>audit_events hash chain")]
        LiveDB[("Live DB (live.db)<br/>Alembic live 0005<br/>deployments, orders, fills,<br/>live_audit_events hash chain")]
        TradeStore[("TradeStore SSOT (trades.db)<br/>SQLite WAL Mode<br/>live_and_forward_trades, PnL,<br/>active_strategy_params, safety_wall")]
        MarketDB[("Market DB (market.db)<br/>NSE EOD index_bars, option_eod,<br/>historical events, bhavcopies")]
        ExcelFiles[("trades/ Workbooks<br/>live_and_forward_trades.xlsx<br/>todays_live_trades.xlsx<br/>by_strategy/*.xlsx")]
    end

    FastAPI --> ResearchDB
    FastAPI --> LiveDB
    FastAPI --> MarketDB
    LiveScanner --> TradeStore
    LiveScanner --> LiveDB
    SupervisorDaemon --> LiveDB
    LiveScanner --> ExcelFiles
    ExcelMaintainer --> TradeStore
    ExcelMaintainer --> ExcelFiles
```

### Invariants & Non-Repudiation
1. **Zero LLM Trade Authority**: Trading entries and exits are strictly executed by deterministic algorithms in code. The LLM has zero authority to open, modify, or close a trade.
2. **Cryptographic Provenance**: Every approved deployment freezes `content_sha256` and `rules_sha256`. The execution engine validates these hashes on every tick.
3. **Dual Cryptographic Hash Chains**: Every state mutation produces a record in `audit_events` or `live_audit_events` with `hash = sha256(prev_hash + canonical_json_payload)`, verifiable via `GET /api/audit/verify`.
