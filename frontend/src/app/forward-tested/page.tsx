"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { api, inr } from "@/lib/api";

type ForwardTrade = {
  date: string;
  time: string;
  strategy: string;
  setup: string;
  side: "LONG" | "SHORT";
  status: string;
  entry: number;
  stop: number;
  target: number;
  risk: number;
  exit_spot: number;
  exit_time: string;
  exit_reason: string;
  points: number;
  r_multiple: number;
  option: string;
  expiry: string;
  entry_premium: number;
  exit_premium: number;
  capital_deployed: number;
  exit_value: number;
  roi_pct: number;
  est_pnl_inr: number;
  lots: number;
  qty: number;
  rationale: string;
};

type AggregateMetrics = {
  total_trades: number;
  wins: number;
  losses: number;
  breakevens: number;
  win_rate: number;
  win_rate_ex_be: number;
  total_r: number;
  total_points: number;
  net_pnl_inr: number;
  profit_factor: number;
  total_capital_deployed: number;
  total_exit_value: number;
  avg_trade_capital: number;
  total_roi_pct: number;
};

type StrategyMetric = {
  trades: number;
  wins: number;
  losses: number;
  be: number;
  win_rate: number;
  total_r: number;
  capital_deployed: number;
  net_pnl_inr: number;
  roi_pct: number;
};

type ApiResponse = {
  metrics: AggregateMetrics;
  strategy_metrics: Record<string, StrategyMetric>;
  trades: ForwardTrade[];
};

