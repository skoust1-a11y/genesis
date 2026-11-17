"use client";

import Link from "next/link";
import { useState } from "react";
import { api, setToken } from "@/lib/api";

export default function Signup() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [submitted, setSubmitted] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault(); setError("");
    if (password !== confirmation) { setError("Passwords do not match"); return; }
    setBusy(true); setToken("");
    try {
      await api("/api/auth/signup", { body: { username, password } });
      setPassword(""); setConfirmation(""); setSubmitted(true);
    } catch (e) { setError(e instanceof Error ? e.message : "Unable to submit signup request"); }
    finally { setBusy(false); }
  }

  return <main className="mx-auto max-w-sm px-4 py-16">
    {submitted ? <section className="space-y-4 rounded-xl border bg-white p-6 shadow-sm"><h1 className="text-lg font-semibold">Approval requested</h1><p className="text-sm text-zinc-600">An owner must approve your account before you can log in. Contact the owner to review your request.</p><Link href="/login" className="inline-block font-medium text-emerald-700 underline">Back to login</Link></section>
    : <form onSubmit={submit} className="space-y-4 rounded-xl border bg-white p-6 shadow-sm">
      <h1 className="text-lg font-semibold">Sign up</h1>
      <p className="text-sm text-zinc-600">Request access to the trading workspace. Account access requires owner approval.</p>
      <label className="block space-y-1"><span className="text-xs font-medium text-zinc-700">Username</span><input className="w-full rounded-md border px-3 py-2" autoComplete="username" required maxLength={64} pattern="[A-Za-z0-9_.@\-]+" title="Use letters, numbers, dots, underscores, @ or hyphens" value={username} onChange={e => setUsername(e.target.value)} /></label>
      <label className="block space-y-1"><span className="text-xs font-medium text-zinc-700">Password</span><input className="w-full rounded-md border px-3 py-2" type="password" autoComplete="new-password" required minLength={8} maxLength={1024} value={password} onChange={e => setPassword(e.target.value)} /><span className="text-xs text-zinc-500">At least 8 characters</span></label>
      <label className="block space-y-1"><span className="text-xs font-medium text-zinc-700">Confirm password</span><input className="w-full rounded-md border px-3 py-2" type="password" autoComplete="new-password" required value={confirmation} onChange={e => setConfirmation(e.target.value)} /></label>
      {error && <p role="alert" className="text-sm text-red-700">{error}</p>}
      <button disabled={busy} className="w-full rounded-md bg-emerald-600 px-4 py-2 text-white disabled:opacity-50">{busy ? "Submitting…" : "Request account"}</button>
      <p className="text-center text-sm text-zinc-600">Already have an account? <Link href="/login" className="text-emerald-700 underline">Log in</Link></p>
    </form>}
  </main>;
}
