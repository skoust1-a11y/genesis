# Parameter governance specification

Date: 4 Nov 2026.

## Three files, three meanings

| File | Who writes | Who reads for trading |
|---|---|---|
| `research_proposals.jsonl` append-only | Parameter agent and humans | Nobody in the order path |
| `validated_params.json` | A person, after a result file exists | Backtests that opt in |
| `trades/active_strategy_params.json` | A person only | Daemon, at startup |

Today `active_strategy_params.json` is written by `save_calibrated_parameters` from `PostMarketManager.run_daily_review`. That call is removed from the daemon and from `run_post_market_audit.py`. The function may remain for tests if it writes only the proposal log.

## A proposal row

Required keys. Missing key means the row is not a proposal.

- `parameter`
- `previous_value` read from the live file at proposal time
- `proposed_value`
- `reason` one sentence
- `evidence` a path to a result file, or `none`
- `backtest_version` the rule-card version, or `none`
- `oos` a number with a window, or `none`
- `validation_status` `proposed` only. The agent cannot set `validated`
- `approval_status` `pending`. The agent cannot set `approved`
- `created_at` IST

`expected_point_gain` is not a field. The current +25 and +45 are not evidence.

The ADX sentence "filtered out 100% of sideways false breakouts" is not emitted unless a result file counted sideways days and false breakouts.

## What the daemon does with the live file

Reads `otm_steps` and, only if the rule card says the waterfall buffer is live, `micro_wick_stop_buffer`. Ignores unknown keys. `dynamic_rr_ceiling` is unused today; leave it out of the live file or stop writing it. Do not wire it into `TrendAgent` as part of this work.

## Displayed performance

`RESEARCH_CATALOG` stops shipping `win_rate`, `net_pnl_inr`, `profit_factor`, `max_drawdown_inr`, and `trades_count` as if they were measurements. The API returns the thesis and the file name. Numbers come from a result file path the operator attaches, or they are omitted.

`reports/daily_audit_*.md` prints index points and R from the journal, and prints option rupees only under `options_accounting_model.md`. It does not print "7 lots" from the reserve.

## Approval

A person edits `approval_status` to `approved` in the proposal log and copies the value into `active_strategy_params.json`. There is no automatic copy. A bad study cannot reach Monday's open by being the only process awake at 15:31.
