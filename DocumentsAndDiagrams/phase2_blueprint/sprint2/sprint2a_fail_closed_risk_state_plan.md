# Sprint 2A — Fail-closed risk state (acceptance revision)

Date: 5 Nov 2026. Planning only. This document replaces the C2/C3/C4 revision from the same day, and this pass corrects the two remaining verification findings: the post-replacement durability incident, and test isolation. No application code, tests, configuration, or databases were changed while writing it. `backend/genesis.db`, `backend/live.db`, and `trades/trades.db` were not opened. `trades/daily_risk_config.json` was not opened. The daemon was not started. Live trading stays off. PDC V0 is unchanged. Sprint 2B is not this plan. Outcome arithmetic is not changed. Pytest was not run.

Sprint 2A is S2-04 plus the one writer removal C4 requires. The repository has no task named Sprint 2B. Later work stays later: S2-06 journal fail-closed, S2-07 capital fields, S2-01 executions, S2-02 outcome accounting, S2-03 null readers, S2-05 signal selection, recency.

Line ranges below were read from the current files in this working tree.

## Verdict

**B. Ready to implement this containment and this file contract after the plan is accepted. Not a trading release.**

A is not available. Unattended recovery of a full exit is still open, and `POSTED_DURABILITY_UNCONFIRMED` does not close it. C is not the verdict: the durability status, the failed-read hold, and the test-isolation edit are specified below. Nothing in those three is left for the implementer to invent.

C2 cannot be made replay-safe inside S2A. That limitation is a release blocker. A journaled exit whose replace did not happen is reconciled by section 3.5. A journaled exit whose replace completed and whose directory `fsync` failed is section 3.6. Nothing in the process replays either one. `True` from `save_config` means atomic replacement and directory `fsync` both finished. A post-replacement `fsync` failure is not `True`, does not roll memory back, and blocks further risk writes in that process. It is not `RECONCILED_UNPOSTED`. Acceptance of this plan accepts the blocker, the exit-journaling deviation, the `fsync` block, the decision to keep `reset_daily_counters`, and the rule that the two `EntryAgent` tests are not run until they inject a `tmp_path` risk agent. Do not edit source until this document is accepted. Do not start a process that journals live exits on this change. Do not run the full suite. This pass did not demonstrate the isolation edit, because the tests were not changed.

## 1. Final architecture and ownership

One ledger: `trades/daily_risk_config.json`, addressed only through the `config_path` the agent was constructed with. Tests use a path under `tmp_path`. One lock file: `risk_lock_path(config_path) = config_path.parent / (config_path.name + ".lock")`. Production lock path: `trades/daily_risk_config.json.lock`. `Path.with_suffix(".lock")` is the wrong file.

**Model B. Locked read-modify-write.** Model A is rejected. `scripts/live_scanner.py` and any second `RiskAgent` on the same path are real writers. A comment that says "only one writer" does not stop the second process. `fcntl.flock` on one host and one local filesystem does.

There is one replace implementation: `RiskAgent.save_config`. Every business mutation re-reads the ledger under that lock, applies the change to the document just read, validates it, then asks `save_config` to replace it. A stale in-memory snapshot is not the document that gets replaced.

`SafetyWallAgent.process_daily_pnl` is not a second writer. Its priced branch is removed in S2A. Leaving it dormant behind an empty `PREMIUM_PNL_METHODS` is rejected. The moment that set is non-empty, the branch truncates the ledger, sets `daily_losses_count` to 0, and drops `session_date`.

`session_blocked`, `config_unreadable`, and `directory_durability_unconfirmed` are process-local. They are not recovery records. They are not written to the ledger. A restart does not reload them.

`exit_held_for_risk` is withdrawn. `save_active_trades` truncates and swallows errors (`journal_agent.py` 77–81). A key written that way is not a durable recovery record, and S2A will not replay it.

## 2. Revised state transitions

| State | `begin_session` | Disk | Entries | Full exit |
|---|---|---|---|---|
| Missing file | Not unreadable. Under the lock, create on `True`. In-memory reserve is kept because there is no newer disk document. | New file only if replace returns. | Allowed only after `True`. | Current order. This order is not crash-safe. See section 4. |
| Corrupt or wrong type | Return `False` before any mutation. No write. | Bytes unchanged. | Blocked for this process. Both flags true. | Not journaled. Active row not removed. No alert. No outcome call. |
| Readable, stored date is today, `session_blocked` false, durability flag false | Return `True`. No re-read. No zero. No write. | Unchanged. | Existing rules. | Current order. |
| Readable, stored date is today, `session_blocked` true | Return `False`. No zero. No write. Flag stays true. | Unchanged. | Blocked. | Not journaled. This is the containment after a failed outcome on an already-open session. It is not a replay. |
| Readable, date missing or different, disk under the lock already today | Adopt that document. Do not reset. Do not replace. | Unchanged by this call. | Existing rules on the adopted document. | Current order only if this call then returns `True` with the flag clear. |
| Readable, date missing or different, disk still not today, replace and directory `fsync` both finished | Zero count and R on the adopted document, clear the wall lock unless `manual_lock`, set the date, replace. Marker lists copied. Reserve unchanged. `session_blocked` false. Return `True`. | New document, directory entry durable to the extent this filesystem honors `fsync`. | Allowed. | Current order. |
| Readable, replace did not happen | Restore the document read under the lock. If the file was missing, restore the pre-call memory. `session_blocked` true. Return `False`. `directory_durability_unconfirmed` stays false. | Last complete file, or still absent. Temp removed. | Blocked until a later `begin_session` returns `True`. Next tick retries the rollover because the stored date is still old. | Not journaled on a tick whose `begin_session` is not `True`. |
| Replace happened, directory `fsync` failed | Do not restore. Memory stays on the replaced document. `directory_durability_unconfirmed` true. `session_blocked` true. Return `False`. This is not `True`. | The new document is the current name. The previous file is not authoritative. Directory-entry durability is unconfirmed. | Blocked for this process. No further risk replace. | Not journaled on a later tick in this process. Section 5.1. |
| Outcome replace did not happen | `record_trade_outcome` restores the document read under the lock and returns `False`. Sets `session_blocked`. | Last complete file. The outcome is absent. | Blocked for this process. Same-date `begin_session` does not clear the flag. | If `record_exit` already returned a row, the active id is already gone. S2A does not put it back. Section 3.5. This row is never `POSTED_DURABILITY_UNCONFIRMED`. |
| Outcome replace happened, directory `fsync` failed | Do not restore the counters. Return `False`. Both block flags true. | New document is the name. The previous file is not authoritative. | Further risk writes blocked in this process. No second outcome post. | Journal already happened. Section 3.6. Not section 3.5. Not `RECONCILED_UNPOSTED`. No replay. |

Same stored date never zeros the count. Today those lines fall through and zero (`risk_agent.py` 151–160).

## 3. Exit and recovery sequence

### 3.1 Current sequence, unchanged until implementation

Full close inside `on_market_tick` (`manager.py` 356–382), after `begin_session`'s return is discarded (`manager.py` 192):

1. Exit detection. Holding path builds an `ExitDecision` with `is_fully_closed=True` (`manager.py` 288–296), or `ExitAgent.evaluate_exit` returns one (`manager.py` 302–311). No broker order is sent. `TradeManager` does not call `ExecutionManager`.
2. `JournalAgent.record_exit` (`journal_agent.py` 156–214):
   - Load the active file again (`165–167`). `load_failed` or a missing id returns `None` and does not delete.
   - Write exit fields onto the row (`169–182`), including `pnl_rupees=None` and `pnl_method="index_points_not_priced"`.
   - Delete the id and truncate-save the active file (`184–186`).
   - Read `live_executed_trades.json`. A parse failure becomes `[]` (`188–196`).
   - Append one row and truncate-save the executed log (`198–205`).
   - SQLite upsert is best-effort and does not roll the JSON back (`207–212`).
3. `send_exit_alert` (`manager.py` 359–364). A person can flatten from that alert. The code cannot see the broker.
4. `record_trade_outcome` (`manager.py` 366–371`), including when `record_exit` returned `None`.
5. `del active_trades[trade_id]` and `save_active_trades` again (`manager.py` 381–382`). That second save writes the tick's in-memory book. `save_active_trades` returns without writing when `load_failed` is still true (`journal_agent.py` 67–69).

`ReconciliationAgent.reconcile_positions` (`reconciliation_agent.py` 33–56) is not on this path. `BrokerBridgeAgent.place_order` rejects a live flag (`broker_bridge_agent.py` 77–89). There is no broker position for this tick to reconcile.

### 3.2 S2A sequence

This is containment. It is not replay-safe recovery.

On a full exit:

