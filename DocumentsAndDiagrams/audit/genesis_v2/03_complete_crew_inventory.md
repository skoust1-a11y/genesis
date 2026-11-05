# 03 — Complete crew inventory

A crew here is a manager module or a LangGraph graph. None of them is a CrewAI `Crew`. Process is sequential Python or a compiled `StateGraph`. There is no hierarchical delegation and no crew memory store.

## Inventory

| Crew | File | Agents | Tasks | Process | Manager | Trigger | Output |
|---|---|---|---|---|---|---|---|
| Pre-market | `pre_market_crew/pre_market_manager.py` `PreMarketManager` | Gap, Gamma, EventRisk | `run_pre_market_setup` | Sequential, one function | the class itself | Daemon, once, if `trades/.pre_market_{date}.lock` is absent and not `--once` and not `--no-telegram`. `live_scanner.py` L241 | `PreMarketBattleplan`, `trades/pre_market_plan.json`, optional Telegram |
| Scanner | `scanner_crew/scanner_manager.py` `ScannerManager` | Feed, Levels, Consolidation, News, Detector | `produce_scan_snapshot` | Sequential L75–107 | the class itself | Every daemon loop, and once for the daily frame before pre-market | `ScanSnapshot` in memory. Not a queue |
| Trade (daemon) | `crew/manager.py` `TradeManager` | Trend, OrderFlow, Gamma, Risk, Sniper, Entry, Exit, Holding, Alert, Journal | `on_market_tick` | Sequential inside one method | the class itself | Daemon after each snapshot. `live_scanner.py` L334 | action dict. Journal files. Risk file. Telegram |
| Execution | `execution_crew/execution_manager.py` `ExecutionManager` | SmartRouting, BrokerBridge, Reconciliation | `execute_entry`, `execute_exit`, `reconcile` | Sequential | the class itself | Tests only. Daemon source does not construct it | `ExecutionReceipt` |
| Post-market | `post_market_crew/post_market_manager.py` `PostMarketManager` | Autopsy, Parameter, SafetyWall, CodeQuality, Briefing | `run_daily_review` | Sequential L95–131 | the class itself | Daemon when local clock is outside 09:15–15:30 and `>= 15:31` and the report lock is absent | Markdown under `reports/`, proposals jsonl, and possibly the risk file |
| Trade (deployment) | `agents/trade_crew/graph.py` `build_trade_graph` | nodes, not classes. Opinion LLM optional | `run_tick` | LangGraph, conditional skip to `END` via `_next` | `Supervisor` subprocess | `python -m app.agents.trade_crew run` | live DB heartbeat, orders through `OrderManager` |
| Review | `agents/review_crew/graph.py` `build_review_graph` | reviewer, improver, hard stop, simulator | `run_review_crew` | LangGraph, fixed edges L207–214 | API route | `app.api.routes` review call | verdict and proposals in the HTTP response |

`scripts/run_post_market_audit.py` can call `PostMarketManager` outside the daemon. **PARTIAL:** the script’s name was seen in the sprint note and a constructor search; the script body was not re-read in this pass.

## Per crew

### PreMarketManager

- Entry: `run_pre_market_setup` L67.
- Model: sequential. No retries. Exceptions in the daemon caller are logged and the lock is not written (`live_scanner.py` L242–249), so the next start tries again.
- Context: reads `daily_risk_config.json` for lot fields and `active_strategy_params.json` for `otm_steps` L95–111. Does not read `effective_capital`.
- Gift Nifty argument is omitted by the daemon, so the gap is a formula.
- Tool permission: Telegram if env tokens exist. No orders.
- Timeout: none.
- Production: yes, once per process start, with the lock caveats above.
- The tick does not load `pre_market_plan.json` as a veto. The plan is a message, not a control.

### ScannerManager

- Entry: `produce_scan_snapshot` L62.
- Model: sequential. Feed failure returns a cached or empty frame (`feed_agent.py` L56–57). Empty 5m still yields `cmp` default 23000 (`scanner_manager.py` L87). That default can enter the tick if the frame is empty.
- `DetectorAgent` may set `is_valid_for_trade_crew` false for some trendline and related setups (file L161–166, L218–223). Sweeps are forced true (L288, L343, L382).
- News score is attached. It is not, by itself, the trade gate.
- No shared memory with `TradeManager` except the snapshot object passed in the same process.
- Production: yes, every loop.

