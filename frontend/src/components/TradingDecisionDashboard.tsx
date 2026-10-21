"use client";

import { useState } from "react";

export type DecisionData = {
  market_regime?: string;
  htf_trend?: string;
  intraday_trend?: string;
  market_structure?: string;
  vwap_position?: string;
  nearest_support?: string;
  nearest_resistance?: string;
  nearest_liquidity_pool?: string;
  momentum_condition?: string;
  reversal_or_continuation?: string;
};

export type RegimeData = {
  classification?: string;
  score?: number;
  adx?: number;
  reasons?: string[];
  details?: Record<string, unknown>;
};

export type MTFRow = {
  timeframe: string;
  trend: string;
  structure: string;
  momentum: string;
  bias: string;
  last_price: number;
  key_level: number | null;
};

export type MTFReportData = {
  matrix?: MTFRow[];
  alignment?: string;
  conflict_notes?: string[];
};

export function TradingDecisionDashboard({
  decision,
  regime,
  mtf,
  symbol,
  currentPrice,
}: {
  decision?: DecisionData;
  regime?: RegimeData;
  mtf?: MTFReportData;
  symbol: string;
  currentPrice?: number;
}) {
  const [showReasons, setShowReasons] = useState(false);
  const [showMTF, setShowMTF] = useState(false);
  const [collapsed, setCollapsed] = useState(false);

  const classification = regime?.classification || decision?.market_regime || "Sideways / Consolidation";
  const score = regime?.score ?? 0;

  const isBull = classification.toLowerCase().includes("bullish");
  const isBear = classification.toLowerCase().includes("bearish");
  const isStrong = classification.toLowerCase().includes("strong");

  const badgeColor = isBull
    ? isStrong
      ? "bg-emerald-600 text-white border-emerald-700"
      : "bg-emerald-50 dark:bg-emerald-950/40 text-emerald-800 dark:text-emerald-300 border-emerald-300 dark:border-emerald-800"
    : isBear
    ? isStrong
      ? "bg-rose-600 text-white border-rose-700"
      : "bg-rose-50 dark:bg-rose-950/40 text-rose-800 dark:text-rose-300 border-rose-300 dark:border-rose-800"
    : "bg-amber-50 dark:bg-amber-950/40 text-amber-800 dark:text-amber-300 border-amber-300 dark:border-amber-800";

  return (
    <div className="rounded-xl border border-zinc-200 dark:border-[#2a2e39] bg-white dark:bg-[#131722] p-3 shadow-xs space-y-3 font-sans text-xs transition-colors">
      {/* Top Banner: Regime Badge, Score, Quick Action */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-100 dark:border-[#2a2e39] pb-2.5">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[11px] font-semibold uppercase tracking-wider text-zinc-500 dark:text-zinc-400">
            Strategy Intelligence ({symbol}):
          </span>
          <span
            className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-semibold ${badgeColor}`}
          >
            <span
              className={`h-1.5 w-1.5 rounded-full ${
                isStrong ? "bg-white animate-pulse" : isBull ? "bg-emerald-500" : isBear ? "bg-rose-500" : "bg-amber-500"
              }`}
            />
            {classification}
            <span className="font-mono text-[11px] opacity-85">
              ({score > 0 ? `+${score}` : score})
            </span>
          </span>
          {regime?.reasons && regime.reasons.length > 0 && (
            <button
              onClick={() => setShowReasons(!showReasons)}
              className="text-[11px] text-zinc-500 dark:text-zinc-400 hover:text-zinc-800 dark:hover:text-zinc-200 underline underline-offset-2"
            >
              {showReasons ? "Hide Factors" : "View Factors"}
            </button>
          )}
        </div>

        <div className="flex items-center gap-2">
          {mtf?.alignment && (
            <span
              className={`rounded-md border px-2 py-0.5 text-[11px] font-medium ${
                mtf.alignment.includes("Bullish")
                  ? "border-emerald-200 dark:border-emerald-800 bg-emerald-50/50 dark:bg-emerald-950/30 text-emerald-700 dark:text-emerald-300"
                  : mtf.alignment.includes("Bearish")
                  ? "border-rose-200 dark:border-rose-800 bg-rose-50/50 dark:bg-rose-950/30 text-rose-700 dark:text-rose-300"
                  : "border-zinc-200 dark:border-zinc-700 bg-zinc-50 dark:bg-zinc-800/40 text-zinc-700 dark:text-zinc-300"
              }`}
            >
              MTF: {mtf.alignment}
            </span>
          )}
          <button
            onClick={() => setShowMTF(!showMTF)}
            className={`rounded-md border px-2 py-0.5 text-[11px] font-medium transition ${
              showMTF
                ? "bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900 border-zinc-900"
                : "text-zinc-600 dark:text-zinc-400 hover:bg-zinc-50 dark:hover:bg-[#1e222d] border-zinc-200 dark:border-[#2a2e39]"
            }`}
          >
            MTF Matrix {showMTF ? "▲" : "▼"}
          </button>
          <button
            onClick={() => setCollapsed(!collapsed)}
            className="text-[11px] text-zinc-400 hover:text-zinc-700 dark:hover:text-zinc-200 px-1"
            title={collapsed ? "Expand Dashboard" : "Collapse Dashboard"}
          >
            {collapsed ? "Expand ▼" : "Collapse ▲"}
          </button>
        </div>
      </div>

      {!collapsed && (
        <>
          {/* Explanatory Factors Dropdown */}
          {showReasons && regime?.reasons && (
            <div className="rounded-lg border border-zinc-100 dark:border-[#2a2e39] bg-zinc-50/80 dark:bg-[#1e222d]/60 p-2.5 text-[11px] text-zinc-700 dark:text-zinc-300 space-y-1">
              <div className="font-semibold text-zinc-800 dark:text-zinc-200">Scoring Formula Factors (Scale: -10 to +10):</div>
              <ul className="list-disc pl-4 space-y-0.5 text-zinc-600 dark:text-zinc-400">
                {regime.reasons.map((r, i) => (
                  <li key={i}>{r}</li>
                ))}
              </ul>
            </div>
          )}

          {/* Synchronized MTF Matrix */}
          {showMTF && mtf?.matrix && (
            <div className="overflow-x-auto rounded-lg border border-zinc-200 dark:border-[#2a2e39] bg-zinc-50/50 dark:bg-[#1e222d]/40 p-2 text-xs space-y-2">
              <div className="flex items-center justify-between">
                <span className="font-semibold text-zinc-800 dark:text-zinc-200">Synchronized Multi-Timeframe Matrix</span>
                {mtf.conflict_notes && mtf.conflict_notes.length > 0 && (
                  <span className="text-[11px] text-amber-600 dark:text-amber-400 font-medium">
                    {mtf.conflict_notes[0]}
                  </span>
                )}
              </div>
              <table className="w-full border-collapse font-mono text-[11px]">
                <thead>
                  <tr className="border-b border-zinc-200 dark:border-[#2a2e39] text-zinc-500 dark:text-zinc-400 text-left">
                    <th className="py-1 px-2 font-medium">Timeframe</th>
                    <th className="py-1 px-2 font-medium">Trend</th>
                    <th className="py-1 px-2 font-medium">Structure</th>
                    <th className="py-1 px-2 font-medium">Momentum</th>
                    <th className="py-1 px-2 font-medium">Bias</th>
                    <th className="py-1 px-2 font-medium">Last Price</th>
                  </tr>
                </thead>
                <tbody>
                  {mtf.matrix.map((row) => (
                    <tr key={row.timeframe} className="border-b border-zinc-100 dark:border-[#2a2e39]/50 hover:bg-white/60 dark:hover:bg-white/5">
                      <td className="py-1 px-2 font-semibold text-zinc-900 dark:text-zinc-100">{row.timeframe}</td>
                      <td
                        className={`py-1 px-2 font-medium ${
                          row.trend === "Bullish"
                            ? "text-emerald-600 dark:text-emerald-400"
                            : row.trend === "Bearish"
                            ? "text-rose-600 dark:text-rose-400"
                            : "text-zinc-500"
                        }`}
                      >
                        {row.trend}
                      </td>
                      <td className="py-1 px-2 text-zinc-700 dark:text-zinc-300">{row.structure}</td>
                      <td className="py-1 px-2 text-zinc-700 dark:text-zinc-300">{row.momentum}</td>
                      <td
                        className={`py-1 px-2 font-semibold ${
                          row.bias === "Bullish"
                            ? "text-emerald-600 dark:text-emerald-400"
                            : row.bias === "Bearish"
                            ? "text-rose-600 dark:text-rose-400"
                            : "text-zinc-500"
                        }`}
                      >
                        {row.bias}
                      </td>
                      <td className="py-1 px-2 text-zinc-800 dark:text-zinc-200">
                        {row.last_price ? row.last_price.toFixed(2) : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* Actionable Strategic Signals Grid */}
          <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-5 gap-2 text-[11px]">
            {/* 1. Market Structure */}
            <div className="rounded-lg border border-zinc-100 dark:border-[#2a2e39] bg-zinc-50/50 dark:bg-[#1e222d]/60 p-2.5">
              <span className="block text-[10px] uppercase font-semibold text-zinc-400 dark:text-zinc-500">Structure</span>
              <span className="block font-medium text-zinc-800 dark:text-zinc-200 break-words mt-0.5">
                {decision?.market_structure || "Analyzing..."}
              </span>
            </div>

            {/* 2. VWAP Status */}
            <div className="rounded-lg border border-zinc-100 dark:border-[#2a2e39] bg-zinc-50/50 dark:bg-[#1e222d]/60 p-2.5">
              <span className="block text-[10px] uppercase font-semibold text-zinc-400 dark:text-zinc-500">VWAP Positioning</span>
              <span className="block font-medium text-zinc-800 dark:text-zinc-200 break-words mt-0.5">
                {decision?.vwap_position || "VWAP Active"}
              </span>
            </div>

            {/* 3. Nearest Support */}
            <div className="rounded-lg border border-emerald-100 dark:border-emerald-900/40 bg-emerald-50/30 dark:bg-emerald-950/20 p-2.5">
              <span className="block text-[10px] uppercase font-semibold text-emerald-700 dark:text-emerald-400">Nearest Support</span>
              <span className="block font-medium text-emerald-900 dark:text-emerald-200 break-words mt-0.5">
                {decision?.nearest_support || "—"}
              </span>
            </div>

            {/* 4. Nearest Resistance */}
            <div className="rounded-lg border border-rose-100 dark:border-rose-900/40 bg-rose-50/30 dark:bg-rose-950/20 p-2.5">
              <span className="block text-[10px] uppercase font-semibold text-rose-700 dark:text-rose-400">Nearest Resistance</span>
              <span className="block font-medium text-rose-900 dark:text-rose-200 break-words mt-0.5">
                {decision?.nearest_resistance || "—"}
              </span>
            </div>

            {/* 5. Nearest Liquidity Pool */}
            <div className="rounded-lg border border-amber-100 dark:border-amber-900/40 bg-amber-50/30 dark:bg-amber-950/20 p-2.5">
              <span className="block text-[10px] uppercase font-semibold text-amber-700 dark:text-amber-400">Liquidity Pool</span>
              <span className="block font-medium text-amber-900 dark:text-amber-200 break-words mt-0.5">
                {decision?.nearest_liquidity_pool || "—"}
              </span>
            </div>

            {/* 6. Momentum Condition */}
            <div className="rounded-lg border border-zinc-100 dark:border-[#2a2e39] bg-zinc-50/50 dark:bg-[#1e222d]/60 p-2.5">
              <span className="block text-[10px] uppercase font-semibold text-zinc-400 dark:text-zinc-500">Momentum Condition</span>
              <span className="block font-medium text-zinc-800 dark:text-zinc-200 break-words mt-0.5">
                {decision?.momentum_condition || "Neutral"}
              </span>
            </div>

            {/* 7. Action / Continuation vs Reversal */}
            <div className="col-span-2 md:col-span-3 lg:col-span-4 rounded-lg border border-indigo-100 dark:border-indigo-900/50 bg-indigo-50/30 dark:bg-indigo-950/20 p-2.5 flex flex-wrap items-center justify-between gap-2">
              <div>
                <span className="block text-[10px] uppercase font-semibold text-indigo-700 dark:text-indigo-400">Tactical Strategy Bias</span>
                <span className="block font-semibold text-indigo-950 dark:text-indigo-200 text-[12px] mt-0.5">
                  {decision?.reversal_or_continuation || "Evaluating Price Action near Key Levels..."}
                </span>
              </div>
              {currentPrice && (
                <span className="text-[11px] text-indigo-600 dark:text-indigo-300 font-mono font-semibold">
                  Spot {currentPrice.toFixed(2)}
                </span>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
