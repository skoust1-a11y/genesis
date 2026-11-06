# S1-P8 pre-cutover security and readiness audit

Date: 6 Nov 2026, 03:2x–03:4x IST. Baseline: P7 commit `ebeaaca`. P8 was **not** executed.

Method: read-only. Process and socket inspection, code reading, `GET` probes against the running API, read-only (`mode=ro&immutable=1`) SQLite queries, the full test suite, an isolated frontend build in `/tmp`, and the protected-file manifest. Nothing was restarted, no LaunchAgent was touched, no database was written, no Telegram, broker or order call was made. Secret values were never printed; secrets were compared by SHA-256 prefix only.

**Verdict: NOT READY. P8 is BLOCKED** by four critical findings (section 8).

---

## 1. Security exposure assessment (Part A)

| # | Question | Finding | Evidence |
|---|---|---|---|
| 1 | Bind address | **All interfaces.** `uvicorn app.main:app --host 0.0.0.0 --port 8841 --reload --reload-dir app` | `lsof -nP -iTCP -sTCP:LISTEN` → `python3.1 58178 … TCP *:8841 (LISTEN)` and worker `5440` on the same socket |
| 2 | Public routes | Everything except `/api/live/crew/*`, the three credential routes, and live-mode actions (section 1.1) | 77 operations from `/openapi.json`; probes below |
| 3 | Auth configured | **No.** `API_TOKENS` absent from `.env`; `users` = 0, `user_sessions` = 0 | `GET /api/auth/me` → `{"username":null,"auth_enabled":false}` |
| 4 | P7 loaded by `--reload` | **Yes.** Reloader parent started Tue 3 Nov 05:08; worker respawned Fri 6 Nov 03:18:09. No `app/*.py` is newer than 03:18:09 and the tree equals `ebeaaca` (commit 03:26:40 only recorded files already on disk). The P7-only route `/api/live/crew/trades` answers 403 | `find app -newermt "2026-11-06 03:18:09"` empty; `git diff ebeaaca -- app` empty |
| 5 | Reachable beyond localhost | **Yes.** `curl http://192.168.1.6:8841/api/auth/me` → 200. macOS Application Firewall **disabled**; `/usr/bin/python3` allowed incoming. Request logs carry no client IP, so earlier LAN access **cannot be ruled out** | `socketfilterfw --getglobalstate` → `State = 0` |
| 6 | Scanner / scheduler / dispatcher | **None active.** Only the uvicorn reloader, its worker and resource tracker. `com.genesis.equal-signals` and `com.genesis.liquidity-signals` are not loaded and are `disabled` in launchd. The app has no startup hooks; the only thread is the research-runtime heartbeat, which exists only when that runtime is started by hand. `CREW_JOURNAL_PATH` unset | `launchctl list`, `launchctl print-disabled gui/$UID`, `ps` |
| 7 | Credential / sensitive exposure | API does not return secret values (`/api/live/brokers` returns field name, source, fingerprint only; credential routes 403). **But:** see 1.2 | Probes |

### 1.1 Route exposure today (auth disabled)

| Class | Routes | State |
|---|---|---|
| Always token (fail-closed, 403 with no auth) | `/api/live/crew/*`, `GET/PUT/DELETE /api/live/broker-accounts/{id}/credentials`, live-mode `start`, any `flatten`, live `approvals`, live broker-account create, live `llm-veto` / `capital-events` | OK |
| **Open reads** | trades/forward-tested, journal, reports (+ CSV), strategies, audit + audit/verify, live deployments / positions / equity / orders / opinions / phantom-trades / live-trades, market data, notifications, `/openapi.json`, `/docs` | 200 without token |
| **Open writes** | `ingest/extract`, `ingest/clarify`, `review/run`, `demo/run`, `strategies/analyze`, `strategies/{id}/archive`, `paper/orders`, notifications (create, read, read-all), trade reviews, improvement decisions, paper `approvals`, `approvals/backtest`, paper/sandbox broker-account create, paper `start`/`pause`, **`stop` without `flatten` on any deployment including live** (cancels working broker orders, which would include protective stops) | Accepted without token |

There are 0 deployments and 0 broker accounts today, so the `stop` path has nothing to act on. It becomes live risk the moment a live deployment exists.

