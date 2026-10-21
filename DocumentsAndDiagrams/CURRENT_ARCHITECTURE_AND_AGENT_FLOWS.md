# Genesis: Current System Architecture & Agent Flows Specification

*Source of truth: Active repository code (`backend/app/`, `frontend/src/`, `backend/deploy/`, `backend/scripts/`)*  
*Last updated: 2026-10-22*

---

## 1. System Overview & Core Agents Matrix

The Genesis platform has evolved from a basic backtest-and-review engine into a multi-agent quantitative trading ecosystem comprising:

| Component / Agent | Implementation File(s) | Architecture / Tech | Role & Execution Context |
| :--- | :--- | :--- | :--- |
| **Trade Crew** | `backend/app/agents/trade_crew/graph.py` | LangGraph StateGraph | Live & paper trading decision loop. Runs as an isolated worker subprocess per active deployment. Rules in code decide; LLM only has optional shadow/veto authority. |
| **Review Crew** | `backend/app/agents/review_crew/graph.py` | LangGraph StateGraph | Deterministic multi-scenario backtesting, stress testing, capital hard-stop sweep, per-trade post-mortem review, and improvement proposal generation. |
| **Loss Analyzer Agent** | `backend/app/loss_analyzer.py` | Deterministic Autopsy + LLM Expert | Automated forensic analysis for losing trades (VWAP loss, false breakout traps, noon chop, volatility spikes). Triggers LLM expert interventions on 3-loss streaks. |
| **Strategy Analyzer Agent**| `backend/app/strategies/analyzer.py` | Multi-Pillar Institutional Auditor | 5-pillar institutional strategy audit (Edge, Risk-Reward, Feasibility, Capital Floor, Execution Risk) and catalog evaluation for `/strategies`. |
| **Liquidity Signals Daemon** | `backend/app/liquidity_signals.py` | Background Daemon (`launchd`/`systemd`) | Real-time 5m/15m NIFTY liquidity sweep detection (PDH/PDL raid, 15m MSS, VWAP reclaim, lunch-lull filter, ATM option strike picker, and alert delivery). |
| **Equal Signals Daemon** | `backend/app/equal_signals.py` | Background Daemon (`launchd`/`systemd`) | Real-time detection of Equal Highs (EQH) and Equal Lows (EQL) liquidity pool sweeps. |
| **Market Structure Engine**| `backend/app/market/chart_analytics.py` | Non-repainting Analytics Engine | Mathematically verified swing points (`HH`, `HL`, `LH`, `LL`, `EQH`, `EQL`), Market Structure Break (`BOS`), Change of Character (`CHoCH`), Dynamic Trendlines, and FVG zones. |
| **Supervisor** | `backend/app/live/supervisor.py` | Process Controller | Supervises all active Trade Crew deployments, restarts crashes with exponential backoff (5s to 5m), monitors capital floor pauses. |
| **FastAPI Backend** | `backend/app/main.py`, `backend/app/api/` | FastAPI / Pydantic v2 | Port 8841. Exposes endpoints for Ingest, Review, Strategies & Analyzer, Market Analytics, Paper Trading, and Live Control Plane. |
| **Next.js Frontend** | `frontend/src/` | Next.js 15, React 19, Tailwind CSS | Port 8842 (or 3000). Features `/`, `/markets`, `/strategies`, `/strategy-lab`, `/lab`, `/deploy`, `/deployments`. |

---

## 2. Agent Flow Diagrams

### A. Trade Crew Agent Tick Flow (`backend/app/agents/trade_crew/graph.py`)

Each tick executes the state graph sequentially. The tick stops early if any guard or gate triggers.

