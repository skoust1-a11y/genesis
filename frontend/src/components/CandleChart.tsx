"use client";

import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  LineStyle,
  type MouseEventParams,
  type Time,
  type UTCTimestamp,
  createChart,
} from "lightweight-charts";
import { useEffect, useMemo, useRef, useState } from "react";

type Bar = { date: string; open: number | null; high: number | null; low: number | null; close: number; volume?: number | null };
export type Level = { label: string; price: number };

const UP = "#059669";
const DOWN = "#e11d48";
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const day = (d: string) => `${d.slice(8, 10)} ${MONTHS[Number(d.slice(5, 7)) - 1]}`;
const num = (v: number) => v.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const levelColor = (l: string) =>
  l.startsWith("Entry") ? "#7c3aed" : l.startsWith("PD") ? "#2563eb" : l[0] === "R" ? DOWN : l[0] === "S" ? UP : "#52525b";

// The chart library labels times in UTC, so IST wall-clock times are handed over as if they were UTC.
const toTime = (d: string): Time =>
  d.length > 10
    ? ((Date.UTC(+d.slice(0, 4), +d.slice(5, 7) - 1, +d.slice(8, 10), +d.slice(11, 13), +d.slice(14, 16)) / 1000) as UTCTimestamp)
    : d;

/** Interactive candlestick chart (scroll to zoom, drag to pan). `date` is "YYYY-MM-DD" or "YYYY-MM-DD HH:MM" IST. */
export function CandleChart({ bars, levels = [] }: { bars: Bar[]; levels?: Level[] }) {
  const wrap = useRef<HTMLDivElement>(null);
  const box = useRef<HTMLDivElement>(null);
  const chart = useRef<{ api: IChartApi; candles: ISeriesApi<"Candlestick">; volume: ISeriesApi<"Histogram">; lines: IPriceLine[] }>(null);
  const shownKey = useRef("");
  const [hover, setHover] = useState<number | null>(null);
  const [showLevels, setShowLevels] = useState(true);
  const intraday = (bars[0]?.date.length ?? 0) > 10;

  // The library needs strictly increasing times; the source sometimes repeats the live minute.
  const data = useMemo(
    () => bars.map((b) => ({ ...b, time: toTime(b.date) })).filter((b, i, all) => i === 0 || b.time > all[i - 1].time),
    [bars],
  );

  useEffect(() => {
    const api = createChart(box.current!, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "#ffffff" }, textColor: "#3f3f46", fontSize: 12 },
      grid: { vertLines: { visible: false }, horzLines: { color: "#f4f4f5" } },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { labelBackgroundColor: "#18181b" },
        horzLine: { labelBackgroundColor: "#18181b" },
      },
      rightPriceScale: { borderColor: "#e4e4e7", scaleMargins: { top: 0.08, bottom: 0.22 } },
      timeScale: { borderColor: "#e4e4e7", rightOffset: 8, minBarSpacing: 3 },
      localization: { locale: "en-IN", priceFormatter: num },
    });
    const candles = api.addSeries(CandlestickSeries, {
      upColor: UP, downColor: DOWN, wickUpColor: UP, wickDownColor: DOWN, borderVisible: false,
    });
    const volume = api.addSeries(HistogramSeries, {
      priceScaleId: "", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false,
    });
    volume.priceScale().applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });
    const onMove = (p: MouseEventParams) => setHover(p.logical ?? null);
    api.subscribeCrosshairMove(onMove);
    chart.current = { api, candles, volume, lines: [] };
    return () => {
      api.unsubscribeCrosshairMove(onMove);
      api.remove();
      chart.current = null;
    };
  }, []);

  useEffect(() => {
    const c = chart.current;
    if (!c) return;
    c.api.applyOptions({ timeScale: { timeVisible: intraday, secondsVisible: false } });
    c.candles.setData(
      data.map((b) => ({ time: b.time, open: b.open ?? b.close, high: b.high ?? b.close, low: b.low ?? b.close, close: b.close })),
    );
    c.volume.setData(
      data.map((b) => ({ time: b.time, value: b.volume ?? 0, color: b.close >= (b.open ?? b.close) ? `${UP}40` : `${DOWN}40` })),
    );
    // On a new symbol or timeframe open on the latest ~100 candles; later refreshes keep the user's zoom.
    const key = `${data[0]?.date}|${intraday}`;
    if (data.length && key !== shownKey.current) {
      shownKey.current = key;
      c.api.timeScale().setVisibleLogicalRange({ from: data.length - 100, to: data.length + 8 });
    }
  }, [data, intraday]);

  useEffect(() => {
    const c = chart.current;
    if (!c) return;
    c.lines.forEach((l) => c.candles.removePriceLine(l));
    c.lines = (showLevels ? levels : []).map((l) =>
      c.candles.createPriceLine({
        price: l.price, color: levelColor(l.label), lineWidth: 1, lineStyle: LineStyle.SparseDotted, axisLabelVisible: true,
        title: l.label,
      }),
    );
  }, [levels, showLevels]);

  const shown = data[hover !== null && hover >= 0 && hover < data.length ? hover : data.length - 1];
  const prev = shown && data[data.indexOf(shown) - 1];
  const op = shown ? shown.open ?? shown.close : 0;
  const change = shown ? shown.close - (prev?.close ?? op) : 0;

  return (
    <div ref={wrap} className="space-y-1 bg-white [&:fullscreen]:flex [&:fullscreen]:flex-col [&:fullscreen]:p-4">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 font-mono text-[13px] text-zinc-700">
        {shown && (
          <>
            <span className="font-semibold text-zinc-900">
              {day(shown.date)} {shown.date.slice(0, 4)}
              {intraday && ` · ${shown.date.slice(11)} IST`}
            </span>
            <span>O {num(op)}</span>
            <span>H {num(shown.high ?? shown.close)}</span>
            <span>L {num(shown.low ?? shown.close)}</span>
            <span>C {num(shown.close)}</span>
            <span className={change >= 0 ? "text-emerald-700" : "text-rose-700"}>
              {change >= 0 ? "+" : ""}
              {num(change)} ({((change / (prev?.close ?? op)) * 100).toFixed(2)}%)
            </span>
            {!!shown.volume && <span>Vol {shown.volume.toLocaleString("en-IN")}</span>}
          </>
        )}
        <span className="ml-auto flex gap-1 font-sans text-xs">
          {levels.length > 0 && (
            <button
              aria-pressed={showLevels}
              className={`rounded border px-2 py-0.5 ${showLevels ? "bg-zinc-900 text-white" : "text-zinc-700 hover:bg-zinc-50"}`}
              onClick={() => setShowLevels((v) => !v)}
            >
              Levels
            </button>
          )}
          <button
            className="rounded border px-2 py-0.5 text-zinc-700 hover:bg-zinc-50"
            onClick={() => (document.fullscreenElement ? document.exitFullscreen() : wrap.current?.requestFullscreen())}
          >
            ⛶ Fullscreen
          </button>
        </span>
      </div>
      {showLevels && levels.length > 0 && (
        <div className="flex flex-wrap gap-x-3 font-mono text-xs">
          {levels.map((l) => (
            <span key={l.label} style={{ color: levelColor(l.label) }}>
              {l.label} {num(l.price)}
            </span>
          ))}
        </div>
      )}
      <div className="relative h-[520px] w-full [:fullscreen_&]:h-auto [:fullscreen_&]:flex-1">
        <div ref={box} className="absolute inset-0" />
        {!data.length && (
          <p className="absolute inset-0 grid place-items-center text-sm text-zinc-500">Loading candles…</p>
        )}
      </div>
      <p className="text-right text-[11px] text-zinc-400">
        {[
          "Scroll to zoom · drag to pan · double-click the price axis to reset",
          showLevels && levels.length > 0 && "PDH/PDL/PDC: previous session high/low/close · P, R1–R2, S1–S2: floor pivots",
          intraday && "Times in IST (UTC+5:30)",
        ]
          .filter(Boolean)
          .join(" · ")}
      </p>
    </div>
  );
}
