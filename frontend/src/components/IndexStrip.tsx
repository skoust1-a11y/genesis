"use client";

import { pct } from "@/lib/api";
import { INDICES, type Quote, signed } from "@/lib/market";

export function IndexStrip({
  quotes,
  selected,
  onSelect,
}: {
  quotes: Record<string, Quote>;
  selected?: string;
  onSelect: (symbol: string) => void;
}) {
  return (
    <section className="flex gap-2 overflow-x-auto pb-1">
      {INDICES.map((s) => {
        const q = quotes[s];
        return (
          <button
            key={s}
            onClick={() => onSelect(s)}
            className={`min-w-[150px] rounded-lg border bg-white px-3 py-2 text-left shadow-sm ${
              selected === s ? "border-emerald-500 ring-1 ring-emerald-500" : "hover:border-zinc-400"
            }`}
          >
            <div className="text-[11px] font-medium text-zinc-500">{s}</div>
            <div className="font-semibold">{q ? q.price.toLocaleString("en-IN") : "—"}</div>
            <div className={`text-xs ${signed(q?.change)}`}>
              {q?.change != null ? `${q.change > 0 ? "+" : ""}${q.change.toFixed(2)} (${pct(q.change_pct)})` : "—"}
            </div>
          </button>
        );
      })}
    </section>
  );
}