```mermaid
flowchart TD
    subgraph TradeCrew["Trade Crew Tick (LangGraph StateGraph)"]
        direction TB
        start([Start Tick]) --> verify[1. verify]
        
        verify -->|Snapshot hash mismatch| stop_halt1[Halt: Snapshot Tampered]
        verify -->|Snapshot Valid| gate[2. gate]
        
        gate -->|Deployment paused or stopped| stop_idle[Idle / Exit Tick]
        gate -->|Deployment active| hours[3. hours]
        
        hours -->|Outside NSE trading hours| stop_closed[Market Closed]
        hours -->|NSE session open| observe[4. observe]
        
        observe -->|Missing or stale market quotes| stop_stale[No/Stale Quotes]
        observe -->|Quotes Valid| reconcile[5. reconcile]
        
        reconcile -->|Broker pos != Live DB pos| stop_recon[Halt: Desync Error]
        reconcile -->|Positions In Sync| exits[6. exits]
        
        exits -->|Close order fails| stop_exit_err[Halt: Close Order Error]
        exits -->|Exits evaluated & executed| drawdown[7. drawdown]
        
        drawdown -->|Drawdown ladder or floor breached| stop_pause[Pause: Floor Breached]
        drawdown -->|Drawdown within limits| daily[8. daily]
        
        daily -->|Profit target hit / close_all fail| stop_daily[Daily Target / Halt]
        daily -->|Under daily targets| entry[9. entry]
        
        entry -->|No signal / already holding| stop_nosig[Idle: No Entry Signal]
        entry -->|Signal Triggered| risk[10. risk]
        
        risk -->|Sizing, R:R, Max Lots, Odds Gates| opinion[11. opinion]
        
        opinion -->|LLM Shadow / Veto Evaluation| explain[12. explain]
        
        explain -->|Append explanatory note to reason_text| execute[13. execute]
        
        execute -->|Place Hedges First -> Protective Stops| finish([End Tick])
    end

    classDef stopClass fill:#fee2e2,stroke:#ef4444,stroke-width:1.5px,color:#991b1b;
    classDef stepClass fill:#f0fdf4,stroke:#22c55e,stroke-width:1.5px,color:#14532d;
    class stop_halt1,stop_idle,stop_closed,stop_stale,stop_recon,stop_exit_err,stop_pause,stop_daily,stop_nosig stopClass;
    class verify,gate,hours,observe,reconcile,exits,drawdown,daily,entry,risk,opinion,explain,execute stepClass;
```

---

### B. Review Crew Agent Flow (`backend/app/agents/review_crew/graph.py`)

Invoked via `POST /api/review/run` inside the Strategy Lab.

```mermaid
flowchart TD
    subgraph ReviewCrew["Review Crew Engine (LangGraph StateGraph)"]
        direction TB
        rc_start([Start Review]) --> audit[1. audit]
        audit -->|Validates rules structure & syntax| scenarios[2. scenarios]
        scenarios -->|Generates baseline & stress-test parameter combos| simulate[3. simulate]
        simulate -->|Backtests on NSE EOD bars with Indian cost schedules| hard_stop[4. hard_stop]
        hard_stop -->|Sweeps 2%, 3%, 4%, 5% capital stop levels| trade_review[5. trade_review]
        trade_review -->|Deterministic post-mortem review per simulated trade| verdict[6. verdict]
        verdict -->|Profitable / Conditional / Too few trades / Not profitable| improve[7. improve]
        improve -->|Proposes evidence-backed improvements + LLM rationale| rc_end([Save Report to Research DB])
    end

    classDef reviewClass fill:#eff6ff,stroke:#3b82f6,stroke-width:1.5px,color:#1e3a8a;
    class audit,scenarios,simulate,hard_stop,trade_review,verdict,improve reviewClass;
```

---

### C. Loss Forensic Autopsy & Consecutive Loss Expert Agent (`backend/app/loss_analyzer.py`)

Runs whenever a trade incurs a loss, and escalates to an LLM Senior Risk Partner on consecutive losing streaks.

```mermaid
flowchart TD
    subgraph LossAutopsy["Loss Autopsy & Expert Agent"]
        direction TB
        loss_in([Trade Loss Recorded]) --> vwap_chk[Check VWAP Loss Time]
        vwap_chk --> trap_chk[Check False Breakout / Trap]
        trap_chk --> lunch_chk[Check Noon Chop 11:30-13:00]
        lunch_chk --> vix_chk[Check Volatility Spike / Stagnation]
        vix_chk --> diag[Generate Deterministic Forensic Diagnosis]
        
        diag --> streak_chk{Loss Streak >= 3?}
        streak_chk -->|No| send_single[Dispatch Single Loss Alert to Telegram/Email]
        streak_chk -->|Yes: 3 Consecutive Losses| llm_expert[Invoke LLM Senior Trading Coach]
        llm_expert --> format_expert[Synthesize Root Cause, Regime Shift & Action Plan]
        format_expert --> send_expert[Dispatch HIGH PRIORITY Alert + Pause Warning]
    end

    classDef alertClass fill:#fef3c7,stroke:#f59e0b,stroke-width:1.5px,color:#92400e;
    classDef llmClass fill:#fdf4ff,stroke:#d946ef,stroke-width:1.5px,color:#701a75;
    class vwap_chk,trap_chk,lunch_chk,vix_chk,diag alertClass;
    class llm_expert,format_expert,send_expert llmClass;
```

