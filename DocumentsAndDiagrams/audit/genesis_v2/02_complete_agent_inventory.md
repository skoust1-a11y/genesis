# 02 — Complete agent inventory

Scope: classes under `backend/app/trading/**` whose names end in `Agent`, plus the LangGraph nodes that act as agents. There is no CrewAI `Agent`. Vendor `TradingAgents` is listed once as out of the Genesis runtime.

“Tools” means functions the class calls. None of these classes register a tool protocol.

State is a JSON file, a SQLite row, or a dict the caller already holds. There is no shared agent memory object.

## Inventory

| Agent | File | Responsibility | Inputs | Outputs | Tools | State modified | Called by | Calls |
|---|---|---|---|---|---|---|---|---|
| GapAgent | `pre_market_crew/gap_agent.py` `GapAgent` | Classifies an opening gap | `pdc`, optional gift price, `global_sentiment` | `GapAnalysis` | none | none | `PreMarketManager.run_pre_market_setup` L79 | none |
| GammaAgent | `pre_market_crew/gamma_agent.py` `GammaAgent` | OI walls, PCR bin, a rounded “max pain”, trap text | spot, chain rows | `GammaAnalysis`. Missing walls are filled ±250 and `walls_live` stays false. No chain also invents OI. L89–97 | `OrderFlowAgent.extract_option_oi_walls` when rows look like ce/pe | none | `PreMarketManager` L82; `TradeManager.__init__` builds a second instance L132 | `OrderFlowAgent` |
| EventRiskAgent | `pre_market_crew/event_agent.py` | Calendar risk text | date, optional manual event | `EventRiskStatus` | PARTIAL: calendar helper inside the file | none on the risk file | `PreMarketManager` L88 | calendar loader, not re-read line by line |
| FeedAgent | `scanner_crew/feed_agent.py` | Yahoo candles, 3s cache | timeframe | DataFrame | `app.api.market._chart`, `parse_yahoo_chart` L49–53 | in-memory `candle_cache` | `ScannerManager`, daemon L299 | market chart |
| LevelsAgent | `scanner_crew/levels_agent.py` | PDH, PDL, PDC, OR, equal highs/lows | daily and 5m frames, contracts | `SessionLevels` | pandas | none | `ScannerManager` L90; `TradeManager` constructs more instances inside the tick L662 and L821 | none external |
| ConsolidationAgent | `scanner_crew/consolidation_agent.py` | Compression / chop flag | 5m frame, clock | `ConsolidationStatus` | pandas | none | `ScannerManager`; also injected into `DetectorAgent` | none |
| NewsAgent | `scanner_crew/news_agent.py` | Keyword sentiment and `check_event_risk_guard` | clock, headlines | `NewsSentiment` | `app.market.news` PARTIAL | none | `ScannerManager` L96. **Not** constructed by `TradeManager` | news module |
| DetectorAgent | `scanner_crew/detector_agent.py` | Trendline, sweep, equal-high/low candidates | bars, levels, sentiment, clock | `CandidateSetup` list | consolidation + news guards | none | `ScannerManager.produce_scan_snapshot` L99 | those two agents |
| TrendAgent | `crew/trend_agent.py` | Regime, ADX, target R | 5m, price, direction, levels | `MarketRegime` | pandas | none | `TradeManager` L131, used L703 | none |
| OrderFlowAgent | `crew/order_flow_agent.py` | Sizing cap, absorption veto, OI walls | 5m, direction, chain | `OrderFlowSignal` | pandas | none | `RiskAgent.evaluate_order` L249; `GammaAgent`; holding path | none |
| RiskAgent | `crew/risk_agent.py` | Session, loss count, 55-pt cap, lot size, reserve number | risk JSON, entry, stop | `RiskApproval` | `scaling_law` milestones when not frozen | **`trades/daily_risk_config.json`** via `save_config` L103–121 | `TradeManager` L135 and throughout the tick | `OrderFlowAgent` if injected |
| SniperAgent | `crew/sniper_agent.py` | 1m micro entry and stop | 1m/5m/15m | `SniperExecution` | pandas | none | `TradeManager` L143, L718 | none |
| EntryAgent | `crew/entry_agent.py` | Sweep and waterfall signals | frames, PDH/PDL, walls | `TradeSignal` | trend + risk if used inside the methods | none itself; signal later journaled | `TradeManager` L144, L827, L838 | `RiskAgent` on its own fallback path PARTIAL |
| HoldingAgent | `crew/holding_agent.py` | Preemptive exit, profit, flip, cushion stop | open trade dict, frames, walls, VIX | `HoldingDecision`. Can rewrite `stop_loss` on the dict | `compute_session_vwap` | **caller’s trade dict** L279–280 | `TradeManager` L263 | may build a reversal `CandidateSetup` |
| ExitAgent | `crew/exit_agent.py` | Target, trail, stop, reversal, cutoff | trade dict, cmp, clock | `ExitDecision` | pandas | none; caller journals | `TradeManager` L302. Cutoff passed as `"15:10"` | none |
| JournalAgent | `crew/journal_agent.py` | Open and closed JSON, SQLite copy | `TradeSignal`, `ExitDecision` | trade dict or `None` | `trade_store.upsert_trade` | active JSON, executed JSON, SQLite | `TradeManager` | `uuid`, sqlite |
| AlertAgent | `crew/alert_agent.py` | Telegram text | signals, quotes | HTTP post | httpx PARTIAL | none durable | `TradeManager` | Telegram |
| SmartRoutingAgent | `execution_crew/smart_routing_agent.py` | Limit plan from lots and a price | lots, qty, price, side | `RoutedOrderPlan` | none | none | `ExecutionManager.execute_entry` only | none |
| BrokerBridgeAgent | `execution_crew/broker_bridge_agent.py` | Paper echo or hard reject | order fields | `SIMULATED_FILLED` or `REJECTED`/`UNSENT` L61–90 | none. No adapter | none | `ExecutionManager` only. Daemon does not construct it | `uuid` |
| ReconciliationAgent | `execution_crew/reconciliation_agent.py` | Compare two position lists | dict lists | `ReconStatus` | none | none | `ExecutionManager.reconcile` | none |
| AutopsyAgent | `post_market_crew/autopsy_agent.py` | Day narrative from trades and a frame | frame, trades, PDH/PDL | `DayAutopsy` | pandas | none | `PostMarketManager` L96 | none |
| ParameterAgent | `post_market_crew/parameter_agent.py` | Proposal rows | frame, trades | jsonl proposals. Redirects a write aimed at `active_strategy_params.json` | json | `parameter_proposals.jsonl` | `PostMarketManager` L105 | none |
| SafetyWallAgent | `post_market_crew/safety_wall_agent.py` | Daily P&L and, when a premium method is non-empty, a second reserve writer | trade rows | `SafetyWallSummary` | `scaling_law` | **`safety_wall_state.json` and `daily_risk_config.json`** L128–139 when `priced` is non-empty | `PostMarketManager` L115 | none |
| CodeQualityAgent | `post_market_crew/code_quality_agent.py` | Log / health text | process-local | `CodeQualityReport` | PARTIAL | none verified as trading state | `PostMarketManager` L118 | none |
| BriefingAgent | `post_market_crew/briefing_agent.py` | Markdown report and Telegram | autopsy, safety, code | a report file | httpx | `reports/` | `PostMarketManager` L121 | Telegram |

