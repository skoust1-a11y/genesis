"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { api, inr } from "@/lib/api";

type Account = { capital_inr: string; accounting: { equity_inr: string | null }; net_pnl_inr: string; completed: number };
type ComparisonAccount = { label: string; strategy_version: string; capital_inr: string; equity_inr: string | null; closed_net_pnl_inr: string; entries: number; closed: number; held: number; unresolved: number; rejections?: { reason: string; count: number }[] };
type Operations = {
  runner: string;
  heartbeat_age_seconds?: number;
  status?: { updated_at: string; stream?: string; last_stream_tick?: string; news?: { headlines: number; updated_at?: string }; last_tick?: Record<string, unknown>; blockers?: string[]; session_evidence?: { state: string; market_day: string }; experiment?: { run_id: string; started_at: string; accounts: Record<string, ComparisonAccount> } };
  reconciliation?: { total: number; resolved: number; unresolved: number; note: string };
  strategy_readiness?: { strategies: { name: string; state: string; reasons: string[] }[]; active_families: string[]; paper_timeframes: string[] };
};

export default function PaperForwardPage() {
  const [accounts, setAccounts] = useState<Record<string, Account>>({});
  const [ops, setOps] = useState<Operations | null>(null);
  const [error, setError] = useState("");
  const load = useCallback(async () => {
    try {
      const [stats, operations] = await Promise.all([
        api<{ accounts: Record<string, Account> }>("/api/live/crew/paper/stats"),
        api<Operations>("/api/live/crew/paper/operations"),
      ]);
      setAccounts(stats.accounts); setOps(operations); setError("");
    } catch (e) { setError(e instanceof Error ? e.message : "Paper journal unavailable"); }
  }, []);
  useEffect(() => { void load(); const timer = setInterval(() => void load(), 30000); return () => clearInterval(timer); }, [load]);

  return <main className="mx-auto max-w-7xl space-y-6 px-4 py-8">
    <div><h1 className="text-2xl font-bold">Paper Forward</h1><p className="mt-1 text-sm text-zinc-600">₹50,000 per virtual account · Telegram notifications · No real broker orders</p></div>
    {error && <div role="alert" className="rounded border border-rose-300 bg-rose-50 p-4">{error} <Link className="underline" href="/login?next=/paper-forward">Log in</Link></div>}
    {ops && <section className="rounded-xl border bg-white p-4 space-y-2">
      <h2 className="font-semibold">Runner: {ops.runner.replaceAll("_", " ")}</h2>
      <p className="text-sm">Stream: {ops.status?.stream ?? "Waiting"} · Headlines: {ops.status?.news?.headlines ?? 0} · Heartbeat age: {ops.heartbeat_age_seconds ?? "—"} seconds</p>
      <p className="text-sm">A successful setup check is separate from a verified market-hours paper fill. Simulated fills require fresh timestamped quotes and delta evidence.</p>
      <p className="text-sm">Session evidence: {ops.status?.session_evidence?.market_day ?? ""} {ops.status?.session_evidence?.state.replaceAll("_", " ") ?? "Awaiting market session"}</p>
      {(ops.status?.blockers ?? []).map(x => <p key={x} className="text-sm text-amber-900">{x}</p>)}
      {ops.status?.last_tick && <details className="text-xs"><summary>Latest scan result</summary><pre className="overflow-auto mt-2">{JSON.stringify(ops.status.last_tick, null, 2)}</pre></details>}
    </section>}
    {ops?.status?.experiment ? <section className="overflow-x-auto rounded-xl border bg-white p-4 space-y-3">
      <h2 className="font-semibold">Fresh side-by-side paper test</h2>
      <p className="text-sm text-zinc-600">Run {ops.status.experiment.run_id} · Started {new Date(ops.status.experiment.started_at).toLocaleString("en-IN", { timeZone: "Asia/Kolkata" })} IST. Each account starts independently at ₹50,000. Earlier history is preserved.</p>
      <p className="text-sm">Baseline: existing 5-minute paper rules. Candidate: experimental ±10-point zones from completed 5-minute bars, with completed 1-minute rejection and breakout confirmation. No real orders.</p>
      <table className="w-full text-left text-sm"><thead><tr><th>Account</th><th>Starting capital</th><th>Equity</th><th>Closed net P&amp;L</th><th>Entries / closed</th><th>Held / unresolved</th></tr></thead>
        <tbody>{Object.entries(ops.status.experiment.accounts).map(([id, a]) => <tr key={id} className="border-t"><td className="py-3">{a.label}<span className="block text-xs text-zinc-500">{a.strategy_version}</span></td><td>{inr(Number(a.capital_inr))}</td><td>{a.equity_inr === null ? "Unpriced holdings" : inr(Number(a.equity_inr))}</td><td>{inr(Number(a.closed_net_pnl_inr))}</td><td>{a.entries} / {a.closed}</td><td>{a.held} / {a.unresolved}</td></tr>)}</tbody></table>
      <p className="text-sm text-zinc-600">Unpriced exits retain exposure and cannot silently clear on restart. The baseline’s internal family accounts are diagnostic and are not added to this comparison.</p>
      {Object.values(ops.status.experiment.accounts).filter(a => a.rejections?.length).map(a => <details key={a.label} className="text-sm"><summary>{a.label} rejection reasons</summary><ul className="mt-2 list-disc pl-5">{a.rejections?.map(r => <li key={r.reason}>{r.reason}: {r.count}</li>)}</ul></details>)}
    </section> : <section className="overflow-x-auto rounded-xl border bg-white p-4">
      <h2 className="font-semibold mb-3">Paper accounts</h2>
      <p className="text-sm text-zinc-600 mb-3">Independent accounts and the shared portfolio are compared separately. Their balances are never added together.</p>
      <table className="w-full text-left text-sm"><thead><tr><th>Account</th><th>Starting capital</th><th>Equity</th><th>Closed net P&amp;L</th><th>Closed trades</th></tr></thead>
        <tbody>{Object.entries(accounts).map(([id, a]) => <tr key={id} className="border-t"><td className="py-3">{id}</td><td>{inr(Number(a.capital_inr))}</td><td>{a.accounting.equity_inr === null ? "Unpriced" : inr(Number(a.accounting.equity_inr))}</td><td>{inr(Number(a.net_pnl_inr))}</td><td>{a.completed}</td></tr>)}</tbody></table>
    </section>}
    {ops?.strategy_readiness && <section className="rounded-xl border bg-white p-4 space-y-3"><h2 className="font-semibold">Strategy coverage</h2>
      <p className="text-sm">Paper entry timeframes: {ops.strategy_readiness.paper_timeframes.join(", ")}. Saved strategy documents require their own complete rules and validation; restoring them does not start them.</p>
      <p className="text-sm">Registered scanner families: {ops.strategy_readiness.active_families.join(", ")}</p>
      {ops.strategy_readiness.strategies.map(s => <div key={s.name} className="border-t pt-2 text-sm"><strong>{s.name}</strong> · {s.state}<p className="text-zinc-600">{s.reasons.join("; ")}</p></div>)}
    </section>}
    {ops?.reconciliation && <section className="rounded-xl border bg-white p-4"><h2 className="font-semibold">Historical OPEN reconciliation</h2><p className="mt-2 text-sm">{ops.reconciliation.total} records reviewed · {ops.reconciliation.resolved} resolved by matching evidence · {ops.reconciliation.unresolved} unresolved</p><p className="mt-2 text-sm text-zinc-600">{ops.reconciliation.note}</p></section>}
    <div className="flex gap-4 text-sm"><Link href="/crew-research" className="underline">Research journal</Link><Link href="/forward-tested" className="underline">Legacy history</Link></div>
  </main>;
}
