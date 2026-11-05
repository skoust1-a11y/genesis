# S1-P7 acceptance: read-only crew research reporting, legacy integrity, authentication

Date: 6 Nov 2026. Baseline: P6 commit `5280009`. P8 is not started.

P7 is a read-only research view. Nothing in it migrates a journal, restarts a scanner, changes a LaunchAgent, sends Telegram, calls a broker or trades. No frozen V0 source or historical file changed.

Verdict: **CONDITIONAL PASS**. The conditions are listed in section 9.

## 1. Statistics policy (MIN_SAMPLE = 20, provisional)

`crew_reader.MIN_SAMPLE = 20` is a display floor. It is not a significance test.

| Rule | Where it is enforced | Test |
|---|---|---|
| Below 20 closed eligible trades, `win_rate_pct`, `avg_r` and `avg_points` are null and `label` is `INSUFFICIENT_SAMPLE` | `crew_reader._group` | 11, 11b (19 and then 20 trades) |
| At 20 or more, `label` is `DESCRIPTIVE_RESEARCH_NOT_VALIDATED` and `interpretation` says "Not validated profitability" | `crew_reader.stats`, export Stats sheet | 11b |
| Eligible count, denominators, exclusions and coverage are always returned | `population`, `exclusions`, `not_in_population`, `coverage` | 10, 11 |
| One group per `strategy_id`, for exactly one `strategy_version` and `execution_policy` | `crew_reader.stats` (version and policy are required) | 07, 08, 09, 11d |
| No reader or export combines strategy ids | API groups; export writes one row block per strategy id | 07, 17 |
| No monetary P&L | No premium, rupee, ROI or capital key in any crew response or export column | 12, 17 |

`research_runtime.finalized_results` used to add up every `scanner-v0` strategy (for example `Liquidity Sweep [5M]` and `Breakout [15M]`) into one total. It now returns `groups` keyed by strategy id and has no combined `trades`, `model_points` or `model_r` key. Test 25 adds a second scanner strategy under the same policy and asserts that the two groups stay separate and that no combined key exists. Its only callers are tests.

## 2. Legacy financial reporting (`/api/trades/forward-tested`)

### The None P&L failure

`trade_store.upsert_trade` stores a NULL `est_pnl_inr` and `roi_pct` for index-point trades (`pnl_method = index_points_not_priced`). `compute_metrics` summed these values with `sum()`, which raised `TypeError`. The route's `except Exception` then silently answered from `trades/live_and_forward_trades.xlsx`.

### What changed

- `compute_metrics`: a total is `None` when any of its rows lacks the value. It is never zero and never a partial sum. Wins and losses count only rows with an `r_multiple`. `metrics.missing_values` gives the number of rows missing each field. Profit factor is `None` if any P&L is missing. ROI on zero or unknown capital is `None` (it used to be `0.0`).
- The route adds `data_source`:
  - `sqlite`: rows came from the SQLite store.
  - `excel_fallback`: used only while the SQLite store has no rows (the original "not yet ready" case).
  - `none`: no source has rows.
  - `unavailable`: the SQLite read or computation raised. The response keeps the old shape (`metrics: {}`, `strategy_metrics: {}`, `trades: []`), adds `source_error` (the exception class) and does **not** read Excel.
- `source_warnings` lists anything left out, for example live active trades that could not be read.
- Live active rows no longer default to a 78.67 premium, 260 qty, 4 lots, an 11.34 risk or 1.0R. A value the plan lacks stays `None`.
- Excel rows no longer turn blank cells into 0, 2 lots or 130 qty. A derived value is computed only from cells that are present.
- The warning reads: "₹ premiums, quantities, capital, P&L and ROI are modelled and may be estimated or unverified." It is shown as a prominent `role="alert"` banner on Forward Tested and on the dashboard's forward-test panel. Unknown values display as `—`.

### Compatibility

- Same status (200) and the same keys. Added keys: `data_source`, `source_warnings`, `source_error`, `metrics.missing_values`.
- Numeric fields can now be `null`. The two frontend consumers (`app/page.tsx`, `app/forward-tested/page.tsx`) handle that. No other consumer exists in the repo.
- Behaviour change: a SQLite failure now returns `unavailable` instead of Excel figures.

### Open for approval (not changed)

`upsert_trade`, the legacy writer, still invents values when input is missing: 4 lots, `lots × 65` qty, capital = premium × qty, `roi = 0.0` on zero capital, and 0.0 for missing points or R. Changing it changes future historical rows, so it needs a decision. The crew journal was not substituted for the legacy source.

## 3. Authentication

### Consumers

| Route | Consumers in the repo | How they authenticate |
|---|---|---|
| `/api/trades/forward-tested` | `frontend/src/app/page.tsx`, `frontend/src/app/forward-tested/page.tsx` | `lib/api.ts` sends `Authorization: Bearer <token>` from `localStorage` and redirects to `/login` on 401 |
| `/api/journal` | None in the frontend, scripts or LaunchAgents. Documented in README | n/a |
| `/api/reports*` | None in the frontend, scripts or LaunchAgents. Documented in README and DATABASE_TABLE_REFERENCE | n/a |

