# Liquidity detection analysis

Data: Kaggle NIFTY 1-minute bars rolled to 5m, 2015-01-02 to 2025-12-24 (2295 sessions; the first session and NIFTY expiry days are skipped for every strategy). VWAP: session time-weighted typical price (index has no volume). In-sample to 2020, out-of-sample 2021 on. Costs: 2 index pts a round trip. Results are index points and R; option P&L is not reported because no historical option prices are available.

## Definitions (fixed before testing, none optimised)

- Swing: 2-left/2-right swing highs/lows on 5m and 15m. A swing is usable only from the first 5m bar after its second right-hand bar has closed. External (major) = the session's extreme when confirmed; internal (minor) otherwise.
- Opening range: 09:15-09:30 and 09:15-09:45 high/low, usable from 09:30 / 09:45.
- Equal highs/lows: 2+ confirmed 5m swings within 0.15 x ATR(14, 5m), 4+ bars apart, with a pullback of 0.5 x ATR between them and no trade through the level in between. Usable once the last swing is confirmed.
- Consolidation: 6 bars spanning at most 1.5 x ATR with neighbouring bars overlapping 50%+ on average; usable from the next bar; a window inside the current box does not create a new one.
- Previous hour: high/low of the last completed 60m candle (09:15-10:15, ...), usable only during the following hour.
- Sweep: a 5m bar trades 1 pt past an intact level (the previous 5m close was on the near side). A level is used once.
- Entry: close back past every swept level within 6 bars, 5m structure shift (close past the last confirmed 5m swing) within 12 bars of the sweep, close on the trade's side of VWAP, entries 09:30-14:30 on bar close.
- Stop: sweep extreme - max(2 pts, 0.1 ATR). Exits as the existing strategy: stop to entry after +1R, 15:10 exit.

## No look-ahead

Every level carries the index of the first bar allowed to use it; signals use completed 5m bars only; 15m structure uses completed 15m bars; the previous hour is used only after it closes. Unit tests in backend/tests/test_intraday_liquidity.py check these timings.

## Levels found

| Level type | Levels per session |
|---|---|
| swing | 24.2 |
| or15 | 2 |
| or30 | 2 |
| equal | 2 |
| range | 4.4 |
| hour | 12 |

## Internal vs external, higher vs lower timeframe (swing strategy, baseline trades)

| external | Trades | Win % | Avg R | Total R |
|---|---|---|---|---|
| 0 | 929 | 41.2 | 0.057 | 53.1 |
| 1 | 381 | 38.1 | -0.026 | -10 |

(external 1 = the session's extreme when the swing was confirmed)

| tf | Trades | Win % | Avg R | Total R |
|---|---|---|---|---|
| 15 | 242 | 39.3 | -0.074 | -17.8 |
| 5 | 1068 | 40.5 | 0.057 | 60.9 |

(tf 5 = 5m swing, 15 = 15m swing: lower vs higher timeframe)
