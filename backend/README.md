# Trading Strategy Lab — backend

FastAPI + SQLAlchemy 2 + Alembic. Every strategy, report, simulated trade, review and improvement proposal is stored in a research database; paper and live trading use a separate live database. Both are append-only, each with its own hash-chained audit log.

## Database

```bash
uv sync
uv run alembic upgrade head            # research database
uv run alembic -n live upgrade head    # live database
uv run uvicorn app.main:app --port 8841
```

`DATABASE_URL` (in the repo-root `.env`) selects the research database, and `LIVE_DATABASE_URL` the live one (default `sqlite:///data/genesis_live.db`). Both accept:

| Value | Notes |
| --- | --- |
| `sqlite:///data/genesis.db` (default) | File relative to `backend/`. WAL mode, foreign keys on, writes serialized with `BEGIN IMMEDIATE`. |
| `postgresql+psycopg://user:pass@localhost:5432/genesis` | Install a driver first (`uv add "psycopg[binary]"`). |

Research migrations (`alembic/`): `0001` core + simulation tables, `0002` live-trading tables, `0003` reviews, improvements and the `trade_journal` view, `0004` moves live trading out (drops the live tables, restricts `trade_reviews` and the view to sim trades). `0004` aborts if any of those live tables still hold rows. Live migrations (`alembic_live/`): `0001_live`, `0002_opinions` (LLM opinions and phantom trades), `0003_drawdown` (position snapshots per deployment, capital and drawdown events), `0004_partial_trades` (`live_trades.partial` for partial profit booking). Each migration also installs the append-only triggers for its tables.

### Importing the old file store

Strategies and reports previously saved under `data/strategies/` and `data/reports/` can be imported once (re-running is safe; already-imported items are skipped):

```bash
uv run python scripts/import_file_store.py [--strategies-dir data/strategies] [--reports-dir data/reports]
```

Each imported file is logged as an `import.file_store` audit event with its sha256. Imported reports are marked `inputs.legacy = true` and have no trade rows, because the old files never contained trades.

## Auditability

- **Append-only history.** Strategy versions, ingest runs, reports, simulation runs, trades, reviews, improvements, decisions, audit events and every live table reject `UPDATE` and `DELETE`, both in the ORM and via database triggers. Strategies themselves are never deleted; `POST /api/strategies/{id}/archive` soft-deletes them. Corrections are new rows (e.g. a review with `supersedes_review_id`).
- **Hash-chained audit log.** Every write records an `audit_events` row whose `hash = sha256(prev_hash + canonical_json(event))`. `GET /api/audit/verify` recomputes the whole chain and reports the first broken row, if any. `GET /api/audit` lists events (filter by `entity_type`, `entity_id`, `action`).
- **Who and which request.** Send `X-Actor: <name>` on mutating requests; it is stored on each audit event (default `anonymous`). `X-Request-ID` is accepted or generated and echoed back. `X-Actor` is not authenticated — put the API behind auth before exposing it.
- **Reproducibility.** Each report stores its inputs: cost profile, settings, market-data sha256, `ENGINE_VERSION`, `REVIEW_RULES_VERSION`, git commit and app version. Extraction stores the extractor (`llm`, `fixture`, `passthrough`), model and raw LLM output in `ingest_runs`.
- **Money.** Amounts are `Numeric(18,4)`. Values are quantized to 4 decimals when stored, so database totals reconcile exactly with the sum of their components but can differ from the in-memory float results by less than Rs 0.001.

## Trade journal

Every simulated trade (`sim_trades`) records:

- **Why** — `entry_rule`, `entry_conditions` (each condition with actual vs threshold), `reason_text`, `edge`, `market_snapshot`.
- **How** — `lots`, `credit`, `max_loss`, `notional`, `orders`, `cost_breakdown` (brokerage, STT, exchange, GST, SEBI, stamp, slippage), `exit_rule`, `exit_reason`, `holding_bars`.
- **Vision** — `target_pnl`, `stop_loss_pnl`, `risk_reward`.
- **Outcome** — `gross`, `costs`, `net`, `pnl_vs_target`, `cost_ratio`, `outcome_label` (`win` / `loss` / `scratch`, where scratch means within 1% of the target), `equity_after`, `drawdown_after`.

Live trades (`live_trades`, in the live database) carry the same why / how / vision / outcome shape. `GET /api/journal` merges sim rows from the research `trade_journal` view with live rows from the live database, in the same columns (live rows are skipped if the live database is not migrated):

`kind, trade_id, strategy_id, strategy_version_id, strategy_version, trade_date, mode, entry_rule, reason_text, target_pnl, net, pnl_vs_target, outcome_label, source_id, latest_verdict, mistake_tags`