### 1.2 Findings beyond the route table

1. **SSRF through ingest (HIGH, not exploited).** `POST /api/ingest/extract` with `source_type=document` and a `url` calls `httpx.get(url, follow_redirects=True)` with no host allowlist (`backend/app/ingest/doc.py:7`). Any LAN caller can make the backend fetch loopback-only services. One is listening now: Chrome DevTools on `127.0.0.1:9222`. The fetched text is then sent to the LLM (spending the OpenAI/Groq/Google keys) and stored in the ingest run.
2. **Telegram bot token in plaintext log (HIGH).** `backend/logs/errors.log` lines 564–565 (4 Nov 19:55) contain a full `/bot<token>/sendMessage` URL inside a `requests` exception. It is **not** the current `ALERT_TELEGRAM_BOT_TOKEN` (SHA-256 differs); it is the old hard-coded token, which is also in git history (`c4f81d9`, `012e211`; removed by `b46fecf`) and **those commits are on both `github/main` and `origin/main`**. The same `logger.error(f"...{e}")` pattern still exists in `briefing_agent.py:67`, and seven other modules build `https://api.telegram.org/bot{token}/…` URLs, so a network error can log the **current** token the same way. Logs are git-ignored.
3. `.env` is mode `0644` (readable by other local accounts). Databases and logs likewise.
4. `/openapi.json` and `/docs` publish the full API surface. `/api/health` reveals `llm_enabled`.
5. The uvicorn process is a development server (`--reload`) that has been up since 3 Nov.

### 1.3 Safe remediation: loopback-only development access

Needs operator approval because it restarts the API. Not executed.

```bash
# 0. Record the current state
lsof -nP -iTCP:8841 -sTCP:LISTEN
ps -o pid,ppid,lstart,command -p "$(lsof -tiTCP:8841 -sTCP:LISTEN | tr '\n' ',' | sed 's/,$//')"

# 1. Stop the dev server (SIGTERM to the reloader parent; the worker exits with it)
kill -TERM 58178
sleep 3; lsof -nP -iTCP:8841 -sTCP:LISTEN || echo "8841 free"

# 2. Restart bound to loopback only
cd /Users/skoust/Documents/MyProjects/genesis/backend
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8841 --reload --reload-dir app

# 3. Verify
lsof -nP -iTCP:8841 -sTCP:LISTEN                       # expect 127.0.0.1:8841, never *:8841
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8841/api/health          # 200
curl -s -o /dev/null -w '%{http_code}\n' --max-time 3 http://192.168.1.6:8841/api/health  # 000
```

Optional, separately authorized network change: `sudo /usr/libexec/ApplicationFirewall/socketfilterfw --setglobalstate on`.

Zero-restart alternative (a breaking auth change, needs a rollout decision): `cd backend && uv run python -m app.auth new-user <NAME>`. `auth.enabled()` reads the `users` table per request, so every write and every `TOKEN_READS` read requires a token at once; the frontend must then log in. It does **not** close `/api/strategies`, `/api/audit`, `/api/market/*`, `/api/notifications` (GET), `/openapi.json` or `/docs`, and it does not fix the bind address.

Credential hygiene (operator):
- Confirm in BotFather that the old hard-coded bot token is revoked. If not, revoke it now: it is in pushed history.
- Delete or redact `backend/logs/errors.log` lines 564–565, and rotated copies, after revocation.
- `chmod 600 .env`.

---

## 2. Authentication hardening proposal (Part B)

### Current behaviour

| Case | Today |
|---|---|
| No tokens and no users | **Fail-open**: middleware skips every check (`auth.enabled()` is false) |
| Last user deleted / `API_TOKENS` emptied | **Re-opens** every protected legacy route |
| Invalid token | 401 (fail-closed) ✔ |
| Expired session | `verify()` → None → 401 ✔ |
| Auth enabled, GET outside `TOKEN_READS` | Public (strategies, audit, market, notifications, ingest-runs, reviews, docs) |
| Browser | 12 h session token in `localStorage` (`tsl.token`); no long-lived secret embedded in the bundle ✔ |

### Design

