"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import { BrokerAccountPanel } from "@/components/BrokerAccountPanel";
import { EquityChart } from "@/components/EquityChart";
import { PolicyFormFields } from "@/components/PolicyFormFields";
import { api, type BrokerAccount, getActor, getToken, inr, pct, setActor, setToken } from "@/lib/api";
import { DEFAULT_FORM, type PolicyForm, toRiskLimits } from "@/lib/policy";

type Strategy = { id: string; name: string; current_version: number; archived_at: string | null };
type Preview = {
  name: string;
  version: number;
  rules: { exit?: { kind: string; value: number }[]; risk?: { max_lots: number } } | null;
  rules_sha256: string | null;
  verdict_label: string | null;
  needs_verdict_acknowledgement: boolean;
  blockers: string[];
  warnings: string[];
};
type RunSummary = {
  net_pnl: number;
  return_pct: number;
  total_costs: number;
  trades: number;
  win_rate: number;
  max_drawdown_mtm: number;
  max_drawdown_pct_of_capital: number;
  max_drawdown_days: number;
  exit_reasons: Record<string, number>;
  skipped_signals: Record<string, number>;
  management: { partial_closes?: number; paused_on?: string | null; drawdown_events?: unknown[] };
  equity_curve: { date: string; equity: number }[];
};
type Backtest = {
  data_source: string;
  window: [string | null, string | null];
  rules_only: RunSummary;
  with_policy: RunSummary;
  assumptions: string[];
};

function counts(obj: Record<string, number>): string {
  const entries = Object.entries(obj);
  return entries.length ? entries.map(([k, v]) => `${k} ${v}`).join(", ") : "—";
}

