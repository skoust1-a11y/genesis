"use client";

import {
  type AutoscaleInfo,
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  type IChartApi,
  type IPriceLine,
  type IPrimitivePaneRenderer,
  type IPrimitivePaneView,
  type ISeriesApi,
  type ISeriesPrimitive,
  type SeriesAttachedParameter,
  LineStyle,
  type MouseEventParams,
  type Time,
  type UTCTimestamp,
  createChart,
} from "lightweight-charts";
import { useEffect, useMemo, useRef, useState } from "react";

import { inr } from "@/lib/api";
import type { RiskPlan } from "@/lib/risk";

type Bar = {
  date: string; open: number | null; high: number | null; low: number | null; close: number;
  volume?: number | null; vwap?: number | null;
};
export type Level = { label: string; price: number };
export type Zone = { low: number; high: number; labels: string[]; side: "support" | "resistance" | "at price" };

const UP = "#059669";
const DOWN = "#e11d48";
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const day = (d: string) => `${d.slice(8, 10)} ${MONTHS[Number(d.slice(5, 7)) - 1]}`;
const num = (v: number) => v.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const VWAP = "#c026d3";
const levelColor = (l: string) =>
  l.startsWith("Entry") ? "#7c3aed"
  : l.startsWith("PD") ? "#2563eb"
  : /^(DH|DL|OR)/.test(l) ? "#d97706"
  : /^(BSL|SSL)/.test(l) ? "#0891b2"
  : l[0] === "R" ? DOWN
  : l[0] === "S" ? UP
  : "#52525b";
const zoneColor = (z: Zone) => (z.side === "support" ? UP : z.side === "resistance" ? DOWN : "#52525b");

// The chart library labels times in UTC, so IST wall-clock times are handed over as if they were UTC.
const toTime = (d: string): Time =>
  d.length > 10
    ? ((Date.UTC(+d.slice(0, 4), +d.slice(5, 7) - 1, +d.slice(8, 10), +d.slice(11, 13), +d.slice(14, 16)) / 1000) as UTCTimestamp)
    : d;

type Box = { low: number; high: number; color: string; text: string; right?: boolean };

/** Draws labelled boxes from `start` to the chart's right edge, beneath the candles. */
class Boxes implements ISeriesPrimitive<Time> {
  private boxes: Box[] = [];
  private start: Time | null = null;
  private p: SeriesAttachedParameter<Time> | null = null;

  attached(p: SeriesAttachedParameter<Time>) {
    this.p = p;
  }

  detached() {
    this.p = null;
  }

  set(boxes: Box[], start: Time | null) {
    this.boxes = boxes;
    this.start = start;
    this.p?.requestUpdate();
  }

  paneViews(): IPrimitivePaneView[] {
    const draw = (target: Parameters<IPrimitivePaneRenderer["draw"]>[0]) => {
      const p = this.p;
      if (!p || this.start === null) return;
      const x0 = p.chart.timeScale().timeToCoordinate(this.start) ?? 0;
      target.useBitmapCoordinateSpace(({ context: ctx, bitmapSize, horizontalPixelRatio: hr, verticalPixelRatio: vr }) => {
        ctx.font = `600 ${10 * vr}px ui-sans-serif, system-ui, sans-serif`;
        ctx.textBaseline = "middle";
        for (const b of this.boxes) {
          const top = p.series.priceToCoordinate(b.high);
          const bottom = p.series.priceToCoordinate(b.low);
          if (top === null || bottom === null) continue;
          const [x, y, w, h] = [Math.max(0, x0 * hr), top * vr, bitmapSize.width, (bottom - top) * vr];
          ctx.fillStyle = `${b.color}1a`;
          ctx.fillRect(x, y, w - x, h);
          ctx.strokeStyle = `${b.color}99`;
          ctx.lineWidth = hr;
          ctx.strokeRect(x, y, w - x, h);
          // Label on a white tag, centred on the box, so neighbouring boxes cannot overprint it.
          const [tw, th] = [ctx.measureText(b.text).width + 8 * hr, 14 * vr];
          const [tx, ty] = [b.right ? w - tw - 4 * hr : x + 4 * hr, y + h / 2 - th / 2];
          ctx.fillStyle = "#ffffffe6";
          ctx.fillRect(tx, ty, tw, th);
          ctx.strokeRect(tx, ty, tw, th);
          ctx.fillStyle = b.color;
          ctx.fillText(b.text, tx + 4 * hr, ty + th / 2);
        }
      });
    };
    return [{ zOrder: () => "bottom", renderer: () => ({ draw }) }];
  }
}

