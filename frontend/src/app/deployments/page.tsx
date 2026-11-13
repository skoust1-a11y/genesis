"use client";

import { useCallback, useEffect, useState } from "react";

import { EquityChart } from "@/components/EquityChart";
import { type OddsBand, OddsTable } from "@/components/OddsTable";
import { api, getActor, inr, pct, setActor } from "@/lib/api";

type Deployment = {
  id: string;
  strategy_id: string;
  mode: string;
  status: string;
  created_at: string;
  risk_limits: Record<string, unknown>;
  deployed_strategy?: { name: string; version: number };
  crew_running: boolean;
  last_heartbeat: { at: string; feed: string; outcome: string | null } | null;
  last_heartbeat_age_s: number | null;
};
type KillSwitch = { cancelled: string[]; exits: { live_trade_id: string }[]; error: string | null };

function crewLabel(d: Deployment): string {
  if (d.status !== "started") return "—";
  if (d.crew_running) return `running (${d.last_heartbeat?.feed} feed)`;
  if (!d.last_heartbeat) return "crew not running";
  return `crew not running (last tick ${Math.round((d.last_heartbeat_age_s ?? 0) / 60)} min ago)`;
}
type Equity = {
  capital_inr: number;
  realized_inr: number;
  unrealized_inr: number;
  equity_inr: number;
  peak_inr: number;
  drawdown_inr: number;
  drawdown_pct: number;
  max_drawdown_pct: number;
  curve: { at: string; equity_inr: number }[];
};
type Drawdown = {
  ladder: { at_pct: number; action: string; size_factor?: number; close_open_positions?: boolean }[];
  level: number;
  events: { id: number; created_at: string; level: number; action: string; drawdown_pct: number; detail: string }[];
};
type Position = { open_intent_id: string; opened_at: string; pnl: number | null; peak_pct?: number | null; dte?: number | null; marked_at: string | null; legs?: { quantity: number }[] };
type Money = {
  floor: {
    floor_inr: number;
    raise_once?: { at_equity_inr: number; to_inr: number };
    resume_after_days?: number;
    close_open_positions: boolean;
    basis: string;
    raised: boolean;
    floor_in_force_inr: number;
    equity_inr: number;
    buffer_inr: number;
    on_break: boolean;
    break_days_left: number | null;
  } | null;
  withdrawal: {
    rule: { first_target_inr: number; first_amount_inr: number; then_share_pct?: number };
    due_inr: number;
    withdrawn_inr: number;
    realized_equity_inr: number;
  } | null;
};
type Odds = {
  gate: { vix_bands: number[]; min_trades: number; min_expectancy_inr: number; lookback_years?: number };
  as_of: string;
  vix_date: string | null;
  today: OddsBand & { vix: number | null };
  entries_open: boolean;
  summary: string;
  bands: OddsBand[];
  history_trades: number;
} | null;
type CapitalEvents = { capital_in_force_inr: number; capital_events: { id: number; kind: string; amount_inr: number; note: string | null; actor: string; created_at: string }[] };

const STATUS_CLASS: Record<string, string> = {
  started: "bg-emerald-100 text-emerald-900",
  paused: "bg-amber-100 text-amber-900",
  approved: "bg-sky-100 text-sky-900",
  stopped: "bg-zinc-200 text-zinc-700",
  halted: "bg-rose-100 text-rose-900",
};

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border px-3 py-2">
      <div className="text-xs text-zinc-500">{label}</div>
      <div className="font-medium">{value}</div>
    </div>
  );
}