1. Exit detection stays where it is. Still no broker order.
2. If `config_unreadable` is true, or `begin_session` did not return `True`, or `session_blocked` is true: do not call `record_exit`, do not call `send_exit_alert`, do not call `record_trade_outcome`, do not `del` the id, do not call `save_active_trades` to store a hold key. The active file stays as loaded. The tick result action is `EXIT_BLOCKED`. The decision exists only in that return value and dies with the process.
3. Otherwise call `record_exit` as today. If it returns `None`: do not call `record_trade_outcome`, do not `del`, do not `save_active_trades`. Action `EXIT_JOURNAL_REFUSED`. The active bytes stay. This stops the current path that posts risk and deletes after a refused journal (`manager.py` 365–382).
4. If `record_exit` returns a row: call `record_trade_outcome` as today, with the same arguments the manager already passes. Do not change the formula.
   - Save is `True`: keep today's alert, action append, in-memory `del`, and second `save_active_trades`.
   - Save is not `True`: do not try to recreate the active row. `record_exit` has already deleted it and may already have appended the executed log. Putting the row back would make the next tick append a second journal line. Action `EXIT_RISK_UNPERSISTED`. `session_blocked` is true. Entries stay off for this process. Nothing replays the outcome.

Partial paths (liquidity hop at `manager.py` 313–354, trailing saves at 407, 437, and 445) are not full closes. S2A does not change them. Their truncate saves remain S2-06.

### 3.3 Why a durable replay does not fit in S2A

A replay-safe close needs all of the following, and S2A is forbidden from building them:

- The outcome and its `trade_id` marker commit in one replace, so a second delivery does not apply the formula again. That is S2-02. S2A only copies marker lists when they are already present. It does not add ids and it does not change the win-clear or the R-count.
- The active row stays until that replace has returned, and the executed log is appended at most once. `record_exit` deletes first (`journal_agent.py` 184–186). Changing that is S2-06. S2A does not edit `journal_agent.py`.
- The active file and the executed log have to survive a crash. Both are truncate writes (`journal_agent.py` 77–79 and 204–205). Making them atomic is S2-06.
- The design forbids an outbox and a second reserve file (`sprint2_financial_integrity_design.md`, durable-marker section and atomic-posting section). The retry state is the active row plus the marker inside the one risk document. S2A cannot use the marker as a retry key without implementing S2-02.

Risk-first without a marker duplicates the outcome if the process dies after replace and before the active row is removed. Journal-first loses the outcome if the process dies after the active delete and before replace. Neither order prevents lost exits, duplicate outcomes, duplicate journal lines, silent deletion, and an unreconciled book at the same time. S2A does not pick an order and call it recovery.

### 3.4 Target sequence, owned by S2-02 and S2-06 together

Not implemented in S2A. Neither sprint alone is enough. No outbox.

1. Detect the full exit in memory. Do not alert.
2. Under the risk lock, re-read the ledger. If this `trade_id` is already marked, do not apply the formula.
3. Apply today's later formula and the marker to that document. Validate. Atomic replace. Directory `fsync`.
4. If the replace is not `True`: leave the active row, do not append the executed log, do not alert, do not delete. A later tick retries. An absent marker means the formula did not commit.
5. If the replace is `True`: append the executed row only when that `trade_id` is not already there, then atomic-replace the active file without the id, then alert.
6. Restart after step 3 and before step 5: marker present, active row present. Do not apply the formula again. Continue at the journal step.
7. Restart after the append and before the active removal: do not append again. Remove the id only.

Until that pair ships, the operations in section 13 stay disabled.

### 3.5 Manual reconciliation when the journal succeeded and the replace did not happen

This is an operator procedure. S2A does not implement it in code. It does not add a reader, an outbox, a replay, or a change to the outcome formula. No agent loads the record. Completing the record does not clear `session_blocked` and does not call `record_trade_outcome`.

This section is only the case where `record_exit` has returned a row, or the executed log already contains that `trade_id`, and the risk replace did not happen. `save_config` is not `True`, and `directory_durability_unconfirmed` stays false. The current ledger is still the previous document. The outcome is absent from it. A completed replace is not this section, even though that save is also not `True`. The active id stays deleted. The operator does not put the row back and does not hand-edit `daily_risk_config.json` to post the outcome. Posting is S2-02. A second call to `record_trade_outcome` is not authorized here either. S2A does not make one.

The operator writes one record outside `trades/`, `backend/`, `data/`, and the risk-file directory. The trading process is stopped before the record is treated as finished. The record is not a path `RiskAgent` or `JournalAgent` is given.

Required fields:

| Field | Content |
|---|---|
| `trade_id` | The id `record_exit` returned, or the id in the executed row. |
| `journal_exit_reference` | Executed-log path, plus that row's `trade_id` and `exit_timestamp`. This names the journal row. It is not a second copy of the book. |
| `broker_confirmed_exit_quantity` | Quantity the operator read from the broker terminal. Null when the terminal was not checked or has no fill. Null is not zero. |
| `broker_confirmed_exit_price` | Price the operator read from that fill. Null under the same rule. |
| `broker_confirmed_net_pnl` | Broker-confirmed net the operator read. Null under the same rule. S2A does not calculate it. |
| `risk_accounting_status` | One of the statuses in this section, or `POSTED_DURABILITY_UNCONFIRMED` from section 3.6. One status. The unfinished hold in section 3.6 leaves this field empty. |
| `reconciled_at` | Timestamp the operator finished the record. |
| `operator` | The human's name as they sign it. Not a process, not a flag. |
| `action_reference` | What was done. Section 3.5 allows only: the outcome was left unposted, the active row was not restored, and entries stay off. Section 3.6 uses the text in that section. |
| `resumption_scope` | `none` or `reload_only`. |
| `resumption_authorized_by` | Empty when scope is `none`. Otherwise the same string as `operator`. |
| `resumption_authorized_at` | Empty when scope is `none`. Otherwise the timestamp of that signature. |

### 3.5.1 Incident statuses

A file hash does not assign any status in this table. The operator parses the current ledger pathname and reads the fields below. `save_config` writes those fields at `risk_agent.py` 106–119. `daily_cumulative_r` is stored as `round(self.daily_cumulative_r, 2)` (line 113). `record_trade_outcome` (`risk_agent.py` 123–146) changes `daily_losses_count`, `daily_cumulative_r`, `safety_wall_reserve`, and `is_safety_wall_locked`. It does not take a `trade_id` and it does not write one. S2A copies `loss_counted_trade_ids` and `reserve_posted_for` only when they are already present. It does not append this exit's id.

| Status | Replacement | What the parsed ledger must show | Scope | Entries | Second post | Replay |
|---|---|---|---|---|---|---|
| `UNPOSTED` | Did not happen. | JSON object. This `trade_id` is absent. Accounting fields are still the pre-outcome document: `daily_losses_count`, `daily_cumulative_r`, `safety_wall_reserve`, `is_safety_wall_locked`, and `session_date` are unchanged by this exit. Broker fields on the incident may be empty. | `none` | No | No | No |
| `BROKER_UNCONFIRMED` | Did not happen. | Same ledger evidence as `UNPOSTED`. Any of the three broker fields is null. `BrokerBridgeAgent` has no live adapter (`broker_bridge_agent.py` 77–89), so this is the expected status until a person reads a terminal. | `none` | No | No | No |
| `RECONCILED_UNPOSTED` | Did not happen. | Same ledger evidence as `UNPOSTED`. All three broker fields are present. The journal reference matches `trade_id`. The outcome is absent. | `reload_only` only | No | No | No |
| `POSTED_DURABILITY_UNCONFIRMED` | Completed. Directory `fsync` did not. | Section 3.6. The outcome is in the current pathname. Not this section. | `none` only | No | No | No |

`RECONCILED_UNPOSTED` is illegal when `os.replace` has returned. A completed replace is not "outcome still not in the risk file." `entries_allowed` is not a legal value in S2A. A record that uses it is not a finished reconciliation.

Who authorizes resumption: only the human named in both `operator` and `resumption_authorized_by`. Those two strings must match. A restart, `session_blocked` disappearing on a new process, a hash match, and the existence of the file do not authorize anything.

What that human may authorize: `reload_only`, and only from status `RECONCILED_UNPOSTED`. `reload_only` means a new process may load the risk file. It does not mean entries, alerts, or the scanner. New entries stay off until S2-02 and S2-06 have posted this `trade_id` idempotently under a later accepted plan. The signing human cannot authorize entries while the status is `UNPOSTED`, `BROKER_UNCONFIRMED`, `RECONCILED_UNPOSTED`, or `POSTED_DURABILITY_UNCONFIRMED`. The signing human cannot authorize entries, replay, or a second post from the unfinished hold in section 3.6.