```
GET /api/journal?kind=sim&strategy_id=...&outcome=loss&tag=costs_ate_edge&from=2025-01-01&to=2025-06-30
GET /api/reports/{id}/trades?scenario_index=0&mode=backtest     # full rows + latest review
GET /api/reports/{id}/trades.csv
```

### Reviews

The Review Crew writes a deterministic review for every simulated trade (`reviewer_type = agent`, `rules_version = REVIEW_RULES_VERSION`) with `followed_plan`, `process_verdict`, `what_went_well`, `what_went_wrong`, `mistake_tags` (e.g. `costs_ate_edge`, `high_cost_ratio`, `marginal_entry`, `forward_decay`), `lesson`, `improvement_suggestion` and `suggested_rule_change`.

Humans add reviews with `POST /api/trades/{sim|live}/{trade_id}/reviews`; set `supersedes_review_id` to correct an earlier one. `GET` on the same path lists the history. Live-trade reviews are stored in the live database (`live_trade_reviews`).

### Improvement flow

1. During a review run the crew proposes `strategy_improvements` when a bucket of trades (IV band, DTE band, cost ratio, backtest vs forward) loses while the rest is profitable. Each proposal stores `evidence` (metrics and the exact `trade_keys`) and `proposed_change`. An LLM rationale is appended when `OPENAI_API_KEY` is set.
2. List them: `GET /api/strategies/{id}/improvements` (each has a `status`).
3. Decide: `POST /api/improvements/{id}/decisions` with `{"decision": "accepted" | "rejected" | "applied", "note": "..."}`. `applied` requires `"markdown"` with the edited strategy, which is saved as a new strategy version and linked via `resulting_version_id`. `rejected` and `applied` are final.

## Market data, events and news

Reference data lives in a separate, unaudited SQLite file (`MARKET_DATABASE_URL`, default `data/market_eod.db`). All three importers are safe to re-run.

```bash
uv run python scripts/import_nse_eod.py [--raw-dir data/nse/raw]     # F&O bhavcopy (old and UDiFF), index closes, India VIX
uv run python scripts/import_events.py [--board-meetings bm.csv]     # data/events/*.yaml plus NSE board meetings
uv run python scripts/fetch_news.py [--nse-announcements]            # headlines from NEWS_FEEDS; run it on a schedule
```

- **Backtests** replay the strategy's own rules day by day on imported end-of-day closes. Positions are held across days, expired legs settle at intrinsic value, and charges depend on side and date (`STT_SCHEDULE`, `EXCHANGE_TXN_SCHEDULE`, `EXERCISE_STT_PCT`). Drawdown is mark-to-market. Without imported data for the underlying, the bundled synthetic sample is used and reports say so (`data_source`). IV rank only uses history up to each day (`IV_RANK_LOOKBACK_DAYS`).
- **Events** (`data/events/*.yaml`, RBI policy, budgets, FOMC, results) feed the optional `entry_blackout` rule: `{kinds: [rbi_policy, results], days_before: 1, days_after: 0}`. If no calendar has been imported, a blackout rule fails safe and blocks entries. Check the seed calendar's dates before relying on them.
- **News** is headlines only, stored with the time they were published and fetched. The LLM only sees headlines published at or before the decision time, cleaned and marked as untrusted text. Feeds only list recent items, so history cannot be backfilled.

## Live trading

A strategy trades only after you approve one exact version for paper or live. Each approval freezes a copy of that version and gives it its own Trade Crew. The crew decides only through the strategy's own rules, checked in code. An LLM never opens or closes a trade; with the veto explicitly enabled for a deployment, it can at most skip or shrink an entry the rules approved. Only paper trading works today: there is no real broker adapter yet.

### Strategy rules

Entry, exit and risk rules live in a `rules:` block in the strategy's frontmatter, versioned and hashed with the rest of the strategy:

```yaml
rules:
  instrument: {underlying: NIFTY, structure: iron_condor, short_distance: 1000, wing_width: 200}
  entry:
  - {field: iv_rank, op: '>=', value: 40}
  - {field: dte, op: '>=', value: 7}
  - {field: dte, op: '<=', value: 21}
  exit:
  - {kind: profit_target_pct_of_credit, value: 50}
  - {kind: stop_loss_pct_of_credit, value: 100}
  risk: {max_lots: 2, max_daily_loss_pct: 2, min_risk_reward: 0.4, max_open_positions: 1}
```

