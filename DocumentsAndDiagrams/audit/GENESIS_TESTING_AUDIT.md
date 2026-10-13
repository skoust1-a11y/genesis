# Genesis — Testing and Reliability Audit (Phase 9)

## 1. Current suite (measured 13 Oct 2026)

- Backend: **466 tests, all passing**, ~85 s, deterministic (fixed clocks, seeded bootstrap, in-memory/temporary
  SQLite, sandbox broker, alerts captured). No test uses real credentials or a real broker.
- Frontend: 3 `node --test` tests (`src/lib/risk.test.ts`), plus clean `tsc` and ESLint.
- Coverage % was not measured (`pytest-cov` is not installed and was not added).

Tests per area (top files): live plumbing 45 (incl. the new one), risk rules 18, hard stop 18, policy 17, market
data 17, trade crew 16, rules 15, odds gate 15, money rules 13, drawdown 13, approvals 12, reviews 11, live 11.

## 2. Coverage map against the requested categories

| Category | Present | Gaps |
|---|---|---|
| Unit (rules, costs, policy, drawdown, odds) | Strong | — |
| Indicator | IV rank series, prev-range fields | — (no classical indicators exist) |
| Strategy | Rule validation, entry/exit checks, sim ↔ crew parity | No per-strategy regression snapshot on real data |
| Risk engine | Strong (each check, stricter-wins merge, cooldown) | Daily loss with open MTM (behaviour to be defined, G-05) |
| Backtesting | Gap-through-stop, expiry settlement, MTM drawdown, metrics (new), hand-checked charges (new) | Walk-forward; margin |
| Integration (API ↔ DB ↔ crew) | Strong (FastAPI test client, approvals → deployment → ticks) | — |
| API failure | Rejections, exit retries then halt, margin query failure, stop placement failure, **pending cancel (new)** | `place_order` raising (G-10): only covered through the generic "crew error halts" path |
| Data corruption | Parser tests on real NSE fixtures (legacy + UDiFF) | Corrupt/partial bhavcopy; missing expiry-day file |
| Concurrency | Supervisor restarts; serialized SQLite | Two crews entering at once with LLM steps on (G-28) |
| Regression | Hash-checked reports and immutability triggers | — |
| End-to-end paper | Approve → start → sandbox ticks → exits → journal | — |

## 3. Edge cases

| Edge case | Covered? |
|---|---|
| Empty dataset | Yes (no snapshot → no trade; empty metrics) |
| Missing values (no quote, no IV rank, no VIX) | Yes (checks fail closed) |
| Zero volume | Yes (settlement price, marked stale, entry refused) |
| Large gaps | Yes (stop fills at the close through a gap; protective stop gapped through) |
| Market closed / holidays | Yes |
| Sudden volatility | Partly (VIX sizing bands) |
| API timeout | Partly (see G-10) |
| Duplicate signals | Yes (open-position and per-day limits) |
| Partial execution | Yes (partial fill → cancel → re-price; second-leg rejection unwinds) |
| Process restart | Yes (startup reconciliation; sandbox state persists) |
| Database disconnection | No dedicated test |

## 4. Tests added in this audit

| Test | Proves |
|---|---|
| `test_live_plumbing.py::test_no_reprice_while_the_cancelled_order_can_still_fill` | No second order while a cancelled order can still fill (fails on the old code) |
| `test_reviews_improvements.py::test_synthetic_data_is_never_called_profitable` | Verdict on synthetic data is "Synthetic data only" |
| `test_reviews_improvements.py::test_a_handful_of_winning_trades_is_too_few_to_judge` | 3 + 2 winning trades → "Too few trades" |
| `test_metrics.py::test_performance_matches_hand_calculation` | PF, expectancy, streak, Sharpe, Sortino, drawdown, Calmar, recovery, exposure vs hand/`statistics` values |
| `test_metrics.py::test_ratios_are_none_not_infinite_without_losses_or_spread` | No infinities in stored JSON; empty run |
| `test_metrics.py::test_one_sell_fill_charges_by_hand` | Every charge component of one fill; tick floor vs bps |

## 5. Next tests to write (in order)

1. `place_order` raising after acceptance → order recorded as UNKNOWN, no re-send, halt (with G-10).
2. Daily loss including open MTM, same result in sim and crew (with G-05).
3. Missing expiry-day bhavcopy → settlement on the next day is flagged in the trade.
4. Two crews entering concurrently with opinions on (with G-28).
5. A frozen real-data regression: the example condor's trade list hash on a fixed date range.