### 3.6 Post-replacement durability incident

Operator procedure. Not code. No reader, no outbox, no replay, no second call to `record_trade_outcome`, no hand-edit of the ledger. Completing the record does not clear `session_blocked` and does not call `record_trade_outcome`.

This section applies when `record_exit` has returned a row, or the executed log already contains that `trade_id`, and the outcome replace has returned from `os.replace`, and the directory `fsync` then failed. `save_config` returned not `True`. `directory_durability_unconfirmed` is true. Memory keeps the new document. The current ledger pathname is the replaced document. The previous file is not authoritative. Further risk replaces in that process do not run.

`POSTED_DURABILITY_UNCONFIRMED` means all four of these, and no others:

1. Replacement completed.
2. This trade's outcome is present in the current ledger pathname.
3. Directory durability is not confirmed.
4. No second posting is permitted.

It is not `UNPOSTED`, `BROKER_UNCONFIRMED`, or `RECONCILED_UNPOSTED`. A null broker field does not move it to `BROKER_UNCONFIRMED`. `resumption_scope` is `none`. `resumption_authorized_by` and `resumption_authorized_at` stay empty. The allowed `action_reference` text is: replacement completed, the ledger was read, this `trade_id` and the accounting fields matched the expected outcome, directory durability was not confirmed, the outcome was not posted again, the active row was not restored, entries stay off, and nothing replays.

The same external record as section 3.5 is used. It still lives outside `trades/`, `backend/`, `data/`, and the risk-file directory. No agent is given that path.

#### Verification

Do this while the process that saw the `fsync` failure is still up. Stop the process only after the read is written into the incident record. A restart before that read is not verification.

The error log names the path and says directory `fsync` failed after replace. The log does not contain the file body. The operator opens the current ledger pathname and parses it as JSON. `sha256` and byte size may be written beside that parse. They do not establish the outcome.

Expected outcome for this `trade_id`, taken from the process memory after the formula and before any later mutation. The process is blocked, so there is no later mutation. The `trade_id` is the id `record_exit` returned at `manager.py` 357, which is the id passed into the outcome call at `manager.py` 366–371. The manager does not pass that id into `record_trade_outcome`.

| Ledger field | Expected value |
|---|---|
| `trade_id` inside the document | This incident's id must occur in the parsed object. The only lists that can hold it are `loss_counted_trade_ids` and `reserve_posted_for`, and only if those keys were already present and already contained it. There is no top-level `trade_id` key in the payload at `risk_agent.py` 106–119. |
| `daily_losses_count` | The in-memory count after this outcome. |
| `daily_cumulative_r` | The in-memory R after this outcome, compared to the file as `round(value, 2)`. |
| `safety_wall_reserve` | The in-memory reserve after this outcome. |
| `is_safety_wall_locked` | The in-memory lock after this outcome. |
| `session_date` | The in-memory session date. This call does not change it. |
| `base_capital`, `reserve_basis`, and `manual_lock` when that key is present | Unchanged from that same memory. |
| `effective_capital` | `base_capital + safety_wall_reserve`, which is what `save_config` writes. |

All of those reads have to succeed, and every value has to match, before the status may be signed. A hash of matching bytes is not a substitute for any row. A matching count with a missing or different `trade_id` is not a match. An id that appears only in the journal, and not in the parsed ledger, is not a match.

S2A does not append this exit's id. For a ledger this sprint writes, the `trade_id` row fails. The operator then follows the mismatch hold below and does not sign `POSTED_DURABILITY_UNCONFIRMED`. That is the S2A result. The status stays in the table so this incident is not relabeled `RECONCILED_UNPOSTED`. Signing it would require the id to already be in a copied marker list and the accounting fields to match this outcome. A pre-existing id with different accounting fields is a mismatch, not this status. S2A will not hand-edit the id in to make the status signable.

#### Ledger unreadable, or contents do not match

The pathname cannot be read, the bytes are not one JSON object, the `trade_id` is not in the document, or any accounting field differs from the expected outcome.

- Do not assign `POSTED_DURABILITY_UNCONFIRMED`.
- Do not assign `UNPOSTED`, `BROKER_UNCONFIRMED`, or `RECONCILED_UNPOSTED`. Those three require a readable document whose accounting fields are still the pre-outcome document. A completed replace is not that document. An unreadable path does not prove the outcome is absent.
- Leave `risk_accounting_status` empty. The record is not a finished reconciliation.
- `resumption_scope` is `none`. The signature fields stay empty.
- `action_reference` states the read failed or names the field that differed. It states that no second post was made.
- Do not start a process. Do not write the dead process's memory back. Do not hand-edit the ledger. Do not delete the pathname. Do not call `record_trade_outcome`. Do not restore the active row. Entries stay off. Nothing replays.
- Trading stays stopped. A later plan is what would post this `trade_id` once, idempotently, and only if that plan can show the formula has not already been applied. This hold does not authorize that plan, and it does not authorize a second application.

#### Recovery when the status is signed

The process is stopped after the matching read. No new process is started. `reload_only` is not available from this status: a power loss can still expose the previous directory entry, and loading that entry would make a posted outcome look absent.

A later read of the same pathname, after the stop, is another parse of the same fields, not a hash.

- The id and the accounting fields still match. The current name still holds that outcome. Durability is still unconfirmed. The status stays `POSTED_DURABILITY_UNCONFIRMED`. Entries stay unauthorized. Replay stays unauthorized. A second post stays unauthorized. The active row stays deleted.
- The later read fails, or any field differs. Withdraw the status. The incident becomes the unfinished hold above. Do not write memory back. Do not post again. The formula may already have been applied in a document that is no longer the name.

There is no S2A step that fsyncs the directory after the fact, rewrites the ledger to make it durable, or clears the incident. `True` did not happen. The release blocker in section 13 stays.

## 4. Crash-point analysis

| Boundary | Active file | Executed log | Risk file | After restart |
|---|---|---|---|---|
| Crash before exit detection | Unchanged. | Unchanged. | Unchanged. | Next tick can detect again. |
| S2A block: session not `True` or unreadable | Unchanged. S2A does not write a hold key. | Unchanged. | Unchanged. | Flags are gone. If the file is still corrupt, the new process sets them again from the bytes. If a person replaced the file, the new process can open a session. The exit decision from the dead process is gone. The open row is still there, so a later tick can detect again. That is not a saved decision. |
| Inside `record_exit`, after the active delete save (`journal_agent.py` 186) and before the executed append (`204`) | Id gone. The delete itself is a truncate, so a crash inside that `"w"` can also destroy the rest of the book. | Previous bytes. | Not posted yet. | Row is gone. S2A will not recreate it and will not post it. Lost exit. Release blocker. S2-06. |
| After executed append, before `record_trade_outcome` | Id gone. | One new row. | Previous document. | Journal exists, risk does not, book does not. Restart does not post. Lost risk outcome. Release blocker. |
| `record_trade_outcome` computed new counters, crash before `os.replace` | Id already gone if `record_exit` ran. | Row already appended if `record_exit` ran. | Previous document. Temp is not the ledger. | Same as the previous row. S2A does not replay. |
| `os.replace` returned, crash before the manager's second active save | Id already removed by `record_exit`. | Row present. | New document. | Risk matches the journal. Second save is redundant unless the tick had other in-memory edits. |
| `os.replace` returned, directory `fsync` failed | Active state unchanged by the fsync itself. | Unchanged by the fsync itself. | New name is the new document. The previous file is not authoritative. A power loss can still show the previous directory entry. | Not `True`. Memory stays on the new document. Further risk replaces in this process are blocked. If this replace was an outcome, section 3.6. A hash is not that procedure. Otherwise section 5.1. Do not restart until the matching procedure is signed. |
| `record_exit` returned `None` | S2A does not delete and does not post. | Unchanged. | Unchanged. | Row still loadable. |
| Outcome replace did not happen after `record_exit` returned a row | Already deleted. S2A leaves it deleted. | May already contain the row. | Previous document. | No replay. Operator record in section 3.5. `session_blocked` died with the process. Release blocker. |
| Outcome replace happened, directory `fsync` failed, after `record_exit` returned a row | Already deleted. | Row present. | New document is the name. | Memory keeps the new counters. No further risk write in this process. Section 3.6 only. Not `RECONCILED_UNPOSTED`. No replay. No second post. |
| Crash while `save_active_trades` truncates on a hop or trail | Whole book may be torn. | Unchanged. | Unchanged. | S2-06. S2A does not claim this save is durable. |

`ExecutionManager.execute_exit` (`execution_manager.py` 102–138) is not called. A book that says open and a broker that a person flattened by hand, or the reverse, cannot be repaired by S2A. `ReconciliationAgent` only compares counts when something else calls it. S2A does not wire it.