export default function DeployPage() {
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [strategyId, setStrategyId] = useState("");
  const [version, setVersion] = useState<number | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [form, setForm] = useState<PolicyForm>(DEFAULT_FORM);
  const [backtest, setBacktest] = useState<Backtest | null>(null);
  const [accounts, setAccounts] = useState<BrokerAccount[]>([]);
  const [accountId, setAccountId] = useState("");
  const [actor, setActorName] = useState("");
  const [token, setTokenValue] = useState("");
  const [algoId, setAlgoId] = useState("");
  const [acknowledge, setAcknowledge] = useState(false);
  const [approved, setApproved] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const showError = useCallback((message: string) => setError(message), []);

  useEffect(() => {
    setActorName(getActor());
    setTokenValue(getToken());
    api<{ strategies: Strategy[] }>("/api/strategies")
      .then((d) => setStrategies(d.strategies.filter((s) => !s.archived_at)))
      .catch((e) => setError(e.message));
    api<{ broker_accounts: BrokerAccount[] }>("/api/live/broker-accounts")
      .then((d) => {
        setAccounts(d.broker_accounts);
        if (d.broker_accounts.length) setAccountId(d.broker_accounts[0].id);
      })
      .catch((e) => setError(e.message));
  }, []);

  const account = accounts.find((a) => a.id === accountId);
  const mode = account?.mode;

  useEffect(() => {
    if (!strategyId || version === null) return;
    setPreview(null);
    setBacktest(null);
    setApproved(null);
    const live = mode === "live" ? `&mode=live&broker_account_id=${accountId}` : "";
    api<Preview>(`/api/live/approvals/preview?strategy_id=${strategyId}&version=${version}${live}`)
      .then(setPreview)
      .catch((e) => setError(e.message));
  }, [strategyId, version, mode, accountId, account?.configured]);

  const profitTarget = useMemo(
    () => preview?.rules?.exit?.find((e) => e.kind === "profit_target_pct_of_credit")?.value ?? null,
    [preview],
  );
  const riskLimits = useMemo(() => toRiskLimits(form), [form]);

  async function run<T>(label: string, fn: () => Promise<T>): Promise<T | undefined> {
    setBusy(label);
    setError(null);
    try {
      return await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  async function runBacktest() {
    const out = await run("backtest", () =>
      api<Backtest>("/api/live/approvals/backtest", { body: { strategy_id: strategyId, version, risk_limits: riskLimits } }),
    );
    if (out) setBacktest(out);
  }

  async function approve() {
    setActor(actor.trim());
    setToken(token.trim());
    const out = await run("approve", () =>
      api<{ deployment: { id: string } }>("/api/live/approvals", {
        body: {
          strategy_id: strategyId,
          version,
          mode: account?.mode,
          broker_account_id: accountId,
          confirm_rules_sha256: preview?.rules_sha256,
          risk_limits: riskLimits,
          sebi_algo_id: algoId.trim() || null,
          acknowledge_verdict: acknowledge,
        },
      }),
    );
    if (out) setApproved(out.deployment.id);
  }

  const rows: [string, (r: RunSummary) => string][] = [
    ["Net P&L", (r) => inr(r.net_pnl)],
    ["Return on capital", (r) => pct(r.return_pct)],
    ["Trades", (r) => String(r.trades)],
    ["Win rate", (r) => pct(r.win_rate * 100, 1)],
    ["Costs", (r) => inr(r.total_costs)],
    ["Max drawdown", (r) => `${inr(r.max_drawdown_mtm)} (${pct(r.max_drawdown_pct_of_capital)})`],
    ["Longest drawdown (days)", (r) => String(r.max_drawdown_days)],
    ["Partial closes", (r) => String(r.management.partial_closes ?? 0)],
    ["Paused on", (r) => r.management.paused_on ?? "—"],
    ["Exit reasons", (r) => counts(r.exit_reasons)],
    ["Signals not taken", (r) => counts(r.skipped_signals)],
  ];

  return (
    <div className="mx-auto max-w-6xl px-4 py-10 space-y-6">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">Deploy a strategy</h1>
        <p className="text-sm text-zinc-600 max-w-3xl">
          Set the trade management policy and drawdown ladder, backtest the strategy with and without them, then
          approve. Everything here is frozen with the deployment; a policy can only make trading more careful than
          the strategy&apos;s own rules.
        </p>
      </header>

      {error && <div className="rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-900">{error}</div>}

      <section className="rounded-xl border bg-white p-5 shadow-sm space-y-4">
        <h2 className="font-medium">1. Strategy version</h2>
        <div className="grid gap-3 sm:grid-cols-2">
          <select
            className="rounded-md border px-3 py-2"
            value={strategyId}
            onChange={(e) => {
              const s = strategies.find((x) => x.id === e.target.value);
              setStrategyId(e.target.value);
              setVersion(s ? s.current_version : null);
            }}
          >
            <option value="">Choose a strategy…</option>
            {strategies.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name} (v{s.current_version})
              </option>
            ))}
          </select>
          {strategyId && (
            <input
              className="rounded-md border px-3 py-2"
              type="number"
              min={1}
              value={version ?? ""}
              onChange={(e) => setVersion(Number(e.target.value))}
              aria-label="Version"
            />
          )}
        </div>
        {preview && (
          <div className="text-sm space-y-1">
            <p>
              <span className="font-medium">{preview.name}</span> v{preview.version} · latest verdict{" "}
              <span className="font-medium">{preview.verdict_label ?? "none"}</span> · profit target{" "}
              {profitTarget !== null ? `${profitTarget}% of credit` : "—"} · strategy max lots {preview.rules?.risk?.max_lots ?? "—"}
            </p>
            {preview.blockers.length > 0 && (
              <ul className="list-disc pl-5 text-rose-800">
                {preview.blockers.map((b) => (
                  <li key={b}>{b}</li>
                ))}
              </ul>
            )}
            {preview.warnings.map((w) => (
              <p key={w} className="text-xs text-zinc-500">
                {w}
              </p>
            ))}
          </div>
        )}
      </section>

      {preview && (
        <section className="rounded-xl border bg-white p-5 shadow-sm space-y-4">
          <h2 className="font-medium">2. Trade management policy</h2>
          <PolicyFormFields form={form} onChange={setForm} profitTargetPct={profitTarget} />
          <details className="text-xs">
            <summary className="cursor-pointer text-zinc-600">risk_limits sent to the API</summary>
            <pre className="mt-2 overflow-x-auto rounded bg-zinc-50 p-3">{JSON.stringify(riskLimits, null, 2)}</pre>
          </details>
          <button
            className="rounded-md bg-indigo-600 px-4 py-2 text-white disabled:opacity-50"
            disabled={busy !== null || !preview.rules}
            onClick={runBacktest}
          >
            {busy === "backtest" ? "Backtesting…" : "Backtest with this policy"}
          </button>
        </section>
      )}

      {backtest && (
        <section className="rounded-xl border bg-white p-5 shadow-sm space-y-4">
          <h2 className="font-medium">3. Rules only vs with this policy</h2>
          <p className="text-xs text-zinc-500">
            {backtest.window[0]} to {backtest.window[1]} · data: {backtest.data_source}
          </p>
          <div className="overflow-x-auto">
            <table className="min-w-full text-sm">
              <thead>
                <tr className="border-b text-left">
                  <th className="py-2 pr-4" />
                  <th className="py-2 pr-4">Rules only</th>
                  <th className="py-2 pr-4">With policy</th>
                </tr>
              </thead>
              <tbody>
                {rows.map(([label, fmt]) => (
                  <tr key={label} className="border-b align-top">
                    <td className="py-2 pr-4 text-zinc-600">{label}</td>
                    <td className="py-2 pr-4">{fmt(backtest.rules_only)}</td>
                    <td className="py-2 pr-4">{fmt(backtest.with_policy)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <EquityChart
            series={[
              { label: "Rules only", color: "#a1a1aa", points: backtest.rules_only.equity_curve.map((p) => ({ x: p.date, y: p.equity })) },
              { label: "With policy", color: "#4f46e5", points: backtest.with_policy.equity_curve.map((p) => ({ x: p.date, y: p.equity })) },
            ]}
          />
          <ul className="list-disc pl-5 text-xs text-zinc-500 space-y-0.5">
            {backtest.assumptions.map((a) => (
              <li key={a}>{a}</li>
            ))}
          </ul>
        </section>
      )}

      {preview && (
        <section className="rounded-xl border bg-white p-5 shadow-sm space-y-4">
          <h2 className="font-medium">4. Approve</h2>
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="space-y-1">
              <span className="text-xs font-medium text-zinc-700">API token (required for live; its name is the approver)</span>
              <input
                className="w-full rounded-md border px-3 py-2"
                type="password"
                autoComplete="off"
                value={token}
                onChange={(e) => {
                  setTokenValue(e.target.value);
                  setToken(e.target.value.trim());
                }}
              />
            </label>
            <label className="space-y-1">
              <span className="text-xs font-medium text-zinc-700">Your name (approver when no token is configured)</span>
              <input className="w-full rounded-md border px-3 py-2" value={actor} onChange={(e) => setActorName(e.target.value)} />
            </label>
          </div>
          <BrokerAccountPanel
            accounts={accounts}
            accountId={accountId}
            onAccounts={setAccounts}
            onSelect={setAccountId}
            onError={showError}
          />
          {mode === "live" && (
            <label className="block space-y-1">
              <span className="text-xs font-medium text-zinc-700">SEBI algo id (defaults to the account&apos;s)</span>
              <input className="w-full rounded-md border px-3 py-2" value={algoId} onChange={(e) => setAlgoId(e.target.value)} />
            </label>
          )}
          {preview.needs_verdict_acknowledgement && (
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={acknowledge} onChange={(e) => setAcknowledge(e.target.checked)} />
              The latest verdict is {preview.verdict_label}; approve anyway
            </label>
          )}
          <button
            className="rounded-md bg-zinc-900 px-4 py-2 text-white disabled:opacity-50"
            disabled={
              busy !== null ||
              !(actor.trim() || token.trim()) ||
              !accountId ||
              preview.blockers.length > 0 ||
              (mode === "live" && (!token.trim() || !account?.configured))
            }
            onClick={approve}
          >
            {busy === "approve" ? "Approving…" : `Approve for ${account?.mode ?? "…"} trading`}
          </button>
          {approved && (
            <p className="text-sm text-emerald-800">
              Approved as deployment <span className="font-mono">{approved}</span>. It trades only after you start it on
              the <Link className="underline" href="/deployments">deployments page</Link>.
            </p>
          )}
        </section>
      )}
    </div>
  );
}
