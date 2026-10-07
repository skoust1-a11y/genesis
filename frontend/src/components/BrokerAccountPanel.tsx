"use client";

import { useEffect, useState } from "react";

import { api, type Broker, type BrokerAccount, type CredentialFieldStatus } from "@/lib/api";

type Props = {
  accounts: BrokerAccount[];
  accountId: string;
  onAccounts: (accounts: BrokerAccount[]) => void;
  onSelect: (id: string) => void;
  onError: (message: string) => void;
};

function sourceLabel(f: CredentialFieldStatus): string {
  if (f.source === "db") return `saved · ${f.fingerprint}`;
  if (f.source === "env") return "from .env";
  return f.required ? "missing" : "not set (optional)";
}

/** Pick or create a broker account and manage its credentials (stored encrypted; never shown again). */
export function BrokerAccountPanel({ accounts, accountId, onAccounts, onSelect, onError }: Props) {
  const [brokers, setBrokers] = useState<Broker[]>([]);
  const [broker, setBroker] = useState("paper");
  const [mode, setMode] = useState("paper");
  const [ref, setRef] = useState("");
  const [algoId, setAlgoId] = useState("");
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<{ brokers: Broker[] }>("/api/live/brokers")
      .then((d) => setBrokers(d.brokers))
      .catch((e) => onError(e.message));
  }, [onError]);

  const spec = brokers.find((b) => b.name === broker);
  const account = accounts.find((a) => a.id === accountId);

  async function guarded(fn: () => Promise<void>) {
    setBusy(true);
    try {
      await fn();
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const create = () =>
    guarded(async () => {
      const created = await api<BrokerAccount>("/api/live/broker-accounts", {
        body: { broker, mode, account_ref_masked: ref.trim() || `${broker.toUpperCase()}-1`, default_algo_id: algoId.trim() || null },
      });
      onAccounts([...accounts, created]);
      onSelect(created.id);
      setRef("");
    });

  const replaceAccount = (fields: CredentialFieldStatus[], configured: boolean) =>
    onAccounts(accounts.map((a) => (a.id === accountId ? { ...a, fields, configured } : a)));

  const saveCredentials = () =>
    guarded(async () => {
      const body = Object.fromEntries(Object.entries(values).filter(([, v]) => v.trim()));
      const out = await api<{ fields: CredentialFieldStatus[]; configured: boolean }>(
        `/api/live/broker-accounts/${accountId}/credentials`,
        { method: "PUT", body },
      );
      replaceAccount(out.fields, out.configured);
      setValues({});
    });

  const revokeCredentials = () =>
    guarded(async () => {
      const out = await api<{ fields: CredentialFieldStatus[]; configured: boolean }>(
        `/api/live/broker-accounts/${accountId}/credentials`,
        { method: "DELETE" },
      );
      replaceAccount(out.fields, out.configured);
    });

  return (
    <div className="space-y-4">
      <label className="block space-y-1">
        <span className="text-xs font-medium text-zinc-700">Broker account</span>
        <select className="w-full rounded-md border px-3 py-2" value={accountId} onChange={(e) => onSelect(e.target.value)}>
          {accounts.map((a) => (
            <option key={a.id} value={a.id}>
              {a.broker} {a.account_ref_masked} ({a.mode}){a.configured ? "" : " — not configured"}
            </option>
          ))}
        </select>
      </label>
      {account && !account.configured && (
        <span className="inline-block rounded bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-900">
          not configured{account.registered ? "" : ": broker is not registered"}
        </span>
      )}

      {account && account.fields.length > 0 && (
        <div className="rounded-lg border p-3 space-y-2">
          <p className="text-xs font-medium text-zinc-700">
            Credentials for {account.broker} {account.account_ref_masked} (saved values are encrypted and never shown)
          </p>
          {account.fields.map((f) => (
            <label key={f.name} className="grid gap-2 sm:grid-cols-[12rem_1fr_10rem] items-center text-sm">
              <span>{f.label}</span>
              <input
                className="rounded-md border px-3 py-1.5"
                type={f.secret ? "password" : "text"}
                autoComplete="off"
                placeholder={f.source ? "leave empty to keep" : ""}
                value={values[f.name] ?? ""}
                onChange={(e) => setValues({ ...values, [f.name]: e.target.value })}
              />
              <span className={`text-xs ${f.source ? "text-emerald-700" : f.required ? "text-rose-700" : "text-zinc-500"}`}>
                {sourceLabel(f)}
              </span>
            </label>
          ))}
          <div className="flex gap-2">
            <button
              type="button"
              className="rounded-md border px-3 py-1.5 text-sm disabled:opacity-50"
              disabled={busy || !Object.values(values).some((v) => v.trim())}
              onClick={saveCredentials}
            >
              Save credentials
            </button>
            <button
              type="button"
              className="rounded-md border px-3 py-1.5 text-sm disabled:opacity-50"
              disabled={busy || !account.fields.some((f) => f.source === "db")}
              onClick={revokeCredentials}
            >
              Remove saved values
            </button>
          </div>
        </div>
      )}

      <details className="rounded-lg border p-3">
        <summary className="cursor-pointer text-sm font-medium">New broker account</summary>
        <div className="mt-3 grid gap-3 sm:grid-cols-2">
          <label className="space-y-1">
            <span className="text-xs font-medium text-zinc-700">Broker</span>
            <select
              className="w-full rounded-md border px-3 py-2"
              value={broker}
              onChange={(e) => {
                setBroker(e.target.value);
                const next = brokers.find((b) => b.name === e.target.value);
                if (next && !next.modes.includes(mode)) setMode(next.modes[0]);
              }}
            >
              {brokers.map((b) => (
                <option key={b.name} value={b.name}>
                  {b.label}
                </option>
              ))}
            </select>
          </label>
          <label className="space-y-1">
            <span className="text-xs font-medium text-zinc-700">Mode</span>
            <select className="w-full rounded-md border px-3 py-2" value={mode} onChange={(e) => setMode(e.target.value)}>
              {(spec?.modes ?? ["paper"]).map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </label>
          <label className="space-y-1">
            <span className="text-xs font-medium text-zinc-700">Account reference (masked, e.g. XX1234)</span>
            <input className="w-full rounded-md border px-3 py-2" value={ref} onChange={(e) => setRef(e.target.value)} />
          </label>
          <label className="space-y-1">
            <span className="text-xs font-medium text-zinc-700">Default SEBI algo id (needed for live)</span>
            <input className="w-full rounded-md border px-3 py-2" value={algoId} onChange={(e) => setAlgoId(e.target.value)} />
          </label>
        </div>
        {spec && (
          <p className="mt-2 text-xs text-zinc-500">
            {spec.description} {spec.has_quotes ? "Provides live quotes (broker feed)." : "No quotes: replay feed only."}
          </p>
        )}
        <button
          type="button"
          className="mt-3 rounded-md border px-3 py-2 text-sm disabled:opacity-50"
          disabled={busy || !spec}
          onClick={create}
        >
          Create {mode} account
        </button>
      </details>
    </div>
  );
}
