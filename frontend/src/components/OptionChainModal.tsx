"use client";

import { useEffect, useRef } from "react";
import { money, signed, pct } from "@/lib/market";

export type OptionChainRow = {
  strike: number;
  is_atm: boolean;
  ce: {
    contract_id?: string;
    ltp: number | null;
    close: number | null;
    change: number | null;
    change_pct: number | null;
    oi: number | null;
    iv: number | null;
    delta: number | null;
    theta?: number | null;
  } | null;
  pe: {
    contract_id?: string;
    ltp: number | null;
    close: number | null;
    change: number | null;
    change_pct: number | null;
    oi: number | null;
    iv: number | null;
    delta: number | null;
    theta?: number | null;
  } | null;
};

export type OptionChainData = {
  underlying: string;
  spot: number;
  lot_size: number;
  current_expiry: string;
  expiries: string[];
  atm_strike: number;
  strike_step: number;
  strikes: OptionChainRow[];
};

type OptionChainModalProps = {
  isOpen: boolean;
  onClose: () => void;
  chain: OptionChainData | null;
  symbol: string;
  selectedExpiry: string | null;
  onSelectExpiry: (exp: string) => void;
  selectedStrike: number | null;
  selectedOptionType: "CE" | "PE";
  onSelectContract: (strike: number, type: "CE" | "PE") => void;
  loading?: boolean;
};

