# VWAP and data quality audit

All figures were measured by `backend/scripts/audit_liquidity.py` (`data_quality` L470, `vwap_study` L611) on the frozen snapshot `backend/data/intraday/audit_snapshot.json.gz`. Full tables are in `RESULTS.md`.

## Data sources

| Data | Source | Period | Use |
|---|---|---|---|
| NIFTY 1m, resampled to 5m | Kaggle `NIFTY_1MIN_2015-2025.csv` (`kaggle_5m`) | 2015-01 to 2025 | History |
| NIFTY 5m | Groww (`groww_5m`) | after the Kaggle file ends, to 2026-10-13 | History |
| NIFTY daily OHLC | NSE EOD (`backend/data/market_eod.db`) | 2019 to Dec 2025 | Cross-check only |
| India VIX daily | Yahoo `^INDIAVIX` | 15 years | IV for the option model, regime buckets |
| NIFTY and NIFTYBEES 5m | Yahoo | last 60 days | VWAP study |
| NIFTY option EOD | NSE bhavcopy in `market_eod.db` | 2020 onward (1,318 days with ATM quotes) | Black-76 validation |

**Not available:** intraday option prices, NIFTY futures intraday volume/VWAP, tick data. Nothing in this audit invents them.

## Defect found and fixed: synthetic holiday sessions

The Kaggle file contains **149 sessions (2015-2025) on market holidays** filled with flat bars (one OHLC repeated). They were treated as trading days, so the next real session got a "previous day" range only 5-10 pts wide. Every move on that next day swept both PDH and PDL.

- Effect on the 10-year workbooks: 44 signals on days after a fake session, of which 35 were taken (+14.33R, Rs 45,250 of the reported profit).
- Fix: `kaggle_5m` drops any session whose bars all share one OHLC (`backtest_liquidity.py` L79). Test: `test_kaggle_drops_flat_holiday_sessions`.
- After the fix, "intraday dates absent from NSE EOD" is empty. The 10 NSE dates missing from the intraday data are all special sessions (Muhurat, budget Saturdays, 2024-01-20/05-18 DR drills).
- **Both workbooks in `DocumentsAndDiagrams/NIFTY_Liquidity_Backtest_10y_2026-10-14.xlsx` (both versions) predate this fix and are contaminated.**

## Bar quality (after the fix)

- 217,552 5m bars, 2015-01-01 to 2026-10-13, 243-250 sessions per full year.
- 24 Groww sessions (2026-08-03 to 09-04) have 74 bars: the 15:20 bar is missing. That is after the entry window and after the 15:10 exit, so there is no effect on trades. The 15:20 time-exit variant is slightly affected on those days.
- 2026-02-01 (a Sunday budget session) is included as a trading day. That is correct: NSE traded.
- 5m-derived daily high/low vs NSE EOD (1,722 days): median absolute difference 0, P99 3.0 pts (high) and 2.1 pts (low). 11 and 7 days differ by more than 5 pts. The **close** differs on 462 days because NSE's close is a 30-minute weighted average, not the last trade. The strategy does not use the daily close for levels.
- Kaggle vs Groww on the 2,100 overlapping 5m bars: median absolute difference 0 to 0.05 pts, max 10.75 pts (one high).

**Live PDH/PDL** come from Yahoo daily bars (`session_data` L349). They usually match the 5m-derived values, but a 2-3 pt difference can decide whether a 1-pt sweep happened. The audit has no history of Yahoo's daily bars to measure this.

## VWAP source (Part 3)

The index has no volume, so:

- **Live** uses the NIFTYBEES ETF's VWAP side (`session_data` L347-348).
- **History** (10-year workbook and this audit) uses the running mean of the index typical price ("TWAP", `twap_above` L132-140).
- **NIFTY futures VWAP**, the usual institutional choice, has no intraday data available: **INSUFFICIENT DATA**.

Measured over the last 60 sessions (4,442 index bars):

| Comparison | Agreement |
|---|---|
| Index close vs NIFTYBEES VWAP, against index close vs index TWAP (same side), all bars | 91.6% |
| Same, bars where the close is near the line | 84.5% (2,299 bars) |
| NIFTYBEES close vs its own VWAP, against its own TWAP | 95.9% |
| Index bars with no NIFTYBEES VWAP (signal blocked) | 56 |

Signals over the same 60 days: 17 with NIFTYBEES VWAP and 17 with index TWAP. They differ on one day only (2026-09-01: a short at 13:15 vs 13:10, 25 pts apart).

### Does the VWAP filter add value? (10 years, TWAP proxy)

| Filters on the entry bar | Trades | Expectancy R | OOS R |
|---|---|---|---|
| VWAP + open (original) | 544 | +0.032 | -0.028 |
| Open only | 546 | +0.034 | -0.024 |
| VWAP only | 626 | -0.002 | -0.031 |
| Neither | 641 | -0.018 | -0.041 |

The **day-open filter** does the filtering. With the open filter in place, the TWAP filter removes 2 trades and changes nothing. With a real futures VWAP the result could differ; this is untested. Conclusion: the VWAP-proxy question matters little for the backtest, but the NIFTYBEES dependency adds a live failure mode (56 blocked bars in 60 days).

## Open filter

Removing the open filter costs about 0.034R per trade (+0.034 to -0.002 with VWAP kept). It is the single most useful filter tested, though even this difference is inside the noise (overlapping CIs).

## Assumptions

- TWAP stands in for VWAP for 2016-2026. Evidence for the substitution comes from 60 days only.
- India VIX (a 30-day index) stands in for weekly option IV wherever no contract IV is available.
