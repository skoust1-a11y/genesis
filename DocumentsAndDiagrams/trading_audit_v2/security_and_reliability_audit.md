# Security and reliability audit

Audit date: 4 Nov 2026. `.env` was not opened. No secret values are recorded here.

## Secrets

`Settings` loads API keys from the environment (`.env` is gitignored). `app.auth` stores token hashes, not raw tokens. Broker credential routes in `api/live.py` call `require_auth`. That part is the right shape.

Untracked database files showed up in git status at the start of this session (`backend/genesis.db`, `backend/live.db`, `trades/trades.db`). Databases can hold strategy text and, if the live stack was used, order history. Do not commit them. This audit did not dump them.

Groww access is an unauthenticated HTTP GET of a public page. There is no broker key on that path, and there is also no order.

## Authentication gaps (V2-16)

`ensure_authenticated` is applied to live mutations (approve, transition, credentials, kill switch). It is not applied to:

- `POST /api/review/run` and ingest (`api/routes.py`)
- paper order placement (`api/market.py` `place_paper_order`)
- journal and notification writes

If the API is bound to a non-loopback interface, anyone who can reach it can start a review (LLM cost, if keys are set), write paper orders, and read reports. October G-15 said the same about `--host 0.0.0.0`. This session confirmed the routes are still without `require_auth`. It did not confirm the current bind address.

CORS refuses `*` when `API_TOKENS` is set (`main.py`). Good. Tokens empty means auth is off and that refusal does not apply.

## Reliability

| Risk | Evidence | Impact |
|---|---|---|
| Journal load failure wipes the book on the next save | `load_active_trades` returns `{}` on any exception | Lost open-trade state (V2-13) |
| Risk file load failure is ignored | `RiskAgent._load_config` bare except | Defaults, then `save_config` can replace the file |
| Sqlite errors swallowed | `record_entry` / `record_exit` debug log | JSON and DB diverge |
| No file lock on `daily_risk_config.json` | Plain read/write | Two writers, last write wins. A torn write fails the next load and hits the bare except |
| Post-market writes parameters from canned rules | `save_calibrated_parameters` | Silent change of the next session's stop buffer |
| Clock | `datetime.now()` without a zone | Session bounds and "today" follow the laptop |
| Daemon exception | Caught, sleep 5s, continue | Gap in exit checks |
| Fake fill | V2-01 | A future "turn on live" flag creates a lie, not a position. If a person hedges that lie by trading manually, the hedge is the position |

## Monitoring

What exists: Python logging, Telegram heartbeats once an hour when flat, a markdown daily audit, an audit-chain middleware on the API (`app/audit/`).

What the daily audit is not: an independent check. On 3 Nov it reported a healthy system and zero missed setups while the daemon's review call does not pass the day's bars. Alerts that repeat the system's own story will not catch V2-03 or V2-06.

No paging path was found for "process dead while a trade is open" beyond the absence of heartbeats. A human has to notice.

## Kill switch

Deployments can flatten through `kill_switch.stop`, and the trade-crew graph has an `exit.kill_switch` check. The scanner does not register a deployment, so the switch does not see scanner trades. Flattening a paper JSON row is not implemented as that switch.

## Backup

`trades/` and `data/*.db` are local files. `.gitignore` ignores `data/*.db*` and `trades/.*` (dotfiles such as `.active_trades_*.json`). A git clone does not restore the open book. That is correct for secrets and accidental commits. It means the only copy of an open paper trade may be one laptop.

## Security conclusion

For a single user on localhost, the token design on the live routes is adequate and the scanner has little to steal beyond Telegram chat ids in the environment. The reliability issues are the ones that lose track of a position or invent a fill. Fix those before any network exposure and before any broker key is placed in `.env` for this daemon.
