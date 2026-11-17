"use client";

import { useState } from "react";
import Link from "next/link";

import { api, setToken } from "@/lib/api";

export default function Login() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    setToken("");
    try {
      const res = await api<{ token: string }>("/api/auth/login", { body: { username, password } });
      setToken(res.token);
      const next = new URLSearchParams(window.location.search).get("next");
      const destination = new URL(next || "/", window.location.origin);
      window.location.assign(destination.origin === window.location.origin && destination.pathname !== "/login"
        ? destination.pathname + destination.search + destination.hash : "/");
    } catch (err) {
      setError(err instanceof Error ? err.message.replace(/ — log in or set an API token$/, "") : String(err));
      setBusy(false);
    }
  }

  return (
    <main className="mx-auto max-w-sm px-4 py-16">
      <form onSubmit={submit} className="space-y-4 rounded-xl border bg-white p-6 shadow-sm">
        <h1 className="text-lg font-semibold">Log in</h1>
        <label className="block space-y-1">
          <span className="text-xs font-medium text-zinc-700">Username</span>
          <input
            className="w-full rounded-md border px-3 py-2"
            autoComplete="username"
            required
            value={username}
            onChange={(e) => setUsername(e.target.value)}
          />
        </label>
        <label className="block space-y-1">
          <span className="text-xs font-medium text-zinc-700">Password</span>
          <input
            className="w-full rounded-md border px-3 py-2"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        {error && <p role="alert" className="text-sm text-red-700">{error}</p>}
        <button className="w-full rounded-md bg-emerald-600 px-4 py-2 text-white disabled:opacity-50" disabled={busy}>
          {busy ? "Logging in…" : "Log in"}
        </button>
        <p className="text-center text-sm text-zinc-600">Need an account? <Link href="/signup" className="font-medium text-emerald-700 underline">Sign up</Link></p>
      </form>
    </main>
  );
}
