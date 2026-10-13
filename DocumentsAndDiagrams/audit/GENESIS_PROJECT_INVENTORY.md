# Genesis — Project Inventory (Phase 0)

Audit date: 13 Oct 2026. Commit `53aa500` plus the uncommitted working tree at that date.
Everything below was read in code or measured; claims taken only from the README are marked as such.

## 1. What Genesis actually is

Genesis is an **end-of-day, rules-based NIFTY / BANKNIFTY index-options strategy lab** with a paper/live
execution layer. It is not a technical-indicator trading bot: strategies are YAML `rules:` blocks over a
small set of market fields (IV rank, days to expiry, spot, India VIX, distance from the previous day's
high/low) that open one of seven option structures (iron condor, short strangle/straddle, long call/put,
bull call / bear put spread).

| Part | Tech | Size |
|---|---|---|
| Backend API and engine | Python 3.12, FastAPI, SQLAlchemy 2, Pydantic 2, LangGraph | ~15.2k lines in `backend/app` + `backend/scripts` |
| Databases | SQLite x3: `genesis.db` (research, audited), `genesis_live.db` (live, append-only + hash chain), `market_eod.db` (NSE reference data, 694 MB) | Alembic migrations for the first two; the market DB self-creates |
| Frontend | Next.js 15 / React, Tailwind | 6 pages (`/`, `/lab`, `/markets`, `/deploy`, `/deployments`, `/login`) |
| Tests | pytest (backend), `node --test` (frontend) | 466 backend tests (after this audit), 3 frontend tests |
| Optional LLM | OpenAI / Anthropic / Groq / Google / HF | Strategy extraction, reason text, trade "opinions" (off by default) |

## 2. Module inventory

| Area | Module | Responsibility |
|---|---|---|
| Strategy definition | `app/strategies/schema.py`, `rules.py`, `clarify.py`, `store.py` | Parse markdown + frontmatter, validate `StrategyRules` (Pydantic, `extra=forbid`), entry/exit/risk checks as pure functions |
| Strategy ingest | `app/extract/extractor.py`, `app/ingest/*` | LLM extraction from YouTube/Instagram/docs into a strategy draft |
| Decisions shared by sim and live | `app/trading/decisions.py`, `app/agents/trade_crew/legs.py` | Build legs, entry economics (target/stop/max loss/RR), risk checks, mark-to-market, exit checks |
| Trade policy | `app/trading/policy.py`, `app/trading/odds.py` | Sizing (fixed / risk %), compounding, VIX sizing, booking/partial exits, daily target, odds gate |
| Simulator | `app/engine/sim.py`, `costs.py`, `metrics.py` (new) | Day-by-day replay, Indian F&O charges, performance statistics |
| Review Crew | `app/agents/review_crew/*` | Scenarios, backtest + forward windows, verdict, hard-stop sweep, trade reviews, improvements |
| Market data | `app/market/nse.py`, `store.py`, `historical.py`, `feed.py`, `source.py`, `calendar.py`, `hours.py`, `news.py` | NSE bhavcopy/index/VIX import, IV rank, historical and broker feeds, events, holidays, RSS news |
| Live trading | `app/agents/trade_crew/graph.py` (1.4k lines), `runner.py`, `app/live/*` | Tick graph, approvals, gates, drawdown ladder, money floor, kill switch, supervisor, equity |
| Orders and brokers | `app/trading/orders.py`, `app/brokers/{base,paper,sandbox,registry,credentials}.py` | Order manager (hedge-first, freeze split, re-price), adapters, Fernet-encrypted credentials |
| Persistence / audit | `app/db/*`, `app/audit/*`, `app/reports/store.py`, `app/journal/store.py`, `app/live/store.py` | Append-only tables, DB triggers, SHA-256 hash chain, audit context |
| API | `app/api/*`, `app/main.py`, `app/auth` | REST endpoints, bearer tokens / user sessions (scrypt) |

**Broker adapters:** only `paper` (instant fills) and `sandbox` (simulated exchange). **No real broker
adapter exists**, so no code path can place a real-money order today. `sandbox` can run in "live" mode only
with `SANDBOX_ALLOW_LIVE=true`, and it is still simulated.

## 3. Lifecycle: where each stage lives