/** Interactive candlestick chart (scroll to zoom, drag to pan). `date` is "YYYY-MM-DD" or "YYYY-MM-DD HH:MM" IST. */
export function CandleChart({
  bars, levels = [], zones = [], plans = [],
}: { bars: Bar[]; levels?: Level[]; zones?: Zone[]; plans?: { label: string; plan: RiskPlan }[] }) {
  const wrap = useRef<HTMLDivElement>(null);
  const box = useRef<HTMLDivElement>(null);
  const chart = useRef<{
    api: IChartApi; candles: ISeriesApi<"Candlestick">; volume: ISeriesApi<"Histogram">; vwap: ISeriesApi<"Line">;
    lines: IPriceLine[]; boxes: Boxes;
  }>(null);
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
    const vwap = api.addSeries(LineSeries, {
      color: VWAP, lineWidth: 1, title: "VWAP", priceLineVisible: false, crosshairMarkerVisible: false,
    });
    const onMove = (p: MouseEventParams) => setHover(p.logical ?? null);
    api.subscribeCrosshairMove(onMove);
    const boxes = new Boxes();
    candles.attachPrimitive(boxes);
    chart.current = { api, candles, volume, vwap, lines: [], boxes };
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
    // Indices report no volume, so give the candles the space the volume bars would take.
    c.candles.priceScale().applyOptions({ scaleMargins: { top: 0.08, bottom: data.some((b) => b.volume) ? 0.22 : 0.05 } });
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
    chart.current?.vwap.setData(
      showLevels ? data.flatMap((b) => (b.vwap != null ? [{ time: b.time, value: b.vwap }] : [])) : [],
    );
  }, [data, showLevels]);

  // Boxes start at the latest session's first candle (the last candle on the daily chart) and run to the right edge.
  // A position's loss/reward boxes show even with levels hidden: they are about money at stake.
  useEffect(() => {
    const last = data.at(-1);
    const start = intraday ? data.find((b) => b.date.slice(0, 10) === last?.date.slice(0, 10)) : last;
    const zoneBoxes = (showLevels ? zones : []).map((z) => ({
      low: z.low, high: z.high, color: zoneColor(z),
      text: `${z.side.toUpperCase()} ${num(z.low)}–${num(z.high)} · ${z.labels.join(" ")}`,
    }));
    const riskBoxes = plans.flatMap(({ label, plan: r }) => [
      {
        low: Math.min(r.entry, r.stop), high: Math.max(r.entry, r.stop), color: DOWN, right: true,
        text: `${label} · STOP ${num(r.stop)} · RISK ${inr(r.riskRs)} (${num(r.risk)} pts)`,
      },
      ...(r.target === null ? [] : [{
        low: Math.min(r.entry, r.target), high: Math.max(r.entry, r.target), color: UP, right: true,
        text: `TARGET ${num(r.target)} · ${inr(r.rewardRs ?? 0)} · ${r.rr}R`,
      }]),
    ]);
    chart.current?.boxes.set(start ? [...zoneBoxes, ...riskBoxes] : [], start?.time ?? null);
  }, [zones, plans, data, intraday, showLevels]);

  useEffect(() => {
    const c = chart.current;
    if (!c) return;
    c.lines.forEach((l) => c.candles.removePriceLine(l));
    const prices = [
      ...(showLevels ? levels : []).map((l) => l.price),
      ...plans.flatMap(({ plan: r }) => (r.target === null ? [r.stop] : [r.stop, r.target])),
    ];
    // Price lines do not widen the axis by themselves, so on 1m/5m the levels sat off-screen.
    c.candles.applyOptions({
      autoscaleInfoProvider: (base: () => AutoscaleInfo | null) => {
        const r = base();
        return r?.priceRange && prices.length
          ? { ...r, priceRange: { minValue: Math.min(r.priceRange.minValue, ...prices), maxValue: Math.max(r.priceRange.maxValue, ...prices) } }
          : r;
      },
    });
    // A zone box already marks its levels, and levels at one price share a line, so the price axis stays readable.
    const byPrice = new Map<number, string[]>();
    for (const l of showLevels ? levels : []) {
      if (l.label.startsWith("Entry") || !zones.some((z) => z.low <= l.price && l.price <= z.high)) {
        byPrice.set(l.price, [...(byPrice.get(l.price) ?? []), l.label]);
      }
    }
    c.lines = [...byPrice].map(([price, labels]) =>
      c.candles.createPriceLine({
        price, color: levelColor(labels[0]), lineWidth: 1, lineStyle: LineStyle.SparseDotted, axisLabelVisible: true,
        title: labels.join("/"),
      }),
    );
  }, [levels, zones, plans, showLevels]);

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
          showLevels && intraday &&
            "DH/DL: today's high/low · ORH/ORL: 15-min opening range · BSL/SSL: untouched swing highs/lows today (1 = latest)",
          showLevels && zones.length > 0 && "Boxes: 2+ levels within 0.3%, where price tends to react",
          intraday && "Times in IST (UTC+5:30)",
        ]
          .filter(Boolean)
          .join(" · ")}
      </p>
    </div>
  );
}