export default function ForwardTestedPage() {
  const [data, setData] = useState<ApiResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Filters
  const [selectedStrategy, setSelectedStrategy] = useState<string>("all");
  const [selectedOutcome, setSelectedOutcome] = useState<string>("all");
  const [selectedSide, setSelectedSide] = useState<string>("all");
  const [expandedRow, setExpandedRow] = useState<number | null>(null);

  const loadData = () => {
    setLoading(true);
    api<ApiResponse>("/api/trades/forward-tested")
      .then((res) => {
        setData(res);
        setLoading(false);
      })
      .catch((err) => {
        setError(err instanceof Error ? err.message : "Failed to load forward-tested trades");
        setLoading(false);
      });
  };

  useEffect(() => {
    loadData();
  }, []);

  const strategies = useMemo(() => {
    if (!data?.trades) return [];
    return Array.from(new Set(data.trades.map((t) => t.strategy))).sort();
  }, [data]);

  const filteredTrades = useMemo(() => {
    if (!data?.trades) return [];
    return data.trades.filter((t) => {
      if (selectedStrategy !== "all" && t.strategy !== selectedStrategy) return false;
      if (selectedSide !== "all" && t.side !== selectedSide) return false;
      if (selectedOutcome === "wins" && t.r_multiple <= 0.05) return false;
      if (selectedOutcome === "losses" && t.r_multiple >= -0.05) return false;
      if (selectedOutcome === "be" && Math.abs(t.r_multiple) > 0.05) return false;
      return true;
    });
  }, [data, selectedStrategy, selectedOutcome, selectedSide]);

  if (loading) {
    return (
      <main className="mx-auto max-w-7xl px-4 py-12 text-center">
        <div className="inline-block h-8 w-8 animate-spin rounded-full border-4 border-emerald-600 border-t-transparent" />
        <p className="mt-4 text-sm font-medium text-zinc-600">Loading forward-tested trades, option values & capital...</p>
      </main>
    );
  }

  if (error || !data) {
    return (
      <main className="mx-auto max-w-7xl px-4 py-12 text-center">
        <div className="rounded-xl border border-rose-200 bg-rose-50 p-6 text-rose-800">
          <h2 className="text-lg font-bold">Failed to Load Forward Tested Trades</h2>
          <p className="mt-2 text-sm">{error || "No data available"}</p>
          <button
            onClick={loadData}
            className="mt-4 rounded-lg bg-rose-600 px-4 py-2 text-sm font-medium text-white hover:bg-rose-700"
          >
            Retry
          </button>
        </div>
      </main>
    );
  }

  const { metrics, strategy_metrics } = data;

  return (
    <main className="mx-auto max-w-7xl px-4 py-8 space-y-8">
      {/* Page Header */}
      <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between border-b pb-6">
        <div>
          <div className="flex items-center gap-2">
            <span className="inline-flex items-center rounded-full bg-emerald-100 px-2.5 py-0.5 text-xs font-semibold text-emerald-800">
              ● Forward-Tested (Oct 15 – Present)
            </span>
            <span className="inline-flex items-center rounded-full bg-blue-100 px-2.5 py-0.5 text-xs font-semibold text-blue-800">
              Option Value & Capital Deployed Active
            </span>
          </div>
          <h1 className="mt-2 text-2xl font-bold tracking-tight text-zinc-900 md:text-3xl">
            NIFTY Forward-Tested Performance & Option Ledger
          </h1>
          <p className="mt-1 text-sm text-zinc-600">
            Real-time journal of suggested institutional setups with exact option premiums, capital deployed,
            exit values, and net returns since October 15.
          </p>
        </div>

        <div className="flex items-center gap-3">
          <button
            onClick={loadData}
            className="inline-flex items-center gap-1.5 rounded-lg border border-zinc-200 bg-white px-3 py-1.5 text-xs font-medium text-zinc-700 shadow-sm hover:bg-zinc-50"
          >
            🔄 Refresh
          </button>
          <Link
            href="/markets"
            className="inline-flex items-center gap-1.5 rounded-lg bg-emerald-600 px-3 py-1.5 text-xs font-medium text-white shadow-sm hover:bg-emerald-700"
          >
            📈 View Live Chart
          </Link>
        </div>
      </div>

      {/* Primary KPI Header Cards */}
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4 lg:grid-cols-8">
        <div className="rounded-xl border border-zinc-200 bg-white p-3.5 shadow-sm">
          <div className="text-[11px] font-medium uppercase tracking-wider text-zinc-500">Forward Trades</div>
          <div className="mt-1.5 flex items-baseline gap-1.5">
            <span className="text-xl font-bold text-zinc-900">{metrics.total_trades}</span>
            <span className="text-[10px] font-medium text-zinc-500">
              ({metrics.wins}W/{metrics.losses}L/{metrics.breakevens}BE)
            </span>
          </div>
        </div>

        <div className="rounded-xl border border-zinc-200 bg-white p-3.5 shadow-sm">
          <div className="text-[11px] font-medium uppercase tracking-wider text-zinc-500">Win Rate</div>
          <div className="mt-1.5 flex items-baseline gap-1.5">
            <span className="text-xl font-bold text-emerald-700">{metrics.win_rate}%</span>
            <span className="text-[10px] font-semibold text-emerald-800">({metrics.win_rate_ex_be}% ex-BE)</span>
          </div>
        </div>

        <div className="rounded-xl border border-zinc-200 bg-white p-3.5 shadow-sm">
          <div className="text-[11px] font-medium uppercase tracking-wider text-zinc-500">Realized R-Gain</div>
          <div className="mt-1.5 flex items-baseline gap-1">
            <span className="text-xl font-bold text-emerald-600">+{metrics.total_r}R</span>
          </div>
        </div>

        <div className="rounded-xl border border-zinc-200 bg-white p-3.5 shadow-sm">
          <div className="text-[11px] font-medium uppercase tracking-wider text-zinc-500">Index Points</div>
          <div className="mt-1.5 flex items-baseline gap-1">
            <span className="text-xl font-bold text-zinc-900">+{metrics.total_points}</span>
            <span className="text-[10px] text-zinc-500">pts</span>
          </div>
        </div>

        <div className="rounded-xl border border-zinc-200 bg-white p-3.5 shadow-sm">
          <div className="text-[11px] font-medium uppercase tracking-wider text-zinc-500">Capital Deployed</div>
          <div className="mt-1.5 flex items-baseline gap-1">
            <span className="text-xl font-bold text-zinc-900">{inr(metrics.total_capital_deployed)}</span>
          </div>
        </div>

        <div className="rounded-xl border border-zinc-200 bg-white p-3.5 shadow-sm">
          <div className="text-[11px] font-medium uppercase tracking-wider text-zinc-500">Total Exit Value</div>
          <div className="mt-1.5 flex items-baseline gap-1">
            <span className="text-xl font-bold text-blue-900">{inr(metrics.total_exit_value)}</span>
          </div>
        </div>

        <div className="rounded-xl border border-zinc-200 bg-white p-3.5 shadow-sm">
          <div className="text-[11px] font-medium uppercase tracking-wider text-zinc-500">Net Option P&L</div>
          <div className="mt-1.5 flex items-baseline gap-1">
            <span className={`text-xl font-bold ${metrics.net_pnl_inr >= 0 ? "text-emerald-700" : "text-rose-700"}`}>
              {inr(metrics.net_pnl_inr)}
            </span>
          </div>
        </div>

        <div className="rounded-xl border border-zinc-200 bg-white p-3.5 shadow-sm">
          <div className="text-[11px] font-medium uppercase tracking-wider text-zinc-500">Option ROI / PF</div>
          <div className="mt-1.5 flex items-baseline gap-1">
            <span className="text-xl font-bold text-emerald-700">+{metrics.total_roi_pct}%</span>
            <span className="text-[10px] font-semibold text-purple-700">({metrics.profit_factor} PF)</span>
          </div>
        </div>
      </div>

      {/* Strategy Breakdown Cards */}
      <div className="space-y-3">
        <h2 className="text-xs font-semibold uppercase tracking-wider text-zinc-500">Strategy Performance & Capital Allocation</h2>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
          {Object.entries(strategy_metrics).map(([stratName, m]) => (
            <div
              key={stratName}
              className={`rounded-xl border p-4 shadow-sm transition-all cursor-pointer ${
                selectedStrategy === stratName
                  ? "border-emerald-600 bg-emerald-50/40 ring-1 ring-emerald-600"
                  : "border-zinc-200 bg-white hover:border-zinc-300"
              }`}
              onClick={() => setSelectedStrategy(selectedStrategy === stratName ? "all" : stratName)}
              role="button"
              tabIndex={0}
            >
              <div className="flex items-center justify-between">
                <span className="text-sm font-bold text-zinc-900">{stratName}</span>
                <span className="rounded-md bg-zinc-100 px-2 py-0.5 text-xs font-medium text-zinc-600">
                  {m.trades} trades ({m.wins}W / {m.losses}L / {m.be}BE)
                </span>
              </div>
              <div className="mt-3 grid grid-cols-4 gap-2 border-t pt-3 text-xs">
                <div>
                  <span className="text-zinc-500">Win Rate</span>
                  <div className="font-semibold text-zinc-900">{m.win_rate}%</div>
                </div>
                <div>
                  <span className="text-zinc-500">Total R</span>
                  <div className={`font-semibold ${m.total_r >= 0 ? "text-emerald-700" : "text-rose-700"}`}>
                    {m.total_r >= 0 ? `+${m.total_r}R` : `${m.total_r}R`}
                  </div>
                </div>
                <div>
                  <span className="text-zinc-500">Capital</span>
                  <div className="font-semibold text-zinc-800">{inr(m.capital_deployed)}</div>
                </div>
                <div>
                  <span className="text-zinc-500">Net Return</span>
                  <div className={`font-bold ${m.net_pnl_inr >= 0 ? "text-emerald-700" : "text-rose-700"}`}>
                    {inr(m.net_pnl_inr)}
                    <span className="block text-[10px] font-normal text-zinc-500">
                      ({m.roi_pct >= 0 ? `+${m.roi_pct}%` : `${m.roi_pct}%`})
                    </span>
                  </div>
                </div>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Filters Toolbar */}
      <div className="flex flex-wrap items-center justify-between gap-4 rounded-xl border border-zinc-200 bg-white p-4 shadow-sm">
        <div className="flex flex-wrap items-center gap-3">
          {/* Strategy Select */}
          <div className="flex items-center gap-2">
            <span className="text-xs font-medium text-zinc-500">Strategy:</span>
            <select
              value={selectedStrategy}
              onChange={(e) => setSelectedStrategy(e.target.value)}
              className="rounded-lg border border-zinc-300 bg-white px-2.5 py-1.5 text-xs font-medium text-zinc-800 shadow-sm focus:border-emerald-600 focus:outline-none"
            >
              <option value="all">All Strategies ({data.trades.length})</option>
              {strategies.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </div>

          {/* Outcome Filter */}
          <div className="flex items-center gap-2">
            <span className="text-xs font-medium text-zinc-500">Outcome:</span>
            <div className="inline-flex rounded-lg border border-zinc-200 bg-zinc-50 p-0.5 text-xs">
              {(["all", "wins", "losses", "be"] as const).map((opt) => (
                <button
                  key={opt}
                  onClick={() => setSelectedOutcome(opt)}
                  className={`rounded-md px-2.5 py-1 font-medium capitalize transition-all ${
                    selectedOutcome === opt ? "bg-white text-zinc-900 shadow-sm" : "text-zinc-600 hover:text-zinc-900"
                  }`}
                >
                  {opt === "be" ? "Breakeven" : opt}
                </button>
              ))}
            </div>
          </div>

          {/* Side Filter */}
          <div className="flex items-center gap-2">
            <span className="text-xs font-medium text-zinc-500">Side:</span>
            <div className="inline-flex rounded-lg border border-zinc-200 bg-zinc-50 p-0.5 text-xs">
              {(["all", "LONG", "SHORT"] as const).map((s) => (
                <button
                  key={s}
                  onClick={() => setSelectedSide(s)}
                  className={`rounded-md px-2.5 py-1 font-medium transition-all ${
                    selectedSide === s ? "bg-white text-zinc-900 shadow-sm" : "text-zinc-600 hover:text-zinc-900"
                  }`}
                >
                  {s}
                </button>
              ))}
            </div>
          </div>
        </div>

        <div className="text-xs text-zinc-500">
          Showing <span className="font-semibold text-zinc-800">{filteredTrades.length}</span> of {data.trades.length} trades
        </div>
      </div>

      {/* Trade Ledger Table */}
      <div className="overflow-hidden rounded-xl border border-zinc-200 bg-white shadow-sm">
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-zinc-200 text-left text-xs">
            <thead className="bg-zinc-800 text-white font-semibold">
              <tr>
                <th className="px-3 py-3">Date & Time</th>
                <th className="px-3 py-3">Strategy & Setup</th>
                <th className="px-2 py-3 text-center">Side</th>
                <th className="px-3 py-3 text-right">Spot Entry / SL / TP</th>
                <th className="px-3 py-3 text-center">Status</th>
                <th className="px-3 py-3">Option Contract</th>
                <th className="px-3 py-3 text-right">Entry Prem & Cap</th>
                <th className="px-3 py-3 text-right">Exit Prem & Value</th>
                <th className="px-3 py-3 text-right">Net P&L (ROI %)</th>
                <th className="px-2 py-3 text-center">R:R (Pts)</th>
                <th className="px-2 py-3 text-center">Why</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-100 font-mono">
              {filteredTrades.map((t, idx) => {
                const isWin = t.r_multiple > 0.05;
                const isLoss = t.r_multiple < -0.05;
                const isExpanded = expandedRow === idx;

                return (
                  <tr
                    key={`${t.date}-${t.time}-${idx}`}
                    className={`hover:bg-zinc-50/80 transition-colors ${
                      isExpanded ? "bg-amber-50/40" : idx % 2 === 0 ? "bg-white" : "bg-zinc-50/30"
                    }`}
                  >
                    {/* Date & Time */}
                    <td className="px-3 py-3 whitespace-nowrap font-medium text-zinc-900 font-sans">
                      <div>{t.date}</div>
                      <div className="text-[11px] text-zinc-500">{t.time} IST</div>
                    </td>

                    {/* Strategy & Setup */}
                    <td className="px-3 py-3 whitespace-nowrap font-sans font-medium text-zinc-800">
                      <div>{t.strategy}</div>
                      <div className="text-[10px] text-zinc-500 font-normal">{t.setup}</div>
                    </td>

                    {/* Side */}
                    <td className="px-2 py-3 text-center whitespace-nowrap">
                      <span
                        className={`inline-block rounded px-1.5 py-0.5 text-[10px] font-bold ${
                          t.side === "LONG" ? "bg-emerald-100 text-emerald-800" : "bg-rose-100 text-rose-800"
                        }`}
                      >
                        {t.side}
                      </span>
                    </td>

                    {/* Spot Levels */}
                    <td className="px-3 py-3 text-right whitespace-nowrap text-zinc-900">
                      <div>Ent: {t.entry.toFixed(2)}</div>
                      <div className="text-[10px] text-zinc-500">
                        SL: <span className="text-rose-600 font-semibold">{t.stop.toFixed(1)}</span> | TP: <span className="text-emerald-600 font-semibold">{t.target.toFixed(1)}</span>
                      </div>
                    </td>

                    {/* Status */}
                    <td className="px-3 py-3 text-center whitespace-nowrap font-sans">
                      <span
                        className={`inline-flex items-center rounded-md px-2 py-0.5 text-[10px] font-semibold ${
                          t.status.includes("TARGET")
                            ? "bg-emerald-100 text-emerald-800"
                            : t.status.includes("TRAILED")
                            ? "bg-blue-100 text-blue-800"
                            : t.status.includes("FLAT")
                            ? "bg-zinc-100 text-zinc-800"
                            : "bg-rose-100 text-rose-800"
                        }`}
                      >
                        {t.status}
                      </span>
                      <div className="text-[10px] text-zinc-500 font-mono mt-0.5">{t.exit_time}</div>
                    </td>

                    {/* Option Contract */}
                    <td className="px-3 py-3 whitespace-nowrap font-sans text-zinc-800">
                      <div className="font-semibold text-zinc-900">{t.option}</div>
                      <div className="text-[10px] text-zinc-500">
                        {t.lots} Lots ({t.qty} Qty) | Exp: {t.expiry}
                      </div>
                    </td>

                    {/* Entry Premium & Capital Deployed */}
                    <td className="px-3 py-3 text-right whitespace-nowrap font-sans">
                      <div className="font-bold text-zinc-900">₹{t.entry_premium.toFixed(2)}</div>
                      <div className="text-[10px] font-semibold text-zinc-600">
                        Cap: {inr(t.capital_deployed)}
                      </div>
                    </td>

                    {/* Exit Premium & Value */}
                    <td className="px-3 py-3 text-right whitespace-nowrap font-sans">
                      <div className="font-bold text-zinc-900">₹{t.exit_premium.toFixed(2)}</div>
                      <div className="text-[10px] font-semibold text-blue-800">
                        Val: {inr(t.exit_value)}
                      </div>
                    </td>

                    {/* Net P&L & Option ROI */}
                    <td className="px-3 py-3 text-right whitespace-nowrap font-sans">
                      <div className={`font-bold ${isWin ? "text-emerald-700" : isLoss ? "text-rose-700" : "text-zinc-600"}`}>
                        {t.est_pnl_inr >= 0 ? `+${inr(t.est_pnl_inr)}` : inr(t.est_pnl_inr)}
                      </div>
                      <div className={`text-[10px] font-semibold ${t.roi_pct >= 0 ? "text-emerald-700" : "text-rose-700"}`}>
                        {t.roi_pct >= 0 ? `+${t.roi_pct.toFixed(1)}%` : `${t.roi_pct.toFixed(1)}%`} ROI
                      </div>
                    </td>

                    {/* R-Multiple & Points */}
                    <td className="px-2 py-3 text-center whitespace-nowrap font-sans">
                      <div className={`font-bold ${isWin ? "text-emerald-700" : isLoss ? "text-rose-700" : "text-zinc-600"}`}>
                        {t.r_multiple >= 0 ? `+${t.r_multiple.toFixed(2)}R` : `${t.r_multiple.toFixed(2)}R`}
                      </div>
                      <div className="text-[10px] text-zinc-500">
                        {t.points >= 0 ? `+${t.points.toFixed(1)}` : t.points.toFixed(1)} pts
                      </div>
                    </td>

                    {/* Why Accordion Button */}
                    <td className="px-2 py-3 text-center whitespace-nowrap font-sans">
                      <button
                        onClick={() => setExpandedRow(isExpanded ? null : idx)}
                        className="rounded border border-zinc-200 bg-white px-2 py-0.5 text-[11px] font-medium text-zinc-600 hover:bg-zinc-50 hover:text-zinc-900 shadow-xs"
                      >
                        {isExpanded ? "Hide" : "Why"}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>

        {/* Expanded Rationale Drawer if open */}
        {expandedRow !== null && filteredTrades[expandedRow] && (
          <div className="border-t border-amber-200 bg-amber-50/70 p-4 font-sans">
            <div className="flex items-center justify-between">
              <h3 className="text-xs font-bold text-amber-950 uppercase tracking-wider">
                Setup Rationale & Forensic Conditions: {filteredTrades[expandedRow].strategy} ({filteredTrades[expandedRow].date} {filteredTrades[expandedRow].time})
              </h3>
              <button
                onClick={() => setExpandedRow(null)}
                className="text-xs font-medium text-amber-800 hover:underline"
              >
                ✕ Close
              </button>
            </div>
            <p className="mt-2 text-xs leading-relaxed text-amber-900 whitespace-pre-wrap">
              {filteredTrades[expandedRow].rationale || "No detailed condition text recorded."}
            </p>
          </div>
        )}
      </div>
    </main>
  );
}