`scripts/genesis.sh` calls only `/api/health`. The two LaunchAgents (`com.genesis.equal-signals`, `com.genesis.liquidity-signals`) run the scanners and do not call these routes. No public client was found.

### Token handling in the browser

The browser keeps a login session token (`POST /api/auth/login`, expires after `SESSION_HOURS`, default 12, revoked by a password reset) or a pasted static token in `localStorage`. Only its SHA-256 is stored server side. No secret is built into the bundle: the only `NEXT_PUBLIC_*` variable is `NEXT_PUBLIC_API_URL`. Limit: `localStorage` is readable by any script that runs on the page (XSS), and it is not an httpOnly cookie. That is acceptable for a single-operator local tool and should be revisited before any shared deployment.

### Enforcement decision

`AuditContextMiddleware._needs_token` now covers `TOKEN_READS = ("/api/live", "/api/trades/forward-tested", "/api/journal", "/api/reports")`.

- Once auth is enabled (any `API_TOKENS` entry or any login user): no token or an invalid token gets 401. Tests 16a and the live check on the isolated server confirm this for all four prefixes.
- While auth is not configured these routes stay public, as before (test 16a2). The crew routes stay stricter: `require_auth` answers 403 until auth exists (tests 01, 02).
- **Current deployment:** `.env` has no `API_TOKENS` and the database has 0 users, so legacy-read enforcement is **not active yet**. The backend listens on `0.0.0.0:8841`, so these routes remain readable from the local network until a login is created.

Rollout:
1. `cd backend && uv run python -m app.auth new-user NAME`
2. Log in at `/login` in the browser.
3. Check that Forward Tested and Crew Research load and that `curl /api/journal` without a token returns 401.

Rollback: delete the user (or the `API_TOKENS` entry), which makes the legacy reads public again. Or revert `TOKEN_READS`. Once auth is on, anonymous dashboard viewers are redirected to `/login`.

Other legacy GET routes outside these prefixes (for example `/api/paper/*` and `/api/trades/{kind}/{id}/reviews`) remain public while auth is enabled. That is unchanged and out of P7 scope.

## 4. Read-only SQLite and sidecars

The reader opens `file:…?mode=ro` with `PRAGMA query_only = ON` and does each response in one transaction (one WAL snapshot). These results come from isolated temporary journals (test 03b and a manual probe):

| Starting state | Read result | Journal file | WAL contents | Sidecars |
|---|---|---|---|---|
| No `-wal` or `-shm` (cleanly closed) | OK | unchanged | none existed | SQLite creates an empty `-wal` and a `-shm` |
| Crashed writer: `-wal` with committed frames, plus `-shm` | OK, the WAL rows are visible | unchanged | unchanged (no checkpoint) | `-shm` wal-index rewritten |
| `-wal` frames, `-shm` missing | OK, the WAL rows are visible | unchanged | unchanged | `-shm` recreated |

Also verified:
- No schema creation or migration, and no session recovery or lease. Test 03 replaces `connect`, `migrate`, `transaction`, `acquire`, `recover`, `heartbeat` and `release` with functions that fail.
- Writes are refused.
- Missing, unconfigured, incompatible, old-schema and corrupt files return 503 (`MISSING`, `NOT_CONFIGURED`, `INCOMPATIBLE`, `UNREADABLE`) and are never created or changed (tests 04, 05).

**Limitation:** zero filesystem mutation cannot be guaranteed for a WAL journal under `mode=ro`. SQLite needs the `-shm` shared-memory index for a consistent concurrent read, so it may create the `-wal`/`-shm` files or update `-shm`. The only mode that writes nothing, `immutable=1`, takes no locks and ignores the WAL, so it can return torn or stale data while the scanner writes. It was rejected. Guaranteed: the journal file and every committed WAL frame stay byte-identical, so nothing is checkpointed, recovered or written. The legacy `data/trades.db` reader has the same property. Its `-shm` was byte-identical across this phase.

## 5. Frontend

| Check | Result |
|---|---|
| `tsc --noEmit` | Pass |
| `eslint` on changed files | Pass |
| `next build` (types and lint included) | Pass from a clean copy of the working tree. In place it fails at "Collecting build traces" with `EMFILE` even at the 10,240 per-process maximum. The committed P6 tree also builds from a copy, so the failure is caused by the workspace location, not the code |

Visual check in a browser against an isolated stack: backend on 127.0.0.1:8851 with temporary databases and journals and the legacy store pointed at a temp copy; frontend production build on :3100.

