# Implementation roadmap

## What changed

| File | Change |
|---|---|
| `backend/app/intraday_liquidity.py` | New. Level detection (swing 5m/15m with internal/external, opening range 15m/30m, equal highs/lows, consolidation boxes, previous hour), one sweep -> reclaim -> structure shift -> VWAP entry routine (`scan_levels`) shared by every intraday strategy, opposing-liquidity lookup, and liquidity event grouping (`group_events`). |
| `backend/scripts/research_intraday_liquidity.py` | New. Runs the existing PDH/PDL `scan` and every intraday strategy on identical data, exits and costs; targets, minimum risk and confirmation timeframe varied one at a time; in-/out-of-sample split, yearly walk-forward, Monte Carlo, regimes, feature study, portfolios; writes reports 1-11 and the charts. |
| `backend/tests/test_intraday_liquidity.py` | New. Level timing (no look-ahead), the full long entry with stop and target, equal-low noise rejection, event grouping. |
| `backend/app/liquidity_signals.py` | Not changed. The live PDH/PDL alerts and their backtest keep working as before; the research script memoises its two time helpers at runtime for speed only. |

The 11 module names in the brief were folded into these two files: detection, signal generation and research stay
separate (module vs script), and nothing duplicates `liquidity_signals` (it reuses `pivots`, `to_15m`, `exit_of`,
`scan`) or `scripts/backtest_liquidity.py` (`kaggle_5m`, `expiry_for`) or `app.engine.metrics.performance`.

## Common signal interface

Every signal from `scan_levels` is a dict with: `strategy`, `side`, `time` (close of the entry bar), `entry`, `stop`,
`target`, `risk`, `level` (the reclaimed price), `levels` (ids of every level swept), `sweep` (time of the first
sweep), `event_id` (set by `group_events`), `opp` (nearest opposing liquidity 1R+ away), `features` (the quality
inputs) and `why`. The PDH/PDL signals are given the same keys by the research script without changing `scan`.

## Run

    cd backend
    uv run --with openpyxl python scripts/research_intraday_liquidity.py   # ~3 min, offline: reports 1-11, charts/, Excel workbook
    uv run pytest tests/test_intraday_liquidity.py tests/test_liquidity_signals.py

## Safety

Nothing here sends alerts, places orders or is wired into `app.liquidity_signals.main`; live trading is unchanged.

## Next steps, in order

1. Do not trade any intraday setup live yet. Paper-track the strongest candidate (see STRATEGY_COMPARISON.md) for
   3+ months with real option fills, since every result here is on the index, not on options.
2. Get real VWAP (NIFTY futures volume) and historical option prices; re-run. The session TWAP proxy and the flat
   2-point cost are the largest modelling gaps.
3. Test confluence explicitly (a trade only when 2+ liquidity types are swept in one event): the event tables show
   shared events beat solo ones, but that rule was found by looking at all the data and needs its own out-of-sample
   test before use.
4. Re-check the PDH/PDL strategy: it earned in-sample and about nothing from 2021 on (BACKTEST_RESULTS.md).
5. Only then consider wiring a passing setup into the alert loop, behind its own flag, alerts only.