1. **`AUTH_MODE` setting, default `required`.**
   - `required`: every route needs a valid token except an explicit allowlist: `POST /api/auth/login`, `OPTIONS`, and a minimal `GET /api/health` (`{"status":"ok"}`, no `llm_enabled`). With no user and no token provisioned, protected routes answer **503 `AUTH_NOT_PROVISIONED`**, never 200. Deleting the last user leaves the API closed.
   - `open-dev`: today's behaviour, but the middleware rejects (403) any request whose ASGI `scope["client"]` host is not `127.0.0.1` or `::1`. Opening the API is then bound to loopback in code, not only by the uvicorn flag.
   - The middleware decides on `AUTH_MODE`, not `auth.enabled()`. `ensure_authenticated()` keeps its 403 for broker/live actions in both modes.
2. `/docs` and `/openapi.json` are disabled in `required` (`FastAPI(docs_url=None, openapi_url=None)`).
3. **Browser session cookie instead of `localStorage`.**
   - `POST /api/auth/login` also sets `genesis_session`, `HttpOnly; SameSite=Strict; Path=/api` (plus `Secure` once served over HTTPS). The middleware accepts the cookie or `Authorization: Bearer`.
   - Serve the API same-origin through a Next.js rewrite (`/api/:path*` → `http://127.0.0.1:8841/api/:path*`), so no CORS credentials are needed and `NEXT_PUBLIC_API_URL` stops being baked into the bundle.
   - CSRF: `SameSite=Strict`, plus mutating requests must carry `X-Requested-With: genesis` or a same-origin `Origin`; others get 403.
   - `localStorage` exposure today: any XSS on the frontend origin can read a 12 h token. HttpOnly removes that, though XSS can still act within the open tab.
4. Static `API_TOKENS` remain for scripts only, never in `NEXT_PUBLIC_*`.
5. Ingest: require a token and refuse URLs that resolve to loopback, link-local or private ranges, re-checked after each redirect, or disable `follow_redirects`.
6. Login lockout stays per-process. It is already marked `ponytail:`, which is fine for a single worker.

### Tests (to add with the change)

- Route-table guard: iterate `app.routes`; in `required` mode every non-allowlisted route answers 401 without a token, so a new route cannot ship public.
- No users and no tokens in `required` mode → 503 on every protected route, 200 only on the allowlist.
- Create a user, then delete it → still 503, not 200.
- Invalid token, expired session (frozen clock), and token after password reset → 401.
- `open-dev` with client `192.168.1.6` → 403; `127.0.0.1` → 200.
- Cookie login → `/api/auth/me` returns the user; mutating request without CSRF header → 403; with header → 200.
- Logout invalidates the cookie and the bearer token.
- Consumer compatibility: Bearer static token still works for scripts; frontend login, redirect and crew-research flow pass in a browser run; `test_crew_read_api.py` and `test_login.py` stay green.
- Rollback: `AUTH_MODE=open-dev` plus a restart restores today's behaviour (loopback-only). No schema migration is involved, so rollback is configuration only.

Rollout: ship with `AUTH_MODE=open-dev` and verify; create the operator user; switch to `required`; observe for one session; remove the `localStorage` token code in the next release.

---

## 3. Legacy financial-writer remediation plan (Part C)

### 3.1 Where missing values become invented: `trade_store.upsert_trade`

| Field | When missing it becomes | Notes |
|---|---|---|
| `lots` | **4** (code) / column `DEFAULT 4` | The research runtime uses 3 |
| `qty` | **`lots × 65`** / column `DEFAULT 260` | Hard-coded NIFTY lot; wrong for BANKNIFTY (30). Journal key `quantity` is ignored |
| `entry_premium`, `exit_premium` | **0.0** | |
| `capital_deployed` | `entry_premium × qty` → **0** | |
| `exit_value` | `exit_premium × qty` → **0** | |
| `est_pnl_inr` | `exit_value − capital` when neither a P&L key nor `pnl_method` is given | Entry premium known and exit missing (an OPEN trade) → **−capital, a fake total loss**; both missing → **0, a fake breakeven** |
| `roi_pct` | `pnl / capital`, or **0.0** when capital is 0 | |
| `r_multiple` | **0.0** | The journal writes `realized_r`, which is never read |
| `points` | **0.0** | The journal writes `realized_points` |
| `exit_spot` | **`entry_spot`** (a flat exit) | The journal writes `exit_price` |
| `entry/stop/target_spot` | **0.0** | The journal writes `entry_price` / `stop_loss` / `target_price` |
| `risk_pts` | `|entry − stop|` → **0** | |
| `side` | **LONG** | The journal writes `direction` (CALL/PUT), so every PUT trade is stored LONG |
| `date`, `time` | **""** | The journal writes `timestamp` |

