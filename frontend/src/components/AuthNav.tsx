"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { api, getToken, setToken } from "@/lib/api";

type Me = { username: string | null; auth_enabled: boolean };

export function AuthNav() {
  const [me, setMe] = useState<Me | null>(null);

  useEffect(() => {
    api<Me>("/api/auth/me").then(setMe).catch(() => setMe(null));
  }, []);

  async function logout() {
    if (getToken()) await api("/api/auth/logout", { method: "POST" }).catch(() => undefined);
    setToken("");
    window.location.assign("/login");
  }

  if (!me?.auth_enabled) return null;
  if (!me.username) {
    return (
      <Link href="/login" className="text-zinc-600 hover:text-emerald-700">
        Log in
      </Link>
    );
  }
  return (
    <span className="flex items-center gap-2 text-zinc-600">
      {me.username}
      <button onClick={logout} className="rounded-md border px-2 py-1 text-xs hover:bg-zinc-100">
        Log out
      </button>
    </span>
  );
}