Supported: entry fields `iv_rank`, `dte`, `spot` with `>=`, `<=`, `>`, `<`, `==`; structures `iron_condor`, `short_strangle`, `short_straddle`; exits `profit_target_pct_of_credit`, `stop_loss_pct_of_credit` (both required) and `dte_at_or_below`. Unknown fields are validation errors, never skipped. The extractor emits this block but never invents thresholds; anything it cannot express goes into Gaps. The demo strategy deliberately lacks `short_distance`, so it cannot be approved.

### Approval

```
GET  /api/live/approvals/preview?strategy_id=...&version=1   # rules, rules_sha256, blockers, verdict, warnings
POST /api/live/broker-accounts   {"broker": "paper", "account_ref_masked": "PAPER-1", "mode": "paper"}
POST /api/live/approvals         {"strategy_id", "version", "mode": "paper"|"live", "broker_account_id",
                                  "confirm_rules_sha256",
                                  "risk_limits": {"capital_inr", "max_lots", "max_daily_loss_pct", "drawdown", "policy"},
                                  "sebi_algo_id", "acknowledge_verdict"}
POST /api/live/approvals/backtest {"strategy_id", "version", "risk_limits"}   # rules only vs with policy and ladder
```

Approval is refused unless the rules parse, Gaps is empty, `confirm_rules_sha256` matches the preview (proving you approved exactly these rules), a completed report exists for the version (a verdict other than `Profitable` needs `acknowledge_verdict: true`), `X-Actor` is set, the account mode matches, and no other deployment of the strategy is active in that mode.

**Backtest warning:** report verdicts come from four fixed parameter sets and a synthetic same-day outcome. They do not test the strategy's own `rules:` block, so a `Profitable` verdict is not evidence that the rules make money.

Approval writes the frozen snapshot (`deployed_strategies`), the `Deployment` and an `approved` event to the live database in one transaction, then a `strategy.approved_for_live` event to the research audit log. If the second write fails, find and repair the gap with:

```bash
uv run python -m app.live reconcile [--fix]
```

A newer strategy version never replaces a running deployment: stop it and approve the new version.

### Start, pause, stop (kill switch)

```
POST /api/live/deployments/{id}/start   {"reason": "..."}   # needs X-Actor; from approved or paused
POST /api/live/deployments/{id}/pause                       # from started
POST /api/live/deployments/{id}/stop                        # kill switch; final
```

The crew trades only while the latest event is `started`. `stopped` and `halted` are final. The crew halts itself if the snapshot's hashes no longer match, an order is rejected, or a tick raises an error.

### Running the Trade Crew

```bash
uv run python -m app.agents.trade_crew run --deployment ID [--interval 60] [--max-ticks N] [--explain]
```

Run one process per deployment, separate from the API. Every write is stamped `trade_crew/<deployment_id>`. Paper deployments use `PaperBroker` (fills LIMIT orders at the quote plus `SLIPPAGE_BPS`, charges from the cost model) and `ReplayFeed` (replays `data/sample_nifty_options.csv` one bar per tick with synthetic option quotes). Live deployments and non-paper brokers are refused until a broker adapter exists.

Each tick:

1. **Verify:** recompute the frozen snapshot's `content_sha256` and `rules_sha256`; a mismatch halts the deployment.
2. **Gate:** do nothing unless the deployment is `started`.
3. **Observe:** take a market snapshot. One older than `MAX_SNAPSHOT_AGE_SECONDS` stops the tick.
4. **Exits:** check each open position against the exit rules, then (only if none fired) the policy's booking and time exits. A trigger writes a close intent, sends orders and records a `LiveTrade` with gross, estimated and broker-reported costs, slippage against quotes, net and outcome. A partial booking records a `LiveTrade` with `partial = true` for the lots it closed; the rest stays open. Positions still open are written to `position_snapshots` with their marked P&L and peak profit.
5. **Drawdown:** measure drawdown from peak equity and apply the deployment's drawdown ladder (see below). Reaching a pause level pauses the deployment and ends the tick.
6. **Daily target:** if the policy has a daily profit target and today's P&L (realized plus open, since 00:00 UTC) has reached it, block new entries or close everything, as configured.
7. **Entry:** skipped while the deployment holds its maximum open positions. Otherwise every entry condition must pass. Policy sizing picks the lots, then a `reduce_size` drawdown level scales them down.
8. **Risk:** lots, risk-reward, open positions and daily loss (against capital in force) for the deployment, `PORTFOLIO_MAX_OPEN_POSITIONS` and `PORTFOLIO_MAX_DAILY_LOSS_INR` across all active deployments, `drawdown.entries_allowed`, `policy.sizing` and `policy.daily_profit_target`.
9. **Opinion (optional):** see below. Only for entries the rules approved.
10. **Explain (optional):** with `--explain` or `TRADE_CREW_EXPLAIN=true` and an `OPENAI_API_KEY`, an LLM appends a note to `reason_text` after the decision is made. It is never an input.
11. **Execute:** if every check passed, a `TradeIntent` with `approval = auto` and one LIMIT order per leg. If a risk check failed, a `TradeIntent` with `approval = blocked` naming the failed rules, and no orders. A veto writes `approval = vetoed` and no orders. No entry signal writes nothing.