| Scenario | Observed |
|---|---|
| Populated journal | Loads. Breakout (21 eligible) is labelled `DESCRIPTIVE_RESEARCH_NOT_VALIDATED` with win rate and avg R. Sweep (3) is `INSUFFICIENT_SAMPLE`, win rate "— (needs 20)". Each card shows strategy id · version · policy, the exclusions and coverage as n/d |
| No token / invalid token (auth on) | 401, redirected to `/login?next=/crew-research` |
| Auth not configured | 403 banner with setup instructions, no statistics |
| Missing journal | "Crew journal unavailable — MISSING: the crew journal does not exist" |
| Inconsistent journal | "1 finding(s) — statistics withheld", no groups |
| Empty journal | "No admissions or model trades for this version" |
| Legacy warning | Amber alert on Forward Tested (with `Source: sqlite` and missing-value count) and on the dashboard. Unpriced P&L, ROI and PF show `—` |
| Secrets and paths | Page text contains no `/tmp`, `/Users`, `journal.db`, token or hash |

The dashboard's quote strip called public Yahoo Finance delayed quotes; it is not a broker.

## 6. Protected-file manifest

- **P6 reported 1,929.** Its list combined `find .` output (`./path`) with `find trades data …` output (`path`). It was de-duplicated with `sort -u`, so 23 files appeared twice under two spellings. There were 1,906 unique files.
- **P7 reported 1,859.** It used a narrower glob: `data/ trades/ reports/ backend/data/ backend/genesis.db backend/live.db logs/*.json*`. That left out 47 real files:
  - 24 Telegram sample-export files
  - 18 `DocumentsAndDiagrams` backtest and audit workbooks
  - 3 `backend/research/out` workbooks
  - `backend/trades/daily_risk_config.json` and `backend/trades/safety_wall_state.json`
- The difference was not harmless: two risk-state files were unguarded in the P7 count. They were unchanged against P6's after-hashes.

There is now one stable scope: `backend/scripts/protected_manifest.py`. It covers:
- every file under `data/`, `trades/`, `reports/`, `backend/data/` and `backend/trades/`
- JSON under `logs/`
- every `*.db`, `*.db-wal`, `*.db-shm`, `*.sqlite*` and `*.xlsx` elsewhere
- the frozen V0 sources (`backend/scripts/backtest_pdc_15ema.py`, `backend/app/trading/crew/`, `scanner_crew/`, `strategies/`)

Paths are repo-relative with no `./`, so each file is listed once. That gives 1,930 files: the 1,906 P6 files plus 24 frozen sources.

Results:
- All 1,906 P6 files match P6's after-hashes.
- All 1,859 files in P7's own earlier snapshot are unchanged.
- Before and after this remediation, all 1,930 are identical (manifest digest `4a5ba74b0dbd1fde` both times).
- Reference hashes: `data/trades.db` `c50875f4…`, `backtest_pdc_15ema.py` `62230eec…`, `backend/trades/safety_wall_state.json` `00d3f434…`.
- `git diff HEAD` on the frozen V0 directories is empty.

The untracked, zero-byte `backend/genesis.db`, `backend/live.db` and `trades/trades.db` are dated 1–2 Nov, before P4. They are not P7 artifacts and are not committed.

## 7. Tests

| Run | Result |
|---|---|
| P7 `test_crew_read_api.py` | 32 passed (24 before, plus 03b ×3, 16a, 16a2, 16b, 16c, 16d) |
| P6 `test_research_runtime.py` | 32 passed |
| Full backend suite | 888 passed, 1 failed: the known `test_scanner_crew.py::test_news_agent_sentiment_and_event_risk`. No new failures |
| Ruff on changed files | No new findings. All 10 remaining findings exist at HEAD (`routes.py`, `trade_store.py`). The new files are clean |
| Frontend | Section 5 |
| Protected files | 1,930 identical |

## 8. Changed files

New: `backend/app/api/crew.py`, `backend/app/db/crew_reader.py`, `backend/app/reports/crew_export.py`, `backend/scripts/protected_manifest.py`, `backend/tests/test_crew_read_api.py`, `frontend/src/app/crew-research/page.tsx`, this report.

Modified: `backend/app/api/routes.py`, `backend/app/audit/middleware.py`, `backend/app/config.py`, `backend/app/db/trade_store.py`, `backend/app/main.py`, `backend/app/trading/research_runtime.py`, `backend/tests/test_research_runtime.py`, `frontend/src/app/forward-tested/page.tsx`, `frontend/src/app/layout.tsx`, `frontend/src/app/page.tsx`, `frontend/src/lib/api.ts`.

## 9. Conditions and remaining production blockers

1. Legacy-read authentication is enforced only once a login or API token exists. Neither exists today, and the API listens on `0.0.0.0:8841`. Create a login (section 3) or bind the API to `127.0.0.1`.
2. SQLite `mode=ro` may create or update `-wal`/`-shm` files. The journal and WAL data are never changed (section 4).
3. The legacy writer `upsert_trade` still defaults missing lots, qty, capital and ROI (section 2). This needs approval.
4. In-place `next build` fails with `EMFILE` in this workspace (section 5).
5. `CREW_JOURNAL_PATH` is unset in the deployment, so Crew Research shows 503 `NOT_CONFIGURED` until a journal is configured. That is P8 territory.
6. Browser tokens live in `localStorage` (section 3).
