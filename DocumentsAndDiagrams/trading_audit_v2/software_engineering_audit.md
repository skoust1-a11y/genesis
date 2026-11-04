# Software engineering audit

Audit date: 4 Nov 2026. Priority is money and false confidence, not style.

## Shape

About 144 Python modules under `backend/app`, 48 test modules, a Next.js app, and a large `vendor/TradingAgents` tree. The trading behaviour that matters is concentrated in `liquidity_signals.py` (research signals), `engine/sim.py` (daily sim), and `trading/crew/manager.py` (the daemon). Many crew classes are wrappers that a reader can mistake for infrastructure.

## Quality issues that change trading outcomes

| ID | Issue |
|---|---|
| V2-01 | Unreachable safety: live `place_order` returns success |
| V2-02 | Dead object on the hot path: `ExecutionManager` constructed, unused. Dead code that operators trust |
| V2-06 | `ParameterAgent` is a template, not a calculation, and it writes config |
| V2-07 | Caller passes empty data into a report that sounds measured |
| V2-10 | `RESEARCH_CATALOG` literals presented through the API as performance |
| V2-13 | `except` on journal load returns an empty book; save writes it back |
| V2-05 | `reset_daily_counters` is unused |

`RiskAgent._load_config` uses a bare `except: pass`. A truncated JSON file keeps the in-memory defaults (₹50,000, zero losses) and the next `save_config` can overwrite the broken file with those defaults. Same family of bug as V2-13.

Logging of sqlite failures at `logger.debug` hides a split brain between JSON and `trades.db`.

## Modularity

The research engine is a reasonable split: rules, feed, costs, metrics, orders. The scanner crew splits by "agent" while the decisions that interact (lunch override, second position, idempotency, VIX) all sit in `TradeManager.on_market_tick`, which is long enough that a gate change is easy to miss. That is a maintainability problem because it is where orders would be born.

`strategies/analyzer.py` mixes a marketing catalog with an analysis entry point. The catalog should not own numbers.

## Dependencies and runtime

`backend/pyproject.toml` is the dependency list (no `requirements.txt` at the backend root). This session did not upgrade or audit CVEs. LLM providers are optional and default keys are unset. Trading math uses the standard library (`math.erf` for the normal CDF). That is the right dependency choice.

No `.github` workflow (V2-17). Tests exist and are not a gate on change. October's pyright count (264) was not re-run.

## Data stores

Several SQLite files (`genesis.db`, live DB, `market_eod.db`, `data/trades.db`, plus a `trades/trades.db` and `backend/genesis.db` / `backend/live.db` showing up as untracked). More than one "source of truth." The daemon's journal path is explicit (`data/trades.db`). The others are easy to read by mistake.

JSON files under `trades/` are the live control plane (`daily_risk_config.json`, `active_strategy_params.json`, `safety_wall_state.json`, active-trade files). They are not locked. Two processes (daemon and a manual script) can clobber them. `genesis.sh` is documented as the supervisor; this session did not prove it takes a lock.

## Performance

One operator, a 10-second loop, a few HTTP calls. CPU is not the risk. The risk is a loop that sleeps through an exit, and a post-market job that does work on empty inputs and still writes the parameter file.

`GrowwQuotes` fetches expiries with a thread pool of 8. Fine for a scrape. Not a market-data bus.

## Tests that matter and tests that do not

Worth keeping: cost hand-calculations, plumbing tests that refuse a double fill on the **sandbox** order manager, signal tests for `scan()`, the live-suggestion fixes around stale bars and quotes.

Misleading if read as a green light: `test_pre_market_and_execution_crews.py` checks the simulated bridge and "slippage saved." It does not fail when the live branch fabricates a fill. A test that sets `is_live_broker=True` and expects `success is False` until a real adapter exists would lock V2-01.

## Lean notes

The execution crew, as wired, adds a false order path. The smallest fix is to delete the live branch's success return and to stop constructing `ExecutionManager` in the daemon until it performs I/O. The parameter agent's numeric "expected gain" fields should be deleted, not generalized into an optimizer.

Vendor `TradingAgents` is a second platform inside the repo. It is not on the daemon path. Leaving it is a comprehension cost. Removing it is optional and was not done.

## Deployment

Local processes on ports described in the architecture note (API 8841, UI 3000). No container or CI deploy file was found in the top-level tree during this pass. Recovery is "start the script again." That matches a personal research box. It does not match unattended orders.