| Stage | Backtest path | Live/paper path |
|---|---|---|
| Market data | `scripts/import_nse_eod.py` → `nse.parse_fo_bhavcopy` / `parse_daily_bars` → `MarketStore` | `QuoteSource` (sandbox) → `BrokerFeed.snapshot` |
| Data validation | `historical.option_price` (close if traded, else settlement flagged stale); `trading_dates` needs close + IV rank + option rows | `quote_price` (spread > `MAX_SPREAD_PCT` → stale); `graph._observe` rejects snapshots older than `MAX_SNAPSHOT_AGE_SECONDS` |
| "Indicators" | `nse.iv_rank_series` (VIX min-max rank, 252 days, past + same day only); `MarketSnapshot.pct_*` properties | `feed.vix_iv_rank` (history strictly before today + current VIX) |
| Strategy → signal | `rules.check_entry` (+ `check_blackout`) | same function |
| Risk validation | `decisions.entry_risk_checks` → `rules.check_risk`; policy sizing, ladder, floor, odds gate inside `sim.simulate` | `graph._entry` / `_risk` / `_risk_checks` (+ margin, portfolio limits) |
| Decision | `sim.simulate` opens at the day's close | `graph._execute` writes a `TradeIntent` (approval auto/blocked/vetoed) |
| Execution | cost model `costs.leg_costs` | `OrderManager.execute` → adapter `place_order`, poll, cancel, re-price |
| Monitoring | `decisions.mark_position` daily | `graph._reconcile` (broker positions vs DB, halt on mismatch), protective stop-limits |
| Exit | `decisions.exit_checks` (expiry first), `policy.booking_exit` | `graph._exits` → `_close_position`; `live/kill_switch.stop` |
| Journal | `reports/store.ReportStore` (`SimulationRun`, `SimTrade` with hashes) | `LiveStore.close_live_trade`, `journal/store.JournalStore` |
| Performance | `review_crew/graph._metrics` + `engine/metrics.performance` (new) | `live/equity.py`, `live/opinion_eval.py` |

The simulator and the Trade Crew call the **same** decision functions (`check_entry`, `build_entry_proposal`,
`entry_risk_checks`, `exit_checks`, policy functions). This is the strongest design choice in the project, and
`tests/test_simulator.py::test_trade_crew_and_simulator_make_the_same_decisions` checks it.

## 4. Existing functionality (verified)

- Deterministic rule engine; LLM can only reduce lots or veto, never unblock (`graph._opinion`, `_clamp`).
- Real NSE data imported: NIFTY options 2020-01-01 → 2026-10-13 (1,676 days, 5.8 M option rows), index and
  India VIX 2019 → 2026; BANKNIFTY index to 2026-10-06.
- Date-effective STT and exchange charges, exercise STT, stamp duty, GST, SEBI fees.
- Expiry settlement at intrinsic value against the index close.
- Live safeguards: `LIVE_TRADING_ENABLED` off by default, defined-risk structures only by default, SEBI algo
  id required, reconciliation at startup and every tick, protective stops, kill switch (stop recorded before
  cancels), drawdown ladder, capital floor, supervisor with restart backoff and heartbeat alerts.
- Append-only audit trail with DB triggers and a hash chain.

## 5. Missing functionality

- Real broker adapter (Zerodha/Upstox/…): none.
- Intraday data and intraday backtesting (the research folder does this outside the app).
- Margin model in backtests; portfolio Greeks/exposure; correlation between deployments.
- Walk-forward / rolling validation, Monte Carlo trade resampling (bootstrap CI on expectancy now added).
- Classical indicators (RSI, EMA, ATR, …): **not implemented**, and not needed by the current rule fields.
- CI pipeline (no `.github/` or equivalent).

## 6. Dead or unused code

| Item | Evidence |
|---|---|
| `app/engine/costs.apply_trade_costs` | defined, never called |
| `src/trading_strategy_lab_backend` | uv scaffold `main()` that prints "Hello" |
| `vendor/TradingAgents` (5.2 MB, mostly untracked) | not imported anywhere in `backend/app` or `backend/scripts` |

## 7. Architectural concerns (summary; details in the phase reports)

1. Backtest is end-of-day close-to-close; the live crew ticks every 60 s from 09:15 and evaluates the same
   rules on intraday values (G-04).
2. `trade_crew/graph.py` is 1.4k lines mixing orchestration, booking, stops and opinions; hard to review.
3. Daily-loss limits look at realized P&L only (G-05).
4. Three SQLite databases with a single writer; fine for one user, not for concurrent crews at scale.