export function OptionChainModal({
  isOpen,
  onClose,
  chain,
  symbol,
  selectedExpiry,
  onSelectExpiry,
  selectedStrike,
  selectedOptionType,
  onSelectContract,
  loading = false,
}: OptionChainModalProps) {
  const atmRef = useRef<HTMLTableRowElement | null>(null);

  useEffect(() => {
    if (isOpen && atmRef.current) {
      setTimeout(() => {
        atmRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
      }, 100);
    }
  }, [isOpen, chain]);

  if (!isOpen) return null;

  const currentExpiry = selectedExpiry || chain?.current_expiry;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-xs p-4">
      <div className="relative flex max-h-[90vh] w-full max-w-5xl flex-col rounded-2xl border border-zinc-200 bg-white shadow-2xl overflow-hidden">
        {/* Header */}
        <div className="flex flex-wrap items-center justify-between gap-4 border-b border-zinc-100 bg-zinc-50/80 px-6 py-4">
          <div>
            <div className="flex items-center gap-2">
              <h3 className="text-lg font-bold text-zinc-900">{symbol} Option Chain</h3>
              <span className="rounded-full bg-emerald-100 px-2 py-0.5 text-xs font-semibold text-emerald-800">
                Live
              </span>
            </div>
            <div className="flex items-baseline gap-2 text-xs text-zinc-500 mt-0.5">
              <span>Spot: <strong className="text-zinc-800">{money(chain?.spot)}</strong></span>
              <span>·</span>
              <span>ATM: <strong className="text-zinc-800">{chain?.atm_strike}</strong></span>
              <span>·</span>
              <span>Lot: <strong className="text-zinc-800">{chain?.lot_size}</strong></span>
            </div>
          </div>

          {/* Expiry Selector */}
          <div className="flex items-center gap-2">
            <span className="text-xs font-medium text-zinc-600">Expiry:</span>
            <div className="flex flex-wrap gap-1">
              {(chain?.expiries ?? []).slice(0, 4).map((exp) => (
                <button
                  key={exp}
                  onClick={() => onSelectExpiry(exp)}
                  className={`rounded-lg px-2.5 py-1 text-xs font-medium transition-all ${
                    currentExpiry === exp
                      ? "bg-zinc-900 text-white shadow-xs"
                      : "border border-zinc-200 bg-white text-zinc-700 hover:bg-zinc-100"
                  }`}
                >
                  {exp}
                </button>
              ))}
            </div>
          </div>

          <button
            onClick={onClose}
            className="rounded-lg p-1.5 text-zinc-400 hover:bg-zinc-200 hover:text-zinc-700 transition"
            aria-label="Close"
          >
            ✕
          </button>
        </div>

        {/* Content Table */}
        <div className="flex-1 overflow-auto">
          {loading && (
            <div className="flex h-64 items-center justify-center text-sm text-zinc-500">
              Loading option chain data...
            </div>
          )}

          {!loading && (!chain || !chain.strikes || chain.strikes.length === 0) && (
            <div className="flex h-64 items-center justify-center text-sm text-zinc-500">
              No option contracts available for {symbol} at expiry {currentExpiry}.
            </div>
          )}

          {!loading && chain && chain.strikes && chain.strikes.length > 0 && (
            <table className="w-full border-collapse text-left text-xs">
              <thead className="sticky top-0 z-10 border-b border-zinc-200 bg-zinc-100 font-semibold text-zinc-700 shadow-xs">
                <tr>
                  <th colSpan={5} className="bg-emerald-50/70 px-3 py-2 text-center text-emerald-900 border-r border-zinc-200">
                    CALLS (CE)
                  </th>
                  <th className="bg-zinc-200/80 px-4 py-2 text-center font-bold text-zinc-900">
                    STRIKE
                  </th>
                  <th colSpan={5} className="bg-rose-50/70 px-3 py-2 text-center text-rose-900 border-l border-zinc-200">
                    PUTS (PE)
                  </th>
                </tr>
                <tr className="border-b border-zinc-200 text-[11px] text-zinc-500">
                  <th className="px-3 py-1.5 text-right font-medium">OI</th>
                  <th className="px-2 py-1.5 text-right font-medium">IV</th>
                  <th className="px-2 py-1.5 text-right font-medium">Delta</th>
                  <th className="px-2 py-1.5 text-right font-medium">Chg %</th>
                  <th className="px-3 py-1.5 text-right font-bold text-emerald-900 border-r border-zinc-200">LTP</th>

                  <th className="px-4 py-1.5 text-center font-bold text-zinc-900 bg-zinc-100">Price</th>

                  <th className="px-3 py-1.5 text-left font-bold text-rose-900 border-l border-zinc-200">LTP</th>
                  <th className="px-2 py-1.5 text-left font-medium">Chg %</th>
                  <th className="px-2 py-1.5 text-left font-medium">Delta</th>
                  <th className="px-2 py-1.5 text-left font-medium">IV</th>
                  <th className="px-3 py-1.5 text-left font-medium">OI</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-100">
                {chain.strikes.map((row) => {
                  const isAtm = row.is_atm;
                  const isCeActive = selectedOptionType === "CE" && selectedStrike === row.strike;
                  const isPeActive = selectedOptionType === "PE" && selectedStrike === row.strike;
                  const isItmCall = chain.spot > row.strike;
                  const isItmPut = chain.spot < row.strike;

                  return (
                    <tr
                      key={row.strike}
                      ref={isAtm ? atmRef : null}
                      className={`transition-colors hover:bg-zinc-50/90 ${
                        isAtm ? "bg-amber-50/60 font-medium" : ""
                      }`}
                    >
                      {/* CE Details */}
                      <td className={`px-3 py-2 text-right text-zinc-500 ${isItmCall ? "bg-amber-50/20" : ""}`}>
                        {row.ce?.oi != null ? row.ce.oi.toLocaleString("en-IN") : "—"}
                      </td>
                      <td className={`px-2 py-2 text-right text-zinc-500 ${isItmCall ? "bg-amber-50/20" : ""}`}>
                        {row.ce?.iv != null ? `${row.ce.iv.toFixed(1)}%` : "—"}
                      </td>
                      <td className={`px-2 py-2 text-right text-zinc-600 ${isItmCall ? "bg-amber-50/20" : ""}`}>
                        {row.ce?.delta != null ? row.ce.delta.toFixed(2) : "—"}
                      </td>
                      <td className={`px-2 py-2 text-right ${signed(row.ce?.change_pct)} ${isItmCall ? "bg-amber-50/20" : ""}`}>
                        {row.ce?.change_pct != null ? pct(row.ce.change_pct) : "—"}
                      </td>
                      <td
                        onClick={() => {
                          if (row.ce) {
                            onSelectContract(row.strike, "CE");
                            onClose();
                          }
                        }}
                        className={`cursor-pointer px-3 py-2 text-right font-semibold border-r border-zinc-200 transition-colors ${
                          isCeActive
                            ? "bg-emerald-600 text-white font-bold"
                            : isItmCall
                            ? "bg-emerald-50/50 text-emerald-950 hover:bg-emerald-100"
                            : "text-emerald-700 hover:bg-emerald-50"
                        }`}
                      >
                        {row.ce?.ltp != null ? `₹${row.ce.ltp.toFixed(2)}` : "—"}
                      </td>

                      {/* STRIKE */}
                      <td className="px-4 py-2 text-center bg-zinc-50/60 font-bold text-zinc-900 whitespace-nowrap">
                        <div className="flex items-center justify-center gap-1.5">
                          <span>{row.strike}</span>
                          {isAtm && (
                            <span className="rounded bg-amber-500/20 px-1 py-0.2 text-[9px] font-extrabold text-amber-900 uppercase">
                              ATM
                            </span>
                          )}
                        </div>
                      </td>

                      {/* PE Details */}
                      <td
                        onClick={() => {
                          if (row.pe) {
                            onSelectContract(row.strike, "PE");
                            onClose();
                          }
                        }}
                        className={`cursor-pointer px-3 py-2 text-left font-semibold border-l border-zinc-200 transition-colors ${
                          isPeActive
                            ? "bg-rose-600 text-white font-bold"
                            : isItmPut
                            ? "bg-rose-50/50 text-rose-950 hover:bg-rose-100"
                            : "text-rose-700 hover:bg-rose-50"
                        }`}
                      >
                        {row.pe?.ltp != null ? `₹${row.pe.ltp.toFixed(2)}` : "—"}
                      </td>
                      <td className={`px-2 py-2 text-left ${signed(row.pe?.change_pct)} ${isItmPut ? "bg-amber-50/20" : ""}`}>
                        {row.pe?.change_pct != null ? pct(row.pe.change_pct) : "—"}
                      </td>
                      <td className={`px-2 py-2 text-left text-zinc-600 ${isItmPut ? "bg-amber-50/20" : ""}`}>
                        {row.pe?.delta != null ? row.pe.delta.toFixed(2) : "—"}
                      </td>
                      <td className={`px-2 py-2 text-left text-zinc-500 ${isItmPut ? "bg-amber-50/20" : ""}`}>
                        {row.pe?.iv != null ? `${row.pe.iv.toFixed(1)}%` : "—"}
                      </td>
                      <td className={`px-3 py-2 text-left text-zinc-500 ${isItmPut ? "bg-amber-50/20" : ""}`}>
                        {row.pe?.oi != null ? row.pe.oi.toLocaleString("en-IN") : "—"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>

        {/* Footer info */}
        <div className="flex items-center justify-between border-t border-zinc-200 bg-zinc-50 px-6 py-2.5 text-xs text-zinc-500">
          <span>Click any LTP (Call or Put) to instantly view its live candlestick chart.</span>
          <button
            onClick={() => {
              if (atmRef.current) {
                atmRef.current.scrollIntoView({ behavior: "smooth", block: "center" });
              }
            }}
            className="text-xs font-semibold text-emerald-700 hover:underline"
          >
            Scroll to ATM Strike ({chain?.atm_strike})
          </button>
        </div>
      </div>
    </div>
  );
}