## 5. Persistence result contract

`save_config` returns `bool`. The only success value is `True`, and `True` means both of the following finished: `os.replace` returned, then directory `fsync` returned. The caller checks `save_config() is True`. `False`, `None`, any other value, and any exception that escapes are not success. The implementation does not use a bare `return`. Today's `begin_session` treats "did not raise" as success (`risk_agent.py` 164–171).

Three different facts:

- **Atomic replacement.** `os.replace` has returned. The ledger name is the new document. The previous file is not authoritative. Memory stays on the new document.
- **Directory-entry durability.** The parent's directory `fsync` has returned after that replace. This is required before `save_config` may return `True`.
- **Post-replacement fsync failure.** Replace has already succeeded and the directory `fsync` then raises. Durability is unconfirmed. The return is `False`. `directory_durability_unconfirmed` is set. Memory is not rolled back. The previous document is not treated as the ledger.

`save_config` catches I/O errors from the temp write, the temp `fsync`, `os.replace`, and the directory `fsync`. It does not raise those. It must not raise after `os.replace` has returned, because an exception there would send `begin_session` into the restore path. A monkeypatched `save_config` may still raise. `begin_session` treats that exception as a replace that did not happen. The existing test (`test_sprint1_guards.py` 94–104) keeps raising. The fsync-after-replace path must not use that same exception. It returns `False` and sets the flag.

Caller order after `save_config` returns, before any restore:

1. If the result is `True`: clear `directory_durability_unconfirmed` and `session_blocked`. Commit.
2. If `directory_durability_unconfirmed` is true: do not restore. Set `session_blocked`. Return `False`.
3. Otherwise the replace did not happen: restore, set `session_blocked`, return `False`.

| Outcome | Return | Flag | Exception from `save_config` | Memory | Disk | Later risk operations in this process |
|---|---|---|---|---|---|---|
| Atomic replacement completed, directory `fsync` not finished yet | No return yet. | Not set yet. | None yet. | New document. Must stay the new document. | Ledger name is the new document. Temp is gone. | No return yet, so no further call is inside this save. |
| Directory `fsync` completed | `True` | Cleared. | None. | New document. | New document. Directory entry durable to the extent this filesystem honors `fsync`. | Not blocked. `begin_session` commits and returns `True`. |
| Post-replacement `fsync` failure | `False` | `directory_durability_unconfirmed` true. | Caught inside `save_config`. Not propagated. | New document. Not rolled back. | New document is the name. Previous file is not authoritative. Directory entry may not survive power loss. | Blocked. `begin_session` and `record_trade_outcome` return `False` and do not replace again. `evaluate_order` rejects. An outcome replace is section 3.6. Any other replace is section 5.1. |
| Replacement failed | `False` | Flag stays false. | Caught inside `save_config` for its own I/O before replace. A monkeypatch may still raise; the caller treats a raise as this row. | Caller restores the document read under the lock. If the file did not exist, caller restores pre-call memory. | Previous complete file, or still absent. Temp removed. | Entries blocked. Next tick may retry a rollover when the stored date is still old and the file is readable. An outcome that already followed a successful `record_exit` is not retried. |
| Save skipped because the configuration is unreadable | `False` | Flag stays false. | None. | Unchanged. Constructor defaults if load failed. | Unchanged. No temp. No replace. | Every later `save_config`, `begin_session`, and `record_trade_outcome` returns `False` and does not write. `evaluate_order` rejects. Flags are not cleared and are not reloaded after a restart. |

A skipped save, a failed replace, and a post-replacement `fsync` failure are never `True`. Memory is not rolled back after `os.replace` has succeeded.

### 5.1 Directory-durability reconciliation

Operator procedure. Not code. The process that saw the `fsync` failure is still up and is not allowed to replace the ledger again.

This section is a replace that is not an outcome. A `record_trade_outcome` replace uses section 3.6 and does not use this hash path. A hash does not establish that an outcome was posted, and it does not assign `POSTED_DURABILITY_UNCONFIRMED`.

1. The error log from `save_config` names the path and says directory `fsync` failed after replace. The log does not contain the file body.
2. While that process is still up, the operator parses the ledger and records `session_date`, `daily_losses_count`, `daily_cumulative_r`, `safety_wall_reserve`, and `is_safety_wall_locked`, plus `sha256`, byte size, path, log timestamp, `operator`, and `action_reference`. The hash is additional. The record lives outside `trades/`, `backend/`, `data/`, and the risk-file directory. No agent reads it.
3. The operator then stops the process. A restart before step 2 is not reconciliation.
4. The operator parses the path again. If those five fields and the hash match, the directory entry survived until shutdown. The operator may sign `reload_only` on that durability record. The new process loads the path. It does not inherit the flag. If an exit incident from section 3.5 is also open, section 3.5 still forbids entries. If section 3.6 is open, including the unfinished hold, `reload_only` is not signed.
5. If the fields or the hash do not match, the name on disk is not the document that was replaced. Do not start the process. Do not write the dead process's memory back. The bytes now on the path are what the filesystem has. Trading stays stopped. Restore the backup taken before the session if those bytes are not an acceptable ledger. The rule that the previous file is not authoritative applies while the replacement is still the name. It does not invent a new document that is no longer the name.

The signing human is the only authority, same rule as section 3.5. The flag, a restart, and the hash file's existence do not by themselves authorize entries.

In-memory reserve after a corrupt load stays at the constructor value. That value is not written back.

Temp name, in the ledger directory: `.{ledger name}.{pid}.{uuid}.tmp`. Under the lock, before creating a temp, delete files in that directory whose names match `.{ledger name}.*.tmp`. Do not delete the ledger. Do not delete the `.lock` file. A crash before replace leaves the previous ledger. The next locked save sweeps the temp. `_load_config` opens only `config_path`.

## 6. Writer inventory and concurrency policy

Lock: `fcntl.flock` exclusive on `risk_lock_path(config_path)`, one file descriptor for the whole critical section. The agent counts depth so `save_config` does not open a second descriptor and deadlock the same process. The lock file is not the ledger and is not deleted. The kernel releases the lock when the process dies or the last fd closes. A leftover lock file is not a held lock. The next `flock` works.

Supported: macOS and Linux, local filesystem, one host. If `fcntl` or `flock` fails, `save_config` returns `False` and does not write. Windows, NFS, and a second host are unsupported.

Stale memory: a mutation adopts the bytes just read. The replace writes that adopted document after the mutation, not a copy loaded on a previous tick. A direct `save_config` that finds the file bytes different from the bytes this critical section adopted does not replace. It loads those bytes into memory and returns `False`. `begin_session` does not then restore a pre-call snapshot over that adopted document.

Same-date `begin_session` is safe without a re-read because it is not a disk mutation. The early return (`risk_agent.py` 151–152) returns when `session_date` already equals today and `session_blocked` is false. S2A keeps that return, and also returns `False` without writing when `session_blocked` or `directory_durability_unconfirmed` is true. Neither return calls `save_config`. A stale memory image cannot replace the ledger if no replace runs. This is not a claim that memory matches disk. Another process may have written a newer document. This process keeps its stale counters until the next real mutation. That next mutation is `record_trade_outcome` or a date-change `begin_session`: both re-read under the lock and apply the change to the document just read, so the stale counters are not what gets replaced.

The unsaved count in `test_sprint1_guards.py` 75–77 stays across the same-date call for that reason. The test does not assert that the unsaved 1 survives the later `record_trade_outcome`. That later call re-reads. A win still clears the count under the existing formula, which is what the test asserts next.

Every disk mutation uses locked read-modify-write against the latest disk document:

- Date-change `begin_session` (`risk_agent.py` 153–171, save at 165).
- `record_trade_outcome` (`risk_agent.py` 123–146, save at 146).
- `reset_daily_counters` after the delegation, because it becomes `begin_session` (`risk_agent.py` 173–179).
- Direct `save_config` (`risk_agent.py` 103–121). Under the lock it re-reads. If the file exists and this hold did not just adopt those bytes, it adopts them, returns `False`, and does not replace.

`__init__` (`risk_agent.py` 69–76) and `_load_config` (`risk_agent.py` 80–101) change memory only. `evaluate_order` (`risk_agent.py` 181–279) does not change the counters and does not write. They are not disk mutations. The search of `*.py` for `save_config(` finds only the three call sites at `risk_agent.py` 146, 165, and 179. Tests replace the method (`test_sprint1_guards.py` 97). No script calls `save_config` directly.