Other readers that also invent values:
- `post_market_manager.py:76–85` turns `pnl_rupees=None` (index points, not priced) into **0.0 rupees**, drops `pnl_method`, defaults `risk_points` to 25 and `lots` to 1, and labels every journal trade **PUT** (it reads `Side`, which the journal never writes).
- `manager.py:417` and `autopsy_agent.py:82` default risk to 25 points.

### 3.2 Measured impact on `data/trades.db` (read-only, `immutable=1`)

1,522 rows in total:
- **1,496** rows have an empty date, `side=LONG`, and zero entry, stop, risk, option, premium and capital. They are journal-agent rows hit by the key mismatch.
- **1,499** rows have `r_multiple = 0`, and **1,496** have `est_pnl_inr = 0`.
- **41 `TEST_SWEEP` OPEN rows** are test pollution in the real ledger.
- The API filter `date >= '2026-10-15'` hides the empty-date rows; the forward-tested view shows 26 dated rows.

### 3.3 Future-only correction (historical rows untouched)

1. **Retire the legacy writer at cutover.** Add a `LEGACY_LEDGER_WRITES` setting, default off. When off, `upsert_trade` raises; the crew journal is the only writer after P8. The daemon's journal agent is frozen code that swallows the exception at `debug` level, so its JSON ledger is unaffected, but the operator must know the SQLite ledger will no longer change.
2. **If the legacy writer must stay on:** fix it in `trade_store.py`, the non-frozen shared code, not in the frozen `journal_agent.py`:
   - Map the journal keys.
   - Write NULL for every missing money or outcome field. Because the existing columns are `NOT NULL`, new rows go to a new nullable table, `live_and_forward_trades_v2`, with a `pnl_method` column. Readers union the two tables and mark v1 rows `legacy_unverified`.
3. **Read-side labelling, with no data change.**
   - In `get_all_trades`/`compute_metrics`, map v1 rows where `capital_deployed = 0 AND entry_premium = 0` to `None` money fields.
   - Map rows where `r_multiple = 0 AND points = 0 AND exit_spot = entry_spot` to `None` outcomes.
   - Exclude `TEST_%` strategies and empty dates.
   - `_total` already turns any `None` into an unknown total, so the UI shows "—" instead of ₹0.
4. **Index points never become rupees.** The crew path already stores `model_points`/`model_r` only, and `finalized_results` is "index points and R only". Add tests:
   - A closed `index_points_not_priced` trade stores `est_pnl_inr` and `roi_pct` as NULL, and no rupee figure is derived from points.
   - `post_market_manager` keeps `None`, not 0.0.
   - Telegram text that estimates `risk × lot × 0.4` stays labelled as an estimate and is never persisted.
5. Each change needs operator approval. None touches stored rows.

---

## 4. Runtime readiness matrix (Part D)