---

### D. Real-Time Liquidity Sweep Daemon Flow (`backend/app/liquidity_signals.py`)

Runs every 5 minutes during the market session via macOS `launchd` / `systemd`.

```mermaid
flowchart TD
    subgraph LiquidityDaemon["Liquidity Signals Loop (Daemon)"]
        direction TB
        tick_start([5-Minute Bar Close]) --> fetch_bar[Fetch NIFTY 5m Bar & NIFTYBEES VWAP]
        fetch_bar --> time_gate{09:30 <= Time <= 14:30?}
        time_gate -->|No| sleep_out[Sleep until next 5m]
        time_gate -->|Yes| lunch_gate{Inside Lunch Lull 11:30-13:00?}
        lunch_gate -->|Yes & skip_lunch| sleep_out
        lunch_gate -->|No| vix_gate{11.5 <= INDIA VIX <= 22.0?}
        vix_gate -->|No| sleep_out
        vix_gate -->|Yes| sweep_chk{Sweep of PDH or PDL?}
        
        sweep_chk -->|PDL Sweep & Bullish Reclaim| long_chk[15m MSS + Close > VWAP + Close > Day Open]
        sweep_chk -->|PDH Sweep & Bearish Reclaim| short_chk[5m MSS + Close < VWAP + Close < Day Open]
        sweep_chk -->|No Sweep| sleep_out
        
        long_chk & short_chk --> risk_filt{Risk >= 30 pts & Daily Limit OK?}
        risk_filt -->|No| sleep_out
        risk_filt -->|Yes| strike_sel[Select ATM Option Strike & Calculate 3R Target]
        strike_sel --> gen_chart[Generate Technical Chart Visualization]
        gen_chart --> dispatch[Dispatch Signal + Chart to Telegram & SMTP]
    end

    classDef daemonClass fill:#ecfdf5,stroke:#10b981,stroke-width:1.5px,color:#065f46;
    class fetch_bar,time_gate,lunch_gate,vix_gate,sweep_chk,long_chk,short_chk,risk_filt,strike_sel,gen_chart,dispatch daemonClass;
```

---

## 3. High-Level System Architecture Diagram

