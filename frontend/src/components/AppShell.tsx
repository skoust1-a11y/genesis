"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

import { AuthNav } from "@/components/AuthNav";
import { api, AuthError, getToken, setToken } from "@/lib/api";

const NAV = [
  { href: "/", label: "Dashboard" },
  { href: "/markets", label: "Markets" },
  { href: "/strategies", label: "Strategies & Analyzer" },
  { href: "/forward-tested", label: "Forward Tested" },
  { href: "/paper-forward", label: "Paper Forward" },
  { href: "/crew-research", label: "Crew Research" },
  { href: "/lab", label: "Strategy Lab" },
  { href: "/deploy", label: "Deploy" },
  { href: "/deployments", label: "Deployments" },
];

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const [session, setSession] = useState<{ path: string; error?: string }>({ path: "" });
  const [attempt, setAttempt] = useState(0);
  const loginPage = pathname === "/login" || pathname === "/signup";

  useEffect(() => {
    let active = true;
    const toLogin = () => {
      setToken("");
      if (!loginPage) window.location.replace(`/login?next=${encodeURIComponent(pathname + window.location.search)}`);
    };
    if (!getToken()) {
      toLogin();
      return;
    }
    api<{ username: string | null }>("/api/auth/me")
      .then((me) => {
        if (!active) return;
        if (!me.username) { toLogin(); return; }
        if (loginPage) { window.location.replace("/"); return; }
        setSession({ path: pathname });
      })
      .catch((error) => {
        if (!active) return;
        if (error instanceof AuthError) { toLogin(); return; }
        setSession({ path: pathname, error: "Unable to verify your session. Check that the backend is running, then retry." });
      });
    return () => { active = false; };
  }, [pathname, loginPage, attempt]);

  if (loginPage) return children;
  if (session.path !== pathname) return <main className="px-4 py-16 text-center text-sm text-zinc-600">Checking your session…</main>;
  if (session.error) return <main className="mx-auto max-w-md space-y-4 px-4 py-16"><p role="alert">{session.error}</p><button className="rounded border bg-white px-4 py-2" onClick={() => { setSession({ path: "" }); setAttempt((n) => n + 1); }}>Retry</button></main>;

  return <>
    <nav className="sticky top-0 z-10 border-b bg-white/90 backdrop-blur">
      <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-4 px-4 py-3 text-sm">
        <Link href="/" className="flex items-center gap-2 font-semibold"><span className="inline-block h-6 w-6 rounded-md bg-emerald-600" aria-hidden />Trading Strategy Lab</Link>
        {NAV.map((item) => <Link key={item.href} href={item.href} className="text-zinc-600 hover:text-emerald-700">{item.label}</Link>)}
        <span className="ml-auto rounded-full bg-amber-100 px-3 py-1 text-xs font-medium text-amber-900">Paper trading</span>
        <AuthNav />
      </div>
    </nav>
    {children}
  </>;
}