| Area | State | Evidence / gap |
|---|---|---|
| Session lease and heartbeat | READY (unit-tested) | Single fenced row, generation and token hash, 30 s TTL, 10 s heartbeat, fence on every write (`crew_session.py`). Gap: heartbeat failure only logs a warning; no alert |
| Startup recovery | READY (unit-tested) | Consistency check under the write lock; prior-day opens → `EXPIRED_UNRESOLVED`; SENDING → UNKNOWN. Not exercised with a real process kill |
| Orphan handling | PARTIAL | Current-day orphans are reported and left untouched; there is **no operator resolution path** ("no reconciliation path yet") |
| UNKNOWN publications | PARTIAL | Never resent and they block recommendations; there is **no operator resolve tool** (read API only) |
| Market-hours gating | **MISSING** | Neither `research_runtime` nor `admission` checks session hours, weekends or holidays. Only the lookout window and the 15:10 exit cutoff exist |
| IST trading calendar | **MISSING / stale** | `backend/data/holidays/nse.yaml` covers 2020–2026 and is read by `market/hours.py`, not by the crew runtime. **No 2027 dates** ("a date missing here is treated as a trading day"). `market_day` is the IST calendar day |
| SQLite concurrent readers | READY with condition | WAL, `busy_timeout` 30 s, `BEGIN IMMEDIATE` writers, reader `mode=ro` + `query_only`. A `mode=ro` open still creates `-wal`/`-shm` sidecars. **This audit's GET probes created 4 such sidecars** (section 10) |
| Clock assumptions | RISK | Lease expiry uses the wall clock (`datetime.now(IST)`), not a monotonic clock. An NTP step of more than 30 s can expire a live lease or allow a takeover. Fencing keeps writes safe, but the runtime would stop |
| Scanner admission and journal paths | READY | Research-only; ACTIONABLE raises; journal refused inside protected data dirs or on a non-crew DB; never opens legacy ledgers |
| Frontend build reproducibility | CONDITIONAL | `tsc` passes and the 3 unit tests pass; `next build` passes in an isolated `/tmp` copy with the existing `node_modules`; in-place build hits `EMFILE` (P7); clean `npm ci` from the lockfile not tested; three `node_modules*` directories present |
| Production config isolation | **MISSING** | One shared `.env` (symlink) for dev and prod; DB paths are relative to CWD; `CREW_JOURNAL_PATH` unset; no separate prod settings file |
| Log and secret handling | **FAIL** | A token in `errors.log`; exception text that can include token URLs in 8 modules; no client IP in request logs; `.env` 0644 |
| Process supervision and shutdown | **MISSING** | Runtime is hand-started; SIGINT/SIGTERM → clean release ✔; no supervisor, restart policy, health file or alert; API runs as a `--reload` dev server |

Missing operational controls: process supervisor with a no-auto-restart-on-fence policy; alert on lease loss, heartbeat failure, consistency refusal, UNKNOWN and orphan counts; market-hours and holiday gate with 2027 dates; NTP and clock-step check at startup; journal backup schedule and restore drill; client IP in request logs; secret redaction filter on log handlers; a separate production config.

---

## 5. Strategy-integrity matrix (Part E)

| Strategy / behaviour | Identity | Where | Active after cutover? | Statistics |
|---|---|---|---|---|
| Frozen PDC Zone + 15 EMA V0 | Frozen card (`audit/genesis_v2/08…`) | Not implemented. `backtest_pdc_15ema.py` does **not** match the card (P6 §1) | **No** | Benchmark only; never mixed |
| Existing scanner-v0 strategies | `strategy_id` = scanner setup name, `strategy_version=scanner-v0` | `DetectorAgent` → research runtime | **Yes, research-only**, if the operator starts the runtime | Per `strategy_id`; no combined total |
| `research-exec-v1-monotonic-stop` | `execution_policy` on each `MODEL_ENTRY` | `research_runtime` exits | **Yes**, with the above | Filtered by policy; other policies excluded |
| Legacy daemon execution | `TradeManager` / `HoldingAgent` / reversal, legacy ledgers | LaunchAgents (disabled) | **No** | `data/trades.db` is unverified (section 3) and never merged with crew stats |

Gates active after cutover: P3 admission (signal staleness, quote missing or stale, contract validity, executability; capital, cash and max-loss unverified, so the class is RESEARCH_ONLY at best and ACTIONABLE is impossible); P5 lease fence; P6 monotonic stop; lookout window; 15:10 intraday cutoff; `MODEL_LOTS=3`. Not active: Telegram outbox, broker, live trading, legacy RiskAgent and safety wall.

Frozen sources unchanged: `git diff HEAD` on `backtest_pdc_15ema.py`, `trading/crew/`, `trading/scanner_crew/` and `strategies/` is empty, and `backtest_pdc_15ema.py` is `62230eec…`.

---

## 6. P8 cutover plan (design only; nothing automatic)

**Preconditions (all must be true)**
- Every critical blocker in section 8 is closed with evidence.
- `AUTH_MODE=required` is live and the API listens on `127.0.0.1` only.
- The old Telegram token is confirmed revoked.
- `LEGACY_LEDGER_WRITES` is off.
- Market-hours and holiday gate in place, with 2027 dates.
- The suite is green (888 + new tests; only the known NewsAgent failure).
- Protected manifest captured.

