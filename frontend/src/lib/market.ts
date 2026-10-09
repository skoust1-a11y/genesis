import { api } from "@/lib/api";

export type Segment = "index" | "stock" | "commodity";

export type Quote = {
  symbol: string;
  segment: Segment;
  name: string;
  currency: string;
  price: number;
  change: number | null;
  change_pct: number | null;
  time: string | null;
  lot_size: number | null;
};

export type Position = {
  underlying: string;
  instrument: string;
  strike: number | null;
  expiry: string | null;
  lot_size: number;
  quantity: number;
  avg_price: number;
  realized_pnl: number;
  unrealized_pnl: number;
  charges: number;
  net_pnl: number;
  mark_price: number | null;
  mark_time: string | null;
};

export type Order = {
  id: string;
  underlying: string;
  instrument: string;
  strike: number | null;
  expiry: string | null;
  side: "BUY" | "SELL";
  lots: number;
  lot_size: number;
  price: number;
  charges: { total: number };
  note: string | null;
  created_at: string;
};

export const INDICES = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCAP50", "NIFTYIT", "SENSEX", "INDIAVIX"];
export const COMMODITIES = ["GOLD", "SILVER", "CRUDEOIL", "NATURALGAS", "COPPER", "USDINR"];

export const segmentOf = (s: string): Segment =>
  INDICES.includes(s) ? "index" : COMMODITIES.includes(s) ? "commodity" : "stock";

export const money = (v: number | null | undefined, currency = "INR") =>
  v == null ? "—" : `${currency === "USD" ? "$" : "₹"}${v.toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;

export const contract = (o: { underlying: string; instrument: string; strike: number | null; expiry: string | null }) =>
  o.instrument === "EQ"
    ? `${o.underlying} · shares`
    : o.instrument === "FUT"
      ? `${o.underlying} FUT`
      : `${o.underlying} ${o.strike ?? ""} ${o.instrument}${o.expiry ? ` · ${o.expiry}` : ""}`;

export const signed = (v: number | null | undefined) =>
  v == null || v === 0 ? "text-zinc-700" : v > 0 ? "text-emerald-700" : "text-rose-700";

export async function fetchQuotes(symbols: string[]) {
  const d = await api<{ quotes: Quote[] }>(`/api/market/quotes?symbols=${encodeURIComponent(symbols.join(","))}`);
  return Object.fromEntries(d.quotes.map((q) => [q.symbol, q])) as Record<string, Quote>;
}