LangGraph nodes in `app/agents/trade_crew/graph.py` (`_verify` through `_execute`) are functions, not agent classes. `opinion.py` `get_opinion` is an LLM call clamped by `normalize`. `review_crew/reviewer.py` and `improver.py` are the same pattern for strategy documents. They are not rows in the table above because they are not `*Agent` classes and they do not run inside `live_scanner.py`.

## Findings

### 1. Existing agents

Twenty-four classes, all listed above. **VERIFIED** by class search under `backend/app`.

### 2. Duplicates and overlap

| Overlap | Evidence | What to do |
|---|---|---|
| Two `GammaAgent` instances | Pre-market and `TradeManager.__init__` | Keep one class. Do not add a third |
| `PREMIUM_PNL_METHODS` copied | `risk_agent.py` L22 and `safety_wall_agent.py` L28, both empty | One frozenset next to the method strings, as the financial design says |
| Reserve accounting | `record_trade_outcome` L132–133 and `process_daily_pnl` L100–114 | One owner: `record_trade_outcome` |
| News vs event agents | `NewsAgent.check_event_risk_guard` and `EventRiskAgent` | Leave both. Recency must not add a third veto |
| `LevelsAgent` rebuilt inside the tick | `manager.py` L662, L821 | Call the scanner’s instance. Do not add an agent |
| `DirectionalThesis` | dataclass on `TradeSignal`, filled in the tick L777 | Not an engine. Do not promote it to an agent |
| Post-market autopsy vs `review_crew` | Different inputs: journal rows vs `StrategyDocument` | Do not merge |

### 3. Excessive responsibility

`TradeManager` owns gates, holding, exits, sizing, journaling, alerts, and a stock side-path. `RiskAgent` owns session reset, the loss count, the R breaker, lot policy, and the reserve file. `SafetyWallAgent.process_daily_pnl` both reports P&L and, in the priced branch, replaces the risk file and resets `daily_losses_count` to 0 in that document (L135).

### 4. Shared-state writers