Lot sizes and strike steps come from `LOT_SIZES` and `STRIKE_STEPS` (e.g. `NIFTY:75,BANKNIFTY:35`) unless the rules set `lot_size`.

### Equity, capital and drawdown

**Equity** = capital in force + realized net P&L + unrealized P&L of open positions (marked at quotes in the latest position snapshot, before exit costs). Capital in force is the approved `capital_inr` plus deposits minus withdrawals. A capital event moves equity and peak by the same amount, so it never looks like a gain or a drawdown.

```
POST /api/live/deployments/{id}/capital-events   {"kind": "deposit"|"withdrawal", "amount_inr", "note"}   # needs X-Actor
GET  /api/live/deployments/{id}/capital-events   # events and capital in force
GET  /api/live/equity?deployment_id=|mode=paper&basis=mtm|realized&curve=true
GET  /api/live/positions?deployment_id=|mode=paper   # open positions with their latest marks
GET  /api/live/deployments/{id}/drawdown          # ladder in force, current level, level history
```

The **drawdown ladder** is set at approval in `risk_limits.drawdown` and frozen with the deployment:

```yaml
drawdown:
  basis: mtm                 # or realized: ignore open positions' unrealized P&L
  ladder:
  - {at_pct: 3, action: warn}
  - {at_pct: 5, action: reduce_size, size_factor: 0.5}
  - {at_pct: 8, action: stop_new_entries}
  - {at_pct: 10, action: pause, close_open_positions: false}
  restore_below_pct: 2       # levels step down only below this; immediately if omitted
```

Levels are percentages of drawdown from peak equity. They must increase, actions may only get stronger up the ladder, and each level also applies the actions below it. `reduce_size` trades `size_factor` x the usual lots (at least 1). `stop_new_entries` records entry signals as `blocked` with `drawdown.entries_allowed` failed. `pause` pauses the deployment on the tick it reaches that level, closing open positions first if `close_open_positions` is set. While paused, the crew does nothing at all, exits included. After a manual restart, entries stay blocked until the level steps down, and the deployment pauses again only if it steps down and later reaches the pause level again. Exits always run before the drawdown check.

`MAX_DRAWDOWN_PCT` (default 20) adds a pause level to every deployment, with or without a policy. Policy levels may not go past it. Set it to 0 to disable. Every level change is written to `drawdown_events`.

### Trade management policy

`risk_limits.policy` sets sizing, profit booking, time exits and a daily profit target. Like the ladder, it is validated against the strategy's rules and frozen at approval; to change it, stop the deployment and approve again. A policy can only make trading more careful: it never trades more than the approved `max_lots` and never removes the strategy's own exits. No LLM is involved.

```yaml
policy:
  sizing: {mode: risk_pct, risk_pct: 1, min_lots: 1}   # or {mode: fixed, lots: 1} or {mode: risk_inr, risk_inr: 5000}
  compounding: {profit_share_pct: 50, ceiling_inr: 800000}   # risk sizing only
  booking:
    partial: [{at_pct_of_credit: 25, close_pct: 50}]          # up to 5 levels, increasing
    lock_in: {after_pct_of_credit: 30, lock_pct_of_credit: 10}
    trailing: {after_pct_of_credit: 25, giveback_pct: 40}
  time_exit: {days_before_expiry: 1, at: "15:00", before_events: [rbi_policy, budget], event_days_before: 1}
  daily_target: {profit_inr: 10000, on_hit: stop_new_entries}   # or close_all
```

