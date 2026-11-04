# Research and live parity specification

Date: 4 Nov 2026.

## Requirement

`backtest()` and `DetectorAgent` call `scan`, `scan_retest`, and `scan_trendline_reversal` with one argument object, versioned. `TradeManager` does not replace stop or target after the function returns, unless the backtest applies the same replacement.

If a live-only gate remains (VIX band, missing wall, stale bar), it is listed on the rule card as a **live filter**. The backtest must either apply it when the input exists or the report must say "N signals removed live-only; not in this expectancy".

## Rule card

A JSON file, not a module docstring. Fields:

- `strategy_id` and `version` (integer, bumped when arguments change)
- function name
- arguments: `min_risk`, `min_sweep`, `dynamic_targets`, `include_orh_orl`, `allow_reentry`, `bar_minutes` list, `skip_lunch`, `stop_at`
- whether the trend agent may replace the target (default **no** until a study includes that replacement)
- VWAP: one of `niftybees`, `twap`, `typical_on_bars`
- exit: `bar_extreme` or `intrabar_cmp`. One of them. The daemon's 10-second CMP exit is `intrabar_cmp` and is not what `exit_of` does
- git revision is recorded on each run, not inside the card

The October study is version 0 of `scan` on 5-minute bars without dynamic targets. It stays as the benchmark. The daemon's current call is an unnumbered variant. Promoting it requires a new version id, not a silent edit.

## Parity test

Given a fixed list of bars, PDH, PDL, and a card:

1. Signals from a pure function used by the backtest.
2. Signals from `DetectorAgent` with news and compression vetoes **off**.
3. Entry time, side, entry, stop, and target are equal.

A second test turns the trend-agent rewrite on and expects the targets to differ. That test exists so the rewrite cannot sneak back in while the card says off.

## Truncation test

For each bar index *i*, signals using `bars[: i+1]` equal the full-sample signals whose time is that bar. Run on a 5-minute fixture and on a 1-minute fixture if the card allows 1-minute. Fail on a mismatch.

## Timestamp

Bar time is the bar **start**, and a signal is valid only after close (start + `bar_minutes`). The daemon already drops the forming 5-minute bar. The backtest must not signal on a bar and fill at that bar's close if the card's exit model is next-bar. For the current `exit_of` behaviour (fill when a later bar's high or low trades through), document it as "touch on a later completed bar". Do not call it a tick fill.

## Out of scope for the parity change

Changing `MIN_RISK`, R multiples, or OTM steps. Deleting equal-high logic can be a separate card with version 0 and zero claimed expectancy. `evaluate_sweep` stays out of the daemon card.

## Acceptance

The daemon refuses to start a session if the card file is missing or its hash is not the one printed at startup. A backtest invocation without the same card prints the hash it used. Two hashes, two strategies, two reports.
