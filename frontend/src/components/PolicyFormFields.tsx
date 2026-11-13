"use client";

import type { ReactNode } from "react";

import { EVENT_KINDS, LADDER_ACTIONS, type LadderAction, type PolicyForm } from "@/lib/policy";

type Props = { form: PolicyForm; onChange: (form: PolicyForm) => void; profitTargetPct: number | null };

const input = "w-full rounded-md border px-2 py-1.5 text-sm";

function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="block space-y-1">
      <span className="text-xs font-medium text-zinc-700">{label}</span>
      {children}
      {hint && <span className="block text-xs text-zinc-500">{hint}</span>}
    </label>
  );
}

function Section({ title, note, children }: { title: string; note: string; children: ReactNode }) {
  return (
    <fieldset className="rounded-lg border p-4 space-y-3">
      <legend className="px-1 text-sm font-medium">{title}</legend>
      <p className="text-xs text-zinc-500">{note}</p>
      {children}
    </fieldset>
  );
}

function Toggle({ checked, onChange, label }: { checked: boolean; onChange: (v: boolean) => void; label: string }) {
  return (
    <label className="flex items-center gap-2 text-sm">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      {label}
    </label>
  );
}

export function PolicyFormFields({ form, onChange, profitTargetPct }: Props) {
  const set = <K extends keyof PolicyForm>(key: K, value: PolicyForm[K]) => onChange({ ...form, [key]: value });
  const target = profitTargetPct !== null ? `${profitTargetPct}%` : "the strategy's";

  return (
    <div className="space-y-4">
      <Section title="Capital and limits" note="Frozen at approval. Capital can change later only through deposits and withdrawals.">
        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="Capital (₹)">
            <input className={input} inputMode="decimal" value={form.capital_inr} onChange={(e) => set("capital_inr", e.target.value)} />
          </Field>
          <Field label="Max lots" hint="Blank: the strategy's max_lots">
            <input className={input} inputMode="numeric" value={form.max_lots} onChange={(e) => set("max_lots", e.target.value)} />
          </Field>
          <Field label="Max daily loss (% of capital)" hint="Blank: the strategy's rule">
            <input className={input} inputMode="decimal" value={form.max_daily_loss_pct} onChange={(e) => set("max_daily_loss_pct", e.target.value)} />
          </Field>
        </div>
      </Section>

      <Section title="Position sizing" note="How many lots each entry trades. Never more than the approved max lots.">
        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="Mode">
            <select className={input} value={form.sizing_mode} onChange={(e) => set("sizing_mode", e.target.value as PolicyForm["sizing_mode"])}>
              <option value="fixed">Fixed lots</option>
              <option value="risk_pct">Risk % of capital per trade</option>
              <option value="risk_inr">Risk ₹ per trade</option>
            </select>
          </Field>
          {form.sizing_mode === "fixed" && (
            <Field label="Lots" hint="Blank: max lots">
              <input className={input} inputMode="numeric" value={form.fixed_lots} onChange={(e) => set("fixed_lots", e.target.value)} />
            </Field>
          )}
          {form.sizing_mode === "risk_pct" && (
            <Field label="Risk per trade (% of capital)" hint="Lots are sized so the trade's max loss fits">
              <input className={input} inputMode="decimal" value={form.risk_pct} onChange={(e) => set("risk_pct", e.target.value)} />
            </Field>
          )}
          {form.sizing_mode === "risk_inr" && (
            <Field label="Risk per trade (₹)">
              <input className={input} inputMode="decimal" value={form.risk_inr} onChange={(e) => set("risk_inr", e.target.value)} />
            </Field>
          )}
          {form.sizing_mode !== "fixed" && (
            <Field label="Min lots" hint="Below this the entry is blocked">
              <input className={input} inputMode="numeric" value={form.min_lots} onChange={(e) => set("min_lots", e.target.value)} />
            </Field>
          )}
        </div>
        {form.sizing_mode !== "fixed" && (
          <div className="space-y-2">
            <Toggle checked={form.compounding} onChange={(v) => set("compounding", v)} label="Compound: size from capital plus a share of realized profit" />
            {form.compounding && (
              <div className="grid gap-3 sm:grid-cols-3">
                <Field label="Share of profit added (%)">
                  <input className={input} inputMode="decimal" value={form.profit_share_pct} onChange={(e) => set("profit_share_pct", e.target.value)} />
                </Field>
                <Field label="Ceiling (₹)" hint="Optional cap on sizing capital">
                  <input className={input} inputMode="decimal" value={form.ceiling_inr} onChange={(e) => set("ceiling_inr", e.target.value)} />
                </Field>
              </div>
            )}
          </div>
        )}
      </Section>

      <Section
        title="VIX sizing"
        note="Fewer lots when India VIX is high: the highest band at or below today's VIX scales lots, never below one lot. With no VIX reading, the last band applies."
      >
        <Toggle checked={form.vix_sizing} onChange={(v) => set("vix_sizing", v)} label="Scale lots by India VIX" />
        {form.vix_sizing && (
          <div className="space-y-2">
            {form.vix_bands.map((b, i) => {
              const update = (patch: Partial<typeof b>) =>
                set("vix_bands", form.vix_bands.map((q, j) => (j === i ? { ...q, ...patch } : q)));
              return (
                <div key={i} className="grid grid-cols-[1fr_1fr_auto] items-end gap-2">
                  <Field label="VIX at or above">
                    <input className={input} inputMode="decimal" value={b.at_or_above} onChange={(e) => update({ at_or_above: e.target.value })} />
                  </Field>
                  <Field label="Size factor (0–1)">
                    <input className={input} inputMode="decimal" value={b.size_factor} onChange={(e) => update({ size_factor: e.target.value })} />
                  </Field>
                  <button type="button" className="rounded border px-2 py-1.5 text-xs" onClick={() => set("vix_bands", form.vix_bands.filter((_, j) => j !== i))}>
                    Remove
                  </button>
                </div>
              );
            })}
            <button
              type="button"
              className="rounded border px-2 py-0.5 text-xs disabled:opacity-50"
              disabled={form.vix_bands.length >= 5}
              onClick={() => set("vix_bands", [...form.vix_bands, { at_or_above: "", size_factor: "" }])}
            >
              Add band
            </button>
          </div>
        )}
      </Section>

      <Section
        title="Odds gate"
        note="Trade only when the odds favour you: an entry needs the strategy's own past trades in today's India VIX band to have made money on average, per lot after costs. Past trades come from a rules-only backtest and only count once closed, so early on every band is blocked until it has enough of them."
      >
        <Toggle checked={form.odds_gate} onChange={(v) => set("odds_gate", v)} label="Enter only when the odds favour the trade" />
        {form.odds_gate && (
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="VIX band edges" hint="Comma-separated, rising, up to 4. 13, 16 gives below 13, 13–16 and 16 or more">
              <input className={input} value={form.odds_vix_bands} onChange={(e) => set("odds_vix_bands", e.target.value)} />
            </Field>
            <Field label="Min past trades in the band" hint="5 to 500; fewer blocks the entry">
              <input className={input} inputMode="numeric" value={form.odds_min_trades} onChange={(e) => set("odds_min_trades", e.target.value)} />
            </Field>
            <Field label="Min average net per lot (₹)" hint="The band's past trades must beat this">
              <input className={input} inputMode="decimal" value={form.odds_min_expectancy_inr} onChange={(e) => set("odds_min_expectancy_inr", e.target.value)} />
            </Field>
            <Field label="Look back (years)" hint="Blank: all history">
              <input className={input} inputMode="numeric" value={form.odds_lookback_years} onChange={(e) => set("odds_lookback_years", e.target.value)} />
            </Field>
          </div>
        )}
      </Section>

      <Section
        title="Profit booking"
        note={`Runs only when the strategy's own exits did not fire, so thresholds must sit below ${target} profit target. Percentages are of the credit received, or of the premium paid when buying options.`}
      >
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-sm">Partial booking levels</span>
            <button
              type="button"
              className="rounded border px-2 py-0.5 text-xs disabled:opacity-50"
              disabled={form.partials.length >= 5}
              onClick={() => set("partials", [...form.partials, { at_pct_of_credit: "25", close_pct: "50" }])}
            >
              Add level
            </button>
          </div>
          {form.partials.map((p, i) => (
            <div key={i} className="grid grid-cols-[1fr_1fr_auto] items-end gap-2">
              <Field label="At profit (% of credit)">
                <input
                  className={input}
                  inputMode="decimal"
                  value={p.at_pct_of_credit}
                  onChange={(e) => set("partials", form.partials.map((q, j) => (j === i ? { ...q, at_pct_of_credit: e.target.value } : q)))}
                />
              </Field>
              <Field label="Close (% of lots opened)">
                <input
                  className={input}
                  inputMode="decimal"
                  value={p.close_pct}
                  onChange={(e) => set("partials", form.partials.map((q, j) => (j === i ? { ...q, close_pct: e.target.value } : q)))}
                />
              </Field>
              <button type="button" className="rounded border px-2 py-1.5 text-xs" onClick={() => set("partials", form.partials.filter((_, j) => j !== i))}>
                Remove
              </button>
            </div>
          ))}
          {form.partials.length > 0 && <p className="text-xs text-zinc-500">At least one lot always stays open for the strategy&apos;s own target.</p>}
        </div>
        <Toggle checked={form.lock_in} onChange={(v) => set("lock_in", v)} label="Lock in profit" />
        {form.lock_in && (
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Once profit reaches (% of credit)">
              <input className={input} inputMode="decimal" value={form.lock_after_pct} onChange={(e) => set("lock_after_pct", e.target.value)} />
            </Field>
            <Field label="Close if it falls back to (% of credit)">
              <input className={input} inputMode="decimal" value={form.lock_at_pct} onChange={(e) => set("lock_at_pct", e.target.value)} />
            </Field>
          </div>
        )}
        <Toggle checked={form.trailing} onChange={(v) => set("trailing", v)} label="Trailing stop" />
        {form.trailing && (
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Starts once profit reaches (% of credit)">
              <input className={input} inputMode="decimal" value={form.trail_after_pct} onChange={(e) => set("trail_after_pct", e.target.value)} />
            </Field>
            <Field label="Close after giving back (% of peak profit)">
              <input className={input} inputMode="decimal" value={form.giveback_pct} onChange={(e) => set("giveback_pct", e.target.value)} />
            </Field>
          </div>
        )}
      </Section>

      <Section title="Time and event exits" note="Close the whole position before expiry or ahead of scheduled events.">
        <Toggle checked={form.time_exit} onChange={(v) => set("time_exit", v)} label="Enable time and event exits" />
        {form.time_exit && (
          <div className="space-y-3">
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="Days before expiry" hint="Blank: no expiry exit">
                <input className={input} inputMode="numeric" value={form.days_before_expiry} onChange={(e) => set("days_before_expiry", e.target.value)} />
              </Field>
              <Field label="From time of day (IST, HH:MM)" hint="Optional; backtests use the close">
                <input className={input} placeholder="15:00" value={form.exit_at} onChange={(e) => set("exit_at", e.target.value)} />
              </Field>
            </div>
            <div className="space-y-1">
              <span className="text-xs font-medium text-zinc-700">Exit before these events</span>
              <div className="flex flex-wrap gap-3">
                {EVENT_KINDS.map((kind) => (
                  <Toggle
                    key={kind}
                    label={kind}
                    checked={form.before_events.includes(kind)}
                    onChange={(v) => set("before_events", v ? [...form.before_events, kind] : form.before_events.filter((k) => k !== kind))}
                  />
                ))}
              </div>
            </div>
            {form.before_events.length > 0 && (
              <Field label="Days before the event">
                <input className={input} inputMode="numeric" value={form.event_days_before} onChange={(e) => set("event_days_before", e.target.value)} />
              </Field>
            )}
          </div>
        )}
      </Section>

      <Section title="Daily profit target" note="Once today's P&L (realized plus open) reaches the target.">
        <Toggle checked={form.daily_target} onChange={(v) => set("daily_target", v)} label="Enable daily profit target" />
        {form.daily_target && (
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Target (₹)">
              <input className={input} inputMode="decimal" value={form.daily_profit_inr} onChange={(e) => set("daily_profit_inr", e.target.value)} />
            </Field>
            <Field label="Then">
              <select className={input} value={form.daily_on_hit} onChange={(e) => set("daily_on_hit", e.target.value as PolicyForm["daily_on_hit"])}>
                <option value="stop_new_entries">Stop new entries for the day</option>
                <option value="close_all">Close all positions and stop for the day</option>
              </select>
            </Field>
          </div>
        )}
      </Section>

      <Section
        title="Drawdown ladder"
        note="Actions as equity falls from its peak. They only get stronger up the ladder; MAX_DRAWDOWN_PCT always adds a final pause."
      >
        <Toggle checked={form.drawdown} onChange={(v) => set("drawdown", v)} label="Use a drawdown ladder" />
        {form.drawdown && (
          <div className="space-y-3">
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="Measure drawdown on">
                <select className={input} value={form.drawdown_basis} onChange={(e) => set("drawdown_basis", e.target.value as PolicyForm["drawdown_basis"])}>
                  <option value="mtm">Mark-to-market equity (includes open positions)</option>
                  <option value="realized">Closed trades only</option>
                </select>
              </Field>
              <Field label="Step down once drawdown is below (%)" hint="Blank: step down immediately">
                <input className={input} inputMode="decimal" value={form.restore_below_pct} onChange={(e) => set("restore_below_pct", e.target.value)} />
              </Field>
            </div>
            {form.ladder.map((level, i) => {
              const update = (patch: Partial<typeof level>) =>
                set("ladder", form.ladder.map((l, j) => (j === i ? { ...l, ...patch } : l)));
              return (
                <div key={i} className="grid grid-cols-[1fr_1.5fr_1fr_auto] items-end gap-2">
                  <Field label="At drawdown (%)">
                    <input className={input} inputMode="decimal" value={level.at_pct} onChange={(e) => update({ at_pct: e.target.value })} />
                  </Field>
                  <Field label="Action">
                    <select className={input} value={level.action} onChange={(e) => update({ action: e.target.value as LadderAction })}>
                      {LADDER_ACTIONS.map((a) => (
                        <option key={a} value={a}>
                          {a.replaceAll("_", " ")}
                        </option>
                      ))}
                    </select>
                  </Field>
                  {level.action === "reduce_size" ? (
                    <Field label="Size factor (0–1)">
                      <input className={input} inputMode="decimal" value={level.size_factor} onChange={(e) => update({ size_factor: e.target.value })} />
                    </Field>
                  ) : level.action === "pause" ? (
                    <Toggle checked={level.close_open_positions} onChange={(v) => update({ close_open_positions: v })} label="Close positions" />
                  ) : (
                    <span />
                  )}
                  <button type="button" className="rounded border px-2 py-1.5 text-xs" onClick={() => set("ladder", form.ladder.filter((_, j) => j !== i))}>
                    Remove
                  </button>
                </div>
              );
            })}
            <button
              type="button"
              className="rounded border px-2 py-0.5 text-xs"
              onClick={() => set("ladder", [...form.ladder, { at_pct: "", action: "pause", size_factor: "", close_open_positions: false }])}
            >
              Add level
            </button>
          </div>
        )}
      </Section>

      <Section
        title="Capital floor"
        note="A fixed ₹ line under equity. An entry needs equity above it and its max loss within the buffer left (equity minus floor). Falling below it pauses the deployment."
      >
        <Toggle checked={form.floor} onChange={(v) => set("floor", v)} label="Use a capital floor" />
        {form.floor && (
          <div className="space-y-3">
            <div className="grid gap-3 sm:grid-cols-3">
              <Field label="Floor (₹)" hint="Below the capital">
                <input className={input} inputMode="decimal" value={form.floor_inr} onChange={(e) => set("floor_inr", e.target.value)} />
              </Field>
              <Field label="Break (trading days)" hint="Blank: only a person restarts it">
                <input className={input} inputMode="numeric" value={form.floor_resume_days} onChange={(e) => set("floor_resume_days", e.target.value)} />
              </Field>
              {!form.drawdown && (
                <Field label="Measure equity on">
                  <select className={input} value={form.drawdown_basis} onChange={(e) => set("drawdown_basis", e.target.value as PolicyForm["drawdown_basis"])}>
                    <option value="mtm">Mark-to-market (includes open positions)</option>
                    <option value="realized">Closed trades only</option>
                  </select>
                </Field>
              )}
            </div>
            <Toggle checked={form.floor_close_positions} onChange={(v) => set("floor_close_positions", v)} label="Close open positions when the floor is broken" />
            <Toggle checked={form.floor_raise} onChange={(v) => set("floor_raise", v)} label="Raise the floor once" />
            {form.floor_raise && (
              <div className="grid gap-3 sm:grid-cols-2">
                <Field label="Once equity reaches (₹)" hint="Above the capital">
                  <input className={input} inputMode="decimal" value={form.raise_at_equity_inr} onChange={(e) => set("raise_at_equity_inr", e.target.value)} />
                </Field>
                <Field label="Raise the floor to (₹)">
                  <input className={input} inputMode="decimal" value={form.raise_to_inr} onChange={(e) => set("raise_to_inr", e.target.value)} />
                </Field>
              </div>
            )}
            <p className="text-xs text-zinc-500">
              After the break the supervisor starts the deployment again by itself if equity is above the floor (deposit to top it up), with the floor back at its starting
              level. Backtests top up to the capital and report it.
            </p>
          </div>
        )}
      </Section>

      <Section
        title="Withdrawal rule"
        note="When profit should come out, on closed trades. Live, you get an alert and record the withdrawal yourself; backtests take it on the first day with no open position."
      >
        <Toggle checked={form.withdrawal} onChange={(v) => set("withdrawal", v)} label="Use a withdrawal rule" />
        {form.withdrawal && (
          <div className="grid gap-3 sm:grid-cols-3">
            <Field label="First, once equity reaches (₹)" hint="Above the capital">
              <input className={input} inputMode="decimal" value={form.first_target_inr} onChange={(e) => set("first_target_inr", e.target.value)} />
            </Field>
            <Field label="Withdraw (₹)">
              <input className={input} inputMode="decimal" value={form.first_amount_inr} onChange={(e) => set("first_amount_inr", e.target.value)} />
            </Field>
            <Field label="Then, share of each rise (%)" hint="Blank: only the first withdrawal">
              <input className={input} inputMode="decimal" value={form.then_share_pct} onChange={(e) => set("then_share_pct", e.target.value)} />
            </Field>
          </div>
        )}
      </Section>
    </div>
  );
}
