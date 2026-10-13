# Genesis — Market Data Quality Audit (Phase 2)

## 1. Sources

| Source | Used for | Code | Reliability |
|---|---|---|---|
| NSE F&O bhavcopy (legacy until 2024-07, UDiFF after) | Option close, settlement, volume, OI, lot size | `market/nse.py` `parse_fo_bhavcopy`, `scripts/import_nse_eod.py` | Official EOD. Downloaded from `nsearchives.nseindia.com`; NSE throttles and blocks scripted access at times (UNVERIFIED rate limits) |
| NSE index history export | NIFTY/BANKNIFTY OHLC | `parse_daily_bars` | Official |
| India VIX history export | IV rank | `import_vix` | Official, but it is NIFTY's 30-day vol only |
| Bundled `data/sample_nifty_options.csv` | Fallback when no NSE data | `market/source.SampleSource` | **Synthetic**: 60 rows, Bachelier prices from IV rank. Not a market |
| Broker quotes (sandbox only today) | Live snapshots | `market/feed.BrokerFeed` | Simulated |
| RSS news | LLM context only | `market/news.py` | Not used by rules |

yfinance is not used anywhere.

## 2. Measurements on the real `market_eod.db` (read-only)

| Check | Result |
|---|---|
| NIFTY index bars | 1,929 days, 2019-01-01 → 2026-10-13; VIX on every day; IV rank on 1,910 (first 19 lack history) |
| NIFTY option rows | 5,807,488 rows on 1,676 dates, 2020-01-01 → 2026-10-13 |
| Duplicate index dates / ordering | 0 duplicates; sorted |
| OHLC violations (high < low, close outside range) | 0 |
| Negative option prices | 0 |
| Option dates without an index bar | 0 |
| Gaps over 4 calendar days | 1: 2022-04-13 → 2022-04-18 (Ambedkar Jayanti + Good Friday, genuine holidays) |
| IV rank recomputed independently (300 random days) | 0 mismatches |
| `market_events` rows | **0**: the event YAMLs in `data/events/` are not imported into this database (G-13) |

## 3. Decision-time correctness

| Question | Finding |
|---|---|
| Does the backtest use data unavailable at decision time? | IV rank: no. Previous-day range: no. Events: scheduled in advance, fine. **But** the decision uses the day's close, VIX close and day high/low, and the fill is at that same close. Those are known only after 15:30; the NSE index close is a weighted average of the last 30 minutes and cannot be traded (G-08) |
| Are option and index prices synchronous? | No. An option's `close` is its last trade of the day, which for thin strikes can be hours before the index close. Untraded contracts use the settlement price and are flagged stale (entries refused, but marks still use them) (G-11) |
| Live vs historical consistency | Different by construction: live uses intraday mid quotes and intraday high/low so far; history uses daily closes (G-04) |
| Stale live data | Index quote age > `MAX_SNAPSHOT_AGE_SECONDS` stops the tick. Option quotes are **not** aged individually; a wide spread marks them stale (G-11) |

## 4. Indian market specifics

| Item | Status |
|---|---|
| Timezone | IST via fixed `+05:30` offset (`feed.IST`); DB timestamps UTC. PASS |
| Holidays | `data/holidays/nse.yaml` 2020–2026 (97 days). An unlisted holiday is treated as a trading day; the stale check then stops the tick. PASS with a LOW note (G-22) |
| Session | 09:15–15:30 configurable; pre-open (09:00–09:08) is not modelled and not traded. PASS |
| Expiry handling | From the data (handles the weekly-day changes); expiry day is never entered; settlement at intrinsic against the index close. PASS |
| Lot sizes | Per row from UDiFF, inferred from turnover for legacy rows, dated table fallback (75 → 50 → 25 → 75 → 65 for NIFTY). Spot-checked against known NSE changes. PASS |
| Tick size | ₹0.05 hard-coded (`brokers/sandbox.TICK`, `engine/costs.TICK`). PASS for index options |
| Freeze quantity | `FREEZE_QUANTITIES` setting (NIFTY 1800). Static: must be updated when NSE revises it. WARNING |
| STT / charges | Dated schedule to 2024-10-01. Union Budget 2026 is reported to have raised STT on options; **UNVERIFIED**, needs a check against the Finance Act before trusting post-April-2026 costs (G-14) |
| Corporate actions | Not applicable (index options only) |

## 5. Gaps and recovery

- Imports are idempotent (file SHA-256 in `import_files`; upserts). PASS.
- No automatic gap detector: a missing trading day (no bhavcopy) silently becomes "no snapshot"; positions are
  then marked on the next available day. A missing expiry-day file would settle at intrinsic against a later
  close. Recommendation: a data-completeness report (trading days per holiday calendar vs imported) before each review.
- The synthetic fallback runs silently when no data is imported; reports carry `data_source`, and after this
  audit the verdict becomes "Synthetic data only" (G-02, fixed).

## 6. Verdict

Historical data integrity: **good** (verified). Decision-time realism: **weak** (close-to-close fills, asynchronous
option closes, live/backtest timing mismatch). Event calendar: **not loaded**.
