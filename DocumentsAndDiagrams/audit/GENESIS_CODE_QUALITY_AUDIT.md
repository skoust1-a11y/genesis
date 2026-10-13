# Genesis — Python Engineering and Security Audit (Phase 8)

Tools were run with `uvx` (nothing added to the project). No secret values were printed or copied.

## 1. Tool results

| Tool | Scope | Result |
|---|---|---|
| pytest | `backend/tests` | **466 passed** (460 before this audit + 6 new) in ~85 s |
| node --test / tsc / eslint | `frontend/src` | 3 passed / 0 errors / 0 errors |
| Ruff (project defaults) | `app`, `scripts` | 146 findings: 59 B008 (FastAPI `Depends` defaults, intended), 16 unsorted imports, 16 unused `noqa`, 15 blind `except Exception`, misc style |
| Ruff `S`, `BLE` | security subset | 1 S608 (false positive, see below), 1 S314 (XML), 1 S311 (seeded bootstrap, not security), 1 S110 (`try/except/pass`) |
| Bandit | `app`, `scripts` (12.7k LOC) | 0 high, 2 medium (B608 false positive; B314 RSS XML), 8 low (subprocess for `git rev-parse` and the supervisor, both fixed argv) |
| pip-audit | 228 locked runtime deps | **No known vulnerabilities** |
| Pyright | `app` | **264 errors**, mostly Optional misuse (e.g. `cancel_order(order.broker_order_id)` where the id is `str | None`; `float(None)` paths in `orders.sync`; `policy.py:261`) |

## 2. Security

| Item | Finding | Severity |
|---|---|---|
| Hardcoded secrets | None found in tracked files; `.env`, DBs, sandbox state, logs and research are git-ignored | OK |
| Credential storage | Broker credentials Fernet-encrypted with `BROKER_CREDENTIALS_KEY`; API tokens stored as SHA-256; passwords scrypt (n=2^14) with constant-time compare and lockout | OK |
| SQL injection | `journal/store.py:158` builds a WHERE from **fixed** clause strings with bound parameters (Bandit false positive). Market DB `ALTER TABLE` uses model metadata, not input | OK |
| Unsafe deserialization | `yaml.safe_load` everywhere; no pickle in the app (pickle only in git-ignored research) | OK |
| XML | RSS parsed with stdlib `ElementTree` (B314). Python 3.12's expat mitigates entity expansion; switching to `defusedxml` would need a new dependency | LOW (G-18) |
| Auth coverage | Middleware: once any token/user exists, every mutating request and every `/api/live` read needs a token. Live mutations and credentials **always** need a token (403 when auth is not configured) | OK |
| Open by default | With no tokens/users, research and paper endpoints (`/api/review/run`, `/api/paper/orders`, `/api/ingest/*`, …) are unauthenticated, and the root README starts uvicorn with `--host 0.0.0.0`, exposing them to the LAN | MEDIUM (G-15) |
| CORS | Explicit origins; `*` refused with tokens set | OK |
| Sensitive logs | Alerts carry deployment ids and reasons, not credentials | OK |
| Insecure HTTP | NSE/RSS over HTTPS | OK |

## 3. Engineering

| Area | Assessment |
|---|---|
| Structure | Clear layers (strategies → trading decisions → engine / trade crew → stores). Decision code shared by sim and live: excellent |
| Size hotspots | `agents/trade_crew/graph.py` 1,412 lines, `engine/sim.py` 880 lines (`simulate` ~300 lines with nested closures). Hard to review; split booking/stops/opinion out of `graph.py` when next touched |
| Validation | Pydantic everywhere at trust boundaries (`extra=forbid` on rules, settings with bounds) |
| Error handling | Live errors halt the deployment (fail-closed). 15 blind excepts, most deliberate (alerts must never raise, LLM optional). `graph.py:171` swallows a failed freeze-quantity lookup silently (falls back to no split) — log it |
| Money types | `Decimal` in live orders and stores; `float` in the simulator (acceptable for research, rounding at 4 dp) |
| Concurrency | One process per deployment; SQLite `BEGIN IMMEDIATE` serializes writes; G-28 race when LLM steps are on |
| Dependencies | Lean; no unused runtime deps found. `vendor/TradingAgents` unused |
| Dead code | `costs.apply_trade_costs`, `src/trading_strategy_lab_backend` scaffold |
| Types | 264 Pyright errors: not run in CI; most are latent `None` paths |
| CI | None (G-26) |

## 4. Recommendations

1. Add a CI job: pytest, ruff (with a pinned rule set), frontend tests/tsc/eslint, pip-audit.
2. Fix Pyright errors in `trading/orders.py`, `trading/policy.py`, `trading/decisions.py` first (money paths), then ratchet.
3. Bind uvicorn to `127.0.0.1` in the README, or require auth whenever host ≠ localhost.
4. Delete `apply_trade_costs` and the scaffold entry point.
