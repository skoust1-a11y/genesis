/** The approval form's state, and its conversion to the backend's `risk_limits`. */

export const LADDER_ACTIONS = ["warn", "reduce_size", "stop_new_entries", "pause"] as const;
export const EVENT_KINDS = ["rbi_policy", "budget", "fomc", "election", "results", "board_meeting", "other"] as const;

export type LadderAction = (typeof LADDER_ACTIONS)[number];

export type LevelForm = {
  at_pct: string;
  action: LadderAction;
  size_factor: string;
  close_open_positions: boolean;
};

export type PartialForm = { at_pct_of_credit: string; close_pct: string };

export type VixBandForm = { at_or_above: string; size_factor: string };

export type PolicyForm = {
  capital_inr: string;
  max_lots: string;
  max_daily_loss_pct: string;

  sizing_mode: "fixed" | "risk_pct" | "risk_inr";
  fixed_lots: string;
  risk_pct: string;
  risk_inr: string;
  min_lots: string;
  compounding: boolean;
  profit_share_pct: string;
  ceiling_inr: string;

  vix_sizing: boolean;
  vix_bands: VixBandForm[];

  partials: PartialForm[];
  lock_in: boolean;
  lock_after_pct: string;
  lock_at_pct: string;
  trailing: boolean;
  trail_after_pct: string;
  giveback_pct: string;

  time_exit: boolean;
  days_before_expiry: string;
  exit_at: string;
  before_events: string[];
  event_days_before: string;

  daily_target: boolean;
  daily_profit_inr: string;
  daily_on_hit: "stop_new_entries" | "close_all";

  drawdown: boolean;
  drawdown_basis: "mtm" | "realized";
  restore_below_pct: string;
  ladder: LevelForm[];

  floor: boolean;
  floor_inr: string;
  floor_raise: boolean;
  raise_at_equity_inr: string;
  raise_to_inr: string;
  floor_close_positions: boolean;
  floor_resume_days: string;

  withdrawal: boolean;
  first_target_inr: string;
  first_amount_inr: string;
  then_share_pct: string;

  odds_gate: boolean;
  odds_vix_bands: string;
  odds_min_trades: string;
  odds_min_expectancy_inr: string;
  odds_lookback_years: string;
};

export const DEFAULT_FORM: PolicyForm = {
  capital_inr: "50000",
  max_lots: "",
  max_daily_loss_pct: "",
  sizing_mode: "risk_inr",
  fixed_lots: "",
  risk_pct: "3",
  risk_inr: "1500",
  min_lots: "1",
  compounding: false,
  profit_share_pct: "50",
  ceiling_inr: "",
  vix_sizing: false,
  vix_bands: [
    { at_or_above: "18", size_factor: "0.5" },
    { at_or_above: "25", size_factor: "0.25" },
  ],
  partials: [],
  lock_in: false,
  lock_after_pct: "30",
  lock_at_pct: "10",
  trailing: false,
  trail_after_pct: "25",
  giveback_pct: "40",
  time_exit: false,
  days_before_expiry: "1",
  exit_at: "",
  before_events: [],
  event_days_before: "1",
  daily_target: false,
  daily_profit_inr: "1500",
  daily_on_hit: "stop_new_entries",
  drawdown: true,
  drawdown_basis: "mtm",
  restore_below_pct: "",
  ladder: [
    { at_pct: "5", action: "warn", size_factor: "", close_open_positions: false },
    { at_pct: "8", action: "reduce_size", size_factor: "0.5", close_open_positions: false },
    { at_pct: "12", action: "stop_new_entries", size_factor: "", close_open_positions: false },
  ],
  floor: false,
  floor_inr: "45000",
  floor_raise: false,
  raise_at_equity_inr: "60000",
  raise_to_inr: "55000",
  floor_close_positions: true,
  floor_resume_days: "5",
  withdrawal: false,
  first_target_inr: "60000",
  first_amount_inr: "5000",
  then_share_pct: "70",
  odds_gate: false,
  odds_vix_bands: "13, 16",
  odds_min_trades: "20",
  odds_min_expectancy_inr: "0",
  odds_lookback_years: "",
};

/** "13, 16" -> [13, 16]; blank entries are dropped. */
export function parseBands(value: string): number[] {
  return value
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
    .map(Number);
}

