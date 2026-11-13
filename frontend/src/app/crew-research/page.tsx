"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";

type Ratio = { numerator: number; denominator: number; ratio: number | null };

type Group = {
  strategy_id: string;
  strategy_version: string;
  execution_policy: string;
  population: Record<string, number | string | Record<string, number>>;
  exclusions: Record<string, number>;
  not_in_population: Record<string, number>;
  coverage: Record<string, Ratio>;
  results: {
    closed_eligible: number;
    wins: number;
    losses: number;
    flat: number;
    sum_points: number;
    sum_r: number;
    sufficient_sample: boolean;
    label: string;
    win_rate_pct: number | null;
    avg_r: number | null;
    avg_points: number | null;
  };
};

type Stats = {
  strategy_version: string;
  execution_policy: string;
  units: Record<string, string>;
  monetary_metrics: string;
  eligibility: string;
  applicable_signal_rule: string;
  min_sample: number;
  interpretation: string;
  as_of: string;
  consistency: { ok: boolean; count: number; findings: { kind: string; trade_id?: string }[] };
  statistics_withheld: boolean;
  groups: Group[];
};

type Listed = { count: number; items: Record<string, unknown>[] };

type Exceptions = {
  lease_holder: string | null;
  consistency: { ok: boolean; count: number };
  missing_model_entries: Listed;
  orphaned_trades: Listed;
  open_prior_day_trades: Listed;
  expired_unresolved_trades: Listed;
  quarantined_trades: Listed;
  unknown_publications: Listed;
};

type Trade = {
  trade_id: string;
  market_day: string;
  strategy_id: string;
  status: string;
  direction: string;
  entry_spot: number | null;
  opened_at: string;
  exit_reason: string | null;
  model_points: number | null;
  model_r: number | null;
};

const fmt = (v: number | null | undefined, digits = 2) => (v === null || v === undefined ? "—" : v.toFixed(digits));