- **Sizing.** `fixed` trades `lots` (default `max_lots`). `risk_pct` and `risk_inr` trade as many lots as keep the trade's max loss within the budget, capped at `max_lots`; below `min_lots` the entry is `blocked` with `policy.sizing` failed. `risk_pct` is of capital in force. With `compounding`, `profit_share_pct` of realized net profit is added to that capital, up to `ceiling_inr`; a net loss leaves it at capital in force. A `reduce_size` drawdown level then scales the result.
- **Booking.** Percentages are of the credit received. Every threshold must sit below the strategy's profit target, or approval is refused. A partial level closes `close_pct` of the lots opened (at least one lot, always leaving one open). `lock_in` closes the rest once profit falls back to `lock_pct_of_credit` after reaching `after_pct_of_credit`. `trailing` closes once profit gives back `giveback_pct` of its peak.
- **Time exits.** Close the whole position `days_before_expiry` days before expiry (from `at`, IST, if set), or `event_days_before` days before a scheduled event of the listed kinds.
- Order each tick: the strategy's exits first, then time, event, lock-in, trailing, partial.

`POST /api/live/approvals/backtest` replays the strategy's rules over all available history twice, rules only and with the policy and ladder, and returns both summaries and equity curves. It uses end-of-day closes, so a time of day is ignored and a pause stops entries for the rest of the history. The frontend's deploy page (`/deploy`) builds the policy in a form, runs this comparison and approves; `/deployments` shows equity, positions, the ladder and capital events.

### LLM trade opinions: off, shadow, veto

`LLM_TRADE_OPINION` (default `off`) lets an LLM review entries the rules already approved. It sees the proposal, every check, the market snapshot, upcoming events, recent headlines and the deployment's recent lessons. It can answer take, reduce (fewer lots) or skip; it can never add lots, unblock a trade, or touch exits. A timeout or error is recorded and the rules decide.

- **shadow:** the opinion is stored in `trade_opinions` with the prompt hash, context, model and latency. Trading is unchanged.
- **Lessons:** while opinions are on, each closed trade gets a short LLM post-trade review, stored as a `reviewer_type = agent` review (`reviewer = llm:<model>`). Its lesson is fed into later opinions. Lessons never change rules; that still goes through improvements and human approval.
- **Evaluation:** `GET /api/live/opinions/evaluation?deployment_id=` joins each opinion to what happened. It compares net P&L, win rate and drawdown by decision, and builds an equity curve of the rules against one that follows the opinion. It also gives a bootstrap 95% confidence interval for the difference. There is no verdict until `OPINION_EVAL_MIN_SAMPLES` (30) skip or reduce opinions have closed.
- **veto:** set `LLM_TRADE_OPINION=veto`, then enable it per deployment with `POST /api/live/deployments/{id}/llm-veto {"enable": true}`. This needs `X-Actor` and a `veto_helps` verdict, which is stored as evidence in the audit log. A skip writes a `vetoed` intent with no orders. A reduction re-runs the risk checks on fewer lots. The lots the LLM removed are tracked as phantom positions until an exit rule fires (`phantom_trades`), so the evaluation keeps working. If the last `OPINION_EVAL_WINDOW` outcomes show the opinion losing money, the crew switches back to shadow (`llm_veto_disabled` by `system:veto_auto_revert`). `{"enable": false}` turns it off by hand.
- Live accounts are refused unless `LLM_VETO_LIVE_ALLOWED=true`. Keep it off until the SEBI classification of LLM-assisted algos is confirmed.

### Reading live data

`/api/live/deployments`, `/api/live/deployments/{id}`, `/api/live/deployments/{id}/intents`, `/api/live/orders/{id}`, `/api/live/live-trades`, `/api/live/opinions`, `/api/live/opinions/evaluation`, `/api/live/phantom-trades`, `/api/live/audit` and `/api/live/audit/verify` (the live database's own hash chain). They return 503 until the live database is migrated. `app/live/store.py` is the only write path and audits every row; `app/brokers/base.py` defines the `BrokerAdapter` protocol a real broker must implement.

## Tests

```bash
uv run pytest -q
```

Tests run against temporary research and live SQLite files migrated with Alembic, so the triggers are exercised. They cover versioning, immutability (ORM and raw SQL), both audit chains, trade reconciliation, reviews and improvements, live-schema constraints, the journal, the API, the import script, migration drift, strategy rules, every approval refusal, and the Trade Crew (entry, profit-target and stop-loss exits, blocked intents, tampered snapshots, the kill switch, stale data, rejected orders, and identical decisions with or without the LLM explanation), position snapshots, capital events, equity and every drawdown ladder action, and the trade policy (validation, sizing and compounding, partial booking, trailing, lock-in, time and event exits, the daily target, the approval freeze, and the policy in the simulator and its backtest endpoint). They also cover the NSE importers, the day-by-day simulator and its parity with the Trade Crew, events and blackouts, news parsing and point-in-time context, and LLM opinions with a fake LLM: shadow mode never changes orders, lessons, the evaluation, the veto clamp, vetoed intents, phantom trades and the auto-revert. No test calls a real LLM.
