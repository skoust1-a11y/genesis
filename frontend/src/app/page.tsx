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
  const [forwardMetrics, setForwardMetrics] = useState<{
    total_trades: number;
    win_rate: number;
    net_pnl_inr: number;
    total_roi_pct: number;
    total_capital_deployed: number;
    total_exit_value: number;
  } | null>(null);

  useEffect(() => {
    const fail = (e: unknown) => setError(e instanceof Error ? e.message : "Could not load dashboard");
    const load = () => {
      fetchQuotes(INDICES).then(setQuotes).catch(fail);
      Promise.all([
        api<{ positions: Position[]; net_pnl: number; charges: number }>("/api/paper/positions"),
        api<{ orders: Order[] }>("/api/paper/orders"),
        api<{ metrics: { total_trades: number; win_rate: number; net_pnl_inr: number; total_roi_pct: number; total_capital_deployed: number; total_exit_value: number } }>("/api/trades/forward-tested"),
      ])
        .then(([p, o, f]) => {
          setPositions(p.positions);
          setBook({ net_pnl: p.net_pnl, charges: p.charges });
          setOrders(o.orders);
          setForwardMetrics(f.metrics);
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

      {/* Forward-Tested Strategy Engine Connection */}
      <section className="rounded-xl border border-emerald-200/80 bg-gradient-to-r from-emerald-50/90 via-teal-50/70 to-blue-50/70 p-4 shadow-sm">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
          <div className="space-y-1">
            <div className="flex items-center gap-2">
              <span className="inline-flex items-center rounded-full bg-emerald-600 px-2.5 py-0.5 text-[10px] font-bold text-white uppercase tracking-wider">
                Strategy Forward-Testing Engine
              </span>
              <span className="text-xs font-semibold text-emerald-900">
                ● Live Out-of-Sample Paper Simulation (Oct 15 – Present)
              </span>
            </div>
            <h3 className="text-base font-bold text-zinc-900">
              Automated Strategy Edge & Black-76 Option Execution Blueprint
            </h3>
            <p className="text-xs text-zinc-600 max-w-2xl">
              Continuously scanned across 1m, 5m, and 15m timeframes. All setups are valued with true Black-76 option pricing, statutory charges, and dynamic breakeven stops — serving as the automated execution blueprint for future live trading.
            </p>
          </div>

          <div className="flex flex-wrap items-center gap-3">
            {forwardMetrics && (
              <div className="grid grid-cols-4 gap-2.5 bg-white/90 backdrop-blur rounded-lg border border-emerald-200 p-2.5 text-center shadow-sm">
                <div className="px-1">
                  <div className="text-[10px] text-zinc-500 font-medium">Forward Trades</div>
                  <div className="font-bold text-zinc-900 text-sm">{forwardMetrics.total_trades}</div>
                </div>
                <div className="px-1">
                  <div className="text-[10px] text-zinc-500 font-medium">Win Rate</div>
                  <div className="font-bold text-emerald-700 text-sm">{forwardMetrics.win_rate}%</div>
                </div>
                <div className="px-1">
                  <div className="text-[10px] text-zinc-500 font-medium">Net P&L</div>
                  <div className={`font-bold text-sm ${forwardMetrics.net_pnl_inr >= 0 ? "text-emerald-700" : "text-rose-700"}`}>
                    {forwardMetrics.net_pnl_inr >= 0 ? `+${inr(forwardMetrics.net_pnl_inr)}` : inr(forwardMetrics.net_pnl_inr)}
                  </div>
                </div>
                <div className="px-1">
                  <div className="text-[10px] text-zinc-500 font-medium">Option ROI</div>
                  <div className="font-bold text-emerald-700 text-sm">+{forwardMetrics.total_roi_pct}%</div>
                </div>
              </div>
            )}
            <Link
              href="/forward-tested"
              className="inline-flex items-center gap-1.5 rounded-lg bg-emerald-700 px-3.5 py-2 text-xs font-semibold text-white shadow-sm hover:bg-emerald-800 transition"
            >
              <span>📊 View Forward Ledger & Options</span>
              <span>→</span>
            </Link>
          </div>
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
