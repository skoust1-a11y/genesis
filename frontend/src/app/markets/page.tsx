"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import {
  CandleChart,
  type Level,
  type Zone,
  type SwingPoint,
  type StructureEvent,
  type TrendlineData,
  type LiquiditySweepData,
} from "@/components/CandleChart";
import { OptionChainModal, type OptionChainData } from "@/components/OptionChainModal";
import type { IndicatorData } from "@/components/MomentumPanel";
import type { DecisionData, RegimeData, MTFReportData } from "@/components/TradingDecisionDashboard";
import { IndexStrip } from "@/components/IndexStrip";
import { api, inr, pct } from "@/lib/api";
import { riskPlan } from "@/lib/risk";
import {
  COMMODITIES, INDICES, type Position, type Quote, type Segment, fetchQuotes, money, segmentOf, signed,
} from "@/lib/market";

type Bar = {
  date: string; open: number | null; high: number | null; low: number | null; close: number;
  volume: number | null; vwap?: number | null;
};

type OptionContractDetails = {
  underlying: string;
  strike: number;
  option_type: "CE" | "PE";
  expiry: string;
  ltp: number;
  close: number | null;
  change: number | null;
  change_pct: number | null;
  oi: number;
  iv_pct: number;
  delta: number | null;
  theta: number | null;
  gamma: number | null;
  vega: number | null;
  lot_size: number;
  spot: number;
};

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
  const [sensitivity, setSensitivity] = useState(2);
  const [bars, setBars] = useState<Bar[]>([]);
  const [levels, setLevels] = useState<Level[]>([]);
  const [zones, setZones] = useState<Zone[]>([]);
  const [marketStructure, setMarketStructure] = useState<{ swings: SwingPoint[]; events: StructureEvent[] } | undefined>();
  const [trendlines, setTrendlines] = useState<TrendlineData[]>([]);
  const [sweeps, setSweeps] = useState<LiquiditySweepData[]>([]);
  const [indicators, setIndicators] = useState<IndicatorData | undefined>();
  const [regime, setRegime] = useState<RegimeData | undefined>();
  const [decision, setDecision] = useState<DecisionData | undefined>();
  const [mtf, setMtf] = useState<MTFReportData | undefined>();
  const [positions, setPositions] = useState<Position[]>([]);
  const [ticket, setTicket] = useState<Ticket>(blankTicket("index"));
  const [error, setError] = useState<string | null>(null);
  const [placed, setPlaced] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Options State
  const [chartMode, setChartMode] = useState<"spot" | "option">("spot");
  const [optionType, setOptionType] = useState<"CE" | "PE">("CE");
  const [selectedStrike, setSelectedStrike] = useState<number | null>(null);
  const [selectedExpiry, setSelectedExpiry] = useState<string | null>(null);
  const [optionChain, setOptionChain] = useState<OptionChainData | null>(null);
  const [contractInfo, setContractInfo] = useState<OptionContractDetails | null>(null);
  const [showChainModal, setShowChainModal] = useState(false);
  const [loadingChain, setLoadingChain] = useState(false);

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

  // Load option chain for supported index symbols
  useEffect(() => {
    if (!["NIFTY", "BANKNIFTY", "FINNIFTY"].includes(symbol)) {
      setOptionChain(null);
      setChartMode("spot");
      return;
    }
    setLoadingChain(true);
    api<OptionChainData>(
      `/api/market/option-chain?underlying=${encodeURIComponent(symbol)}${selectedExpiry ? `&expiry=${encodeURIComponent(selectedExpiry)}` : ""}`,
    )
      .then((data) => {
        setOptionChain(data);
        if (!selectedStrike || Math.abs(selectedStrike - data.atm_strike) > data.strike_step * 15) {
          setSelectedStrike(data.atm_strike);
        }
        if (!selectedExpiry) {
          setSelectedExpiry(data.current_expiry);
        }
      })
      .catch(() => {})
      .finally(() => setLoadingChain(false));
  }, [symbol, selectedExpiry]);

  // Load Candles (spot or option strike)
  useEffect(() => {
    const isOpt = chartMode === "option" && selectedStrike != null;
    const url = isOpt
      ? `/api/market/option-candles?symbol=${encodeURIComponent(symbol)}&strike=${selectedStrike}&option_type=${optionType}&expiry=${encodeURIComponent(selectedExpiry || "")}&interval=${timeframe}&sensitivity=${sensitivity}`
      : `/api/market/candles?symbol=${encodeURIComponent(symbol)}&interval=${timeframe}&sensitivity=${sensitivity}`;

    const load = () =>
      api<{
        bars: Bar[];
        levels: Level[];
        zones: Zone[];
        market_structure?: { swings: SwingPoint[]; events: StructureEvent[] };
        trendlines?: TrendlineData[];
        sweeps?: LiquiditySweepData[];
        indicators?: IndicatorData;
        regime?: RegimeData;
        decision?: DecisionData;
        mtf?: MTFReportData;
        contract?: OptionContractDetails;
      }>(url)
        .then((d) => {
          setBars(d.bars);
          setLevels(d.levels ?? []);
          setZones(d.zones ?? []);
          setMarketStructure(d.market_structure);
          setTrendlines(d.trendlines ?? []);
          setSweeps(d.sweeps ?? []);
          setIndicators(d.indicators);
          setRegime(d.regime);
          setDecision(d.decision);
          setMtf(d.mtf);
          if (d.contract) {
            setContractInfo(d.contract);
          } else {
            setContractInfo(null);
          }
        })
        .catch((e) => setError(e instanceof Error ? e.message : "Could not load candles"));

    setBars([]);
    setLevels([]);
    setZones([]);
    setMarketStructure(undefined);
    setTrendlines([]);
    setSweeps([]);
    setIndicators(undefined);
    setRegime(undefined);
    setDecision(undefined);
    setMtf(undefined);
    load();
    const timer = setInterval(load, 60_000);
    return () => clearInterval(timer);
  }, [symbol, timeframe, sensitivity, chartMode, optionType, selectedStrike, selectedExpiry]);

  const loadPositions = () =>
    api<{ positions: Position[] }>("/api/paper/positions")
      .then((d) => setPositions(d.positions))
      .catch(() => setPositions([]));
  useEffect(() => {
    loadPositions();
  }, []);

  // Positions on chart: if option chart mode, show positions for this specific strike & type
  const onChart = useMemo(() => {
    if (chartMode === "option" && selectedStrike != null) {
      return positions.filter(
        (p) =>
          p.underlying === symbol &&
          p.quantity !== 0 &&
          p.instrument === optionType &&
          Number(p.strike) === selectedStrike,
      );
    }
    return positions.filter(
      (p) => p.underlying === symbol && p.quantity !== 0 && (p.instrument === "EQ" || p.instrument === "FUT"),
    );
  }, [positions, symbol, chartMode, optionType, selectedStrike]);

  const chartLevels = useMemo<Level[]>(
    () => [
      ...levels,
      ...onChart.map((p) => ({ label: `Entry ${p.instrument} ${p.quantity > 0 ? "long" : "short"}`, price: p.avg_price })),
    ],
    [levels, onChart],
  );
  const plans = useMemo(
    () =>
      onChart.flatMap((p) => {
        const plan = riskPlan(p.avg_price, p.quantity, levels, zones);
        return plan ? [{ label: `${p.instrument} ${p.quantity > 0 ? "LONG" : "SHORT"} ${Math.abs(p.quantity)}`, plan }] : [];
      }),
    [onChart, levels, zones],
  );

  const lastPrice = bars.at(-1)?.close ?? quote?.price;
  useEffect(() => {
    if (chartMode === "option" && selectedStrike != null) {
      const p = contractInfo?.ltp ?? bars.at(-1)?.close;
      setTicket((t) => ({
        ...t,
        instrument: optionType,
        strike: String(selectedStrike),
        expiry: selectedExpiry || optionChain?.current_expiry || "",
        price: p ? p.toFixed(2) : t.price,
      }));
    } else if (chartMode === "spot") {
      if ((ticket.instrument === "FUT" || ticket.instrument === "EQ") && lastPrice) {
        setTicket((t) => ({ ...t, price: lastPrice.toFixed(2) }));
      }
    }
  }, [chartMode, optionType, selectedStrike, selectedExpiry, contractInfo?.ltp, lastPrice]);

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
          {/* Top Bar: Title / Price or Option Title / Price + Mode Switcher + Timeframes */}
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="space-y-1">
              <div className="flex flex-wrap items-center gap-2">
                {/* Spot or Option Title */}
                {chartMode === "spot" ? (
                  <h2 className="text-lg font-bold text-zinc-900">
                    {symbol} <span className="text-sm font-normal text-zinc-500">{quote?.name}</span>
                  </h2>
                ) : (
                  <div className="flex items-center gap-2">
                    <h2 className="text-lg font-bold text-zinc-900">
                      {symbol} {selectedStrike}{" "}
                      <span className={optionType === "CE" ? "text-emerald-700 font-extrabold" : "text-rose-700 font-extrabold"}>
                        {optionType}
                      </span>
                    </h2>
                    <span className="rounded-md bg-zinc-100 px-2 py-0.5 text-xs text-zinc-600 font-medium">
                      Expiry {selectedExpiry || optionChain?.current_expiry}
                    </span>
                  </div>
                )}

                {/* Spot / Option Mode Switcher (only for index with options) */}
                {segment === "index" && ["NIFTY", "BANKNIFTY", "FINNIFTY"].includes(symbol) && (
                  <div className="inline-flex rounded-lg bg-zinc-100 p-0.5 text-xs font-medium ml-2">
                    <button
                      onClick={() => setChartMode("spot")}
                      className={`rounded-md px-2.5 py-1 transition-all ${
                        chartMode === "spot" ? "bg-white text-zinc-900 shadow-xs font-semibold" : "text-zinc-600 hover:text-zinc-900"
                      }`}
                    >
                      Spot Index
                    </button>
                    <button
                      onClick={() => {
                        setChartMode("option");
                        if (!selectedStrike && optionChain?.atm_strike) {
                          setSelectedStrike(optionChain.atm_strike);
                        }
                      }}
                      className={`rounded-md px-2.5 py-1 transition-all flex items-center gap-1 ${
                        chartMode === "option"
                          ? "bg-emerald-600 text-white shadow-xs font-semibold"
                          : "text-zinc-600 hover:text-zinc-900"
                      }`}
                    >
                      <span>Option Chart</span>
                      <span className="rounded bg-emerald-800/50 px-1 py-0.2 text-[9px] text-emerald-100">Live</span>
                    </button>
                  </div>
                )}
              </div>

              {/* Price display */}
              {chartMode === "spot" ? (
                <>
                  <div className="flex items-baseline gap-2">
                    <span className="text-2xl font-bold">{money(quote?.price, quote?.currency)}</span>
                    <span className={`text-sm ${signed(quote?.change)}`}>
                      {quote?.change != null && `${quote.change > 0 ? "+" : ""}${quote.change.toFixed(2)} (${pct(quote.change_pct)})`}
                    </span>
                  </div>
                  <div className="text-[11px] text-zinc-500">
                    {quote?.time ? `Last update ${quote.time} IST` : ""} · Yahoo Finance, delayed · refreshes every minute
                  </div>
                </>
              ) : (
                <>
                  <div className="flex items-baseline gap-2">
                    <span className="text-2xl font-bold">
                      {money(contractInfo?.ltp ?? bars.at(-1)?.close)}
                    </span>
                    <span className={`text-sm font-medium ${signed(contractInfo?.change)}`}>
                      {contractInfo?.change != null &&
                        `${contractInfo.change > 0 ? "+" : ""}${contractInfo.change.toFixed(2)} (${pct(contractInfo.change_pct)})`}
                    </span>
                    <span className="text-xs text-zinc-500 font-normal">
                      · Spot: <strong className="text-zinc-700">{money(quote?.price)}</strong>
                    </span>
                  </div>
                  {/* Greeks & Contract Details Badge */}
                  <div className="flex flex-wrap items-center gap-2 text-[11px] text-zinc-600">
                    <span className="rounded bg-zinc-100 px-1.5 py-0.5">
                      Delta: <strong className="text-zinc-900">{contractInfo?.delta != null ? contractInfo.delta.toFixed(2) : "—"}</strong>
                    </span>
                    <span className="rounded bg-zinc-100 px-1.5 py-0.5">
                      Theta: <strong className="text-zinc-900">{contractInfo?.theta != null ? contractInfo.theta.toFixed(1) : "—"}</strong>
                    </span>
                    <span className="rounded bg-zinc-100 px-1.5 py-0.5">
                      IV: <strong className="text-zinc-900">{contractInfo?.iv_pct != null ? `${contractInfo.iv_pct.toFixed(1)}%` : "—"}</strong>
                    </span>
                    <span className="rounded bg-zinc-100 px-1.5 py-0.5">
                      OI: <strong className="text-zinc-900">{contractInfo?.oi != null ? contractInfo.oi.toLocaleString("en-IN") : "—"}</strong>
                    </span>
                    <span className="rounded bg-zinc-100 px-1.5 py-0.5">
                      Lot: <strong className="text-zinc-900">{contractInfo?.lot_size ?? optionChain?.lot_size ?? 65}</strong>
                    </span>
                  </div>
                </>
              )}
            </div>

            {/* Timeframe Buttons */}
            <div className="flex flex-wrap gap-1">
              {TIMEFRAMES.map(([tf, label]) => (
                <button
                  key={tf}
                  onClick={() => setTimeframe(tf)}
                  className={`rounded-md px-2.5 py-1 text-sm font-medium transition ${
                    timeframe === tf ? "bg-zinc-900 text-white shadow-xs" : "border border-zinc-200 bg-white hover:bg-zinc-50"
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>

          {/* Option Selector Bar (when in Option Chart mode) */}
          {chartMode === "option" && optionChain && (
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-emerald-100 bg-emerald-50/40 p-2.5 text-xs">
              <div className="flex flex-wrap items-center gap-2">
                {/* CE / PE toggle */}
                <div className="inline-flex rounded-md bg-white border border-zinc-200 p-0.5">
                  <button
                    onClick={() => setOptionType("CE")}
                    className={`rounded px-3 py-1 font-bold transition-all ${
                      optionType === "CE"
                        ? "bg-emerald-600 text-white shadow-xs"
                        : "text-zinc-600 hover:text-zinc-900"
                    }`}
                  >
                    Call (CE)
                  </button>
                  <button
                    onClick={() => setOptionType("PE")}
                    className={`rounded px-3 py-1 font-bold transition-all ${
                      optionType === "PE"
                        ? "bg-rose-600 text-white shadow-xs"
                        : "text-zinc-600 hover:text-zinc-900"
                    }`}
                  >
                    Put (PE)
                  </button>
                </div>

                {/* Expiry Selector */}
                <select
                  value={selectedExpiry || optionChain.current_expiry}
                  onChange={(e) => setSelectedExpiry(e.target.value)}
                  className="rounded-md border border-zinc-200 bg-white px-2.5 py-1 font-medium text-zinc-800 shadow-2xs focus:outline-none"
                >
                  {optionChain.expiries.map((exp) => (
                    <option key={exp} value={exp}>
                      Expiry: {exp} {exp === optionChain.current_expiry ? "(Current)" : ""}
                    </option>
                  ))}
                </select>

                {/* Strike Step Controls & Selector */}
                <div className="flex items-center gap-1">
                  <button
                    title="Previous strike"
                    onClick={() => {
                      if (selectedStrike != null) setSelectedStrike(selectedStrike - optionChain.strike_step);
                    }}
                    className="rounded border border-zinc-200 bg-white px-2 py-1 font-mono font-bold text-zinc-700 hover:bg-zinc-100"
                  >
                    -
                  </button>

                  <select
                    value={selectedStrike || optionChain.atm_strike}
                    onChange={(e) => setSelectedStrike(Number(e.target.value))}
                    className="rounded-md border border-zinc-200 bg-white px-2.5 py-1 font-mono font-semibold text-zinc-800 shadow-2xs focus:outline-none"
                  >
                    {optionChain.strikes.map((s) => {
                      const opt = optionType === "CE" ? s.ce : s.pe;
                      const tag = s.is_atm
                        ? " [ATM]"
                        : optionType === "CE"
                        ? optionChain.spot > s.strike
                          ? " (ITM)"
                          : " (OTM)"
                        : optionChain.spot < s.strike
                        ? " (ITM)"
                        : " (OTM)";
                      return (
                        <option key={s.strike} value={s.strike}>
                          {s.strike}
                          {tag}
                          {opt?.ltp != null ? ` · ₹${opt.ltp.toFixed(2)}` : ""}
                        </option>
                      );
                    })}
                  </select>

                  <button
                    title="Next strike"
                    onClick={() => {
                      if (selectedStrike != null) setSelectedStrike(selectedStrike + optionChain.strike_step);
                    }}
                    className="rounded border border-zinc-200 bg-white px-2 py-1 font-mono font-bold text-zinc-700 hover:bg-zinc-100"
                  >
                    +
                  </button>

                  <button
                    onClick={() => setSelectedStrike(optionChain.atm_strike)}
                    className={`rounded px-2 py-1 font-bold text-[11px] transition ${
                      selectedStrike === optionChain.atm_strike
                        ? "bg-amber-100 text-amber-900 border border-amber-300"
                        : "bg-white border border-zinc-200 text-zinc-600 hover:bg-zinc-100"
                    }`}
                  >
                    ATM ({optionChain.atm_strike})
                  </button>
                </div>
              </div>

              {/* Live Option Chain Button */}
              <button
                onClick={() => setShowChainModal(true)}
                className="flex items-center gap-1.5 rounded-lg border border-emerald-300 bg-white px-3 py-1 font-semibold text-emerald-800 shadow-2xs hover:bg-emerald-50 transition"
              >
                <span>Option Chain</span>
                <span>📊</span>
              </button>
            </div>
          )}
          <CandleChart
            bars={bars}
            levels={chartLevels}
            zones={zones}
            plans={plans}
            marketStructure={marketStructure}
            trendlines={trendlines}
            sweeps={sweeps}
            indicators={indicators}
            regime={regime}
            decision={decision}
            mtf={mtf}
            selectedSensitivity={sensitivity}
            onSensitivityChange={setSensitivity}
          />
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

      {/* Option Chain Modal */}
      <OptionChainModal
        isOpen={showChainModal}
        onClose={() => setShowChainModal(false)}
        chain={optionChain}
        symbol={symbol}
        selectedExpiry={selectedExpiry}
        onSelectExpiry={(exp) => setSelectedExpiry(exp)}
        selectedStrike={selectedStrike}
        selectedOptionType={optionType}
        onSelectContract={(strike, type) => {
          setSelectedStrike(strike);
          setOptionType(type);
          setChartMode("option");
        }}
        loading={loadingChain}
      />
    </div>
  );
}