| Writer | Site | Class | S2A rule |
|---|---|---|---|
| `RiskAgent.save_config` | `risk_agent.py` 103–121 | Direct replace. | Only replace implementation. Bool contract in section 5. Refuses a stale snapshot. |
| `RiskAgent.begin_session` | `risk_agent.py` 148–171, call at 165 | Indirect. | Lock, re-read when the date must change, mutate the adopted document, replace. |
| `RiskAgent.record_trade_outcome` | `risk_agent.py` 123–146, call at 146 | Indirect. | Lock, re-read, apply the existing formula to the adopted counters, replace. No arithmetic edit. |
| `RiskAgent.reset_daily_counters` | `risk_agent.py` 173–179, call at 179 | Indirect, dormant. No `*.py` caller. | Delegates to `begin_session`. Not a second reset. |
| `TradeManager.on_market_tick` | `manager.py` 192 and 366–371 | Indirect, through the two methods above. | Observes the bool. Containment in section 3.2. |
| `SafetyWallAgent.process_daily_pnl` | `safety_wall_agent.py` 78–149. Unpriced return 85–94. Reserve mutation 100–114. Lot walk 116–125. Risk-file truncate 127–137. `_save_state()` at 139. | Direct risk-file writer on the priced path. Dormant today only because `PREMIUM_PNL_METHODS` is empty at line 28. | Writer removal only. Unpriced return at 85–94 stays, including its text. Priced path still sums `today_net_pnl` for the report. It does not add or subtract `self.reserve_balance`, does not change `current_lots`, does not run the lot walk as a published tier, does not open the risk file, and does not call `_save_state`. `added_to_reserve` is 0. `current_reserve_balance` and `tomorrow_approved_lots` stay the loaded values. `action_taken` says the reserve was not moved and the risk file was not written. Briefing, autopsy, and the summary type stay. |
| `SafetyWallAgent._save_state` | `safety_wall_agent.py` 69–76 | Direct truncate of the state file. | Not called from `process_daily_pnl` after this change. The method can stay. |
| `PostMarketManager.run_daily_review` | `post_market_manager.py` 43–47 and 115 | Indirect. | Still calls `process_daily_pnl`. That call no longer writes either file. |
| `scripts/live_scanner.py` | `TradeManager` at 199–204, tick at 334–344, `PostMarketManager` at 208–212, review at 277–286 | Indirect process on `trades/`. | Not edited. After S2A its risk writes are only `RiskAgent` under the lock. It stays operationally disabled for this change. |
| `scripts/diagnose_trades.py` | `TradeManager` at 23–29, tick at 86 | Indirect. Hard-coded `tempfile.mkdtemp()`. | Not a production-path writer as written. Not edited. Still disabled against `trades/`. |
| `scripts/resimulate_since_oct15.py` | Temp dir at 72–74, `TradeManager` at 84–90, ticks at 181, 208, 234 | Indirect. Temp dir. | Same as diagnose. |
| `EntryAgent.__init__` | `entry_agent.py` 133, `RiskAgent()` when no agent is injected | Dormant constructor. Default path `trades/daily_risk_config.json` (`risk_agent.py` 66). | Does not call `save_config` today. Calls are `evaluate_order` only. The daemon injects the manager's agent (`manager.py` 144–148). S2A does not remove the fallback. A later save on that object uses the same lock and the same stale-snapshot refusal. |
| `PreMarketManager` | Read at `pre_market_manager.py` 95–103. `_save_plan` writes `pre_market_plan.json` at 142–164. | Reader of the risk file. | No risk write. Not edited. |
| `ExecutionManager`, `BrokerBridgeAgent`, `ReconciliationAgent` | Cited in section 3. | Not risk-file writers. Not on the tick. | Not edited. Not a reconciliation design. |

Lock failure and process death: no replace. The ledger stays the last successful document. The next process loads that document. In-memory counters from the dead process are gone. That includes a mutation that had not reached `os.replace`. It also includes `session_blocked` set because a journaled exit failed to post. Restart does not reconstruct that block. That is why restart is not a recovery procedure.

## 7. Failure and recovery scenarios

**Corrupt `{`.** Load sets both flags and assigns nothing from the file. No save. The active row is not removed and the exit is not journaled. Risk bytes stay. A later tick in the same process does not write. Restart blocks again only if the bytes are still bad.

**Readable file, no `session_date`.** Not corrupt. The first `begin_session` that gets `True` writes today's date, count 0, and R 0, and copies reserve and any marker lists. This is the one allowed reset. The numbers on disk were not read for this plan.

**Missing file.** Not unreadable. `begin_session` creates one on `True`. Deleting the live file is not recovery. A create persists the in-memory reserve.

**JSON array, string reserve, `daily_losses_count` of `1.5`, a bool stored as a string, a marker that is not a list of strings.** Same as corrupt. No write.

**`save_config` returns `False` or `None` without raising, and the durability flag is false.** Not success. Rollover restores the adopted document and returns `False`.

**Directory `fsync` fails after replace.** Return `False`. Durability flag true. Memory stays on the new document. Disk name is the new document. The previous file is not authoritative. Further risk replaces in this process do not run. An outcome replace is section 3.6. Any other replace is section 5.1.

**Exit while the session is not open.** Active row stays. No journal line. No alert. No risk post. The decision is not stored. Recovery is "the row is still active and a later successful session may detect an exit again," which can be a different price. S2A does not freeze the old decision.

**Exit journaled, risk replace failed.** Active row stays deleted. S2A does not recreate it. Risk document stays previous. This process blocks entries. A restart will not post the missing outcome and will not see the active row. The operator fills section 3.5. The software does not. Release blocker.

**Exit journaled, replace succeeded, directory `fsync` failed.** Memory and the current name hold the new counters. Further risk writes in this process are blocked. Section 3.6. The operator reads the trade id and the accounting fields. A hash does not sign `POSTED_DURABILITY_UNCONFIRMED`. A missing id or a field mismatch leaves the status empty and does not use `RECONCILED_UNPOSTED`. No replay. No second post.

**Second `RiskAgent` on one host.** The lock and the re-read stop a lost update between them. They do not make the outcome idempotent. The same trade posted twice still applies the formula twice. S2-02.

**Priced `process_daily_pnl`.** Does not open the risk file and does not call `_save_state`. `self.reserve_balance` stays. `added_to_reserve` is 0. `today_net_pnl` is still the sum of the priced rows. The unpriced return is unchanged.

**Stale temp.** Next locked save deletes `.{name}.*.tmp`. Load ignores them.

**Person edits the ledger while a process is in memory.** The next mutation re-reads under the lock. A direct save whose adopted bytes do not match does not overwrite the newer file.

## 8. Revised test matrix

New file: `backend/tests/test_sprint2a_risk_file.py`. All paths under `tmp_path`. No test opens `trades/daily_risk_config.json`, `trades/daily_risk_config.json.lock`, `trades/live_executed_trades.json`, `backend/genesis.db`, `backend/live.db`, or `trades/trades.db`. No new test constructs `RiskAgent()` or `EntryAgent()` without a `tmp_path` config. The two existing constructors that still do are section 8.1. They are edited before any pytest that would collect them. Run named node ids from `backend/` with `MPLCONFIGDIR` set, and only after that edit. Do not pass the module path `backend/tests/test_trading_and_post_market_crews.py`. Do not run the full suite. Do not start the daemon.

Do not edit the outcome assertions in `backend/tests/test_sprint1_guards.py`.