**Operator approvals (named, recorded):** network change; auth mode switch; journal path and ownership; legacy-ledger freeze; the runtime start command; the rollback owner.

**Steps**
1. Outside market hours. Confirm no runtime holds a lease: `sqlite3 "file:$J?mode=ro" "select * from crew_lease"`.
2. Backups. Use `sqlite3 SRC ".backup DST"`, or a filesystem copy with the server stopped, for:
   - `backend/data/genesis.db`
   - `backend/data/genesis_live.db`
   - `data/trades.db`
   - the existing crew journal, if any
   - `trades/*.json`
   - `.env`
   Record SHA-256 for each.
3. Restore validation. Restore each backup to a scratch directory, then:
   - run `PRAGMA integrity_check`
   - compare row counts
   - run `cs.recover` consistency findings, read-only, on the journal copy
4. Schema migration:
   - The crew journal: `cj.migrate()` is additive and idempotent. Run it on the copy first, then on the target.
   - The legacy DBs get no migration (frozen).
   - Any v2 legacy table is created only if section 3.3 step 2 is approved.
5. Configuration:
   - Set `CREW_JOURNAL_PATH` to an absolute path outside the protected dirs.
   - Set `AUTH_MODE=required`.
   - Bind with `--host 127.0.0.1` and no `--reload`.
   - `chmod 600 .env`.
6. Legacy isolation:
   - LaunchAgents stay `disabled`. Verify with `launchctl print-disabled`.
   - Confirm no process imports `app.liquidity_signals`, `app.equal_signals` or `TradeManager`.
7. Journal ownership:
   - Only the research runtime writes, through the lease.
   - The API reads it with `mode=ro`.
   - Record the file owner and mode.
8. Read-only verification:
   - With a token, `/api/live/crew/{trades,admissions,sessions,exceptions,stats}` all return 200.
   - Without a token, everything returns 401/503.
   - From the LAN, the connection fails (000).
9. The runtime is started by the operator in a later, separately approved step. **P8 itself starts nothing**: no scanner, no Telegram, no broker.

**Post-cutover checks:** listener is `127.0.0.1`; `auth/me` reports enforced; the manifest shows only expected changes; crew endpoints return 200; legacy ledger hash unchanged; logs carry no secret patterns; no new UNKNOWN entries or orphans.

**Emergency stop:**
1. `kill -TERM <runtime pid>`. This releases the lease and takes no broker action.
2. If the process hangs, `kill -KILL`. The lease expires in 30 s, and recovery on the next start handles open trades.
3. Stop the API: `kill -TERM <uvicorn pid>`.
4. Check that LaunchAgents are still disabled.
5. Live trading stays disabled (`LIVE_TRADING_ENABLED=false`).

**Evidence for final approval:** command transcripts for every step; before and after manifests; backup hashes and restore checks; `lsof` and LAN-probe output; auth test run; suite output; the signed approvals list.

---

## 7. Rollback plan

| Trigger | Action |
|---|---|
| Auth breaks a consumer | `AUTH_MODE=open-dev` + restart (loopback still enforced). Config only |
| Crew journal consistency refusal or migration failure | Stop the runtime; restore the journal from the validated backup; repoint `CREW_JOURNAL_PATH`; keep the failed file for analysis |
| Unexpected write to a protected file (manifest diff) | Stop the API and runtime; restore that file from backup; investigate before any restart |
| Lease thrash or clock fault | Stop the runtime; check NTP; restart only after the clock is stable |
| Any sign of Telegram, broker or order activity | Emergency stop; revoke the bot token; check broker and audit logs |

Code rollback: deploy the pre-cutover commit (`ebeaaca`, or whichever commit was approved) with `git revert` of the cutover commits. P3–P7 are local only, so no force-push is involved. Rollback is complete when the manifest matches the pre-cutover capture and the API is back on loopback.

---

## 8. Remaining critical blockers

1. **Network exposure.** The API is on `0.0.0.0:8841`, reachable from the LAN, with the firewall off.
2. **Fail-open authentication.** No user or token exists; legacy reads and many writes are public, including `stop` on any deployment; there is an SSRF-capable ingest route.
3. **Secret leak.** The old bot token sits in a local log and in pushed git history, and revocation is unconfirmed. Token URLs can still reach logs.
4. **Financial integrity of the legacy ledger.** The writer synthesizes lots, qty, capital, P&L, ROI, R, points and side. 1,496 rows are zero-filled and 41 test rows sit in the real ledger. Legacy endpoints still serve it.