function num(value: string): number | undefined {
  return value.trim() === "" ? undefined : Number(value);
}

function compact<T extends Record<string, unknown>>(obj: T): Partial<T> {
  return Object.fromEntries(Object.entries(obj).filter(([, v]) => v !== undefined)) as Partial<T>;
}

export function toRiskLimits(f: PolicyForm): Record<string, unknown> {
  const policy: Record<string, unknown> = {};
  if (f.sizing_mode === "fixed") {
    if (num(f.fixed_lots) !== undefined) policy.sizing = { mode: "fixed", lots: num(f.fixed_lots) };
  } else {
    policy.sizing = compact({
      mode: f.sizing_mode,
      risk_pct: f.sizing_mode === "risk_pct" ? num(f.risk_pct) : undefined,
      risk_inr: f.sizing_mode === "risk_inr" ? num(f.risk_inr) : undefined,
      min_lots: num(f.min_lots),
    });
    if (f.compounding) {
      policy.compounding = compact({ profit_share_pct: num(f.profit_share_pct), ceiling_inr: num(f.ceiling_inr) });
    }
  }
  const booking = compact({
    partial: f.partials.length
      ? f.partials.map((p) => ({ at_pct_of_credit: num(p.at_pct_of_credit), close_pct: num(p.close_pct) }))
      : undefined,
    lock_in: f.lock_in
      ? { after_pct_of_credit: num(f.lock_after_pct), lock_pct_of_credit: num(f.lock_at_pct) }
      : undefined,
    trailing: f.trailing
      ? { after_pct_of_credit: num(f.trail_after_pct), giveback_pct: num(f.giveback_pct) }
      : undefined,
  });
  if (Object.keys(booking).length) policy.booking = booking;
  if (f.time_exit) {
    policy.time_exit = compact({
      days_before_expiry: num(f.days_before_expiry),
      at: f.exit_at.trim() || undefined,
      before_events: f.before_events.length ? f.before_events : undefined,
      event_days_before: f.before_events.length ? num(f.event_days_before) : undefined,
    });
  }
  if (f.daily_target) policy.daily_target = { profit_inr: num(f.daily_profit_inr), on_hit: f.daily_on_hit };
  if (f.vix_sizing && f.vix_bands.length) {
    policy.vix_sizing = {
      bands: f.vix_bands.map((b) => ({ at_or_above: num(b.at_or_above), size_factor: num(b.size_factor) })),
    };
  }

  const limits: Record<string, unknown> = compact({
    capital_inr: num(f.capital_inr),
    max_lots: num(f.max_lots),
    max_daily_loss_pct: num(f.max_daily_loss_pct),
  });
  if (f.withdrawal) {
    policy.withdrawal = compact({
      first_target_inr: num(f.first_target_inr),
      first_amount_inr: num(f.first_amount_inr),
      then_share_pct: num(f.then_share_pct),
    });
  }
  if (f.odds_gate) {
    const bands = parseBands(f.odds_vix_bands);
    policy.odds_gate = compact({
      vix_bands: bands.length ? bands : undefined,
      min_trades: num(f.odds_min_trades),
      min_expectancy_inr: num(f.odds_min_expectancy_inr),
      lookback_years: num(f.odds_lookback_years),
    });
  }
  if (Object.keys(policy).length) limits.policy = policy;
  const ladder = f.drawdown && f.ladder.length;
  if (ladder || f.floor) {
    limits.drawdown = compact({
      basis: f.drawdown_basis,
      restore_below_pct: ladder ? num(f.restore_below_pct) : undefined,
      ladder: ladder
        ? f.ladder.map((l) =>
            compact({
              at_pct: num(l.at_pct),
              action: l.action,
              size_factor: l.action === "reduce_size" ? num(l.size_factor) : undefined,
              close_open_positions: l.action === "pause" ? l.close_open_positions : undefined,
            }),
          )
        : undefined,
      floor: f.floor
        ? compact({
            floor_inr: num(f.floor_inr),
            raise_once: f.floor_raise
              ? { at_equity_inr: num(f.raise_at_equity_inr), to_inr: num(f.raise_to_inr) }
              : undefined,
            close_open_positions: f.floor_close_positions,
            resume_after_days: num(f.floor_resume_days),
          })
        : undefined,
    });
  }
  return limits;
}