| Id | Assert in S2A |
|---|---|
| B-01 | File bytes are `{`. `begin_session` is `False`. `save_config()` is `False`. `evaluate_order` is not approved. `record_trade_outcome` returns `False` and does not write. A manager tick that would fully exit does not call `record_exit`, does not alert, and does not drop the active id. The active file has no `exit_held_for_risk` key. Risk bytes unchanged. |
| B-03 | Readable file, `manual_lock` true, wall locked. Lock and reserve survive the new date. Entry rejected. |
| B-04 | `os.replace` raises after a good document with count 2. Count, date, and bytes stay. Later `evaluate_order` rejected. A `save_config` that returns `False` and does not raise is failure. A `save_config` that returns `None` is failure. |
| B-05 write half | Covered by S-01. The unpriced early return is not edited. |
| B-26h | Stored date is today, count is 2, not blocked. `begin_session` returns `True`. Count stays 2. No write. |
| B-26i | Save failure keeps the old date. Unreadable bytes are not replaced. A 0 count is not written over them. |
| J-01 | Journal failure. After a good session, `record_exit` returns `None` on a full exit. `record_trade_outcome` is not called. The active id remains. Risk bytes unchanged. No second executed row. |
| R-01 | Risk persistence failure after journal success. Real `record_exit` against a `tmp_path` book, then `save_config` returns `False` before replace. Executed log contains the `trade_id`. Active file does not. Risk bytes unchanged. A second tick does not call `record_trade_outcome` and does not append a second executed row. |
| M-01 | Manual reconciliation state for a replace that did not happen. The test builds the section 3.5 record under `tmp_path` outside the agent's directory. Required fields are present. A null broker field forces `BROKER_UNCONFIRMED` and forbids `entries_allowed`. A readable pre-outcome fixture is `UNPOSTED` or `RECONCILED_UNPOSTED` only when the accounting fields are unchanged. The risk agent is not given that path. A new `RiskAgent` on the unchanged risk fixture does not post the outcome. |
| M-02 | Section 3.6 under `tmp_path`. A fixture ledger that contains this `trade_id` and the expected `daily_losses_count`, `daily_cumulative_r`, `safety_wall_reserve`, `is_safety_wall_locked`, and `session_date` may be labeled `POSTED_DURABILITY_UNCONFIRMED`. Scope is `none`. It is not `RECONCILED_UNPOSTED`. A hash with no parsed fields cannot take that label. A parsed ledger whose count differs, or whose `trade_id` is absent, leaves `risk_accounting_status` empty and does not take `RECONCILED_UNPOSTED`. Bytes `{` do the same. The agent is not given the incident path and does not post. |
| F-01 | Replacement success, then directory `fsync` failure. `os.replace` has returned and the directory `fsync` raises. `save_config` is `False`. `directory_durability_unconfirmed` is true. Memory keeps the new date and the new count. The path reads as the new document. A later `begin_session` or `record_trade_outcome` does not change the bytes and does not restore the old date. |
| W-01 | Concurrent stale writers. Two threads on one `tmp_path` file, count starting at 0, each calls `record_trade_outcome` with unpriced `realized_r=-0.8`. The file ends at count 2. Separately, memory count 1 against a file that already has count 7: direct `save_config` returns `False` and the file still has 7. |
| S-01 | Safety-wall priced branch cannot write. The test patches `PREMIUM_PNL_METHODS` in `safety_wall_agent` to include `broker_premium_net` for that test only. One positive row. Risk path and state path stay absent or unchanged. `reserve_balance` unchanged. `added_to_reserve` is 0. `today_net_pnl` equals the row. The production frozenset stays empty. |
| Same-date blocked | `session_blocked` true and the stored date is today. `begin_session` returns `False`, count unchanged, no write. |
| B-02 session half | Readable reserve and R come from the fixture. No `session_date`. After a later date and `True`: reserve unchanged, R 0, count 0, date is the new date, pre-seeded marker lists unchanged, absent marker keys still absent. Do not assert `cash_inr`, `margin_inr`, or `risk_fraction`. Those are S2-07. |
| B-26g session half | Count 2 on yesterday. First `True` sets count 0 and keeps marker lists. Second `begin_session` for today does not zero. Do not add the S2-02 loss call. |

Also in the new file:

- Missing file: `config_unreadable` is false, and a `True` result creates a document.
- Root is a JSON array: unreadable, bytes unchanged.
- `safety_wall_reserve` is a string, `daily_losses_count` is `1.5`, `is_safety_wall_locked` is `"false"`, a marker is an object: each is unreadable, bytes unchanged.
- `session_date` null is the missing-date reset, not corrupt.
- `risk_lock_path` equals `parent / (name + ".lock")` and the path is under `tmp_path`. `Path.with_suffix(".lock")` is not that path. The production lock path does not exist after the test.
- A leftover `.{name}.*.tmp` is not what `_load_config` reads, and a later locked save removes it.
- `reset_daily_counters` still exists. Same date does not zero. A new date returns the `begin_session` bool. The warning is a `DeprecationWarning`.

Existing tests, not edited:

- `test_ist_session_resets_on_date_change_and_blocks_when_save_fails`. Date change, same-date keep of the unsaved count (75–77), and the raising `save_config` monkeypatch (94–104) stay. The win-clear and the two unpriced `realized_r=-0.8` increments stay until S2-02. A failed replace may restore the document read under the lock. That test does not assert the count after the raising save. It does assert `session_date == "2026-11-04"`, `session_blocked`, and the rejection string.
- `test_manual_lock_survives_the_new_session`.
- `test_unpriced_exit_leaves_rupees_null_and_reserve_unchanged`.
- `test_safety_wall_50_percent_allocation`. Its row has no `pnl_method`, so it stays on the early return.
- `test_risk_agent_circuit_breaker`, `test_safety_wall_50_percent_ratchet_and_circuit_breaker`, `test_risk_agent_super_trend_4_lots`, `test_risk_agent_with_order_flow`.

### 8.1 Test-isolation inventory

Searched `*.py` for `RiskAgent(` and `EntryAgent(`. `EventRiskAgent()` at `backend/tests/test_pre_market_and_execution_crews.py` 78 is a different class and does not construct `RiskAgent`. Scripts are not tests. `scripts/live_scanner.py` 199–204 builds `TradeManager` on `ROOT_DIR / "trades"` and is not run. `scripts/diagnose_trades.py` 22–27 and `scripts/resimulate_since_oct15.py` 72–90 already pass a temp directory. They stay unrun.

The default path is `Path("trades/daily_risk_config.json")` (`risk_agent.py` 66). It is relative to the process cwd. `_load_config` opens that path when it exists (`risk_agent.py` 80–85). `EntryAgent.__init__` calls `RiskAgent()` when no agent is injected (`entry_agent.py` 133). `evaluate_sweep` then calls `evaluate_order` (`entry_agent.py` 213, 310, and 423). `evaluate_order` does not save. Construction still opens the default file. `TradeManager` sets `config_path` from `trades_dir / "daily_risk_config.json"` when `trades_dir` is set (`manager.py` 134–139) and passes that agent into `EntryAgent` (`manager.py` 144–148). `trades_dir is None` would hit the default. No current test does that.

| Test | Constructor line | Isolation now | Required mechanism |
|---|---|---|---|
| `test_unpriced_exit_leaves_rupees_null_and_reserve_unchanged` | `test_sprint1_guards.py` 47. `cfg` at 46. | `config_path=tmp_path / "risk.json"`. | Keep. Do not retarget. |
| `test_ist_session_resets_on_date_change_and_blocks_when_save_fails` | `test_sprint1_guards.py` 63. `cfg` at 62. | Same. | Keep. |
| `test_manual_lock_survives_the_new_session` | `test_sprint1_guards.py` 117. `cfg` at 107. | Same. | Keep. |
| `test_journal_failed_load_refuses_entry_and_manager_does_not_trade` | `test_sprint1_guards.py` 230. `trades` at 204. | `TradeManager(trades_dir=tmp_path / "trades")`. Injected `EntryAgent`. | Keep. |
| `test_risk_agent_circuit_breaker` | `test_trading_and_post_market_crews.py` 88. `cfg` at 87. | `config_path=tmp_path / "daily_risk.json"`. | Keep. Run by node id only, after the two edits below. |
| `test_risk_agent_super_trend_4_lots` | Same file, 110. `cfg` at 109. | Same. | Keep. Node id only. |
| `test_safety_wall_50_percent_ratchet_and_circuit_breaker` | Same file, 614. `cfg` at 613. | Same. | Keep. Node id only. |
| `test_directional_thesis_structure_and_enforcement` | Same file, 383. `EntryAgent(otm_steps=2)`. | **Not isolated.** Default `RiskAgent()`. | Add `tmp_path`. Pass `risk_agent=RiskAgent(config_path=tmp_path / "daily_risk_config.json")`. Assert that path. Do not run this test until that call is in the file. |
| `test_gamma_wall_clearance_and_vacuum_gates` | Same file, 424. `EntryAgent(otm_steps=2)`. | **Not isolated.** Same default. | Same injection and the same assertion. Do not run until the call is in the file. |
| `test_session_deadband_gates_and_vix_corridor` | Same file, 497–500. | `trades_dir=tmp_path / "trades"`. | Keep. Node id only. |
| `test_idempotent_order_deduplication` | Same file, 557–560. | Same. | Keep. Node id only. |
| `test_risk_agent_with_order_flow` | `test_order_flow_agent.py` 95. `cfg` at 94. | `config_path=tmp_path / "daily_risk.json"`. | Keep. |
| `test_oi_is_read_from_live_data_and_a_missing_chain_sends_no_alert` | `test_live_suggestion_fixes.py` 150. | `trades_dir=tmp_path / "trades"`. | Keep. |
| `test_missing_vix_and_a_short_adx_sample_veto` | Same file, 163. | Same. | Keep. |
| `test_lunch_rr_does_not_bypass_and_super_trend_sizes_from_adx_only` | Same file, 176. | Same. | Keep. |
| `test_path_check_returns_four_numbers_and_does_not_block` | Same file, 300. | Same. | Keep. |
| `test_stale_bar_blocks_new_trades_and_an_open_trade_still_reaches_holding` | Same file, 320. | Same. | Keep. |
| `test_candle_confirmation_stays_false_and_rc_quick_cut_does_not_close` | Same file, 378. | Same. | Keep. |