High, but not blocking for a research-only cutover: no market-hours or holiday gate (and no 2027 holidays); no supervisor or alerting; no orphan or UNKNOWN resolution tool; no production config isolation; the frontend clean-install build is unproven.

---

## 9. Test evidence

| Run | Result |
|---|---|
| Backend suite (`pytest -q`) | **888 passed, 1 failed**: the known `test_scanner_crew.py::test_news_agent_sentiment_and_event_risk`. 91 s |
| Frontend `tsc --noEmit` | Pass |
| Frontend `npm test` | 3 passed |
| Frontend `next build` (isolated `/tmp/genesis-fe-build`, existing `node_modules`) | Pass, 14 static pages, lint warnings only |
| API probes, all GET on 127.0.0.1 | `auth/me` 200 (`auth_enabled:false`); crew 403; forward-tested, journal, reports, strategies, audit, notifications, deployments, brokers 200 |
| LAN probe | `192.168.1.6:8841` → 200 |
| Not run | Auth hardening tests (not implemented); real crash, kill or clock-step drills; backup and restore drill; `npm ci` clean build |

## 10. Protected-file integrity

- The manifest now lists **1,934** files.
- The 4 extra files are SQLite sidecars created at 03:28:21–22 by the running API when this audit's GET probes opened the databases:
  - `backend/data/genesis.db-wal`
  - `backend/data/genesis.db-shm`
  - `backend/data/genesis_live.db-wal`
  - `backend/data/genesis_live.db-shm`
- The WALs are empty (`e3b0c442…`). They are held open by the live server and were deliberately not deleted.
- **Excluding those 4, the 1,930 baseline files reproduce P7's manifest digest `4a5ba74b0dbd1fde` exactly**, so every protected file is byte-identical to P7.
- Reference hashes match P7: `data/trades.db` `c50875f4…`, `backtest_pdc_15ema.py` `62230eec…`, `safety_wall_state.json` `00d3f434…`.
- The manifest was identical before and after the test suite.

## 11. Git status

- Branch `main`, HEAD `ebeaaca`.
- **`github/main` is at `1c158a1` (P2)**, so P0–P2 *are* pushed. The baseline note "no commits pushed" is inaccurate. P3–P7 (5 commits) are local only.
- `origin/main` (Cursor Origin) is at `c4f81d9`.
- Untracked: `backend/genesis.db`, `backend/live.db` and `trades/trades.db` (zero-byte, pre-P4), plus this report. Nothing was committed or pushed by this audit.

## 12. Final readiness verdict

| Gate | Status | Evidence |
|---|---|---|
| Authentication | **BLOCKED** | Fail-open; 0 users and 0 tokens; section 2 |
| Network exposure | **BLOCKED** | `*:8841`, LAN 200, firewall off |
| Journal integrity | CONDITIONAL | Crew journal invariants, fencing and recovery tested (P1–P6 suites pass); no production journal yet; no backup/restore drill |
| Strategy identity | PASS | scanner-v0 and the execution policy separated (P6 test 20); frozen sources unchanged; V0 not active |
| Financial reporting | **BLOCKED** | Legacy writer invents values; 1,496 synthesized rows served by legacy endpoints; crew reporting is points/R only (OK) |
| Crash recovery | CONDITIONAL | Unit-tested lease, fence and recovery; no real kill, power-loss or clock-step drill; no orphan or UNKNOWN resolution tool |
| Operational monitoring | **BLOCKED** | No supervisor, alerts, client-IP logging or market-hours/holiday gate; secret-leaking log pattern |
| Frontend build | CONDITIONAL | `tsc`, tests and isolated build pass; in-place `EMFILE`; clean install untested |
| Protected-file integrity | PASS | 1,930 files reproduce digest `4a5ba74b0dbd1fde`; +4 empty sidecars from probes |
| Rollback readiness | CONDITIONAL | Plan written; config-only auth rollback; backup/restore drill not performed |

**P8 is not ready.** Close blockers 1–4, then re-run this gate table.
