# Trading Strategy Lab — backend

FastAPI + SQLAlchemy 2 + Alembic. Every strategy, report, simulated trade, review and improvement proposal is stored in a research database; paper and live trading use a separate live database. Both are append-only, each with its own hash-chained audit log. NSE reference data is a third database and is not audited.

This file is the operator guide. The component map, column-level schema, and dependency list are in [DocumentsAndDiagrams/](../DocumentsAndDiagrams/Readme.md).

`Settings` in `backend/app/config.py` supplies defaults when a variable is unset. A copied `.env.example` overrides several of them (`DEFAULT_CAPITAL_INR`, `DEFAULT_MAX_LOTS`, the portfolio caps, and the common risk rules). The table is in the architecture analysis.

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

### Hard stop-loss sweep

Every review also re-runs the strategy with a hard stop at each % of capital in `HARD_STOP_SWEEP_PCTS` (default `2,3,4,5`; empty turns it off), on `DEFAULT_CAPITAL_INR` and the same backtest and forward windows as the scenarios. The target is the strategy's own risk-to-reward ratio x the stop (`profit_target_r_multiple`, or the premium target % over the premium stop %, so 90/30 gives 3R), and the days-to-expiry exit is kept. `payload.hard_stop_sweep` holds `capital_inr`, `risk_reward`, one row per stop (rupee stop and target; trades, win rate and net per window; average and worst loss; largest drawdown; signals blocked by the `max_loss_pct_of_capital` sizing rule) and `recommended_pct` with a `reason`. Only stops profitable in both windows with at least `VERDICT_MIN_BACKTEST_TRADES` and `VERDICT_MIN_FORWARD_TRADES` trades qualify; among them the highest total net over the largest drawdown wins, the smaller stop on a tie, and none is recommended when nothing qualifies. The verdict label still comes from the scenarios only. The recommendation is one stretch of history, and the reason says so. It is also proposed as an improvement (`proposed_change: {section: Exit, field: stop_loss_pct_of_capital}`) with the rows as evidence.

### Improvement flow

1. During a review run the crew proposes `strategy_improvements` when a bucket of trades (IV band, DTE band, cost ratio, backtest vs forward) loses while the rest is profitable, plus the recommended hard stop from the sweep. Each proposal stores `evidence` (metrics and the exact `trade_keys`) and `proposed_change`. An LLM rationale is appended when the key for `LLM_PROVIDER` (`OPENAI_API_KEY` or `ANTHROPIC_API_KEY`) is set.
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

A strategy trades only after you approve one exact version for paper or live. Each approval freezes a copy of that version and gives it its own Trade Crew. The crew decides only through the strategy's own rules, checked in code. An LLM never opens or closes a trade; with the veto explicitly enabled for a deployment, it can at most skip or shrink an entry the rules approved.