Direct writers: `RiskAgent.save_config`, `JournalAgent` JSON methods, `SafetyWallAgent` both files, `PreMarketManager._save_plan`, `ParameterAgent.append_proposals`, `trade_store.upsert_trade`. `HoldingAgent` mutates the trade dict the manager passed in (`stop_loss` assignment at `manager.py` L280). `EntryAgent` is given the manager’s `RiskAgent`, so a fallback `RiskAgent()` inside entry code would be a second risk object. The daemon injects the manager’s agent (`manager.py` L144–148). **VERIFIED** for the injection. The fallback constructor is the unfrozen shape if something else builds `EntryAgent` alone. **PARTIAL.**

### 5. Bypass of the intended orchestrator

- `live_scanner.py` calls managers directly. That is the orchestrator.
- `ExecutionManager` is not on that path. Tests construct it. Production daemon does not.
- `HoldingAgent` can insert a reversal setup at the front of `candidate_setups` (`manager.py` L297–300), which the same tick may then journal.
- `ParameterAgent` cannot write `active_strategy_params.json`. A person, or the daemon’s startup reader, still can. The afternoon review is not a closed loop.

### 6. Cycles

No import cycle was required to explain the tick. The behavioral loop is: holding flip inserts a candidate, the same tick may open it. That is a control-flow cycle inside one function, not a package cycle. **VERIFIED** at L297–300 and the candidate loop that follows.

`GammaAgent` → `OrderFlowAgent` → (no call back). **VERIFIED.**

### 7. Error handling that does not hold

| Site | Behavior | Label |
|---|---|---|
| `RiskAgent._load_config` | Any exception sets `session_blocked` only. Reserve stays 0 in memory. Later `begin_session` writes a new file because `session_date` is missing. L99–101, L148–170 | VERIFIED |
| `begin_session` return | Ignored. `manager.py` L192 | VERIFIED |
| `record_exit` is `None` | `record_trade_outcome` still runs. L356–371 | VERIFIED |
| `record_exit` bad executed file | Exception handler sets `executed = []`, then the function writes that list. L195–205 | VERIFIED |
| `save_active_trades` | Logs and swallows a write error. L77–81 | VERIFIED |
| `load_failed` | One flag. A later good load of another date clears it. L46–57 | VERIFIED |
| `GapAgent` | Missing gift price becomes a formula, not an error. L47–49 | VERIFIED |
| `GammaAgent` | Missing chain returns invented OI and walls. Entries are blocked only because `walls_live` is false. Holding still receives the numbers. L89–97 and `manager.py` L263 | VERIFIED |
| `PostMarketManager` | Empty frame and fixed PDH/PDL. `live_scanner.py` L280–284 | VERIFIED |
| `ExecutionManager` | Adds 1.0 “slippage saved” after the bridge returns, including a reject. `execution_manager.py` L83–85 | VERIFIED |

### 8. Unreachable from the daemon

`SmartRoutingAgent`, `BrokerBridgeAgent`, `ReconciliationAgent`, `ExecutionManager`. Reachable from tests and from nothing in `live_scanner.py`. **VERIFIED.**

`reset_daily_counters` has no production caller found in the trading package search used for this audit. It still calls `save_config`. `risk_agent.py` L173–179. **PARTIAL** on “no caller” (trading tree and the sprint notes; not every script body).

`EntryAgent.evaluate_sweep` runs in production only when the scanner passed `candidate_setups is None`. The daemon always passes a list (`live_scanner.py` L341), so the internal sweep is off on that path. Waterfall still runs. **VERIFIED.**

### 9. Keep, merge, split, or drop

| Class | Decision |
|---|---|
| RiskAgent | Keep. Change `record_trade_outcome`, `save_config`, `begin_session`, `evaluate_order` in place. Delete `reset_daily_counters` |
| SafetyWallAgent | Keep as a report. Remove the write branch |
| JournalAgent | Keep. Add executions on the existing object. Do not add a second book |
| TradeManager | Keep as the daemon orchestrator. Do not split into new agents for this migration |
| HoldingAgent, ExitAgent, SniperAgent, EntryAgent, TrendAgent, DetectorAgent | Keep. They are the live card, not PDC V0. Do not retune them to “become” V0 |
| GammaAgent, GapAgent, EventRiskAgent, NewsAgent, LevelsAgent, FeedAgent, ConsolidationAgent | Keep |
| Execution trio | Leave unwired. The deployment graph already has an order manager |
| Autopsy, Parameter, CodeQuality, Briefing | Keep. Fix the empty-frame call before trusting the report |
| New “Directional Thesis” agent | Not required. The dataclass exists |
| New recency agent | Not required. Sprint 2 specifies functions in `recency.py`, which does not exist yet |

No new agent is justified by a missing responsibility that an existing function cannot hold.
