import { inr, pct } from "@/lib/api";

export type OddsBand = {
  band: string | null;
  trades: number;
  win_rate: number | null;
  breakeven_win_rate: number | null;
  avg_win: number | null;
  avg_loss: number | null;
  expectancy: number | null;
};

/** The odds gate's past trades per India VIX band, per lot after costs. */
export function OddsTable({ bands, current }: { bands: OddsBand[]; current?: string | null }) {
  const cell = (v: number | null, fmt: (n: number) => string) => (v === null ? "—" : fmt(v));
  return (
    <div className="overflow-x-auto">
      <table className="min-w-full text-sm">
        <thead>
          <tr className="border-b text-left">
            <th className="py-1.5 pr-4">Band</th>
            <th className="py-1.5 pr-4">Past trades</th>
            <th className="py-1.5 pr-4">Win rate vs needed</th>
            <th className="py-1.5 pr-4">Average win / loss</th>
            <th className="py-1.5 pr-4">Average net per lot</th>
          </tr>
        </thead>
        <tbody>
          {bands.map((b) => (
            <tr key={b.band ?? "none"} className={`border-b ${current && b.band === current ? "bg-indigo-50" : ""}`}>
              <td className="py-1.5 pr-4">{b.band ?? "no VIX"}</td>
              <td className="py-1.5 pr-4">{b.trades}</td>
              <td className="py-1.5 pr-4">
                {cell(b.win_rate, (n) => pct(n, 0))} vs {cell(b.breakeven_win_rate, (n) => pct(n, 0))}
              </td>
              <td className="py-1.5 pr-4">
                {cell(b.avg_win, inr)} / {cell(b.avg_loss, inr)}
              </td>
              <td className={`py-1.5 pr-4 ${b.expectancy !== null && b.expectancy > 0 ? "text-emerald-700" : "text-rose-700"}`}>
                {cell(b.expectancy, inr)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