export default function DeploymentsPage() {
  const [deployments, setDeployments] = useState<Deployment[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<Deployment | null>(null);
  const [equity, setEquity] = useState<Equity | null>(null);
  const [drawdown, setDrawdown] = useState<Drawdown | null>(null);
  const [positions, setPositions] = useState<Position[]>([]);
  const [capital, setCapital] = useState<CapitalEvents | null>(null);
  const [money, setMoney] = useState<Money | null>(null);
  const [odds, setOdds] = useState<Odds | undefined>(undefined);
  const [oddsError, setOddsError] = useState<string | null>(null);
  const [actor, setActorName] = useState("");
  const [reason, setReason] = useState("");
  const [capitalKind, setCapitalKind] = useState("deposit");
  const [capitalAmount, setCapitalAmount] = useState("");
  const [capitalNote, setCapitalNote] = useState("");
  const [error, setError] = useState<string | null>(null);

  const loadList = useCallback(() => {
    api<{ deployments: Deployment[] }>("/api/live/deployments")
      .then((d) => setDeployments(d.deployments))
      .catch((e) => setError(e.message));
  }, []);

  const loadDetail = useCallback((id: string) => {
    setError(null);
    Promise.all([
      api<Deployment>(`/api/live/deployments/${id}`),
      api<Equity>(`/api/live/equity?deployment_id=${id}&curve=true`),
      api<Drawdown>(`/api/live/deployments/${id}/drawdown`),
      api<{ positions: Position[] }>(`/api/live/positions?deployment_id=${id}`),
      api<CapitalEvents>(`/api/live/deployments/${id}/capital-events`),
      api<Money>(`/api/live/deployments/${id}/money`),
    ])
      .then(([d, eq, dd, pos, cap, m]) => {
        setDetail(d);
        setEquity(eq);
        setDrawdown(dd);
        setPositions(pos.positions);
        setCapital(cap);
        setMoney(m);
      })
      .catch((e) => setError(e.message));
  }, []);

  useEffect(() => {
    setActorName(getActor());
    loadList();
  }, [loadList]);

  useEffect(() => {
    if (selected) loadDetail(selected);
  }, [selected, loadDetail]);

  useEffect(() => {
    if (!selected) return;
    setOdds(undefined);
    setOddsError(null);
    // Loaded on its own: the first request of the day backtests the strategy's whole history.
    api<{ odds: Odds }>(`/api/live/deployments/${selected}/odds`)
      .then((d) => setOdds(d.odds))
      .catch((e) => setOddsError(e.message));
  }, [selected]);

  const [notice, setNotice] = useState<string | null>(null);

  async function act(path: string, body: unknown) {
    setActor(actor.trim());
    setError(null);
    setNotice(null);
    try {
      const out = await api<{ kill_switch?: KillSwitch }>(path, { body });
      const ks = out.kill_switch;
      if (ks) {
        setNotice(
          `Stopped. Cancelled ${ks.cancelled.length} working order(s), closed ${ks.exits.length} position(s).` +
            (ks.error ? ` Failed: ${ks.error}` : ""),
        );
      }
      loadList();
      if (selected) loadDetail(selected);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  const limits = detail?.risk_limits ?? {};

  return (
    <div className="mx-auto max-w-6xl px-4 py-10 space-y-6">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">Deployments</h1>
        <p className="text-sm text-zinc-600">Equity, drawdown, open positions and capital for each paper or live deployment.</p>
      </header>

      {error && <div className="rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-900">{error}</div>}
      {notice && <div className="rounded-lg border border-sky-200 bg-sky-50 px-4 py-3 text-sm text-sky-900">{notice}</div>}

      <section className="rounded-xl border bg-white p-5 shadow-sm">
        <table className="min-w-full text-sm">
          <thead>
            <tr className="border-b text-left">
              <th className="py-2 pr-4">Deployment</th>
              <th className="py-2 pr-4">Mode</th>
              <th className="py-2 pr-4">Status</th>
              <th className="py-2 pr-4">Crew</th>
              <th className="py-2 pr-4">Approved</th>
            </tr>
          </thead>
          <tbody>
            {deployments.map((d) => (
              <tr
                key={d.id}
                className={`cursor-pointer border-b hover:bg-zinc-50 ${selected === d.id ? "bg-indigo-50" : ""}`}
                onClick={() => setSelected(d.id)}
              >
                <td className="py-2 pr-4 font-mono text-xs">{d.id.slice(0, 8)}</td>
                <td className="py-2 pr-4">{d.mode}</td>
                <td className="py-2 pr-4">
                  <span className={`rounded-full px-2 py-0.5 text-xs ${STATUS_CLASS[d.status] ?? ""}`}>{d.status}</span>
                </td>
                <td className={`py-2 pr-4 text-xs ${d.status === "started" && !d.crew_running ? "font-medium text-rose-700" : "text-zinc-600"}`}>
                  {crewLabel(d)}
                </td>
                <td className="py-2 pr-4">{new Date(d.created_at).toLocaleString()}</td>
              </tr>
            ))}
            {deployments.length === 0 && (
              <tr>
                <td className="py-3 text-zinc-500" colSpan={5}>
                  No deployments yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </section>

      {detail && equity && (
        <>
          <section className="rounded-xl border bg-white p-5 shadow-sm space-y-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <h2 className="font-medium">
                {detail.deployed_strategy?.name ?? detail.strategy_id} v{detail.deployed_strategy?.version} · {detail.mode}
              </h2>
              <span className={`rounded-full px-3 py-1 text-sm ${STATUS_CLASS[detail.status] ?? ""}`}>{detail.status}</span>
            </div>
            <div className="flex flex-wrap items-end gap-2">
              <input className="rounded-md border px-3 py-2 text-sm" placeholder="Your name" value={actor} onChange={(e) => setActorName(e.target.value)} />
              <input className="rounded-md border px-3 py-2 text-sm" placeholder="Reason" value={reason} onChange={(e) => setReason(e.target.value)} />
              {["start", "pause", "stop"].map((action) => (
                <button
                  key={action}
                  className="rounded-md border px-3 py-2 text-sm capitalize hover:bg-zinc-50"
                  onClick={() => act(`/api/live/deployments/${detail.id}/${action}`, { reason: reason || null })}
                >
                  {action}
                </button>
              ))}
              <button
                className="rounded-md border border-rose-300 bg-rose-50 px-3 py-2 text-sm text-rose-900 hover:bg-rose-100"
                onClick={() => {
                  if (window.confirm("Stop this deployment and close every open position now (needs an API token)?")) {
                    act(`/api/live/deployments/${detail.id}/stop`, { reason: reason || "kill switch", flatten: true });
                  }
                }}
              >
                Stop and flatten
              </button>
            </div>
            <p className="text-xs text-zinc-500">Crew: {crewLabel(detail)}</p>
            <div className="grid gap-2 sm:grid-cols-3 lg:grid-cols-6">
              <Stat label="Capital in force" value={inr(equity.capital_inr)} />
              <Stat label="Equity" value={inr(equity.equity_inr)} />
              <Stat label="Realized" value={inr(equity.realized_inr)} />
              <Stat label="Open P&L" value={inr(equity.unrealized_inr)} />
              <Stat label="Drawdown" value={`${inr(equity.drawdown_inr)} (${pct(equity.drawdown_pct)})`} />
              <Stat label="Max drawdown" value={pct(equity.max_drawdown_pct)} />
            </div>
            <EquityChart
              series={[{ label: "Equity", color: "#4f46e5", points: equity.curve.map((p) => ({ x: p.at.slice(0, 10), y: p.equity_inr })) }]}
            />
          </section>

          <section className="grid gap-6 lg:grid-cols-2">
            <div className="rounded-xl border bg-white p-5 shadow-sm space-y-3">
              <h3 className="font-medium">Open positions</h3>
              <table className="min-w-full text-sm">
                <thead>
                  <tr className="border-b text-left">
                    <th className="py-1 pr-3">Opened</th>
                    <th className="py-1 pr-3">Qty</th>
                    <th className="py-1 pr-3">Open P&L</th>
                    <th className="py-1 pr-3">Peak</th>
                    <th className="py-1 pr-3">DTE</th>
                  </tr>
                </thead>
                <tbody>
                  {positions.map((p) => (
                    <tr key={p.open_intent_id} className="border-b">
                      <td className="py-1 pr-3">{new Date(p.opened_at).toLocaleString()}</td>
                      <td className="py-1 pr-3">{p.legs?.[0]?.quantity ?? "—"}</td>
                      <td className="py-1 pr-3">{p.pnl === null ? "not marked yet" : inr(p.pnl)}</td>
                      <td className="py-1 pr-3">{p.peak_pct === null || p.peak_pct === undefined ? "—" : pct(p.peak_pct, 1)}</td>
                      <td className="py-1 pr-3">{p.dte ?? "—"}</td>
                    </tr>
                  ))}
                  {positions.length === 0 && (
                    <tr>
                      <td className="py-2 text-zinc-500" colSpan={5}>
                        No open positions.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>

            <div className="rounded-xl border bg-white p-5 shadow-sm space-y-3">
              <h3 className="font-medium">Drawdown ladder · level {drawdown?.level ?? 0}</h3>
              <ul className="text-sm space-y-1">
                {drawdown?.ladder.map((l, i) => (
                  <li key={i} className={i < (drawdown?.level ?? 0) ? "font-medium text-amber-800" : ""}>
                    {l.at_pct}% → {l.action.replaceAll("_", " ")}
                    {l.size_factor ? ` ×${l.size_factor}` : ""}
                    {l.close_open_positions ? " and close positions" : ""}
                  </li>
                ))}
              </ul>
              {drawdown && drawdown.events.length > 0 && (
                <ul className="text-xs text-zinc-600 space-y-0.5">
                  {drawdown.events.map((e) => (
                    <li key={e.id}>
                      {new Date(e.created_at).toLocaleString()}: {e.detail}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </section>

          {(odds || oddsError) && (
            <section className="rounded-xl border bg-white p-5 shadow-sm space-y-3">
              <h3 className="font-medium">Odds gate</h3>
              {oddsError && <p className="text-sm text-rose-800">Could not load the odds: {oddsError}</p>}
              {odds && (
                <>
                  <p
                    className={`rounded-md px-3 py-2 text-sm ${odds.entries_open ? "bg-emerald-50 text-emerald-900" : "bg-amber-50 text-amber-900"}`}
                  >
                    {odds.entries_open ? "Entries allowed" : "Entries blocked"} at the latest VIX
                    {odds.today.vix !== null ? ` (${odds.today.vix.toFixed(2)} on ${odds.vix_date})` : ""}: {odds.summary}.
                  </p>
                  <OddsTable bands={odds.bands} current={odds.today.band} />
                  <p className="text-xs text-zinc-500">
                    Needs at least {odds.gate.min_trades} past trades in the band and an average above {inr(odds.gate.min_expectancy_inr)} per lot
                    after costs. Past trades come from a rules-only backtest of this strategy ({odds.history_trades} trades
                    {odds.gate.lookback_years ? `, last ${odds.gate.lookback_years} years counted` : ""}), counted only once closed. The crew
                    checks the band of the live VIX at each entry.
                  </p>
                </>
              )}
            </section>
          )}

          {money && (money.floor || money.withdrawal) && (
            <section className="grid gap-6 lg:grid-cols-2">
              {money.floor && (
                <div className="rounded-xl border bg-white p-5 shadow-sm space-y-3">
                  <h3 className="font-medium">Capital floor · {inr(money.floor.floor_in_force_inr)}</h3>
                  <div className="grid gap-2 sm:grid-cols-2">
                    <Stat label={`Equity (${money.floor.basis})`} value={inr(money.floor.equity_inr)} />
                    <Stat label="Buffer left" value={inr(money.floor.buffer_inr)} />
                  </div>
                  <ul className="text-sm space-y-1">
                    <li>Starting floor {inr(money.floor.floor_inr)}</li>
                    {money.floor.raise_once && (
                      <li className={money.floor.raised ? "font-medium text-emerald-800" : ""}>
                        Raised once to {inr(money.floor.raise_once.to_inr)} when equity reaches {inr(money.floor.raise_once.at_equity_inr)}
                        {money.floor.raised ? " (raised)" : ""}
                      </li>
                    )}
                    <li>
                      {money.floor.resume_after_days
                        ? `After a floor pause: a ${money.floor.resume_after_days}-trading-day break, then it starts again by itself if equity is above the floor`
                        : "After a floor pause: only a person restarts it"}
                    </li>
                  </ul>
                  {money.floor.on_break && (
                    <p className="rounded-md bg-amber-50 px-3 py-2 text-sm text-amber-900">
                      On a break:{" "}
                      {money.floor.break_days_left
                        ? `${money.floor.break_days_left} trading day(s) left.`
                        : "over; deposit enough to bring equity above the floor and it starts again."}
                    </p>
                  )}
                </div>
              )}
              {money.withdrawal && (
                <div className="rounded-xl border bg-white p-5 shadow-sm space-y-3">
                  <h3 className="font-medium">Withdrawal rule</h3>
                  <p className="text-sm">
                    First {inr(money.withdrawal.rule.first_amount_inr)} once realized equity reaches {inr(money.withdrawal.rule.first_target_inr)}
                    {money.withdrawal.rule.then_share_pct ? `, then ${money.withdrawal.rule.then_share_pct}% of each rise after a withdrawal` : ""}.
                  </p>
                  <div className="grid gap-2 sm:grid-cols-3">
                    <Stat label="Due now" value={inr(money.withdrawal.due_inr)} />
                    <Stat label="Withdrawn so far" value={inr(money.withdrawal.withdrawn_inr)} />
                    <Stat label="Realized equity" value={inr(money.withdrawal.realized_equity_inr)} />
                  </div>
                  {money.withdrawal.due_inr > 0 && (
                    <button
                      className="rounded-md border px-3 py-1.5 text-sm hover:bg-zinc-50"
                      onClick={() => {
                        setCapitalKind("withdrawal");
                        setCapitalAmount(String(money.withdrawal?.due_inr ?? ""));
                        setCapitalNote("withdrawal rule");
                      }}
                    >
                      Fill in a {inr(money.withdrawal.due_inr)} withdrawal below
                    </button>
                  )}
                  <p className="text-xs text-zinc-500">Nothing moves money by itself: take it out at the broker, then record it under Capital.</p>
                </div>
              )}
            </section>
          )}

          <section className="grid gap-6 lg:grid-cols-2">
            <div className="rounded-xl border bg-white p-5 shadow-sm space-y-3">
              <h3 className="font-medium">Capital · {inr(capital?.capital_in_force_inr)} in force</h3>
              <div className="flex flex-wrap gap-2">
                <select className="rounded-md border px-2 py-1.5 text-sm" value={capitalKind} onChange={(e) => setCapitalKind(e.target.value)}>
                  <option value="deposit">Deposit</option>
                  <option value="withdrawal">Withdrawal</option>
                </select>
                <input className="w-32 rounded-md border px-2 py-1.5 text-sm" placeholder="Amount ₹" inputMode="decimal" value={capitalAmount} onChange={(e) => setCapitalAmount(e.target.value)} />
                <input className="flex-1 rounded-md border px-2 py-1.5 text-sm" placeholder="Note" value={capitalNote} onChange={(e) => setCapitalNote(e.target.value)} />
                <button
                  className="rounded-md bg-zinc-900 px-3 py-1.5 text-sm text-white disabled:opacity-50"
                  disabled={!capitalAmount || !actor.trim()}
                  onClick={() =>
                    act(`/api/live/deployments/${detail.id}/capital-events`, {
                      kind: capitalKind,
                      amount_inr: Number(capitalAmount),
                      note: capitalNote || null,
                    }).then(() => setCapitalAmount(""))
                  }
                >
                  Record
                </button>
              </div>
              <p className="text-xs text-zinc-500">Deposits and withdrawals move equity and its peak together, so they never count as gains or drawdown.</p>
              <ul className="text-sm space-y-1">
                {capital?.capital_events.map((e) => (
                  <li key={e.id}>
                    {new Date(e.created_at).toLocaleDateString()} · {e.kind} {inr(e.amount_inr)} by {e.actor}
                    {e.note ? ` (${e.note})` : ""}
                  </li>
                ))}
              </ul>
            </div>

            <div className="rounded-xl border bg-white p-5 shadow-sm space-y-3">
              <h3 className="font-medium">Frozen risk limits and policy</h3>
              <pre className="overflow-x-auto rounded bg-zinc-50 p-3 text-xs">{JSON.stringify(limits, null, 2)}</pre>
            </div>
          </section>
        </>
      )}
    </div>
  );
}
