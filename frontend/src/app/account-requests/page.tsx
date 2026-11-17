"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";

type SignupRequest = { username: string; status: string; created_at: string; decided_by: string | null };

export default function AccountRequests() {
  const [requests, setRequests] = useState<SignupRequest[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const load = useCallback(async () => {
    try { const d = await api<{ requests: SignupRequest[] }>("/api/auth/signup-requests"); setRequests(d.requests); setError(""); }
    catch (e) { setError(e instanceof Error ? e.message : "Unable to load account requests"); }
  }, []);
  useEffect(() => { void load(); }, [load]);
  async function review(username: string, decision: "approve" | "reject") {
    setBusy(username); setError("");
    try { await api(`/api/auth/signup-requests/${encodeURIComponent(username)}`, { body: { decision } }); await load(); }
    catch (e) { setError(e instanceof Error ? e.message : "Unable to review request"); }
    finally { setBusy(null); }
  }
  return <main className="mx-auto max-w-4xl space-y-5 px-4 py-8">
    <div><h1 className="text-2xl font-bold">Account requests</h1><p className="mt-1 text-sm text-zinc-600">Approving an account grants access to this shared trading workspace. Only configured owners can review requests.</p></div>
    {error && <p role="alert" className="rounded border border-red-300 bg-red-50 p-3">{error}</p>}
    {!error && requests.length === 0 && <p className="rounded border bg-white p-4">No signup requests yet.</p>}
    {requests.map(r => <section key={r.username} className="flex flex-wrap items-center justify-between gap-4 rounded-xl border bg-white p-4">
      <div><h2 className="font-semibold">{r.username}</h2><p className="text-sm text-zinc-600">{r.status} · Requested {new Date(r.created_at).toLocaleString()}{r.decided_by ? ` · Reviewed by ${r.decided_by}` : ""}</p></div>
      {r.status === "pending" && <div className="flex gap-2"><button disabled={busy !== null} onClick={() => void review(r.username, "approve")} className="rounded bg-emerald-600 px-4 py-2 text-sm text-white disabled:opacity-50">{busy === r.username ? "Saving…" : "Approve"}</button><button disabled={busy !== null} onClick={() => void review(r.username, "reject")} className="rounded border px-4 py-2 text-sm disabled:opacity-50">Reject</button></div>}
    </section>)}
  </main>;
}