The live plumbing is broker-agnostic. The user picks a broker per account from a registry, and every live safeguard below runs against any broker that implements the adapter interface. Two brokers ship: **Paper**, with instant fills on replayed data, and **Sandbox**, a simulated broker with quotes, asynchronous and partial fills, rejections, margin and stop orders. No real broker (Zerodha, Upstox, Dhan, Angel One, ...) is included yet; adding one is a single module plus a registry entry (see [Adding a broker adapter](#adding-a-broker-adapter)).

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

Supported: entry fields `iv_rank`, `dte`, `spot`, `vix`, `pct_from_prev_high` and `pct_from_prev_low` (spot against the previous trading day's high or low, in %: above 0 broke the high, below 0 broke the low), `pct_high_from_prev_high` and `pct_low_from_prev_low` (the day's own high or low against the previous day's, in %: above 0 swept the high, below 0 swept the low) with `>=`, `<=`, `>`, `<`, `==`; exit `dte_at_or_below` with any structure.

- **Option selling:** `iron_condor`, `short_strangle`, `short_straddle`, sized by `short_distance` (spot to the short strikes) and `wing_width` (short to long strikes). Exits `profit_target_pct_of_credit` and `stop_loss_pct_of_credit` are both required, as % of the premium received.
- **Option buying:** `long_call`, `long_put`, `bull_call_spread`, `bear_put_spread`. `strike_offset` (optional, default 0) is the distance from the at-the-money strike to the bought one, positive meaning out of the money; spreads also sell `wing_width` points further out. Exits `profit_target_pct_of_debit` and `stop_loss_pct_of_debit` are both required, as % of the premium paid; the stop cannot exceed 100. A spread's target is capped at what the spread can pay. Example: `instrument: {underlying: NIFTY, structure: long_call}` with `entry: [{field: pct_from_prev_high, op: '>', value: 0}, ...]` and `exit: [{kind: profit_target_pct_of_debit, value: 80}, {kind: stop_loss_pct_of_debit, value: 40}]`.
- **Hard stop as % of capital (any structure):** `stop_loss_pct_of_capital` (above 0, at most 20) with `profit_target_r_multiple` (above 0) replaces the premium target/stop pair; the two go together and cannot be mixed with the `*_pct_of_credit` / `*_pct_of_debit` kinds. At entry the stop is capital x % for the whole trade (the deployment's capital in force live, `DEFAULT_CAPITAL_INR` in backtests), capped at the premium paid on a bought position, and the target is R x the stop, capped at what a spread or sold position can pay. Both are stored in rupees with the trade and do not move when capital changes later; a partial close scales them to the lots still open. Example: `exit: [{kind: stop_loss_pct_of_capital, value: 3}, {kind: profit_target_r_multiple, value: 3}]` on ₹50,000 closes at a ₹1,500 loss or a ₹4,500 profit. Checks run on daily closes in backtests, so a gap day can still lose more than the stop.
- **Liquidity sweep:** a level swept during the day with the close back inside it. Short: `pct_high_from_prev_high > 0` and `pct_from_prev_high < 0`. Long: `pct_low_from_prev_low < 0` and `pct_from_prev_low > 0`. The day's high and low come from the index bar in backtests and from the broker's index quote live (a broker that does not send them fails these checks).

The previous day's range comes from the imported index history (`scripts/import_nse_eod.py --history-from`), both in backtests and live; without it the `pct_from_prev_*` checks fail and no entry is made. Optional risk keys: `max_entries_per_day`; `max_consecutive_losses` with `cooldown_days` (after that many losing trades in a row, no entries that day or for the next `cooldown_days` trading days); `max_loss_pct_of_capital` (one trade's maximum loss as a % of capital, so `100` forbids risking more than the account); `max_losses_per_day` (no new entries once that many trades have closed at a loss today, so `1` means no re-entry after a loss); `min_minutes_between_trades` (minutes since the latest entry or exit, up to 375; backtests work on daily closes, so there it only blocks a second trade on the same day).

The same rules can be set once for every strategy in `.env`: `MAX_ENTRIES_PER_DAY`, `MAX_CONSECUTIVE_LOSSES` with `LOSS_COOLDOWN_DAYS`, `MAX_LOSS_PCT_OF_CAPITAL`, `MAX_LOSSES_PER_DAY` and `MIN_MINUTES_BETWEEN_TRADES` (0 turns each off). They apply to backtests, reviews and the Trade Crew, existing strategies included, without changing any strategy's rules or hash. When a strategy also sets one, the stricter value wins: fewer entries, a shorter losing streak, a longer cooldown, a smaller loss cap, fewer losses a day, a longer gap between trades. Reports record the values in `settings_snapshot.common_risk`. Unknown fields are validation errors, never skipped. The extractor emits this block but never invents thresholds; anything it cannot express goes into Gaps. The demo strategy deliberately lacks `short_distance`, so it cannot be approved.

### Approval

```
GET  /api/live/approvals/preview?strategy_id=...&version=1[&mode=live&broker_account_id=...]
                                 # rules, rules_sha256, blockers (with mode=live: the live gates too), verdict, warnings
POST /api/live/broker-accounts   {"broker": "paper", "account_ref_masked": "PAPER-1", "mode": "paper"}
POST /api/live/approvals         {"strategy_id", "version", "mode": "paper"|"live", "broker_account_id",
                                  "confirm_rules_sha256",
                                  "risk_limits": {"capital_inr", "max_lots", "max_daily_loss_pct", "drawdown", "policy"},
                                  "sebi_algo_id", "acknowledge_verdict"}
POST /api/live/approvals/backtest {"strategy_id", "version", "risk_limits"}   # rules only vs with policy and ladder
```

Approval is refused unless the rules parse, Gaps is empty, `confirm_rules_sha256` matches the preview (proving you approved exactly these rules), a completed report exists for the version (a verdict other than `Profitable` needs `acknowledge_verdict: true`), `X-Actor` is set, the account mode matches, and no other deployment of the strategy is active in that mode.

A **live** approval also needs an API token (see [API authentication](#api-authentication)) and passes the live gates, which the preview lists as blockers:

- `LIVE_TRADING_ENABLED=true`.
- The account's broker is registered with `live` in its modes, and every required credential resolves (database or `.env`).
- The strategy's structure is in `LIVE_ALLOWED_STRUCTURES` (default `iron_condor,long_call,long_put,bull_call_spread,bear_put_spread`: defined risk only). Allowing `short_strangle` or `short_straddle` means changing that setting; the allowed list in force is written to the approval's audit event.
- With `LIVE_MIN_PAPER_DAYS=N`, the same strategy version has at least N trading days (IST) of Trade Crew ticks on the **broker feed** in paper deployments that were never halted.

**Backtest warning:** report verdicts come from four fixed parameter sets and a synthetic same-day outcome. They do not test the strategy's own `rules:` block, so a `Profitable` verdict is not evidence that the rules make money.

Approval writes the frozen snapshot (`deployed_strategies`), the `Deployment` and an `approved` event to the live database in one transaction, then a `strategy.approved_for_live` event to the research audit log. If the second write fails, find and repair the gap with:

```bash
uv run python -m app.live reconcile [--fix]
```

A newer strategy version never replaces a running deployment: stop it and approve the new version.

### Start, pause, stop (kill switch)

```
POST /api/live/deployments/{id}/start   {"reason": "..."}   # needs X-Actor (live: an API token); from approved or paused
POST /api/live/deployments/{id}/pause                       # from started
POST /api/live/deployments/{id}/stop    {"reason", "flatten": false}   # kill switch; final
```

The crew trades only while the latest event is `started`. `stopped` and `halted` are final. The crew halts itself if the snapshot's hashes no longer match, an entry or exit fails, positions at the broker differ from the live database, or a tick raises an error.

**Kill switch.** `stop` records the stop first, so the crew sends nothing new, then cancels the deployment's working orders at the broker. Protective stops stay in place, because they still guard any open position. With `"flatten": true` (API token required), it also closes every open position at limit prices re-priced toward the market within `MAX_SLIPPAGE_BPS`, booking each as `exit_reason = kill_switch`, then cancels the remaining stops. Flatten also works on a deployment that is already halted or stopped. If cancelling or flattening fails, the deployment stays stopped, the response's `kill_switch.error` says what failed, and an alert is sent. A halt also cancels working orders, but leaves protective stops in place. The `/deployments` page has a "Stop and flatten" button.

### Running the Trade Crew

```bash
uv run python -m app.agents.trade_crew run --deployment ID [--feed replay|broker] [--interval 60] [--max-ticks N] [--explain]
uv run python -m app.live supervise [--feed broker] [--interval 60]   # one crew per started deployment, see below
```

Run one process per deployment, separate from the API, or let the supervisor do it. Every write is stamped `trade_crew/<deployment_id>`. The adapter comes from the account's broker in the registry (`app/brokers/registry.py`), and the market data comes from the feed:

- **`--feed replay`** (default for paper) replays bars one per tick with synthetic option quotes: imported NSE data, or `data/sample_nifty_options.csv`. It is refused for live deployments.
- **`--feed broker`** (default for live) builds each snapshot from the broker's own quotes (`QuoteSource`). It covers one expiry's chain plus every held contract and prices each option at the bid/ask mid. The expiry is chosen as in backtests: the nearest one inside the strategy's `dte` entry window, else the nearest one after today. A spread wider than `MAX_SPREAD_PCT` of the mid marks the quote stale, and an entry with any stale leg is blocked (`market.liquidity`); backtests apply the same check to contracts that did not trade that day. The previous day's high and low come from the market database. Lot size and freeze quantity come from the broker's instrument list. IV rank is today's India VIX quote ranked against the VIX history in the market database. A paper account on a broker with quotes (`sandbox`, or a real broker in paper mode) on this feed is "paper trading on real data": the first stage before live.

`PaperBroker` fills LIMIT orders immediately at the quote plus `SLIPPAGE_BPS`, with charges from the cost model. It has no quotes, so it runs on the replay feed only.

At startup, an adapter that keeps positions (anything but paper) is **reconciled**: its positions must equal the open legs of every deployment on the account in the live database. Otherwise the deployment is halted and the crew refuses to start.

Each tick:

1. **Verify:** recompute the frozen snapshot's `content_sha256` and `rules_sha256`; a mismatch halts the deployment.
2. **Gate:** do nothing unless the deployment is `started`.
3. **Market hours** (broker feed only): outside 09:15 to 15:30 IST (`MARKET_OPEN`, `MARKET_CLOSE`), on weekends and on holidays listed in `data/holidays/nse.yaml`, the tick ends with `market_closed`. Copy the dates from NSE's annual holiday circular; an unlisted holiday only shows up as stale quotes.
4. **Observe:** take a market snapshot. One older than `MAX_SNAPSHOT_AGE_SECONDS` stops the tick.
5. **Reconcile** (adapters that keep positions): record the status and fills of protective stops, then compare broker positions with the live database. Any difference, such as a manual trade in the account, halts the deployment with the diff and sends an alert. A protective stop that filled closes what is left of its position and books a `LiveTrade` with `exit_reason = broker_stop`. The deployment then pauses, since the crew's own stop did not act first.
6. **Exits:** check each open position against the exit rules, then (only if none fired) the policy's booking and time exits. A trigger writes a close intent, sends orders and records a `LiveTrade` with gross, estimated and broker-reported costs, slippage against quotes, net and outcome. A partial booking records a `LiveTrade` with `partial = true` for the lots it closed; the rest stays open. Positions still open are written to `position_snapshots` with their marked P&L and peak profit.
7. **Drawdown:** measure drawdown from peak equity and apply the deployment's drawdown ladder, then its capital floor (see below). Reaching a pause level, or falling below the floor, pauses the deployment, sends an alert and ends the tick.
8. **Daily target:** if the policy has a daily profit target and today's P&L (realized plus open, since 00:00 UTC) has reached it, block new entries or close everything, as configured. A withdrawal rule with money due sends its alert here.
9. **Entry:** skipped while the deployment holds its maximum open positions. Otherwise every entry condition must pass. Policy sizing picks the lots, then a `reduce_size` drawdown level scales them down.
10. **Risk:** lots, risk-reward, open positions and daily loss (against capital in force) for the deployment, `PORTFOLIO_MAX_OPEN_POSITIONS` and `PORTFOLIO_MAX_DAILY_LOSS_INR` across all active deployments, `drawdown.entries_allowed`, `drawdown.floor` and `drawdown.floor_buffer`, `policy.sizing` and `policy.daily_profit_target`, `odds.sample` and `odds.expectancy` (the odds gate), and the strategy's optional rules (entries and losses per day, the losing-streak cooldown, the gap between trades, the loss cap). It also runs **`risk.margin`** when the broker reports margin: the broker's available margin must be at least the basket's margin (hedge benefit included) times `MARGIN_BUFFER` (1.2). A broker that cannot answer fails the check; paper skips it. A shortfall records a `blocked` intent with no orders.
11. **Opinion (optional):** see below. Only for entries the rules approved.
12. **Explain (optional):** with `--explain` or `TRADE_CREW_EXPLAIN=true` and an LLM key, an LLM appends a note to `reason_text` after the decision is made. It is never an input.
13. **Execute:** if every check passed, a `TradeIntent` with `approval = auto`, and its orders go through the order lifecycle below. If a risk check failed, a `TradeIntent` with `approval = blocked` naming the failed rules, and no orders. A veto writes `approval = vetoed` and no orders. No entry signal writes nothing.

After every tick the crew writes a heartbeat (`crew_heartbeats`: time, feed, outcome, pid). `/api/live/deployments` reports `crew_running`, which is false when a started deployment has no heartbeat newer than `HEARTBEAT_MAX_AGE_S`, and `/deployments` shows "crew not running".

Lot sizes and strike steps come from `LOT_SIZES` and `STRIKE_STEPS` (e.g. `NIFTY:75,BANKNIFTY:35`) unless the rules set `lot_size`.

### Order lifecycle

All orders go through `app/trading/orders.py`, for paper and live alike (`PaperBroker`'s instant fills simply pass straight through):

- **Leg order:** hedges first. An entry buys its wings before selling, and an exit buys back the shorts before selling the wings, so no moment leaves a naked short.
- **Waiting:** each order is polled (`get_order`) every `ORDER_POLL_INTERVAL_S` until it is complete, rejected or cancelled, for at most `ORDER_FILL_TIMEOUT_S`. Every status change and fill is recorded.
- **Re-pricing:** an order still open at the timeout is cancelled. Its unfilled remainder is re-sent up to `ORDER_REPRICE_STEPS` times, each step moving the limit toward the market, never more than `MAX_SLIPPAGE_BPS` from the quote. A leg still unfilled after that is an error.
- **Failed entry:** if any leg fails, the legs already filled are unwound with a `close` intent (`exit_reason = unwind`), then the deployment halts and an alert is sent.
- **Failed exit:** a rejected exit order is retried `ORDER_EXIT_RETRIES` times, then the deployment halts with "position still open" and an alert. It never leaves a half-closed position silently.
- **Limits:** orders above the instrument's freeze quantity (from the broker, else `FREEZE_QUANTITIES`) are split into whole lots. An order above `MAX_ORDER_VALUE_INR` (0 = off) is refused without being sent. At most `MAX_ORDERS_PER_MINUTE` orders are sent; SEBI's retail-algo rules cap order rates.

### Protective stops at the broker

With `PROTECTIVE_STOPS=live` (default; `always` also on paper, `off` never), every filled entry gets a **stop-limit (`SL`) BUY at the broker on each short leg**. It is a disaster backstop for when the crew is down or the market gaps, not the strategy's own exit:

- **Trigger:** the leg's entry premium times (1 + `PROTECTIVE_STOP_MULTIPLIER` (1.5) × the strategy's `stop_loss_pct_of_credit`). For a 100% stop, that is 2.5× the premium, so the crew's own exit normally fires first.
- **Limit:** the trigger plus `PROTECTIVE_STOP_LIMIT_BUFFER_PCT` (5%). NSE does not allow `SL-M` on index options.
- **Records:** stops live under a `protect` intent that names the position (`risk_checks.protects`).
- **Exits:** before any normal exit, the crew cancels the position's stops. After a partial booking it places them again for the rest.
- **Fired stop:** a stop that fired is handled by reconciliation (step 5): the trade is booked as `broker_stop`, the deployment pauses and an alert is sent.
- **Failed placement:** if a stop cannot be placed, the entry is unwound at once and the deployment halts.

### Brokers and credentials

```
GET    /api/live/brokers                                 # registered brokers: modes, has_quotes, credential fields, configured (.env)
GET    /api/live/broker-accounts                         # each with registered, configured and per-field source
POST   /api/live/broker-accounts   {"broker", "mode", "account_ref_masked", "default_algo_id"}   # live: API token
PUT    /api/live/broker-accounts/{id}/credentials   {"api_key": "...", "access_token": "..."}   # API token
GET    /api/live/broker-accounts/{id}/credentials        # field, source (db|env), fingerprint, updated_at; never values
DELETE /api/live/broker-accounts/{id}/credentials[?field=access_token]                         # revoke; API token
```

Creating an account is refused for a broker that is not registered or does not support the requested mode. Sandbox supports `live` only with `SANDBOX_ALLOW_LIVE=true`, which lets you exercise the live gates end to end with no money at risk.

Each credential field is looked up in the database first, then in the environment:

- **Database:** the latest row for the field in `broker_credentials`. Rows are append-only: saving again rotates the value, and a revoked row removes it. Values are encrypted with Fernet under `BROKER_CREDENTIALS_KEY`. Generate a key with `uv run python -c "from app.brokers.credentials import generate_key; print(generate_key())"`. Without the key, saving returns 400 and stored values are ignored.
- **Environment:** `BROKER_<BROKER>_<FIELD>`, for example `BROKER_ZERODHA_API_KEY`, from the process environment or `.env`.

API responses and audit events carry only the field name and a 12-character sha256 fingerprint, never the value. Most Indian brokers need a fresh access token every day. Paste it each morning with the `PUT` above (only the changed field) or put it in `.env`; automated login is out of scope. The deploy page (`/deploy`) has a broker picker, a mode selector, the account reference and masked inputs for each field, which show "saved" or "from .env". Live approval stays disabled while the account is not configured.

### API authentication

`API_TOKENS` in `.env` lists `name:sha256(token)` pairs, separated by commas. Only the hashes live there. Create a token with:

```bash
uv run python -m app.auth new-token alice   # prints the token once, and the API_TOKENS entry
```

Clients send `Authorization: Bearer <token>`. A valid token makes its name the audit actor, and `X-Actor` is then ignored. An invalid token gets 401. When `API_TOKENS` is set, every mutating request and every `/api/live` read needs a token, and `CORS_ORIGINS='*'` is refused at startup. When `API_TOKENS` is empty, research and paper flows stay open for local development, with `X-Actor` naming the actor.

These actions always need a token, even with `API_TOKENS` empty; without it they return 403 "set API_TOKENS":

- broker credentials (all three methods)
- creating a live account
- approving, starting, the LLM veto and capital events on a live deployment
- `stop` with `flatten`

The frontend stores the token in the browser's `localStorage`; enter it on `/deploy`. Static tokens suit one user or a small team on a private network; they are not a multi-user login. Expose the API publicly only behind TLS.

### Supervisor and alerts

```bash
uv run python -m app.live supervise --feed broker --interval 60
```

The supervisor starts one Trade Crew subprocess per `started` deployment. It restarts a crashed crew with exponential backoff, from 5 seconds up to 5 minutes, and the restarted crew reconciles before its first tick. It terminates the crew when the deployment is paused, stopped or halted. It also ends floor breaks (see the capital floor below): a deployment paused by its floor with `resume_after_days` is started again by the supervisor, as `system:floor_break`, so breaks only end while it runs. A crew that exits cleanly, because its replay feed ran out, is not restarted. Crew output goes to `DATA_DIR/logs/crew-<deployment>.log`. Service templates are in `deploy/`: `com.genesis.supervisor.plist` for launchd (macOS) and `genesis-supervisor.service` for systemd (Linux).

Alerts (`app/alerts.py`) go to Telegram (`ALERT_TELEGRAM_BOT_TOKEN`, `ALERT_TELEGRAM_CHAT_ID`) and/or email (`ALERT_SMTP_HOST`, `ALERT_SMTP_PORT`, `ALERT_SMTP_USER`, `ALERT_SMTP_PASSWORD`, `ALERT_SMTP_FROM`, `ALERT_SMTP_TO`). They are sent on:

- a halt, a drawdown or floor pause, a reconciliation mismatch or a fired protective stop
- a floor raised, a floor break that ended (`resumed`) or that needs a deposit first (`floor_top_up_needed`), and a withdrawal due (`withdrawal_due`)
- no tick for `HEARTBEAT_MAX_AGE_S` (sent by the supervisor)
- three crashes within an hour
- a kill switch that could not cancel or flatten

Every alert is also logged. A failed delivery is logged and never blocks trading.

### Going live

Live trading needs a registered live-capable broker adapter; none ships yet. With one in place:

1. Import NSE data (`scripts/import_nse_eod.py`) and the holiday list (`data/holidays/nse.yaml`), then re-run the approval backtest on real history.
2. Paper-trade on the **broker feed** for 4 to 6 weeks: a paper account on the real broker (or Sandbox), run with `--feed broker` under the supervisor. Set `LIVE_MIN_PAPER_DAYS` (for example 20) so live approval enforces this.
3. Set `API_TOKENS`, `BROKER_CREDENTIALS_KEY` and the alert channels, and check that a test alert arrives (for example, halt a paper deployment).
4. Go live with **1 lot**, a tight drawdown ladder, `LLM_TRADE_OPINION=off`, `PROTECTIVE_STOPS=live` and a **dedicated broker account** (reconciliation halts on any trade it did not make). Set `LIVE_TRADING_ENABLED=true`, create the live account, save the day's credentials, and approve with your SEBI algo id.
5. Watch the first sessions on `/deployments`, and practise the kill switch ("Stop and flatten") on paper first.
6. Scale only after live results match paper: fills, slippage and costs, not just P&L.

### Adding a broker adapter

One module under `app/brokers/` plus a `register(BrokerSpec(...))` in `app/brokers/registry.py`, with `name`, `label`, `modes`, `has_quotes`, `credential_fields` (and which of them are secret or required) and a factory `(credentials, settings, profile, clock, account) -> adapter`. The adapter must implement:

- **`BrokerAdapter`:** `place_order` (LIMIT and stop-limit `SL` orders with a trigger price, tagged with the SEBI algo id), `cancel_order`, `order_updates`, `fills`, `positions` (net quantity per tradingsymbol) and `charges` (from contract notes where possible).
- **`LiveBrokerAdapter`**, required for live: `reconciles = True`, `get_order` for polling, `margin_required(orders)` (basket margin with hedge benefit) and `available_margin()`.
- **`QuoteSource`**, for the broker feed: `instruments(underlying)`, with lot size, tick size and freeze quantity; `quotes(symbols)`, with bid, ask and last; `underlying_quote`; and `vix_quote`.
- **Mapping and statuses:** map tradingsymbols between the broker and this app's format (`NIFTY26JAN1923000CE`), and map the broker's order statuses onto `OPEN`, `TRIGGER_PENDING`, `COMPLETE`, `REJECTED` and `CANCELLED`.

`app/brokers/sandbox.py` is a complete reference implementation, and `tests/test_live_plumbing.py` shows the behaviour the crew expects.

### Known limitations

- Protective stops are per-leg backstops set wider than the strategy's own stop, not a combined stop on the whole spread. A gap can jump past the limit price and leave the stop unfilled. The crew's own exit then closes the position on its next tick, if it is running.
- SQLite allows one writer at a time, which slows several deployments running at once. Use Postgres for many deployments.
- The credentials encryption key sits on the same host as the database.
- Reconciliation halts on any manual trade in the account, so each live deployment needs a dedicated account.
- The Sandbox broker reflects only our own assumptions about how a real broker behaves.
- SEBI retail-algo rules beyond the algo id (static IP, exchange approval of the strategy) are not enforced.
- Daily broker login (OAuth/TOTP) is manual: paste the access token.

### Equity, capital and drawdown

**Equity** = capital in force + realized net P&L + unrealized P&L of open positions (marked at quotes in the latest position snapshot, before exit costs). Capital in force is the approved `capital_inr` plus deposits minus withdrawals. A capital event moves equity and peak by the same amount, so it never looks like a gain or a drawdown.

```
POST /api/live/deployments/{id}/capital-events   {"kind": "deposit"|"withdrawal", "amount_inr", "note"}   # needs X-Actor
GET  /api/live/deployments/{id}/capital-events   # events and capital in force
GET  /api/live/equity?deployment_id=|mode=paper&basis=mtm|realized&curve=true
GET  /api/live/positions?deployment_id=|mode=paper   # open positions with their latest marks
GET  /api/live/deployments/{id}/drawdown          # ladder in force, current level, level history
GET  /api/live/deployments/{id}/money             # floor in force, buffer left, any break; withdrawal due
GET  /api/live/deployments/{id}/odds              # odds gate: past trades per VIX band, entries open or not
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

A **capital floor** is a fixed rupee line under equity, set in the same block (a ladder is then optional):

```yaml
drawdown:
  basis: mtm
  floor:
    floor_inr: 50000                                   # below capital_inr
    raise_once: {at_equity_inr: 60000, to_inr: 55000}  # optional; at_equity_inr above capital_inr
    close_open_positions: true                         # default
    resume_after_days: 5                               # optional break, in trading days
```

An entry needs equity above the floor and the trade's max loss within the buffer left (equity minus floor), or it is `blocked` with `drawdown.floor` or `drawdown.floor_buffer` failed. Equity below the floor (at it only blocks entries) pauses the deployment, closing open positions unless told not to, and writes a `floor_paused` deployment event. `raise_once` lifts the floor a single time, once equity reaches `at_equity_inr` (`floor_raised`). With `resume_after_days` the pause is a break: after that many trading days the supervisor starts the deployment again if equity is above `floor_inr`, with the floor back at `floor_inr` and the raise available again; otherwise it alerts once (`floor_top_up_needed`) and starts it after a deposit brings equity above the floor. A person starting or pausing the deployment during the break ends the automatic restart. These events are notices: they never change the deployment's status. Equity is on the ladder's `basis`; capital events move it like any equity.

The policy backtest restarts after a floor break with the account topped up to `capital_inr`, and reports floor pauses, restarts and the top-ups; without `resume_after_days` a floor pause is final there too.

### Trade management policy

`risk_limits.policy` sets sizing, profit booking, time exits, a daily profit target, VIX sizing, the withdrawal rule and the odds gate. Like the ladder, it is validated against the strategy's rules and frozen at approval; to change it, stop the deployment and approve again. A policy can only make trading more careful: it never trades more than the approved `max_lots` and never removes the strategy's own exits. No LLM is involved.

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
  vix_sizing: {bands: [{at_or_above: 18, size_factor: 0.5}, {at_or_above: 25, size_factor: 0.25}]}
  withdrawal: {first_target_inr: 60000, first_amount_inr: 5000, then_share_pct: 70}
  odds_gate: {vix_bands: [13, 16], min_trades: 20, min_expectancy_inr: 0, lookback_years: 5}
```

- **Odds gate.** Trade only when the odds favour you. Before each entry, the gate looks up this strategy's own past trades in today's India VIX band (`vix_bands` are rising edges, up to 4: `[13, 16]` gives below 13, 13 to 16, and 16 or more). The past trades come from a rules-only backtest of the same rules over the imported market data (no policy, no ladder), per lot and after costs. Only trades that closed before today count, so a backtest never sees the future. The entry passes only if the band has at least `min_trades` trades (`odds.sample`, 5 to 500) and their average net per lot is above `min_expectancy_inr` (`odds.expectancy`); `lookback_years` drops older trades. With no VIX reading both checks fail. The entry's reason text gives the band, trade count, win rate against the break-even win rate (average loss ÷ (average win + average loss)) and average net per lot. Live, the history is rebuilt on the first tick of each market day and kept in memory; `GET /api/live/deployments/{id}/odds` returns every band and whether the band of the latest imported VIX is open. The policy backtest builds the same history, counts blocked signals (`odds_blocked`) and returns the per-band table. Early in the history every band is blocked until it has `min_trades` trades, so the first year or so trades little or not at all. The gate only filters entries: it does not change trade size, so a capital floor's buffer can still block trades it allows.

- **Withdrawal rule.** On realized equity: `first_amount_inr` is due once equity reaches `first_target_inr` (above `capital_inr`); after that, `then_share_pct` of each rise above the equity left just after the latest recorded withdrawal (omit it for a single withdrawal). A withdrawal never takes equity below the floor in force. Live, nothing moves money: the crew alerts once per withdrawal period (`withdrawal_due`), `/deployments` shows the amount due, and you record the withdrawal as a capital event once the money is out. Backtests take it at the close of the first day with no open position and report `withdrawn_inr`, `topped_up_inr` and the difference; withdrawals lower capital and peak together, so they are not drawdown.

- **VIX sizing.** The highest band at or below today's India VIX multiplies lots by its `size_factor`, together with any `reduce_size` drawdown level, never below one lot. Bands must rise in VIX and fall in factor (up to 5). With no VIX reading the last band applies. Backtests use the imported VIX close; live runs use the broker's VIX quote.
- **Sizing.** `fixed` trades `lots` (default `max_lots`). `risk_pct` and `risk_inr` trade as many lots as keep the trade's max loss within the budget, capped at `max_lots`; below `min_lots` the entry is `blocked` with `policy.sizing` failed. `risk_pct` is of capital in force. With `compounding`, `profit_share_pct` of realized net profit is added to that capital, up to `ceiling_inr`; a net loss leaves it at capital in force. A `reduce_size` drawdown level then scales the result.
- **Booking.** Percentages are of the credit received, or of the premium paid for option-buying structures (the `_pct_of_credit` key names are kept for both). Every threshold must sit below the strategy's profit target, or approval is refused. A partial level closes `close_pct` of the lots opened (at least one lot, always leaving one open). `lock_in` closes the rest once profit falls back to `lock_pct_of_credit` after reaching `after_pct_of_credit`. `trailing` closes once profit gives back `giveback_pct` of its peak.
- **Time exits.** Close the whole position `days_before_expiry` days before expiry (from `at`, IST, if set), or `event_days_before` days before a scheduled event of the listed kinds.
- Order each tick: the strategy's exits first, then time, event, lock-in, trailing, partial.

`POST /api/live/approvals/backtest` replays the strategy's rules over all available history twice, rules only and with the policy (odds gate included) and ladder, and returns both summaries and equity curves. It uses end-of-day closes, so a time of day is ignored and a ladder pause stops entries for the rest of the history. The frontend's deploy page (`/deploy`) builds the policy in a form, runs this comparison and approves; `/deployments` shows equity, positions, the ladder and capital events.

### LLM trade opinions: off, shadow, veto

`LLM_TRADE_OPINION` (default `off`) lets an LLM review entries the rules already approved. It sees the proposal, every check, the market snapshot (including VIX, the day's range and the previous day's high and low), upcoming events, recent headlines and the deployment's recent lessons. It also gets the strategy's write-up (thesis plus the Entry, Exit and Risk bullets) as a checklist, and lists any item the data shows unmet, or cannot verify, as `checklist: ...` risk flags: this covers the parts of a video's method that the `rules:` block cannot express. It uses `LLM_PROVIDER` (OpenAI or Claude). It can answer take, reduce (fewer lots) or skip; it can never add lots, unblock a trade, or touch exits. A timeout or error is recorded and the rules decide.

- **shadow:** the opinion is stored in `trade_opinions` with the prompt hash, context, model and latency. Trading is unchanged.
- **Lessons:** while opinions are on, each closed trade gets a short LLM post-trade review, stored as a `reviewer_type = agent` review (`reviewer = llm:<model>`). Its lesson is fed into later opinions. Lessons never change rules; that still goes through improvements and human approval.
- **Evaluation:** `GET /api/live/opinions/evaluation?deployment_id=` joins each opinion to what happened. It compares net P&L, win rate and drawdown by decision, and builds an equity curve of the rules against one that follows the opinion. It also gives a bootstrap 95% confidence interval for the difference. There is no verdict until `OPINION_EVAL_MIN_SAMPLES` (30) skip or reduce opinions have closed.
- **veto:** set `LLM_TRADE_OPINION=veto`, then enable it per deployment with `POST /api/live/deployments/{id}/llm-veto {"enable": true}`. This needs `X-Actor` and a `veto_helps` verdict, which is stored as evidence in the audit log. A skip writes a `vetoed` intent with no orders. A reduction re-runs the risk checks on fewer lots. The lots the LLM removed are tracked as phantom positions until an exit rule fires (`phantom_trades`), so the evaluation keeps working. If the last `OPINION_EVAL_WINDOW` outcomes show the opinion losing money, the crew switches back to shadow (`llm_veto_disabled` by `system:veto_auto_revert`). `{"enable": false}` turns it off by hand.
- Live accounts are refused unless `LLM_VETO_LIVE_ALLOWED=true`. Keep it off until the SEBI classification of LLM-assisted algos is confirmed.

### Reading live data

`/api/live/deployments`, `/api/live/deployments/{id}`, `/api/live/deployments/{id}/intents`, `/api/live/orders/{id}`, `/api/live/live-trades`, `/api/live/opinions`, `/api/live/opinions/evaluation`, `/api/live/phantom-trades`, `/api/live/audit` and `/api/live/audit/verify` (the live database's own hash chain). They return 503 until the live database is migrated. `app/live/store.py` is the only write path and audits every row; `app/brokers/base.py` defines the `BrokerAdapter`, `LiveBrokerAdapter` and `QuoteSource` protocols a real broker must implement.

### Live trading settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `LIVE_TRADING_ENABLED` | `false` | Master switch for live deployments |
| `LIVE_ALLOWED_STRUCTURES` | `iron_condor,long_call,long_put,bull_call_spread,bear_put_spread` | Structures allowed live, comma-separated |
| `LIVE_MIN_PAPER_DAYS` | `0` | Broker-feed paper days required before a live approval |
| `SANDBOX_ALLOW_LIVE` | `false` | Lets the Sandbox broker have live accounts |
| `SANDBOX_STATE_DIR` | `DATA_DIR/sandbox` | Sandbox orders and positions, one JSON file per account |
| `BROKER_CREDENTIALS_KEY` | — | Fernet key for stored credentials |
| `BROKER_<BROKER>_<FIELD>` | — | Credential fallback, e.g. `BROKER_ZERODHA_ACCESS_TOKEN` |
| `API_TOKENS` | — | `name:sha256` pairs, comma-separated |
| `MAX_SPREAD_PCT` | `5` | Wider bid/ask spreads mark a quote stale, which blocks entries on that leg |
| `ORDER_FILL_TIMEOUT_S` / `ORDER_POLL_INTERVAL_S` | `30` / `1` | How long to wait for a fill, and how often to poll |
| `ORDER_REPRICE_STEPS` / `MAX_SLIPPAGE_BPS` | `2` / `100` | Re-prices toward the market, and how far from the quote |
| `ORDER_EXIT_RETRIES` | `2` | Retries for a rejected exit order before halting |
| `MAX_ORDER_VALUE_INR` | `0` (off) | Largest single order |
| `MAX_ORDERS_PER_MINUTE` | `120` | Order rate cap |
| `FREEZE_QUANTITIES` | `NIFTY:1800,BANKNIFTY:900` | Fallback when the broker gives none |
| `MARKET_OPEN` / `MARKET_CLOSE` | `09:15` / `15:30` | NSE session, IST |
| `PROTECTIVE_STOPS` | `live` | `off`, `live` or `always` |
| `PROTECTIVE_STOP_MULTIPLIER` / `PROTECTIVE_STOP_LIMIT_BUFFER_PCT` | `1.5` / `5` | Stop trigger distance and limit buffer |
| `MARGIN_BUFFER` | `1.2` | Available margin must exceed required × this |
| `HEARTBEAT_MAX_AGE_S` | `180` | A started crew without a tick this long is "not running" |
| `ALERT_TELEGRAM_*`, `ALERT_SMTP_*` | — | Alert channels |

## Tests

```bash
uv run pytest -q
```

Tests run against temporary research and live SQLite files migrated with Alembic, so the triggers are exercised. They cover versioning, immutability (ORM and raw SQL), both audit chains, trade reconciliation, reviews and improvements, live-schema constraints, the journal, the API, the import script, migration drift, strategy rules, every approval refusal, and the Trade Crew (entry, profit-target and stop-loss exits, blocked intents, tampered snapshots, the kill switch, stale data, rejected orders, and identical decisions with or without the LLM explanation), position snapshots, capital events, equity and every drawdown ladder action, the capital floor (buffer check, raise, pause, break, top-up and restart by the supervisor), the withdrawal rule, the re-entry rules, the sweep entry fields and the odds gate (bands, break-even, no future trades, fail-safe, simulator, crew and endpoint), and the trade policy (validation, sizing and compounding, partial booking, trailing, lock-in, time and event exits, the daily target, the approval freeze, and the policy in the simulator and its backtest endpoint). They also cover the NSE importers, the day-by-day simulator and its parity with the Trade Crew, events and blackouts, news parsing and point-in-time context, and LLM opinions with a fake LLM: shadow mode never changes orders, lessons, the evaluation, the veto clamp, vetoed intents, phantom trades and the auto-revert. No test calls a real LLM.

`tests/test_live_plumbing.py` runs the live plumbing against the Sandbox broker:

- **Orders:** partial fill, then timeout, cancel and re-price; giving up after the re-price steps; freeze-quantity splits; the order-value cap; the rate limiter.
- **Failures:** a rejected leg unwinds the filled ones; exit retries, then a halt.
- **Market and margin:** reconciliation mismatches (at startup and per tick), market hours and holidays, a margin shortfall, heartbeats.
- **Protective stops:** placed, cancelled before an exit, fired as `broker_stop`, unfilled on a gap, and a failed placement.
- **Kill switch:** cancelling working orders and flattening.
- **Live gates and approval:** each live-gate refusal, the structure allow-list, `LIVE_MIN_PAPER_DAYS`.
- **Brokers and credentials:** the broker list and `configured`, rejected brokers and modes, and credential lookup order (database before `.env`, revoked values falling back, nothing secret in responses or audit events, no key means 400).
- **Auth:** tokens and the actor, 401 and 403, the CLI.
- **Supervisor and alerts:** restarts with backoff, missed heartbeats, alert delivery that never raises.

No test touches a real broker or network.