### TradeManager

- Entry: `on_market_tick`.
- Model: sequential and blocking. The daemon sleeps 10s after the tick (`--interval` default).
- Delegation: none. The manager calls methods. Agents do not hand work to each other except `RiskAgent` → `OrderFlowAgent` and `GammaAgent` → `OrderFlowAgent`.
- Context: the active-trade dict, risk JSON, and in-memory `executed_order_signatures` (lost on restart).
- Errors: journal load abort; other exceptions inside order-flow during a hold are swallowed (`manager.py` L256–259). The daemon loop catches any other exception, logs it, sleeps 5s (`live_scanner.py` L378–380).
- Retry: the next poll is the only retry. A risk save that failed does not roll back a journal write that already happened.
- Timeout: none on the tick.
- Idempotency: an in-memory hash of strategy, direction, strike, minute, and rounded price (`manager.py` L863–889). Restart clears the set. The journal key is checked only against trades still in the active dict just loaded.
- Production: yes.

### ExecutionManager

- Entry exists. Production daemon does not call it. **VERIFIED.**
- `place_order` with `is_live_broker=True` returns failure and does not call an adapter (`broker_bridge_agent.py` L77–90).
- `is_live_broker=False` returns success and `SIMULATED_FILLED` without a broker (`L61–75`).
- Slippage counter increments on both paths (`execution_manager.py` L83–85 and L122–123).
- No timeout, no partial fill, no unknown order state.
- Do not promote this crew into the daemon. Sprint 2 forbids wiring it. The deployment graph is the order path that already has gates.

### PostMarketManager

- Entry: `run_daily_review` L51.
- The daemon call passes `pd.DataFrame()`, PDH 23150, PDL 22950 (`live_scanner.py` L280–284). Autopsy on that call is not today’s tape. **VERIFIED.**
- `SafetyWallAgent` is constructed on the real `trades/` directory when the daemon runs (L43–46 and daemon L208–212).
- While `PREMIUM_PNL_METHODS` is empty, `process_daily_pnl` returns before the write (`safety_wall_agent.py` L85–94). The write branch is live code, waiting on a non-empty set.
- The normalizer drops `pnl_method` and turns a missing rupee into 0 (`post_market_manager.py` L74–83). That 0 still has no method, so the early return holds for the daemon’s file load.
- Parameter file for the live strategy is not written. **VERIFIED** by the sprint note and `append_proposals` usage at L109–112. The redirect inside `append_proposals` was accepted from `sprint1_independent_verification.md` and not re-opened line by line. **PARTIAL** on the redirect branch.
- Error propagation: a bad executed-log read becomes an empty trade list (L89–90). The review still runs.
- Production: yes, after the clock check, once per lock file.

### LangGraph trade crew

- Entry: `run_tick` L1438. Graph built once and cached in `_GRAPH`.
- Edges: `verify → gate → hours → observe → reconcile → exits → drawdown → daily → entry → risk → opinion → explain → execute`, each able to stop. L1418–1431.
- `run_tick` rolls back the session in `finally`, then writes a heartbeat and commits (L1447–1455). A crash before heartbeat still leaves earlier node commits that the node itself committed. **PARTIAL:** the rollback is verified; which nodes commit internally was not re-read node by node.
- Opinion is an LLM recommendation clamped to the rule lots (`opinion.py` `normalize`). It is not the scanner’s `RiskAgent`.
- This crew is production only when a deployment is started and the supervisor is running. It is not the scanner.

### Review crew

- Entry: `run_review_crew` L218. Fixed sequence. No broker.
- Production: when the API review route is called. Not part of the market loop.

## Consolidation

Do not add crew boundaries. The four daemon managers are the right split. Execution stays off the daemon. Review stays on strategy documents. The deployment graph stays on the live database.

Unnecessary boundary: the startup banner’s fifth crew. Delete the claim in a later doc change if desired. Do not build the crew to make the banner true.

`GammaAgent` living under `pre_market_crew` and being imported by the trade manager is a packaging accident, not a second crew.
