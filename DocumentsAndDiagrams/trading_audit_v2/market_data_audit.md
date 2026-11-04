# Market data audit

Audit date: 4 Nov 2026.

## Sources in use

| Series | Source | Used by | Grade |
|---|---|---|---|
| NIFTY 5-minute, last ~60 sessions | Yahoo chart via `app.api.market._chart` | Default `backtest_liquidity.yahoo_data`, scanner feed | Usable for a short spot study. Yahoo 5-minute history is short and the last bar can still be forming. The daemon drops a bar whose start is less than 5 minutes ago (`_drop_forming`). |
| NIFTY 1-minute, ~2015–2025 | Kaggle CSV rolled to 5-minute, topped up with Groww's chart API | `backtest_liquidity.py --history` | The October audit found 149 synthetic holiday sessions and removed them. That fix lives in the audit harness. This session did not re-scan the CSV. |
| Session "VWAP" | NIFTYBEES when Yahoo is used; time-weighted average price when the index has no volume (`--history`) | `scan()` filter `above_vwap` | October note: proxy matched index TWAP on 16 of 17 signal days checked then. Not a futures VWAP. |
| India VIX | Yahoo `^INDIAVIX`, previous session in the backtest | IV input to Black-76. Live gate 11.5–22 | VIX is a 30-day ATM-ish index, not the IV of the 2-OTM weekly being bought. |
| Option chain | Groww HTML `__NEXT_DATA__` | Strike label, LTP, greeks if present, walls | Last trade only. No bid, no ask. The parser comment says Groww can change the page without notice (V2-14). |
| Option history | Not stored | — | No survivorship-free premium tape. October audit: Black-76 vs NSE closes had a 90th-percentile error of about 29–65%, and a 10% IV move flipped option expectancy from about −0.24R to +0.41R. **Not re-measured today.** |
| Corporate actions | Not applicable to the index level in the same way as single stocks. Lot-size table exists (`market/nse.py`, `lot_sizes/nse.yaml`) | Backtest ledger | Good that lot size is dated. The liquidity ledger still falls back to 75 contracts if the table has no row. |
| Stocks | Yahoo `.NS` 5-minute | Optional stock-flag alerts, only when the NIFTY candidate names do not contain "retest" or "trendline" | Side path. Not the book. |

## Clock and missing bars

- Research simulator steps one `MarketSnapshot` per session. Intraday path inside the bar does not exist there.
- Scanner treats a 5-minute bar as closed only after its end, and `bar_is_stale` rejects a closed bar older than 300 seconds. That part is deliberate and tested in spirit by `test_live_suggestion_fixes.py`. A machine whose clock is not IST will open, close, and stamp the wrong session (`is_market_hours` uses `datetime.now()`).
- Duplicate or missing candles are not repaired. A gap becomes a false swing or a missed sweep. There is no gap report in the daemon loop.
- `to_15m` in `liquidity_signals.py` is built from bars already in hand. The 15-minute close is read when the matching 5-minute bar is the one that finishes that bucket (`_at(start, 10)` equals the bar time). That is causal for a completed bucket. It is not what the live detector uses.

## Selection bias

- Backtests skip the current day (`k < today`) and skip expiry day in `backtest()`. Live blocks new entries from 14:30, which is a different expiry rule.
- Scanner only promotes a candidate when Groww returned both walls (`gamma_info.walls_live`). Days the scrape fails produce no trades and do not appear as a failed sample. That is a data-availability bias in the forward book.
- `option_plan` falls back to VIX=13.5 if Yahoo VIX fails (`except: pass` then the default). The live entry path does the opposite: missing VIX is a hard veto. Research alerts and live entries do not share this rule.

## Validation that exists

`BrokerFeed` ages the index quote (`max_snapshot_age_seconds`, default 120) and treats a missing bid/ask as a wide market. Groww always has missing bid/ask, so those quotes are stale for the research feed. The scanner does not use `BrokerFeed` for entries; it uses the page LTP directly in `contract_quote`.

## What would make the data good enough

A stored chain: timestamp, bid, ask, last, oi, iv, lot, expiry, strike, and the spot at the same second. Until that exists, every rupee number is a model. Spot OHLCV can support an index-point study only, and only after a gap and holiday check on the file actually used.
