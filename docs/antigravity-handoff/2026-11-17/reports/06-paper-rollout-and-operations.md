# Paper Rollout, Operations & Governance Guide

**Date**: 17 November 2026  
**Status**: Research Validated — Unpromoted Staging Architecture  
**Author**: Antigravity  
**Repository Branch**: `main` (Local checkout; unpushed per owner instruction)

---

## 1. Delineation: Active Production vs. Research Scope

To maintain absolute institutional safety and prevent accidental live or paper state corruption, the boundary between active operations and research must be strictly maintained:

| Attribute | Active Paper Candidate (`forward-20261116-172047`) | Experimental Thesis Management (`thesis-management-research-v1`) |
|---|---|---|
| **Status** | **ACTIVE RUNNING SERVICE** | **RESEARCH ONLY / UNPROMOTED** |
| **Strategy ID** | `zone10-5m-1m-v1` | `thesis-management-research-v1` |
| **Exit Engine** | 20% Giveback after 1R Net Profit | Thesis Management (5M Structure + Milestones) |
| **Target Exit** | None (pure giveback trail) | None (runners run until 5M structure shift) |
| **Stop Ratchet** | Initial stop retained until exit | 0.7R Breakeven lock; 2.0R lock (+0.8R) |
| **Early Invalidation** | None (takes full stop if trailing not armed) | 15M breakdown ($-0.3\text{R}$); 5M trap ($-0.35\text{R}$) |
| **Stop Risk Budget** | $\min(\text{INR } 1,000, 2\% \text{ cash})$ | $\min(\text{INR } 1,000, 2\% \text{ cash})$ |
| **Full Premium Cap** | INR 5,000.00 | INR 5,000.00 |
| **Daily Loss Limit** | INR 5,000.00 | INR 5,000.00 |
| **Max Whole Lots** | 3 lots (195 units) | 3 lots (195 units) |
| **Active Journal** | `logs/paper-forward/data/candidate.db` | Separate isolated research DBs |
| **Telegram Delivery**| Operational paper execution alerts only | **STRICTLY FORBIDDEN** (No research recaps) |

> [!CAUTION]
> The experimental thesis management policy demonstrated a +617% improvement in net P&L over the 20% giveback trail. However, **this research finding is NOT authorization to silently promote it into the running candidate account**. The owner must explicitly authorize staging and promotion.

---

## 2. Staged Paper Rollout Protocol (When Authorized)

When the owner directs staging of Thesis Management into forward paper trading, follow this exact versioned migration protocol:

### Step 1: Assign a Fresh Run & Strategy Identity
- Never alter code in-place under an existing `run_id`.
- Create a new immutable run identity:
  `run_id = forward-YYYYMMDD-HHMMSS` (e.g. `forward-20261118-084500`).
- Create an isolated database:
  `logs/paper-forward/data/candidate-thesis-v1.db`.
- The database schema embeds:
  - Exact `RULES` dictionary and `RULE_HASH`.
  - SHA256 source hash of `thesis_management_policy.py`, `forward_candidate_account.py`, and fee policies.
  - Starting capital: `INR 50,000.00`.

### Step 2: Safe Cutover Procedure
1. Verify market status: perform cutover **only outside market hours** (between 15:45 and 08:30 IST) or on weekends.
2. Verify all open positions: ensure active accounts have `state = 'CLOSED'` and zero unresolved holdings or pending exits.
3. Graceful shutdown:
   ```bash
   python3 scripts/paper_watchdog.py --stop
   ```
4. Archive active run manifest to `logs/paper-forward/history/run-forward-20261116-172047.json`.
5. Point new runner to `candidate-thesis-v1.db` and write new `active-run.json`.
6. Start supervision with awake guard active.

---

## 3. Rollback Protocol to Untouched Control Profile

If any anomaly, quote feed divergence, or runtime exception occurs:

1. **Immediate Shutdown**:
   Stop runner process. Do not force-restart if an exit is pending.
2. **Restore Control Manifest**:
   Copy the archived control manifest back to `logs/paper-forward/active-run.json`:
   ```bash
   cp logs/paper-forward/history/run-forward-20261116-172047.json logs/paper-forward/active-run.json
   ```
3. **Database Integrity Verification**:
   Verify that `logs/paper-forward/data/candidate.db` is intact:
   ```bash
   sqlite3 logs/paper-forward/data/candidate.db "SELECT run_id, rules_hash FROM meta WHERE id=1;"
   ```
   Ensure row count matches `forward-20261116-172047`.
4. **Resume Control Services**:
   Restart candidate runner under the verified control configuration.

---

## 4. Daily Operational Checklists

### 4.1 Premarket Checklist (08:45 – 09:10 IST)
- [ ] **Host Hardware**: Mac laptop connected to AC power, lid open, user logged in.
- [ ] **Awake Guard**: Verify `caffeinate` process is running to prevent system sleep.
- [ ] **Network & Loopback**: Verify stable internet connection and local socket availability.
- [ ] **Daily Fee Tariff**: Confirm Upstox tariff attestation has succeeded for today's market date (`logs/paper-forward/fees/YYYY-MM-DD.json`).
- [ ] **Calendar Validation**: Verify today is an active NSE trading day (not a holiday or weekend).
- [ ] **Journal Lock Status**: Verify no stale `.lock` files remain held from interrupted processes.
- [ ] **Risk Reset**: Ensure daily gross loss and loss counter have reset for the new Kolkata session.

### 4.2 Intraday Monitoring (09:15 – 15:30 IST)
- [ ] **Opening Blackout**: Confirm no entries are accepted between 09:15 and 09:30 IST.
- [ ] **Heartbeat Freshness**: Main runner snapshots every ~11 seconds; quote monitor runs every ~1 second.
- [ ] **Watchdog Invariant**: Watchdog must NEVER force-restart while a trade is in `OPEN` or `EXIT_PENDING` state.
- [ ] **Quote Staleness Guard**: If option quotes are missing or older than 10 seconds, trade admission must be refused (`STOP_RISK_UNVERIFIABLE`).
- [ ] **Single Position Invariant**: Verify that at most one active position is held across each account.

### 4.3 End-of-Day Reconciliation (15:30 – 16:00 IST)
- [ ] **Squareoff Verification**: Ensure all positions were closed by 15:10 IST (`INTRADAY_CLOSE`). Zero overnight exposure.
- [ ] **Cash & Fee Reconciliation**: Match broker order fees against calculated charges in `paper_ledger`. Verify cash balance is non-negative.
- [ ] **Audit Trail Export**: Export daily trade summary and rejection diagnostics to daily log folder.

---

## 5. Mac Host Supervision & Limitations

- **Host Environment**: Currently running on a Mac laptop.
- **Critical Dependency**: Mac supervision requires:
  1. Continuous AC power.
  2. Laptop lid remains open.
  3. User remains logged in.
  4. Active network connection.
- **Known Counterexample**: On 17 November, the Mac went to sleep during the midday session, missing valid signals at 12:38, 13:08, and 13:14 IST.
- **Rule**: Never claim continuous background execution while the host is asleep. LaunchAgents installed locally reside in `~/Library/LaunchAgents/` and are outside repository source control.

---

## 6. Git Governance & Submission Instructions

- **Prompt Folder**: All prompt files and documentation remain inside `docs/antigravity-handoff/2026-11-17/`.
- **Branch**: Current branch is `main`.
- **Push Policy**: **DO NOT PUSH**. The repository owner will review all changes, code, tests, and backtest evidence, and will personally push the branch to remote.
