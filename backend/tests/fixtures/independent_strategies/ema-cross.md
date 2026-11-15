---
name: ema-cross
rules_v2:
  data_dependencies:
  - underlying_5m
  - historical_contract_metadata
  entry: []
  entry_blackout: null
  entry_composition: null
  exit: []
  indicators:
  - name: ema
    timeframe: 5m
    period: 15
    params: {}
  instrument:
    lot_size: 75
    short_distance: null
    strike_offset: 0.0
    structure: long_call
    underlying: NIFTY
    wing_width: null
  intraday_entry:
  - direction: LONG
    kind: ema_relation
    params:
      ema_period: 15
      relation: cross_above
    requires_closed_bar: true
    timeframe: 5m
  market: nse_options
  parameters:
    ema_period:
      description: null
      max_value: 50.0
      min_value: 5.0
      mutable_in_research: true
      name: ema_period
      type: int
      value: 15
    iv:
      description: null
      max_value: 0.5
      min_value: 0.01
      mutable_in_research: true
      name: iv
      type: float
      value: 0.02
    stop_loss_points:
      description: null
      max_value: 100.0
      min_value: 10.0
      mutable_in_research: true
      name: stop_loss_points
      type: float
      value: 25.0
    target_r_multiple:
      description: null
      max_value: 5.0
      min_value: 1.0
      mutable_in_research: true
      name: target_r_multiple
      type: float
      value: 2.0
  partial_exits: []
  position_sizing:
    capital_inr: 50000.0
    lot_step_mode: fixed_lots
    max_lots: 1
    risk_policy_id: paper-risk-v2
  provenance:
    author_hints_applied: false
    citations: []
    extraction_mode: manual
    ingest_run_id: null
    source_ref: null
    source_sha256: null
    source_type: manual
  risk:
    cooldown_days: null
    max_consecutive_losses: null
    max_daily_loss_pct: 2.0
    max_entries_per_day: null
    max_loss_pct_of_capital: null
    max_losses_per_day: null
    max_lots: 1
    max_open_positions: 1
    min_minutes_between_trades: null
    min_risk_reward: null
  schema_version: strategy-rules-v2
  session_window:
    blackout_windows: []
    entry_cutoff: '15:10'
    entry_start: 09:15
    square_off: '15:25'
    timezone: Asia/Kolkata
  stop_loss:
    max_points: 100.0
    min_points: 10.0
    mode: index_points
    value: 25.0
  strategy_id: ema-cross
  strategy_version: v1
  target:
    allow_liquidity_hop: false
    mode: r_multiple
    value: 2.0
  timeframes:
    primary: 5m
    reference: null
    structure: null
  unresolved_gaps: []
---
