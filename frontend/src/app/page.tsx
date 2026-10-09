"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { IndexStrip } from "@/components/IndexStrip";
import { api, inr } from "@/lib/api";
import { INDICES, type Order, type Position, type Quote, contract, fetchQuotes, signed } from "@/lib/market";

const exitHref = (p: Position) =>
  `/markets?${new URLSearchParams({
    symbol: p.underlying,
    instrument: p.instrument,
    side: p.quantity > 0 ? "SELL" : "BUY",
    lots: String(Math.abs(p.quantity) / p.lot_size),
    price: String(p.mark_price ?? p.avg_price),
    strike: p.strike != null ? String(p.strike) : "",
    expiry: p.expiry ?? "",
    note: "exit",
  })}`;

export default function Dashboard() {
  const router = useRouter();
  const [quotes, setQuotes] = useState<Record<string, Quote>>({});
  const [positions, setPositions] = useState<Position[]>([]);
  const [book, setBook] = useState({ net_pnl: 0, charges: 0 });
  const [orders, setOrders] = useState<Order[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const fail = (e: unknown) => setError(e instanceof Error ? e.message : "Could not load dashboard");
    const load = () => {
      fetchQuotes(INDICES).then(setQuotes).catch(fail);
      Promise.all([
        api<{ positions: Position[]; net_pnl: number; charges: number }>("/api/paper/positions"),
        api<{ orders: Order[] }>("/api/paper/orders"),
      ])
        .then(([p, o]) => {
          setPositions(p.positions);
          setBook({ net_pnl: p.net_pnl, charges: p.charges });
          setOrders(o.orders);
        })
        .catch(fail);
    };
    load();
    const timer = setInterval(load, 60_000);
    return () => clearInterval(timer);
  }, []);

  const open = positions.filter((p) => p.quantity !== 0);
  const closed = positions.filter((p) => p.quantity === 0);
  const sum = (k: "unrealized_pnl" | "realized_pnl") => positions.reduce((a, p) => a + p[k], 0);
  const cards = [
    ["Net P&L", book.net_pnl],
    ["Unrealized", sum("unrealized_pnl")],
    ["Realized", sum("realized_pnl")],
    ["Charges paid", -book.charges],
  ] as const;

  return (
    <div className="mx-auto max-w-[1400px] space-y-4 px-4 py-4">
      <IndexStrip quotes={quotes} onSelect={(s) => router.push(`/markets?symbol=${s}`)} />

      {error && (
        <div className="flex items-start justify-between rounded-lg border border-rose-200 bg-rose-50 px-4 py-2 text-sm text-rose-900">
          {error}
          <button onClick={() => setError(null)} aria-label="Dismiss">
            ×
          </button>
        </div>
      )}

      <section className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
        {cards.map(([label, v]) => (
          <div key={label} className="rounded-xl border bg-white p-4 shadow-sm">
            <div className="text-xs text-zinc-500">{label}</div>
            <div className={`text-xl font-semibold ${signed(v)}`}>{inr(v)}</div>
          </div>
        ))}
        <div className="rounded-xl border bg-white p-4 shadow-sm">
          <div className="text-xs text-zinc-500">Open positions</div>
          <div className="text-xl font-semibold">{open.length}</div>
          <Link href="/markets" className="text-xs text-emerald-700 hover:underline">
            Trade on Markets →
          </Link>
        </div>
      </section>

      <section className="space-y-3 rounded-xl border bg-white p-4 shadow-sm">
        <div className="flex flex-wrap items-center gap-4">
          <h2 className="mr-auto font-medium">Paper positions</h2>
          <span className="text-xs text-zinc-500">Marked to live quotes (futures against index spot)</span>
        </div>
        {positions.length === 0 ? (
          <p className="text-sm text-zinc-500">
            No paper trades yet.{" "}
            <Link href="/markets" className="text-emerald-700 underline">
              Place one from Markets
            </Link>
            .
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full text-sm">
              <thead>
                <tr className="border-b text-left text-zinc-500">
                  <th className="py-2 pr-4">Contract</th>
                  <th className="py-2 pr-4">Qty</th>
                  <th className="py-2 pr-4">Avg</th>
                  <th className="py-2 pr-4">Mark</th>
                  <th className="py-2 pr-4">Unrealized</th>
                  <th className="py-2 pr-4">Realized</th>
                  <th className="py-2 pr-4">Charges</th>
                  <th className="py-2 pr-4">Net</th>
                  <th className="py-2 pr-4" />
                </tr>
              </thead>
              <tbody>
                {[...open, ...closed].map((p) => (
                  <tr key={contract(p)} className={`border-b ${p.quantity === 0 ? "text-zinc-500" : ""}`}>
                    <td className="py-2 pr-4 font-medium">{contract(p)}</td>
                    <td className={`py-2 pr-4 ${signed(p.quantity)}`}>{p.quantity}</td>
                    <td className="py-2 pr-4">{p.quantity ? p.avg_price : "—"}</td>
                    <td className="py-2 pr-4">
                      {p.mark_price ?? "—"}
                      {p.mark_time && <span className="block text-xs text-zinc-400">{p.mark_time} IST</span>}
                    </td>
                    <td className={`py-2 pr-4 ${signed(p.unrealized_pnl)}`}>{inr(p.unrealized_pnl)}</td>
                    <td className={`py-2 pr-4 ${signed(p.realized_pnl)}`}>{inr(p.realized_pnl)}</td>
                    <td className="py-2 pr-4">{inr(p.charges)}</td>
                    <td className={`py-2 pr-4 font-medium ${signed(p.net_pnl)}`}>{inr(p.net_pnl)}</td>
                    <td className="py-2 pr-4">
                      {p.quantity !== 0 && (
                        <Link href={exitHref(p)} className="rounded-md border px-2 py-1 text-xs hover:bg-zinc-50">
                          Exit
                        </Link>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="space-y-3 rounded-xl border bg-white p-4 shadow-sm">
        <h2 className="font-medium">Order history</h2>
        {orders.length === 0 ? (
          <p className="text-sm text-zinc-500">No orders yet.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full text-sm">
              <thead>
                <tr className="border-b text-left text-zinc-500">
                  <th className="py-2 pr-4">Time</th>
                  <th className="py-2 pr-4">Contract</th>
                  <th className="py-2 pr-4">Side</th>
                  <th className="py-2 pr-4">Qty</th>
                  <th className="py-2 pr-4">Price</th>
                  <th className="py-2 pr-4">Charges</th>
                  <th className="py-2 pr-4">Why</th>
                </tr>
              </thead>
              <tbody>
                {orders.map((o) => (
                  <tr key={o.id} className="border-b">
                    <td className="py-2 pr-4 text-xs text-zinc-500">{new Date(o.created_at).toLocaleString("en-IN")}</td>
                    <td className="py-2 pr-4">{contract(o)}</td>
                    <td className={`py-2 pr-4 font-medium ${o.side === "BUY" ? "text-emerald-700" : "text-rose-700"}`}>
                      {o.side}
                    </td>
                    <td className="py-2 pr-4">{o.instrument === "EQ" ? o.lots : `${o.lots} × ${o.lot_size}`}</td>
                    <td className="py-2 pr-4">{o.price}</td>
                    <td className="py-2 pr-4">{inr(o.charges.total, 2)}</td>
                    <td className="py-2 pr-4 text-zinc-600">{o.note ?? ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