export default function CrewResearchPage() {
  const [version, setVersion] = useState("scanner-v0.1");
  const [policy, setPolicy] = useState("research-exec-v1-monotonic-stop");
  const [stats, setStats] = useState<Stats | null>(null);
  const [exceptions, setExceptions] = useState<Exceptions | null>(null);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    setError(null);
    const q = new URLSearchParams({ strategy_version: version, execution_policy: policy });
    Promise.all([
      api<Stats>(`/api/live/crew/stats?${q}`),
      api<Exceptions>("/api/live/crew/exceptions"),
      api<{ items: Trade[] }>(`/api/live/crew/trades?${q}&limit=50`),
    ])
      .then(([s, e, t]) => {
        setStats(s);
        setExceptions(e);
        setTrades(t.items);
      })
      .catch((err) => {
        setStats(null);
        setExceptions(null);
        setTrades([]);
        setError(err instanceof Error ? err.message : "Failed to load the crew journal");
      });
  }, [version, policy]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <main className="mx-auto max-w-7xl space-y-6 px-4 py-8">
      <div className="border-b pb-4">
        <span className="inline-flex rounded-full bg-indigo-100 px-2.5 py-0.5 text-xs font-semibold text-indigo-800">
          Research only · read-only crew journal
        </span>
        <h1 className="mt-2 text-2xl font-bold tracking-tight">Crew Research</h1>
        <p className="mt-1 text-sm text-zinc-600">
          Hypothetical model trades on the underlying index, in index points and R. Not orders, fills or rupee
          returns, and separate from the legacy Forward Tested ledger.
        </p>
        <form
          className="mt-4 flex flex-wrap items-end gap-3 text-sm"
          onSubmit={(e) => {
            e.preventDefault();
            load();
          }}
        >
          <label className="flex flex-col gap-1">
            <span className="text-xs text-zinc-500">Strategy version</span>
            <input className="rounded border px-2 py-1" value={version} onChange={(e) => setVersion(e.target.value)} />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-xs text-zinc-500">Execution policy</span>
            <input className="w-72 rounded border px-2 py-1" value={policy} onChange={(e) => setPolicy(e.target.value)} />
          </label>
          <button className="rounded bg-indigo-600 px-3 py-1.5 font-medium text-white hover:bg-indigo-700">Load</button>
        </form>
      </div>

      {error && (
        <div role="alert" className="rounded-lg border border-rose-200 bg-rose-50 p-4 text-sm text-rose-800">
          <p className="font-semibold">Crew journal unavailable — no statistics shown</p>
          <p className="mt-1">{error}</p>
        </div>
      )}

      {stats && (
        <section className="space-y-3">
          <div
            className={`rounded-lg border p-3 text-sm ${stats.consistency.ok ? "border-emerald-200 bg-emerald-50 text-emerald-900" : "border-rose-200 bg-rose-50 text-rose-900"}`}
          >
            Journal consistency: {stats.consistency.ok ? "OK" : `${stats.consistency.count} finding(s) — statistics withheld`}
            {!stats.consistency.ok && (
              <ul className="mt-1 list-disc pl-5 text-xs">
                {stats.consistency.findings.map((f, i) => (
                  <li key={i}>
                    {f.kind} {f.trade_id ?? ""}
                  </li>
                ))}
              </ul>
            )}
          </div>
          <dl className="grid gap-1 text-xs text-zinc-600 sm:grid-cols-2">
            <div><dt className="inline font-semibold">Units: </dt><dd className="inline">{stats.units.points}; {stats.units.r}</dd></div>
            <div><dt className="inline font-semibold">Eligibility: </dt><dd className="inline">{stats.eligibility}</dd></div>
            <div><dt className="inline font-semibold">Applicable signals: </dt><dd className="inline">{stats.applicable_signal_rule}</dd></div>
            <div><dt className="inline font-semibold">Money: </dt><dd className="inline">{stats.monetary_metrics}</dd></div>
            <div className="sm:col-span-2"><dt className="inline font-semibold">Interpretation: </dt><dd className="inline">{stats.interpretation}</dd></div>
          </dl>
          {stats.groups.length === 0 && !stats.statistics_withheld && (
            <p className="text-sm text-zinc-500">No admissions or model trades for this version.</p>
          )}
          {stats.groups.map((g) => (
            <div key={g.strategy_id} className="rounded-xl border bg-white p-4 shadow-sm">
              <h2 className="font-semibold">
                {g.strategy_id} <span className="text-xs font-normal text-zinc-500">{g.strategy_version} · {g.execution_policy}</span>
              </h2>
              <div className="mt-3 grid gap-4 text-sm md:grid-cols-3">
                <table className="text-xs">
                  <caption className="mb-1 text-left font-semibold">Population</caption>
                  <tbody>
                    {Object.entries(g.population)
                      .filter(([, v]) => typeof v === "number")
                      .map(([k, v]) => (
                        <tr key={k}><td className="pr-3 text-zinc-500">{k}</td><td>{String(v)}</td></tr>
                      ))}
                  </tbody>
                </table>
                <table className="text-xs">
                  <caption className="mb-1 text-left font-semibold">Results (closed eligible)</caption>
                  <tbody>
                    <tr><td className="pr-3 text-zinc-500">label</td><td className="font-semibold">{g.results.label}</td></tr>
                    <tr><td className="pr-3 text-zinc-500">trades</td><td>{g.results.closed_eligible} ({g.results.wins}W/{g.results.losses}L/{g.results.flat}F)</td></tr>
                    <tr><td className="pr-3 text-zinc-500">sum points</td><td>{fmt(g.results.sum_points)}</td></tr>
                    <tr><td className="pr-3 text-zinc-500">sum R</td><td>{fmt(g.results.sum_r)}</td></tr>
                    <tr><td className="pr-3 text-zinc-500">win rate</td><td>{g.results.win_rate_pct === null ? `— (needs ${stats.min_sample})` : `${g.results.win_rate_pct}%`}</td></tr>
                    <tr><td className="pr-3 text-zinc-500">avg R</td><td>{fmt(g.results.avg_r, 3)}</td></tr>
                  </tbody>
                </table>
                <table className="text-xs">
                  <caption className="mb-1 text-left font-semibold">Exclusions and coverage</caption>
                  <tbody>
                    {Object.entries({ ...g.exclusions, ...g.not_in_population }).map(([k, v]) => (
                      <tr key={k}><td className="pr-3 text-zinc-500">{k}</td><td>{v}</td></tr>
                    ))}
                    {Object.entries(g.coverage).map(([k, c]) => (
                      <tr key={k}><td className="pr-3 text-zinc-500">{k}</td><td>{c.numerator}/{c.denominator}</td></tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ))}
        </section>
      )}

      {exceptions && (
        <section className="rounded-xl border bg-white p-4 text-sm shadow-sm">
          <h2 className="font-semibold">Unresolved exceptions</h2>
          <p className="text-xs text-zinc-500">Read-only. Nothing here repairs, adopts, resends or resolves.</p>
          <ul className="mt-2 grid gap-1 sm:grid-cols-3">
            <li>Lease holder: {exceptions.lease_holder ?? "none"}</li>
            <li>Missing model entries: {exceptions.missing_model_entries.count}</li>
            <li>Orphaned trades (today): {exceptions.orphaned_trades.count}</li>
            <li>Open from a prior day: {exceptions.open_prior_day_trades.count}</li>
            <li>Expired unresolved: {exceptions.expired_unresolved_trades.count}</li>
            <li>Quarantined (legacy): {exceptions.quarantined_trades.count}</li>
            <li>UNKNOWN publications: {exceptions.unknown_publications.count}</li>
          </ul>
        </section>
      )}

      {trades.length > 0 && (
        <section className="overflow-x-auto rounded-xl border bg-white shadow-sm">
          <table className="min-w-full text-xs">
            <caption className="p-3 text-left font-semibold">Latest model trades (50)</caption>
            <thead className="bg-zinc-50 text-left text-zinc-500">
              <tr>
                {["Trade", "Day", "Strategy", "Status", "Side", "Entry", "Exit reason", "Points", "R"].map((h) => (
                  <th key={h} className="px-3 py-2 font-medium">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {trades.map((t) => (
                <tr key={t.trade_id} className="border-t">
                  <td className="px-3 py-1.5 font-mono">{t.trade_id}</td>
                  <td className="px-3 py-1.5">{t.market_day}</td>
                  <td className="px-3 py-1.5">{t.strategy_id}</td>
                  <td className="px-3 py-1.5">{t.status}</td>
                  <td className="px-3 py-1.5">{t.direction}</td>
                  <td className="px-3 py-1.5">{fmt(t.entry_spot)}</td>
                  <td className="px-3 py-1.5">{t.exit_reason ?? "—"}</td>
                  <td className="px-3 py-1.5">{fmt(t.model_points)}</td>
                  <td className="px-3 py-1.5">{fmt(t.model_r)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </main>
  );
}
