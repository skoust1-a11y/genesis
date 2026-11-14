# Unpublished-change review — 14 November 2026

Original source HEAD: `c390e23805ab322b9514810f70df60c98c03cfc4`.
Published independent-root baseline: `81cbbf883a9fbb643c695b7233fa2252c9dbb51b`.

## Coverage and evidence limits

Reviewed metadata/messages/path deltas for 138 original commits: 135 reachable from refs and three additional reflog versions of the markets-page commit. Historical surviving content was compared by Git blob and file mode, not by commit ancestry. The two head trees contain 723 original paths and 391 V3 paths, with 741 paths in their union. Classification: 352 identical source paths, 14 differing source/configuration/documentation paths, 299 original-only paths, 11 V3-only paths, and 65 protected paths. Protected paths were classified from metadata; private operational contents were not opened. A separate local ledger records each path/scope decision and commit history without publishing private evidence.

Byte equivalence proves that surviving implementation is present, not that every historical claim of correctness was true. Author intent beyond commit messages, explicit comments and tests remains unverified. No external issue linkage was established. Historical-only replay databases/results, local state, private reference data, credentials, and generated research artifacts remain excluded. No historical performance claim was revalidated.

## Publication decision

Only restore precise `.gitignore` protections missing from the source-only release. The baseline fails to ignore 20 of 23 invented protected-path examples, including provider credential properties, containment folders, operational output, SQLite sidecars and the development environment. Restore those rules, including the original working-tree Phase 4 replay-output exclusion. Protect research output/profiles specifically: do not restore the original blanket research exclusion, which would hide seven published synthetic strategy documents.

This is protection against accidental staging, not access control; explicit force-add and previously tracked files require separate review. No runtime, frozen source, dependency, financial policy, broker interface or frontend behavior changes.

## Grouped exclusions

| Change | Evidence and decision |
| --- | --- |
| Original `.gitignore` working change | Explicit comment keeps Phase 4 raw replay runs local. **IMPROVE** as part of precise ignore restoration. Original credential-file and research-checkpoint commits provide additional intent evidence. |
| `backend/app/api/market.py` working change | Replaces NIFTY/BANKNIFTY bar volume with ETF volume without per-bar proxy provenance. Invented index bars retain `[0,0]` in V3; candidate returns `[1,9]` and changes the weighted-price input from fallback 150 to 190. V3 already handles absent volume using running typical-price averaging. **REJECT** this unlabeled cross-instrument substitution. No provider request executed. |
| `frontend/src/components/CandleChart.tsx` working change | Raises histogram alpha from 0.22/0.18 to 0.5/0.4. Visibility intent is inference; no demonstrated defect or visual acceptance evidence. **DEFER**. |
| Reference liquidity evaluator, regime contract and candidate tests | Explicitly exploratory wrapper over existing frozen scan/exit functions; regime mapping is inactive. V3 candidate run: 22 passed, 1 failed because the test expects a historical holiday label rather than V3's synthetic label. A separate negative check accepted a nonfinite prior-day high with no data-quality rejection. No before-fix reproduction of a current V3 defect. **DEFER**. |
| Phase 4 scanner-v0.2 candidate source copies | Research setup selection and losers-only six-bar time stop. Compared with V3, runtime copy also removes state-authority binding, synthetic startup checks, pending-exit lifecycle/requote behavior and unambiguous contract selection. **REJECT** wholesale import. Candidate exit logic alters a frozen file and is not authorized for this publication. |
| Phase 4 analysis utilities and ignored research Python scripts | Explore setup selection, costs, risk profiles, walk-forward behavior and historical data integrity; require excluded datasets or journals and lack isolated current-V3 repair evidence. Source inspected statically; not executed against real inputs. **DEFER**. |
| Ignored backup scanners, journal and signal sources | Superseded source copies with operational behavior and no current regression proof. **REJECT**. |
| Legacy launchers, deployment services, journal/notification scripts | Could activate services, update operational journals or deliver signals. No still-relevant isolated fix demonstrated. **DEFER**; not run. |
| Observation/replay/reconciliation CLIs | Existing runtime modules are already published. Utilities require authorized external data or excluded evidence; no demonstrated defect repaired by copying them. **DEFER**. |
| Original documentation, diagrams, converted assets, optional vendor reference | Preserve locally. Not a demonstrated current implementation repair; publication provenance/sanitization is not established. Vendor integration document states it is an architecture reference and is not imported by backend runtime. **DEFER**. |
| Different V3 fixture/guard/font/documentation paths | Deliberate synthetic expectations, portable frozen manifest checks and local fonts. **ALREADY INCLUDED** as release-specific replacements; retain V3 versions. |
| Operational risk configuration, databases, journals, private datasets, generated results | **PROTECTED**. No content reads, execution, copy or publication of those files. |

## Validation

- Ignore regression: baseline test **1 failed**; final focused ignore plus state-contract run **38 passed**. Boundary matrix: **23/23** protected examples ignored and **10/10** source examples visible.
- All **391** published tracked paths remain visible under the final ignore rules.
- Frozen manifest verification: **18 files verified** after restoring Git file permissions in the temporary archive checkout. Default `git archive` permissions initially differed from the manifest; no production file was altered to accommodate the test.
- State provisioning, run in its required separate unittest process: **13 passed**. An initial monolithic pytest attempt hit 36 collection errors because this test installs process-wide offline guards. Those guards were not weakened.
- Focused ignore/frozen-source/portable-manifest regression run: **640 passed**. Broad baseline pytest (excluding process-global provisioning guard): **2,192 passed, 27 failed, 4 skipped**. Rechecking only failures after Git-mode normalization: **1 passed, 26 failed**. The localhost-only failure passes separately outside the socket-restricted sandbox (**1 passed**), leaving **25 unresolved baseline failures**. They concern synthetic calendar/lot assumptions, omitted historical goldens and old-history SHA expectations, excluded legacy caller expectations, and fixture/runtime expectation mismatches. These remain unverified; no trading-safety invariant was weakened to satisfy them.

All execution used an isolated temporary V3 source tree and installed dependencies. Original source and protected operational data were not modified. No broker orders, Telegram messages, live trading, dependency downloads, directory migration, history import or force push were performed.
