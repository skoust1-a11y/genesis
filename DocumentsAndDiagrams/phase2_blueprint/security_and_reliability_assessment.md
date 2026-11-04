# Security and reliability assessment

Date: 4 Nov 2026. `.env` was not opened. No secret values are in this file.

## Trading-safety defects first

| Item | Evidence | Effect |
|---|---|---|
| Fake `FILLED` | F-01 | A flag flip creates a position the exchange does not have |
| Empty journal load then save | F-13, `load_active_trades` | Corrupt JSON can be replaced and the open alert list disappears |
| Risk file load swallows errors | `RiskAgent._load_config` bare `except` | A torn file becomes defaults, and the next save writes those defaults |
| Sqlite errors at debug | `record_entry` / `record_exit` | JSON and `data/trades.db` diverge with no alert |
| No file lock | plain open/write on the risk JSON | Two processes, last write wins |
| Clock without zone | `datetime.now()` in the daemon | Session and "today" follow the laptop |
| Post-market on empty bars | `live_scanner.py` 283–287 | A report and a parameter write from the wrong inputs |

## Secrets and auth

Tokens are stored as hashes (`app.auth`). Broker credential routes use `require_auth`. Research ingest, review, and paper order routes in `api/routes.py` and `api/market.py` do not. If the API is bound off localhost, those routes are open. October said the same about `--host 0.0.0.0`. Bind address was not rechecked this session.

`.gitignore` ignores `.env` and `data/*.db*`. Untracked database files were in the git status at the start of the previous session (`backend/genesis.db`, `backend/live.db`). Do not commit them. This pass did not dump them.

Groww is an unauthenticated GET of a public page. No broker key, and no order, which is the right pairing.

## Reliability of the loop

An exception in the daemon loop is logged and the loop sleeps 5 seconds. Open paper trades are not flattened because there is no broker position. A human following Telegram is unmanaged during the gap and after a crash.

Kill switch exists for deployments (`live/kill_switch.py`) and does not see scanner JSON.

## Monitoring

Telegram and a markdown daily file repeat the system's own story. They cannot catch F-02, because F-02 is how the story is computed. A useful alert is: process down, risk file unreadable, chain fetch failed, unpriced exit, parameter file changed by something other than a person.

## Tests and CI

24 unit tests passed this session (execution crew paper path and money rules). They do not lock the defects above. No CI workflow.

## Changes that are justified

Loopback or auth on research and paper routes. Fail closed on unreadable risk and journal files. One IST clock helper. CI running the existing pytest suite plus the three new accounting tests in the acceptance matrix.

Not justified now: a new secrets manager, a service mesh, or deleting `vendor/TradingAgents`. That vendor tree is not on the daemon path. Removing it is optional housekeeping.
