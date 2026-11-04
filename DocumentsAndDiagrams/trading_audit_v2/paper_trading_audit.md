# Paper trading and forward-validation audit

Audit date: 4 Nov 2026.

## What "paper" means here

Three different papers:

| Path | Fill | Position | Comparable to a backtest? |
|---|---|---|---|
| `brokers/paper.py` | Limit fills immediately in the research tests' description (October G-19). This session did not re-read every fill branch | Research DB orders | Only for the daily sim's assumptions |
| `SandboxBroker` | Used by `test_live_plumbing.py` | Deployment orders | A lifecycle test, not a market |
| Scanner journal | No fill. Spot CMP versus spot stop and target. Exit rupees = points × 0.5 × qty | `trades/.active_trades_*.json` | No |

The daemon sets `is_paper_trading=True` and never calls the execution manager. Telegram is the product. A human who trades the alert at a broker is the execution system. The journal will not know their fill.

## Signal time

The daemon drops the forming 5-minute bar and vetoes a stale one. That removes the repaint the October audit flagged in the old `main` loop. Entries can still use the latest **spot** CMP (`snapshot.cmp`) for gates and exits between bar closes. A stop can fire on a tick the backtest would not see until the bar's high or low. Forward results will show more stop-outs than `exit_of` on the same tape, even with identical signals.

`on_market_tick` does not receive 1-minute bars from the daemon. Any rule that needs the sniper's 1-minute wick does not run in production. Forward trades are 5-minute (and 15-minute) detector output after the manager gates. A backtest that turns the sniper on is a different strategy.

## Reconciliation

`ReconciliationAgent` exists and is not on the daemon loop. There is no broker position to match. `JournalAgent` writes JSON, then tries sqlite, and ignores sqlite errors at debug level. Those stores can disagree. Excel sync is a third copy (`maintain_trades_excel.py`), which prices premiums with Black-76 for the workbook. Three numbers for one trade: journal delta-0.5, Excel Black-76, and whatever the operator did at the broker. None is labelled as the source of truth in the daily audit.

## Latency, rejects, duplicates

- Poll interval default 10 seconds, plus Yahoo and Groww HTTP time. A 1-minute signal's life is several polls.
- No reject state, because no order is sent.
- Duplicate suppression is a hash of date, strategy, direction, strike, minute, and price rounded to 1 point, kept in a `set` on the object and checked against open trades. It does not survive a restart for trades already closed. It does not use a broker idempotency key.

## Why a backtest can make money and the forward book will not

Even if `scan()` had a real index edge (the October CI says that is not established):

1. The daemon calls `scan()` inside the detector, then keeps a different subset (dynamic targets, risk capped at 55, other timeframes, and a target rewritten by the trend agent). The October sample is not that subset.
2. The signal's own stop is kept only if the manager does not run the sniper. It does not run on the daemon, so the stop stays the swing or retest stop, then anything above 55 points has already been dropped. The 22–35 point clamp belongs to `evaluate_sweep` and the waterfall, not to a normal `scan()` alert. Intrabar exits still differ from `exit_of`.
3. Exits can trigger intrabar.
4. Option premium is not the index. Constant-IV backtests were already the wrong sign under a 10-point IV shock in the October stress.
5. The forward rupee is 0.5 delta, so the forward "win" is not the option's win. You can lose money in the option and show a green audit.
6. Gates (VIX, walls, lunch names, delta floor) delete some of the historical trades and keep others. The surviving set was not the set that was measured.
7. Parameter writes change the stop buffer after the fact.

## Gap measurement (use this, do not invent a number yet)

For each forward trade, store four rows and refuse to publish a P&L until row 3 exists:

| Row | Fields |
|---|---|
| Signal | Rule id and git revision, bar timestamp, spot, stop, target, strike, theoretical premium, VIX |
| Intended | Bid, ask, and last at signal time |
| Fill | Broker average price, quantity, charges, timestamps of ack and fill. Empty if the operator did not trade |
| Twin | What `scan()` or the frozen rule would have done on the stored 5-minute bars that day, index R only |

Report: signal count versus twin count (rule gap), intended mid versus fill (execution gap), fill P&L versus 0.5-delta P&L (accounting gap). A strategy is not "working in forward test" because row 4's cousin, the delta-0.5 sum, is positive.

## Minimum before anyone calls it validated

- 30 closed trades is the research verdict floor (`verdict_min_backtest_trades`). It is a floor for **not** calling a sample an edge, not a graduation test. October's 544-trade CI still crossed zero.
- For this book: pre-register the exact function, trade it unchanged for a window that was not used to pick thresholds, record real option fills, and require the option-P&L CI to lie above a cost hurdle you write down first.
- The current forward file fails that standard regardless of the sign of the rupee column.

## Missed-setup section of the daily audit

The 3 Nov report says zero missed setups. The daemon invoked the review with an empty dataframe. A miss detector with no bars cannot see a miss (V2-07). "Zero missed" is not a measurement.
