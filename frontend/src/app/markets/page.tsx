"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { CandleChart, type Level } from "@/components/CandleChart";
import { IndexStrip } from "@/components/IndexStrip";
import { api, inr, pct } from "@/lib/api";
import {
  COMMODITIES, INDICES, type Position, type Quote, type Segment, fetchQuotes, money, segmentOf, signed,
} from "@/lib/market";

type Bar = { date: string; open: number | null; high: number | null; low: number | null; close: number; volume: number | null };

type Instrument = "EQ" | "FUT" | "CE" | "PE";

type Ticket = {
  instrument: Instrument;
  side: "BUY" | "SELL";
  lots: number;
  price: string;
  strike: string;
  expiry: string;
  note: string;
};

const DEFAULT_STOCKS = ["RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "SBIN", "BHARTIARTL", "ITC", "LT", "AXISBANK"];
const STOCKS_KEY = "tsl.watchlist.stocks";

// NSE symbols do not render in TradingView's free embeds, so live charts open on their site.
const TV_SYMBOL: Record<string, string> = {
  NIFTY: "NSE:NIFTY", BANKNIFTY: "NSE:BANKNIFTY", FINNIFTY: "NSE:CNXFINANCE", MIDCAP50: "NSE:NIFTYMIDCAP50",
  NIFTYIT: "NSE:CNXIT", SENSEX: "BSE:SENSEX", INDIAVIX: "NSE:INDIAVIX", GOLD: "MCX:GOLD1!", SILVER: "MCX:SILVER1!",
  CRUDEOIL: "MCX:CRUDEOIL1!", NATURALGAS: "MCX:NATURALGAS1!", COPPER: "MCX:COPPER1!", USDINR: "FX_IDC:USDINR",
};
const GROWW: Record<string, string> = {
  NIFTY: "https://groww.in/options/nifty",
  BANKNIFTY: "https://groww.in/options/nifty-bank",
};
const TIMEFRAMES = [
  ["1m", "1m"],
  ["5m", "5m"],
  ["15m", "15m"],
  ["60m", "1h"],
  ["1d", "1D"],
];

const blankTicket = (segment: Segment): Ticket => ({
  instrument: segment === "stock" ? "EQ" : "FUT", side: "BUY", lots: 1, price: "", strike: "", expiry: "", note: "",
});

export default function Markets() {
  const [tab, setTab] = useState<Segment>("index");
  const [stocks, setStocks] = useState<string[]>(DEFAULT_STOCKS);
  const [newStock, setNewStock] = useState("");
  const [quotes, setQuotes] = useState<Record<string, Quote>>({});
  const [symbol, setSymbol] = useState("NIFTY");
  const [timeframe, setTimeframe] = useState("5m");
  const [bars, setBars] = useState<Bar[]>([]);
  const [levels, setLevels] = useState<Level[]>([]);
  const [positions, setPositions] = useState<Position[]>([]);
  const [ticket, setTicket] = useState<Ticket>(blankTicket("index"));
  const [error, setError] = useState<string | null>(null);
  const [placed, setPlaced] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const segment = segmentOf(symbol);
  const quote = quotes[symbol];

  useEffect(() => {
    const saved = window.localStorage.getItem(STOCKS_KEY);
    if (saved) setStocks(JSON.parse(saved));
    // The dashboard links here as /markets?symbol=X[&instrument=&side=&lots=&price=&strike=&expiry=&note=] to prefill an exit.
    const q = new URLSearchParams(window.location.search);
    const s = q.get("symbol")?.toUpperCase();
    if (!s || !/^[A-Z0-9&-]{1,20}$/.test(s)) return;
    const seg = segmentOf(s);
    setSymbol(s);
    setTab(seg);
    if (seg === "stock") setStocks((list) => (list.includes(s) ? list : [...list, s]));
    const t = blankTicket(seg);
    const instrument = q.get("instrument") as Instrument | null;
    const side = q.get("side");
    setTicket({
      instrument: instrument && ["EQ", "FUT", "CE", "PE"].includes(instrument) ? instrument : t.instrument,
      side: side === "SELL" ? "SELL" : "BUY",
      lots: Math.max(1, Number(q.get("lots")) || 1),
      price: q.get("price") ?? "",
      strike: q.get("strike") ?? "",
      expiry: q.get("expiry") ?? "",
      note: q.get("note") ?? "",
    });
  }, []);

  useEffect(() => {
    const load = () =>
      fetchQuotes([...INDICES, ...stocks, ...COMMODITIES])
        .then(setQuotes)
        .catch((e) => setError(e instanceof Error ? e.message : "Could not load quotes"));
    load();
    const timer = setInterval(load, 60_000);
    return () => clearInterval(timer);
  }, [stocks]);

  useEffect(() => {
    const load = () =>
      api<{ bars: Bar[]; levels: Level[] }>(`/api/market/candles?symbol=${encodeURIComponent(symbol)}&interval=${timeframe}`)
        .then((d) => {
          setBars(d.bars);
          setLevels(d.levels ?? []);
        })
        .catch((e) => setError(e instanceof Error ? e.message : "Could not load candles"));
    setBars([]);
    setLevels([]);
    load();
    const timer = setInterval(load, 60_000);
    return () => clearInterval(timer);
  }, [symbol, timeframe]);

  const loadPositions = () =>
    api<{ positions: Position[] }>("/api/paper/positions")
      .then((d) => setPositions(d.positions))
      .catch(() => setPositions([]));
  useEffect(() => {
    loadPositions();
  }, []);

  // Option entries are premiums, not prices on this chart, so only shares and futures get an entry line.
  const chartLevels = useMemo<Level[]>(
    () => [
      ...levels,
      ...positions
        .filter((p) => p.underlying === symbol && p.quantity !== 0 && (p.instrument === "EQ" || p.instrument === "FUT"))
        .map((p) => ({ label: `Entry ${p.instrument} ${p.quantity > 0 ? "long" : "short"}`, price: p.avg_price })),
    ],
    [levels, positions, symbol],
  );

  const lastPrice = bars.at(-1)?.close ?? quote?.price;
  useEffect(() => {
    if ((ticket.instrument === "FUT" || ticket.instrument === "EQ") && lastPrice)
      setTicket((t) => ({ ...t, price: lastPrice.toFixed(2) }));
  }, [lastPrice, ticket.instrument]);

  function select(s: string) {
    setSymbol(s);
    setTicket(blankTicket(segmentOf(s)));
    setPlaced(null);
  }

  function saveStocks(next: string[]) {
    setStocks(next);
    window.localStorage.setItem(STOCKS_KEY, JSON.stringify(next));
  }

  function addStock() {
    const s = newStock.trim().toUpperCase();
    if (!/^[A-Z0-9&-]{1,20}$/.test(s)) return setError("Enter an NSE symbol, e.g. TATASTEEL");
    if (!stocks.includes(s)) saveStocks([...stocks, s]);
    setNewStock("");
    select(s);
  }

  async function placeOrder() {
    setBusy(true);
    setError(null);
    try {
      const option = ticket.instrument === "CE" || ticket.instrument === "PE";
      await api("/api/paper/orders", {
        body: {
          underlying: symbol,
          instrument: ticket.instrument,
          side: ticket.side,
          lots: ticket.lots,
          price: Number(ticket.price),
          strike: option ? Number(ticket.strike) || null : null,
          expiry: option && ticket.expiry ? ticket.expiry : null,
          note: ticket.note || null,
        },
      });
      setPlaced(`${ticket.side} ${ticket.lots} ${symbol} ${ticket.instrument} @ ${ticket.price}`);
      setTicket((t) => ({ ...t, note: "" }));
      loadPositions();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Order failed");
    } finally {
      setBusy(false);
    }
  }

  const lotSize = segment === "stock" ? 1 : quote?.lot_size ?? 0;
  const value = Number(ticket.price || 0) * ticket.lots * lotSize;
  const list = tab === "index" ? INDICES : tab === "stock" ? stocks : COMMODITIES;
  const tvSymbol = TV_SYMBOL[symbol] ?? `NSE:${symbol}`;

  return (
    <div className="mx-auto max-w-[1400px] space-y-4 px-4 py-4">
      <IndexStrip quotes={quotes} selected={symbol} onSelect={select} />

      {error && (
        <div className="flex items-start justify-between rounded-lg border border-rose-200 bg-rose-50 px-4 py-2 text-sm text-rose-900">
          {error}
          <button onClick={() => setError(null)} aria-label="Dismiss">
            ×
          </button>
        </div>
      )}

      <section className="grid gap-4 lg:grid-cols-[220px_1fr] xl:grid-cols-[220px_1fr_280px]">
        <aside className="rounded-xl border bg-white shadow-sm">
          <div className="grid grid-cols-3 border-b text-xs">
            {(
              [
                ["index", "Indices"],
                ["stock", "Stocks"],
                ["commodity", "Commodities"],
              ] as const
            ).map(([k, label]) => (
              <button
                key={k}
                onClick={() => setTab(k)}
                className={`py-2 ${tab === k ? "border-b-2 border-emerald-600 font-medium text-emerald-700" : "text-zinc-600"}`}
              >
                {label}
              </button>
            ))}
          </div>
          {tab === "stock" && (
            <form
              className="flex gap-1 border-b p-2"
              onSubmit={(e) => {
                e.preventDefault();
                addStock();
              }}
            >
              <input
                className="w-full rounded-md border px-2 py-1 text-sm uppercase"
                placeholder="Add NSE symbol"
                value={newStock}
                onChange={(e) => setNewStock(e.target.value)}
              />
              <button className="rounded-md bg-zinc-900 px-2 text-sm text-white">Add</button>
            </form>
          )}
          {tab === "commodity" && (
            <p className="border-b px-3 py-2 text-[11px] text-zinc-500">
              Global futures (COMEX/NYMEX) in USD. MCX rupee prices need a broker feed.
            </p>
          )}
          <ul className="max-h-[520px] overflow-y-auto">
            {list.map((s) => {
              const q = quotes[s];
              return (
                <li key={s} className="group flex items-center">
                  <button
                    onClick={() => select(s)}
                    className={`flex flex-1 items-center justify-between px-3 py-2 text-left text-sm ${
                      symbol === s ? "bg-emerald-50" : "hover:bg-zinc-50"
                    }`}
                  >
                    <span>
                      <span className="block font-medium">{s}</span>
                      <span className="block max-w-[120px] truncate text-[11px] text-zinc-500">{q?.name ?? ""}</span>
                    </span>
                    <span className="text-right">
                      <span className="block">{q ? money(q.price, q.currency) : "—"}</span>
                      <span className={`block text-[11px] ${signed(q?.change)}`}>{q ? pct(q.change_pct) : ""}</span>
                    </span>
                  </button>
                  {tab === "stock" && (
                    <button
                      className="px-2 text-zinc-400 opacity-0 hover:text-rose-600 group-hover:opacity-100"
                      aria-label={`Remove ${s}`}
                      onClick={() => saveStocks(stocks.filter((x) => x !== s))}
                    >
                      ×
                    </button>
                  )}
                </li>
              );
            })}
          </ul>
        </aside>

        <div className="space-y-3 rounded-xl border bg-white p-4 shadow-sm">
          <div className="flex flex-wrap items-start gap-3">
            <div className="mr-auto">
              <h2 className="text-lg font-semibold">
                {symbol} <span className="text-sm font-normal text-zinc-500">{quote?.name}</span>
              </h2>
              <div className="flex items-baseline gap-2">
                <span className="text-2xl font-semibold">{money(quote?.price, quote?.currency)}</span>
                <span className={`text-sm ${signed(quote?.change)}`}>
                  {quote?.change != null && `${quote.change > 0 ? "+" : ""}${quote.change.toFixed(2)} (${pct(quote.change_pct)})`}
                </span>
              </div>
              <div className="text-[11px] text-zinc-500">
                {quote?.time ? `Last update ${quote.time} IST` : ""} · Yahoo Finance, delayed · refreshes every minute
              </div>
            </div>
            <div className="flex flex-wrap gap-1">
              {TIMEFRAMES.map(([tf, label]) => (
                <button
                  key={tf}
                  onClick={() => setTimeframe(tf)}
                  className={`rounded-md px-2 py-1 text-sm ${timeframe === tf ? "bg-zinc-900 text-white" : "border"}`}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
          <CandleChart bars={bars} levels={chartLevels} />
          <div className="flex flex-wrap gap-2">
            <a
              className="rounded-md border px-3 py-1 text-sm hover:bg-zinc-50"
              href={`https://www.tradingview.com/chart/?symbol=${encodeURIComponent(tvSymbol)}`}
              target="_blank"
              rel="noreferrer"
            >
              {segment === "commodity" && symbol !== "USDINR" ? "MCX chart on TradingView ↗" : "Live chart on TradingView ↗"}
            </a>
            {GROWW[symbol] && (
              <a
                className="rounded-md bg-emerald-600 px-3 py-1 text-sm text-white hover:bg-emerald-700"
                href={GROWW[symbol]}
                target="_blank"
                rel="noreferrer"
              >
                Trade real money on Groww ↗
              </a>
            )}
          </div>
        </div>

        <div className="space-y-4 rounded-xl border bg-white p-4 shadow-sm lg:col-start-2 xl:col-start-auto">
          <div className="flex items-center justify-between">
            <h2 className="font-medium">Paper order · {symbol}</h2>
            <span className="text-xs text-zinc-500">No real money</span>
          </div>
          {segment === "commodity" ? (
            <p className="text-sm text-zinc-600">
              Commodities are view-only for now: these are global USD prices, not MCX contracts. Paper trading opens
              once an MCX price feed (Groww or Kite API) is connected.
            </p>
          ) : symbol === "INDIAVIX" ? (
            <p className="text-sm text-zinc-600">India VIX is a volatility gauge and cannot be traded.</p>
          ) : (
            <>
              <div className="grid grid-cols-2 gap-2">
                {(["BUY", "SELL"] as const).map((s) => (
                  <button
                    key={s}
                    onClick={() => setTicket((t) => ({ ...t, side: s }))}
                    className={`rounded-md py-2 font-medium ${
                      ticket.side === s
                        ? s === "BUY" ? "bg-emerald-600 text-white" : "bg-rose-600 text-white"
                        : "border text-zinc-700"
                    }`}
                  >
                    {s}
                  </button>
                ))}
              </div>
              {segment === "index" && (
                <div className="grid grid-cols-3 gap-2">
                  {(["FUT", "CE", "PE"] as const).map((k) => (
                    <button
                      key={k}
                      onClick={() => setTicket((t) => ({ ...t, instrument: k }))}
                      className={`rounded-md py-1 text-sm ${ticket.instrument === k ? "bg-zinc-900 text-white" : "border"}`}
                    >
                      {k === "FUT" ? "Futures" : k === "CE" ? "Call" : "Put"}
                    </button>
                  ))}
                </div>
              )}
              {(ticket.instrument === "CE" || ticket.instrument === "PE") && (
                <div className="grid grid-cols-2 gap-2">
                  <label className="text-xs text-zinc-600">
                    Strike
                    <input
                      className="mt-1 w-full rounded-md border px-2 py-1 text-sm"
                      inputMode="decimal"
                      value={ticket.strike}
                      onChange={(e) => setTicket((t) => ({ ...t, strike: e.target.value }))}
                    />
                  </label>
                  <label className="text-xs text-zinc-600">
                    Expiry
                    <input
                      type="date"
                      className="mt-1 w-full rounded-md border px-2 py-1 text-sm"
                      value={ticket.expiry}
                      onChange={(e) => setTicket((t) => ({ ...t, expiry: e.target.value }))}
                    />
                  </label>
                </div>
              )}
              <div className="grid grid-cols-2 gap-2">
                <label className="text-xs text-zinc-600">
                  {segment === "stock" ? "Quantity (shares)" : "Lots"}
                  <input
                    type="number"
                    min={1}
                    className="mt-1 w-full rounded-md border px-2 py-1 text-sm"
                    value={ticket.lots}
                    onChange={(e) => setTicket((t) => ({ ...t, lots: Math.max(1, Number(e.target.value) || 1) }))}
                  />
                </label>
                <label className="text-xs text-zinc-600">
                  {ticket.instrument === "CE" || ticket.instrument === "PE" ? "Premium" : "Price"}
                  <input
                    className="mt-1 w-full rounded-md border px-2 py-1 text-sm"
                    inputMode="decimal"
                    value={ticket.price}
                    onChange={(e) => setTicket((t) => ({ ...t, price: e.target.value }))}
                  />
                </label>
              </div>
              <label className="block text-xs text-zinc-600">
                Why this trade? (setup, stop, target)
                <textarea
                  className="mt-1 w-full rounded-md border px-2 py-1 text-sm"
                  rows={2}
                  value={ticket.note}
                  onChange={(e) => setTicket((t) => ({ ...t, note: e.target.value }))}
                />
              </label>
              <div className="text-xs text-zinc-600">
                {segment === "stock"
                  ? `${ticket.lots} shares · value ${inr(value)} · delivery charges`
                  : `${ticket.lots} × ${lotSize || "—"} qty · value ${inr(value)}`}
                <br />
                Fills at the price you enter (prefilled with the last candle&apos;s close).
              </div>
              <button
                disabled={busy || !Number(ticket.price) || !lotSize}
                onClick={placeOrder}
                className={`w-full rounded-md py-2 font-medium text-white disabled:opacity-50 ${
                  ticket.side === "BUY" ? "bg-emerald-600" : "bg-rose-600"
                }`}
              >
                {ticket.side === "BUY" ? "Buy" : "Sell"} {ticket.lots}{" "}
                {segment === "stock" ? "shares" : `lot${ticket.lots > 1 ? "s" : ""}`} (paper)
              </button>
              {!lotSize && segment === "index" && (
                <p className="text-xs text-amber-800">No lot size configured for {symbol}; add it to LOT_SIZES.</p>
              )}
              {placed && (
                <p className="rounded-md bg-emerald-50 px-2 py-1 text-xs text-emerald-800">
                  Placed: {placed}.{" "}
                  <Link href="/" className="underline">
                    View positions on Dashboard
                  </Link>
                </p>
              )}
            </>
          )}
        </div>
      </section>
    </div>
  );
}