No other test constructs `RiskAgent` or `EntryAgent`. The isolation edit is those two call sites. No new fixture module. No change to `entry_agent.py` 133. After the edit, a search of `backend/tests` must show every `RiskAgent(` passing `config_path` under `tmp_path`, every `EntryAgent(` passing `risk_agent`, and every `TradeManager(` passing `trades_dir` under `tmp_path`. That search is the demonstration. The full suite stays unrun in S2A even after it. This planning pass did not make the edit and did not run the search against a changed tree. The current tree still has the default constructors at lines 383 and 424.

## 9. Compatibility and migration

Old readable objects load. Unknown keys other than the two marker lists are still dropped on the next `RiskAgent` save. That is current `save_config`. S2A does not stop writing `effective_capital`. S2-07 does.

A present marker list is copied, including an empty list. An absent marker key is not created. S2-02 is what starts adding ids.

`save_config`'s return type changes from `None` to `bool`. In-repo callers other than `begin_session` and `record_trade_outcome` ignore the return. No caller in this repository's `*.py` tree requires `None`.

`reset_daily_counters` remains importable. Its return changes from `None` to `bool`. Same-day zero without a `session_date` stops. This deviates from the design sentence that says to delete it. A search of `*.py` finds no caller. That search is not every REPL. Deletion stays a later compatibility choice. S2A does not schedule it.

`session_blocked`, `config_unreadable`, and `directory_durability_unconfirmed` are still not stored. After restart, a good file is not blocked. A still-corrupt file is blocked again. A journaled exit whose risk replace failed is not repaired by restart. An outcome durability failure is not repaired by restart. Section 3.6 has to be read first, and that read does not authorize a new process. A non-outcome durability failure uses section 5.1.

The new `.lock` file is ignored by old code. Old `save_config`, if the process is rolled back, will drop marker lists on the next save. S2A does not add those lists unless they were already there.

`process_daily_pnl` stops being able to replace the ledger. Callers that expected tomorrow's lots to be written by that function will not get that write. No in-repo test asserts that write. The unpriced early return stays.

`exit_held_for_risk` is not introduced.

Migration of the live file is the one allowed missing-date reset, plus atomic replace, plus `reserve_basis` if a save happens. It is not a reserve recompute. Do not replay historical trades into the marker lists.

Rollback, before any new process calls `begin_session` on the real file:

1. Stop every process that constructs `TradeManager` or `PostMarketManager` on `trades/`. Do not start one to verify the change.
2. Copy the risk file and the active journal outside `trades/`. Construction loads the risk file and does not write. The first tick can write.

If the new code is wrong:

1. Stop the process.
2. Put the backups back. Delete leftover `.{name}.*.tmp` files in that directory. The lock file can stay.
3. Revert the code. Old code will not undo a reset that was already saved. Only the backup restores the previous count and R.

If the JSON does not parse, the new code will not repair it and will not remove the open trade. Restore the backup and restart.

## 10. Exact source references

| Path | Function | Lines now | Planned change | Reason |
|---|---|---|---|---|
| `backend/app/trading/crew/risk_agent.py` | `risk_lock_path` | Does not exist | Add the one helper in section 1. | One lock path. No second formula. |
| Same | `__init__` | 48–78 | Set `config_unreadable = False` before `_load_config`. | A failed load must be able to set it. |
| Same | `_load_config` | 80–101 | Missing file is not unreadable. Validate root and field types before any `self` assignment. On failure set both flags and leave constructor defaults. Remember marker lists only when the keys are present and valid. | A half-assigned object must not be saved later. |
| Same | `save_config` | 103–121 | Section 5. `True` only after replace and directory `fsync` both return. Post-replacement `fsync` failure returns `False`, sets `directory_durability_unconfirmed`, and does not roll the new document back. Refuse when unreadable. Refuse a stale snapshot. Copy loaded marker lists. Do not create them. Do not decide a credit or a loss count. Keep the current payload keys, including `effective_capital`. | Truncate `"w"` is not a commit. `None` is not success. Directory `fsync` is required for `True`, so its failure cannot be unrestricted success. |
| Same | `begin_session` | 148–171 | Section 2. Unreadable returns `False` before mutation. Same date does not zero and does not write. Date change adopts disk under the lock. Commit only on `is True`. If the durability flag is set, do not restore. Restore only when replace did not happen. | A skipped or failed save is not a new session. A finished replace is not rolled back. |
| Same | `record_trade_outcome` | 123–146 | If unreadable, durability-blocked, or the save is not `True`, return `False`. Under the lock, re-read, apply the existing branches, replace. Restore the adopted document only when replace did not happen. Do not restore after a post-replacement `fsync` failure. Set `session_blocked` on either failure. Do not edit the branches. | A failed save must not report success, and a finished replace must not be undone. |
| Same | `reset_daily_counters` | 173–179 | `DeprecationWarning`, then `return self.begin_session(today)` with Asia/Kolkata when the caller passes nothing. Optional `today: date \| None = None`. Return `bool`. Do not delete. | A second reset that omits `session_date` is unsafe. Deletion can break an unseen caller. |
| Same | `evaluate_order` | 181–279, gate at 194–195 | No change. | `session_blocked` already rejects. Sizing is out of scope. |
| `backend/app/trading/crew/manager.py` | `on_market_tick` | 161–192, full exit 356–382, entry gate 458–464 | Keep the `begin_session` bool. Apply section 3.2. Do not write `exit_held_for_risk`. | Stop a known-bad session from deleting the active row. Do not pretend the success path is crash-safe. |
| `backend/app/trading/crew/journal_agent.py` | `load_active_trades` | 42–63 | No change. | Fail-closed load already refuses a later save. |
| Same | `save_active_trades` | 65–81 | No change. | Atomic active-file replace is S2-06. S2A must not describe this truncate as recovery storage. |
| Same | `record_exit` | 156–214 | No change. | Delete-before-append is the crash window in section 4. Fixing it without markers would duplicate or lose the outcome. S2-06 with S2-02. |
| `backend/app/trading/post_market_crew/safety_wall_agent.py` | `process_daily_pnl` | 78–149 | Leave 85–94 unchanged. On the priced path, keep the sum as `today_net_pnl`. Do not apply 100–114 to `self.reserve_balance`. Do not publish the lot walk at 116–125. Do not open the risk file at 127–137. Do not call `_save_state` at 139. `added_to_reserve` is 0. Loaded reserve and lots stay. | The risk-file truncate is an uncontrolled writer. The unpriced report and the priced net figure stay. |
| Same | `_save_state` | 69–76 | No edit. Not called from the P&L path. | State file is not the risk ledger. It must not move when the risk write is removed. |
| `backend/app/trading/post_market_crew/post_market_manager.py` | `run_daily_review` | 115 | No edit. | The callee no longer writes. |
| `backend/app/trading/crew/entry_agent.py` | `__init__` | 133 | No edit. Tests that call `EntryAgent(otm_steps=2)` stop using this fallback. Section 8.1. | Dormant constructor. The default `RiskAgent()` opens `trades/daily_risk_config.json` when that relative path exists. |
| `backend/app/trading/pre_market_crew/pre_market_manager.py` | risk read | 95–103 | No edit. | Reader. |
| `scripts/live_scanner.py` | daemon setup and tick | 199–212, 277–286, 334–344 | No edit. | Operationally disabled. Lock covers its `RiskAgent` writes after the safety-wall write is gone. |
| `scripts/diagnose_trades.py` | scratch manager | 23–29, 86 | No edit. | Temp dir. Still not a production run. |
| `scripts/resimulate_since_oct15.py` | scratch manager | 72–90, 181, 208, 234 | No edit. | Temp dir. Still not a production run. |
| `backend/app/trading/execution_crew/execution_manager.py` | `execute_exit` | 102–138 | No edit. | Not on the tick. |
| `backend/app/trading/execution_crew/reconciliation_agent.py` | `reconcile_positions` | 33–56 | No edit. | Not on the tick. S2A does not claim broker reconciliation. |
| `backend/app/trading/execution_crew/broker_bridge_agent.py` | `place_order` | 48–90 | No edit. | Live flag already returns `no broker adapter`. |
| `backend/tests/test_trading_and_post_market_crews.py` | `test_directional_thesis_structure_and_enforcement` | 383 | Inject `RiskAgent(config_path=tmp_path / "daily_risk_config.json")`. Assert that path. | This call is `EntryAgent(otm_steps=2)`, which constructs the default risk agent. |
| Same | `test_gamma_wall_clearance_and_vacuum_gates` | 424 | Same injection and the same assertion. | Same default constructor. |

