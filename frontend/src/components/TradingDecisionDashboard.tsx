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

  const classification = regime?.classification || decision?.market_regime || "Sideways / Consolidation";
  const score = regime?.score ?? 0;

  const isBull = classification.toLowerCase().includes("bullish");
  const isBear = classification.toLowerCase().includes("bearish");
  const isStrong = classification.toLowerCase().includes("strong");

  const badgeColor = isBull
    ? isStrong
      ? "bg-emerald-600 text-white border-emerald-700"
      : "bg-emerald-50 text-emerald-800 border-emerald-300"
    : isBear
    ? isStrong
      ? "bg-rose-600 text-white border-rose-700"
      : "bg-rose-50 text-rose-800 border-rose-300"
    : "bg-amber-50 text-amber-800 border-amber-300";

  return (
    <div className="rounded-xl border border-zinc-200 bg-white p-3 shadow-xs space-y-3 font-sans text-xs">
      {/* Top Banner: Regime Badge, Score, Quick Action */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-100 pb-2.5">
        <div className="flex items-center gap-2">
          <span className="text-[11px] font-semibold uppercase tracking-wider text-zinc-500">
            Regime ({symbol}):
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
              className="text-[11px] text-zinc-500 hover:text-zinc-800 underline underline-offset-2"
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
                  ? "border-emerald-200 bg-emerald-50/50 text-emerald-700"
                  : mtf.alignment.includes("Bearish")
                  ? "border-rose-200 bg-rose-50/50 text-rose-700"
                  : "border-zinc-200 bg-zinc-50 text-zinc-700"
              }`}
            >
              MTF: {mtf.alignment}
            </span>
          )}
          <button
            onClick={() => setShowMTF(!showMTF)}
            className={`rounded-md border px-2 py-0.5 text-[11px] font-medium transition ${
              showMTF ? "bg-zinc-900 text-white border-zinc-900" : "text-zinc-600 hover:bg-zinc-50 border-zinc-200"
            }`}
          >
            MTF Matrix {showMTF ? "▲" : "▼"}
          </button>
        </div>
      </div>

      {/* Explanatory Factors Dropdown */}
      {showReasons && regime?.reasons && (
        <div className="rounded-lg border border-zinc-100 bg-zinc-50/80 p-2 text-[11px] text-zinc-700 space-y-1">
          <div className="font-semibold text-zinc-800">Scoring Formula Factors (Scale: -10 to +10):</div>
          <ul className="list-disc pl-4 space-y-0.5 text-zinc-600">
            {regime.reasons.map((r, i) => (
              <li key={i}>{r}</li>
            ))}
          </ul>
        </div>
      )}

      {/* Synchronized MTF Matrix */}
      {showMTF && mtf?.matrix && (
        <div className="overflow-x-auto rounded-lg border border-zinc-200 bg-zinc-50/50 p-2 text-xs space-y-2">
          <div className="flex items-center justify-between">
            <span className="font-semibold text-zinc-800">Synchronized Multi-Timeframe Matrix</span>
            {mtf.conflict_notes && mtf.conflict_notes.length > 0 && (
              <span className="text-[11px] text-amber-700 font-medium">
                {mtf.conflict_notes[0]}
              </span>
            )}
          </div>
          <table className="w-full border-collapse font-mono text-[11px]">
            <thead>
              <tr className="border-b border-zinc-200 text-zinc-500 text-left">
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
                <tr key={row.timeframe} className="border-b border-zinc-100 hover:bg-white/60">
                  <td className="py-1 px-2 font-semibold text-zinc-900">{row.timeframe}</td>
                  <td
                    className={`py-1 px-2 font-medium ${
                      row.trend === "Bullish"
                        ? "text-emerald-700"
                        : row.trend === "Bearish"
                        ? "text-rose-700"
                        : "text-zinc-600"
                    }`}
                  >
                    {row.trend}
                  </td>
                  <td className="py-1 px-2 text-zinc-700">{row.structure}</td>
                  <td className="py-1 px-2 text-zinc-700">{row.momentum}</td>
                  <td
                    className={`py-1 px-2 font-semibold ${
                      row.bias === "Bullish"
                        ? "text-emerald-700"
                        : row.bias === "Bearish"
                        ? "text-rose-700"
                        : "text-zinc-500"
                    }`}
                  >
                    {row.bias}
                  </td>
                  <td className="py-1 px-2 text-zinc-800">
                    {row.last_price ? row.last_price.toFixed(2) : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* 10-Point Actionable Decision Grid */}
      <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-5 gap-2 text-[11px]">
        {/* 1. Market Structure */}
        <div className="rounded-lg border border-zinc-100 bg-zinc-50/40 p-2">
          <span className="block text-[10px] uppercase font-semibold text-zinc-400">Structure</span>
          <span className="block font-medium text-zinc-800 truncate" title={decision?.market_structure}>
            {decision?.market_structure || "Analyzing..."}
          </span>
        </div>

        {/* 2. VWAP Status */}
        <div className="rounded-lg border border-zinc-100 bg-zinc-50/40 p-2">
          <span className="block text-[10px] uppercase font-semibold text-zinc-400">VWAP Positioning</span>
          <span className="block font-medium text-zinc-800 truncate" title={decision?.vwap_position}>
            {decision?.vwap_position || "VWAP Active"}
          </span>
        </div>

        {/* 3. Nearest Support */}
        <div className="rounded-lg border border-emerald-100 bg-emerald-50/30 p-2">
          <span className="block text-[10px] uppercase font-semibold text-emerald-700">Nearest Support</span>
          <span className="block font-medium text-emerald-900 truncate" title={decision?.nearest_support}>
            {decision?.nearest_support || "—"}
          </span>
        </div>

        {/* 4. Nearest Resistance */}
        <div className="rounded-lg border border-rose-100 bg-rose-50/30 p-2">
          <span className="block text-[10px] uppercase font-semibold text-rose-700">Nearest Resistance</span>
          <span className="block font-medium text-rose-900 truncate" title={decision?.nearest_resistance}>
            {decision?.nearest_resistance || "—"}
          </span>
        </div>

        {/* 5. Nearest Liquidity Pool */}
        <div className="rounded-lg border border-amber-100 bg-amber-50/30 p-2">
          <span className="block text-[10px] uppercase font-semibold text-amber-700">Liquidity Pool</span>
          <span className="block font-medium text-amber-900 truncate" title={decision?.nearest_liquidity_pool}>
            {decision?.nearest_liquidity_pool || "—"}
          </span>
        </div>

        {/* 6. Momentum Condition */}
        <div className="rounded-lg border border-zinc-100 bg-zinc-50/40 p-2">
          <span className="block text-[10px] uppercase font-semibold text-zinc-400">Momentum Condition</span>
          <span className="block font-medium text-zinc-800 truncate" title={decision?.momentum_condition}>
            {decision?.momentum_condition || "Neutral"}
          </span>
        </div>

        {/* 7. Action / Continuation vs Reversal */}
        <div className="col-span-2 md:col-span-3 lg:col-span-4 rounded-lg border border-indigo-100 bg-indigo-50/30 p-2 flex items-center justify-between">
          <div>
            <span className="block text-[10px] uppercase font-semibold text-indigo-700">Tactical Strategy Bias</span>
            <span className="block font-semibold text-indigo-950">
              {decision?.reversal_or_continuation || "Evaluating Price Action near Key Levels..."}
            </span>
          </div>
          <span className="text-[11px] text-indigo-600 font-mono">
            {currentPrice ? `Spot ${currentPrice.toFixed(2)}` : ""}
          </span>
        </div>
      </div>
    </div>
  );
}