```mermaid
flowchart TB
    subgraph Frontend["Frontend Client (Next.js 15 / React 19) - Port 8842"]
        UI_Dash["/ (Dashboard & Paper PnL)"]
        UI_Mkt["/markets (CandleChart, FVG, Trendlines, Liquidity)"]
        UI_Strat["/strategies (Strategies Catalog & Analyzer)"]
        UI_LabPlay["/strategy-lab (Strategy Builder & Research)"]
        UI_Lab["/lab (Ingest, Clarify, Review, Journal)"]
        UI_Deploy["/deploy & /deployments (Approvals & Ops)"]
    end

    subgraph BackendAPI["FastAPI Process - Port 8841"]
        MW["Middleware (CORS, AuditContext, Bearer Auth)"]
        API_Mkt["/api/market & /api/paper (Quotes, Candles, Analytics, Orders)"]
        API_Strat["/api/strategies/catalog & /api/strategies/analyze"]
        API_Lab["/api/ingest, /api/review/run, /api/demo/run"]
        API_Live["/api/live (Approvals, Deployments, Kill Switch)"]
        API_Audit["/api/audit & /api/live/audit (Cryptographic Hash Chains)"]
    end

    subgraph BackgroundDaemons["Background Daemons & Workers"]
        Sup["Supervisor (python -m app.live supervise)"]
        TC_Subproc["Trade Crew Subprocess (1 per active deployment)"]
        Liq_Daemon["Liquidity Signals Daemon (com.genesis.liquidity-signals)"]
        Eq_Daemon["Equal Signals Daemon (com.genesis.equal-signals)"]
        Loss_Agent["Loss Analyzer & Autopsy Engine"]
        
        Sup -->|manages lifecycle & backoff| TC_Subproc
        TC_Subproc -->|on trade loss| Loss_Agent
        Liq_Daemon -->|on trade loss| Loss_Agent
    end

    subgraph Storage["Tri-Database Storage Tier"]
        DB_Res[("Research Database<br/>(Alembic: 0006_paper_orders)<br/>strategies, versions, reports, sim_trades,<br/>trade_reviews, paper_orders, audit_events")]
        DB_Live[("Live Database<br/>(Alembic: 0005_broker_credentials)<br/>deployed_strategies, deployments, orders, fills,<br/>live_trades, credentials (Fernet), live_audit_events")]
        DB_Mkt[("Market Database<br/>(Unaudited Reference Store)<br/>index_bars, option_eod, market_events, news_items")]
    end

    subgraph ExternalIntegrations["External APIs & Services"]
        Ext_Yahoo["Yahoo Finance (Delayed Quotes & Historical Candles)"]
        Ext_LLM["LLM Provider (OpenAI / Anthropic via LangChain)"]
        Ext_Brokers["Broker Adapters (Paper & Sandbox Registries)"]
        Ext_Sources["Source Sites (YouTube, Instagram, Web Documents)"]
        Ext_Alerts["Notifications (Telegram Bot & SMTP Email)"]
    end

    %% UI to Backend
    Frontend -->|HTTP REST / Bearer Token / X-Actor| MW
    MW --> API_Mkt & API_Strat & API_Lab & API_Live & API_Audit

    %% API to Database
    API_Mkt --> DB_Res
    API_Mkt --> Ext_Yahoo
    API_Strat --> DB_Res
    API_Strat --> Ext_LLM
    API_Lab --> DB_Res
    API_Lab --> DB_Mkt
    API_Lab --> Ext_LLM
    API_Lab --> Ext_Sources
    API_Live --> DB_Live
    API_Live --> DB_Res

    %% Workers to DB & Externals
    TC_Subproc --> DB_Live
    TC_Subproc --> DB_Mkt
    TC_Subproc --> Ext_Brokers
    TC_Subproc --> Ext_LLM
    Liq_Daemon --> DB_Mkt
    Liq_Daemon --> Ext_Yahoo
    Liq_Daemon --> Ext_Alerts
    Eq_Daemon --> Ext_Yahoo
    Eq_Daemon --> Ext_Alerts
    Loss_Agent --> Ext_LLM
    Loss_Agent --> Ext_Alerts
```

---

## 4. Market Structure & Chart Analytics Architecture

The engine in `backend/app/market/chart_analytics.py` powers the interactive chart in `/markets`:

```mermaid
flowchart LR
    Bars[Raw 5m / 15m OHLCV Bars] --> SwingDetect[Swing High / Low Detection (2L/2R)]
    SwingDetect --> Classify[Classification: HH, HL, LH, LL, EQH, EQL]
    
    Classify --> BOS[BOS & CHoCH Event Detection]
    Classify --> Trendlines[Ascending Support & Descending Resistance Lines]
    Bars --> FVG[Fair Value Gap (FVG) Detection]
    Bars --> OB[Order Block Identification]
    Bars --> VWAP[Robust Session VWAP (ETF Proxy)]
    
    BOS & Trendlines & FVG & OB & VWAP --> Aggregator[enrich_chart_payload]
    Aggregator --> APIOut[FastAPI /api/market/candles Response]
    APIOut --> FrontendChart[CandleChart.tsx + TradingDecisionDashboard.tsx]
```

---

## 5. Security, Auditability, and Invariants

1. **Deterministic Execution**:
   - Trading decisions (entries, sizing, exits, trailing stops) are computed strictly in deterministic Python code.
   - The LLM cannot place, modify, or close trades. It has optional *Shadow* (observation) or *Veto* (skip/reduce approved entry) capabilities only.
2. **Cryptographic Provenance**:
   - When a strategy is approved for deployment, its exact `content_sha256` and `rules_sha256` are frozen into `deployed_strategies`.
   - The Trade Crew verifies these hashes on every single tick; any tampering or discrepancy immediately halts trading.
3. **Dual Append-Only Hash Chains**:
   - Every mutation in the Research DB writes to `audit_events`.
   - Every mutation in the Live DB writes to `live_audit_events`.
   - Each audit event includes `hash = sha256(prev_hash + canonical_json_payload)`, allowing mathematical proof of tamper resistance via `GET /api/audit/verify` and `GET /api/live/audit/verify`.