Load rules used by `_load_config`:

| Condition | Class | Flags | Write |
|---|---|---|---|
| Path does not exist | Missing | `config_unreadable` false. `session_blocked` unchanged at construction. | `begin_session` may create the file on `True`. |
| Empty file, truncated JSON, `{`, directory, permission error, `json.load` error | Corrupt | Both flags true. Memory stays at constructor defaults. | Never. |
| Root is a list, string, number, or null | Corrupt | Both flags true. | Never. |
| Root is `{}` | Readable | Flags false. Missing fields use today's defaults. | Allowed. |
| `session_date` absent or JSON null | Readable, no date | Not corrupt. | The one allowed reset on the first successful `begin_session`. |
| `session_date` present and not a real `YYYY-MM-DD` string | Corrupt | Both flags true. | Never. |
| Numeric field is a bool, a string, a non-finite float, or, for `daily_losses_count`, not a non-negative integer | Corrupt | Both flags true. | Never. `bool` is not accepted as a number. |
| `is_safety_wall_locked` or a present `manual_lock` is not a JSON bool | Corrupt | Both flags true. | Never. |
| `loss_counted_trade_ids` or `reserve_posted_for` present and not a list of strings | Corrupt | Both flags true. | Never. |
| Those keys absent | Readable | Do not invent the keys. | Later saves omit them. |
| Those keys present, including `[]` | Readable | Keep the list, order unchanged. Do not unique them. | Every save copies them. Rollover does not clear them. |

## 11. In scope and out of scope

In scope:

- `config_unreadable`, typed load, and the save contract in section 5, including the durability flag.
- Atomic replace, temp `fsync`, required directory `fsync` for `True`, stale-temp sweep, one lock helper, locked read-modify-write.
- Same-date `begin_session` does not zero and does not write.
- Copy marker lists. Do not implement marker accounting.
- Keep and delegate `reset_daily_counters`.
- Exit containment in section 3.2. No hold key. Operator procedure in section 3.5 for a replace that did not happen. Section 3.6 for a completed outcome replace whose directory `fsync` failed. Neither is code.
- Directory-durability procedure in section 5.1 for a replace that is not an outcome. An outcome replace does not use the hash path.
- The two test edits in section 8.1. No other test constructor changes.
- `process_daily_pnl` no longer writes the risk file or the state file. Unpriced text and the priced net figure stay.
- The new tests in section 8.

Out of scope:

- Outcome formula, broker net, the ₹500 stand-in, win-clear removal, `trade_id` arguments. S2-02.
- Automatic replay of a journaled exit. Release blocker. S2-02 and S2-06 together. The operator records in sections 3.5 and 3.6 are not that replay.
- Hand-editing the risk file to post an outcome, or to insert a `trade_id` so `POSTED_DURABILITY_UNCONFIRMED` can be signed.
- A code reader for the operator record.
- Editing `record_exit`, `save_active_trades`, or the executed log. S2-06.
- Removing `effective_capital`, adding `cash_inr`, `margin_inr`, and `risk_fraction`, deleting the milestone walk. S2-07. The safety-wall write stops in S2A because C4 requires it. The rest of S2-07 does not move.
- Wiring `ExecutionManager` or `ReconciliationAgent` into the tick.
- PDC, live orders, the daemon, the production risk file, the three protected databases.
- A second lock implementation, a lock library, Windows, NFS, an outbox, a second reserve file.

## 12. Remaining unresolved decisions

These are acceptances, not open design choices.

1. Accept keeping `reset_daily_counters` and delegating it, against the design sentence that says to delete it.
2. Accept not journaling a full exit when the session is unreadable, blocked, or `begin_session` is not `True`, against the design sentence that says an exit may still be journaled while the risk file is unreadable. Journaling it today deletes the active row first.
3. Accept the release blocker in section 13. S2A will not replay an exit. The signing human may authorize `reload_only` only from `RECONCILED_UNPOSTED`, and only when the replace did not happen. `POSTED_DURABILITY_UNCONFIRMED` and the section 3.6 hold authorize nothing. Entries stay unauthorized. An S2A ledger does not contain this exit's `trade_id`, so that status is not signed for a file this sprint writes.
4. Accept withdrawing `exit_held_for_risk`.
5. Accept that `True` requires directory `fsync`, and that a post-replacement `fsync` failure blocks further risk writes in that process without rolling memory back. A hash does not prove an outcome. The outcome case is section 3.6.
6. The production file's count, R, and reserve were not read. The one allowed missing-date reset clears count and R on the first `True`. The backup has to be taken from the file at implementation time.

No other lock path, save predicate, writer policy, or recovery protocol is left open. The operator's name is whoever signs the record. There is no software identity to choose.

## 13. Release blockers

Unattended recovery of a full exit is a release blocker. S2A does not close it.

Disabled until S2-02 and S2-06 have both shipped and a later plan accepts them:

- `scripts/live_scanner.py` against `trades/`
- `scripts/diagnose_trades.py` and `scripts/resimulate_since_oct15.py` pointed at `trades/` or at the production risk file
- The trading daemon
- Live trading and `LIVE_TRADING_ENABLED`
- Any restart treated as recovery of an in-flight exit, or as clearance after a directory `fsync` failure before section 3.6 or section 5.1 is signed
- Any use of `session_blocked`, `config_unreadable`, `directory_durability_unconfirmed`, or a key on the active row as a durable exit record
- Treating a section 3.5 record, a section 3.6 record, the unfinished hold, or a matching hash, as permission to take new entries, to replay, or to post the outcome again
- Classifying a completed replace as `RECONCILED_UNPOSTED`
- Signing `POSTED_DURABILITY_UNCONFIRMED` from a hash, from an unread ledger, or from a ledger whose `trade_id` or accounting fields do not match
- Hand-editing the risk file so a journaled exit looks posted, or so a `trade_id` appears in it
- Running `PostMarketManager` as a way to repair or rewrite the risk file
- Running `test_directional_thesis_structure_and_enforcement`, `test_gamma_wall_clearance_and_vacuum_gates`, the module `backend/tests/test_trading_and_post_market_crews.py`, or the full suite before section 8.1 is in the tree

`diagnose_trades.py` and `resimulate_since_oct15.py` already use a temp directory. They stay disabled as production tools. They are not a substitute for the blocker.

What later work must resolve: section 3.4. S2-02 alone still deletes the active row before the marker exists. S2-06 alone can keep the row and still double-apply the formula. Both are required. There is no Sprint 2B in this repository. S2-02 is also what would put this exit's `trade_id` in the ledger. Until that id is there, section 3.6 does not sign `POSTED_DURABILITY_UNCONFIRMED` for a file S2A writes.

## 14. Implementation sequence

Do not start until this plan is accepted.

1. Add `risk_lock_path`, `config_unreadable`, and the validating load.
2. Make `save_config` match section 5. `True` only after replace and directory `fsync`. On post-replacement `fsync` failure, set the durability flag, log the path, and do not raise. Sweep stale temps under the lock. Copy loaded marker lists.
3. Change `begin_session` to section 2. Require `is True`. Check the durability flag before any restore.
4. Guard `record_trade_outcome` with the unreadable flag, the durability flag, the lock, and the re-read. Restore only when replace did not happen. Do not change the arithmetic branches.
5. Delegate `reset_daily_counters`. Do not delete it.
6. In `on_market_tick`, apply section 3.2 only. Do not add a hold key. Do not reorder a successful `record_exit` ahead of or behind the risk save.
7. Narrow `process_daily_pnl` as in section 6. Leave the unpriced return and the priced `today_net_pnl` sum. Do not write either file.
8. Edit the two `EntryAgent` calls in section 8.1 before any pytest. Then add the new tests, including J-01, R-01, M-01, M-02, F-01, W-01, and S-01. Run those by node id, plus the already-isolated tests named in section 8, also by node id. Do not pass `backend/tests/test_trading_and_post_market_crews.py` as a module path. Do not run the full suite. Do not point any test at the production risk file. Do not add a production reader for the operator record. Do not add code that posts from `POSTED_DURABILITY_UNCONFIRMED` or from the unfinished hold.

## 15. What this pass did not do

Pytest was not run. The production risk file was not opened. The three database files were not opened. Directory `fsync` was not measured on this machine. It is specified, not timed. `graphify-out/graph.json` was present and was queried before the source lines were read. Call sites were checked in the current `*.py` tree. The operator procedure was not executed. No reconciliation file was created. The two `EntryAgent` tests were not edited, so isolation is specified and not yet demonstrated in the tree. The full suite was not run.
