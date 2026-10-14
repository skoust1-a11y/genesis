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
import {
  MomentumPanel,
  type IndicatorData,
} from "@/components/MomentumPanel";
import {
  TradingDecisionDashboard,
  type DecisionData,
  type MTFReportData,
  type RegimeData,
} from "@/components/TradingDecisionDashboard";

export type Bar = {
  date: string;
  open: number | null;
  high: number | null;
  low: number | null;
  close: number;
  volume?: number | null;
  vwap?: number | null;
};

export type Level = { label: string; price: number };

export type EnrichedLevel = {
  label: string;
  price: number;
  kind: string;
  color: string;
  style: string;
  importance: "high" | "medium" | "standard";
};

export type Zone = {
  low: number;
  high: number;
  labels: string[];
  side: "support" | "resistance" | "at price";
};

export type SwingPoint = {
  index: number;
  date: string;
  price: number;
  kind: "high" | "low";
  classification: "HH" | "HL" | "LH" | "LL" | "EQH" | "EQL" | "SWING";
  is_major: boolean;
  confirmed_index: number;
  confirmed_date: string;
  label?: string;
};

export type StructureEvent = {
  event_type: "BOS" | "CHoCH";
  direction: "bullish" | "bearish";
  level_price: number;
  swing_index: number;
  swing_date: string;
  break_index: number;
  break_date: string;
  confirmation: "close" | "wick";
  label: string;
};

export type TrendlineData = {
  kind: "ascending" | "descending";
  start_index: number;
  start_date: string;
  start_price: number;
  end_index: number;
  end_date: string;
  end_price: number;
  slope: number;
  channel_high?: number | null;
  channel_low?: number | null;
  is_broken?: boolean;
  break_index?: number | null;
  break_date?: string | null;
  retested?: boolean;
  label?: string;
};

export type LiquiditySweepData = {
  level_label: string;
  level_price: number;
  sweep_type: "wick_sweep" | "close_breakout";
  reclaimed: boolean;
  sweep_index: number;
  sweep_date: string;
  reclaim_index?: number | null;
  reclaim_date?: string | null;
  extreme_price: number;
  note: string;
};

const UP = "#059669";
const DOWN = "#e11d48";
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const dayStr = (d: string) => `${d.slice(8, 10)} ${MONTHS[Number(d.slice(5, 7)) - 1]}`;
const num = (v: number) => v.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

const VWAP_COLOR = "#c026d3";
const EMA9_COLOR = "#06b6d4";
const EMA20_COLOR = "#f59e0b";
const EMA50_COLOR = "#3b82f6";
const EMA200_COLOR = "#8b5cf6";

const levelColor = (l: string) =>
  l.startsWith("Entry")
    ? "#7c3aed"
    : l.startsWith("PDH") || l.startsWith("PWH") || l === "DH"
    ? "#7c3aed"
    : l.startsWith("PDL") || l.startsWith("PWL") || l === "DL"
    ? "#0284c7"
    : /^(EQH|EQL)/.test(l)
    ? "#9333ea"
    : /^(BSL|SSL)/.test(l)
    ? "#0284c7"
    : l[0] === "R"
    ? "#7c3aed"
    : l[0] === "S"
    ? "#0284c7"
    : "#52525b";

const zoneColor = (z: Zone) => (z.side === "support" ? UP : z.side === "resistance" ? DOWN : "#52525b");

// The chart library labels times in UTC, so IST wall-clock times are handed over as if UTC.
const toTime = (d: string): Time =>
  d.length > 10
    ? ((Date.UTC(+d.slice(0, 4), +d.slice(5, 7) - 1, +d.slice(8, 10), +d.slice(11, 13), +d.slice(14, 16)) / 1000) as UTCTimestamp)
    : d;

type Box = { low: number; high: number; color: string; text: string; right?: boolean };

/** Comprehensive Institutional Canvas Overlays (Structure, BOS, CHoCH, Trendlines, Sweeps, Zones) */
class InstitutionalOverlays implements ISeriesPrimitive<Time> {
  private p: SeriesAttachedParameter<Time> | null = null;
  private boxes: Box[] = [];
  private start: Time | null = null;
  private swings: SwingPoint[] = [];
  private events: StructureEvent[] = [];
  private trendlines: TrendlineData[] = [];
  private sweeps: LiquiditySweepData[] = [];
  private timeMap: Map<string, Time> = new Map();
  private showStructure = true;
  private showChannels = true;
  private showLiquidity = true;
  private mode: "clean" | "pro" = "clean";

  attached(p: SeriesAttachedParameter<Time>) {
    this.p = p;
  }

  detached() {
    this.p = null;
  }

  update(
    boxes: Box[],
    start: Time | null,
    swings: SwingPoint[],
    events: StructureEvent[],
    trendlines: TrendlineData[],
    sweeps: LiquiditySweepData[],
    timeMap: Map<string, Time>,
    showStructure: boolean,
    showChannels: boolean,
    showLiquidity: boolean,
    mode: "clean" | "pro" = "clean"
  ) {
    this.boxes = boxes;
    this.start = start;
    this.swings = swings;
    this.events = events;
    this.trendlines = trendlines;
    this.sweeps = sweeps;
    this.timeMap = timeMap;
    this.showStructure = showStructure;
    this.showChannels = showChannels;
    this.showLiquidity = showLiquidity;
    this.mode = mode;
    this.p?.requestUpdate();
  }

  paneViews(): IPrimitivePaneView[] {
    const draw = (target: Parameters<IPrimitivePaneRenderer["draw"]>[0]) => {
      const p = this.p;
      if (!p) return;

      const timeScale = p.chart.timeScale();
      const series = p.series;

      target.useBitmapCoordinateSpace(({ context: ctx, bitmapSize, horizontalPixelRatio: hr, verticalPixelRatio: vr }) => {
        ctx.save();

        // 1. Draw S/R Zones & Risk/Reward Boxes (Only in Pro mode to prevent chart clutter)
        if (this.start !== null && this.mode === "pro") {
          const x0 = timeScale.timeToCoordinate(this.start) ?? 0;
          ctx.font = `600 ${9.5 * vr}px ui-sans-serif, system-ui, sans-serif`;
          ctx.textBaseline = "middle";

          for (const b of this.boxes) {
            const top = series.priceToCoordinate(b.high);
            const bottom = series.priceToCoordinate(b.low);
            if (top === null || bottom === null) continue;

            const [x, y, w, h] = [
              Math.max(0, x0 * hr),
              top * vr,
              bitmapSize.width,
              Math.max(2 * vr, (bottom - top) * vr),
            ];

            // Soft translucent band (unobtrusive)
            ctx.fillStyle = `${b.color}0a`;
            ctx.fillRect(x, y, w - x, h);
            ctx.strokeStyle = `${b.color}28`;
            ctx.lineWidth = hr;
            ctx.strokeRect(x, y, w - x, h);

            // Sleek pill badge pinned to the right edge (never blocking candles)
            const tw = ctx.measureText(b.text).width + 12 * hr;
            const th = 15 * vr;
            const tx = w - tw - 6 * hr;
            const ty = y + h / 2 - th / 2;

            ctx.fillStyle = "#ffffffea";
            ctx.beginPath();
            ctx.roundRect?.(tx, ty, tw, th, 3 * hr);
            ctx.fill();
            ctx.strokeStyle = `${b.color}40`;
            ctx.lineWidth = hr;
            ctx.stroke();

            ctx.fillStyle = b.color;
            ctx.fillText(b.text, tx + 6 * hr, ty + th / 2);
          }
        }

        // 2. Draw Trendlines and Channels (Clean TradingView style)
        if (this.showChannels && this.trendlines.length > 0) {
          const tlColor = "#16a34a"; // TradingView green

          // In Clean View, keep it simple and uncluttered:
          // Pick the 2 primary pattern lines (1 Upper Resistance line and 1 Lower Support line)
          let linesToDraw = this.trendlines;
          if (this.mode === "clean" && this.trendlines.length > 2) {
            const uppers = this.trendlines.filter(
              (tl) => (tl.label && (tl.label.includes("HH") || tl.label.includes("LH"))) || tl.kind === "descending"
            );
            const lowers = this.trendlines.filter(
              (tl) => (tl.label && (tl.label.includes("HL") || tl.label.includes("LL"))) || tl.kind === "ascending"
            );
            const bestUpper = uppers.filter((l) => !l.is_broken).at(-1) || uppers.at(-1);
            const bestLower = lowers.filter((l) => !l.is_broken).at(-1) || lowers.at(-1);
            linesToDraw = [bestUpper, bestLower].filter(Boolean) as TrendlineData[];
          }

          for (const tl of linesToDraw) {
            const t1 = this.timeMap.get(tl.start_date);
            const t2 = this.timeMap.get(tl.end_date);
            if (!t1 || !t2) continue;

            const x1 = timeScale.timeToCoordinate(t1);
            const x2 = timeScale.timeToCoordinate(t2);
            const y1 = series.priceToCoordinate(tl.start_price);
            const y2 = series.priceToCoordinate(tl.end_price);
            if (x1 === null || x2 === null || y1 === null || y2 === null) continue;

            // Trendline Base Line (Clean straight TradingView green line)
            ctx.strokeStyle = tlColor;
            ctx.lineWidth = 1.8 * hr;
            ctx.beginPath();
            ctx.moveTo(x1 * hr, y1 * vr);
            ctx.lineTo(x2 * hr, y2 * vr);
            ctx.stroke();

            // Forward projection ray (Clean straight line extending forward)
            const extX = Math.min(bitmapSize.width, x2 * hr + 150 * hr);
            const slopePx = (y2 * vr - y1 * vr) / ((x2 - x1) * hr || 1);
            const extY = y2 * vr + slopePx * (extX - x2 * hr);

            ctx.save();
            ctx.strokeStyle = `${tlColor}bb`;
            ctx.lineWidth = 1.4 * hr;
            ctx.setLineDash([4 * hr, 3 * hr]);
            ctx.beginPath();
            ctx.moveTo(x2 * hr, y2 * vr);
            ctx.lineTo(extX, extY);
            ctx.stroke();
            ctx.restore();

            // Parallel Channel Line
            const channelP = tl.channel_high ?? tl.channel_low;
            if (channelP != null) {
              const cy1 = series.priceToCoordinate(channelP);
              if (cy1 !== null) {
                const dy = (cy1 - y1) * vr;
                ctx.save();
                ctx.setLineDash([3 * hr, 3 * hr]);
                ctx.strokeStyle = `${tlColor}aa`;
                ctx.lineWidth = 1.4 * hr;
                ctx.beginPath();
                ctx.moveTo(x1 * hr, (y1 * vr) + dy);
                ctx.lineTo(x2 * hr, (y2 * vr) + dy);
                ctx.lineTo(extX, extY + dy);
                ctx.stroke();
                ctx.restore();
              }
            }

            // Only show labels when Structure (BOS) is explicitly enabled by the user
            if (this.showStructure && tl.label) {
              const labelText = tl.label;
              ctx.save();
              ctx.font = `bold ${8.5 * vr}px ui-sans-serif, system-ui, sans-serif`;
              const tw = ctx.measureText(labelText).width + 8 * hr;
              const th = 13 * vr;
              const midX = ((x1 + x2) / 2) * hr - tw / 2;
              const midY = ((y1 + y2) / 2) * vr + (tl.kind === "ascending" ? 4 * vr : -th - 4 * vr);

              ctx.fillStyle = "#ecfdf5f0";
              ctx.strokeStyle = tlColor;
              ctx.lineWidth = hr;
              ctx.beginPath();
              ctx.roundRect?.(midX, midY, tw, th, 2 * hr);
              ctx.fill();
              ctx.stroke();

              ctx.fillStyle = "#047857";
              ctx.textBaseline = "middle";
              ctx.fillText(labelText, midX + 4 * hr, midY + th / 2);
              ctx.restore();
            }
          }
        }

        // 3. Draw BOS & CHoCH (Structure Breaks - clean & unobtrusive)
        if (this.showStructure && this.events.length > 0) {
          ctx.font = `600 ${8.5 * vr}px ui-sans-serif, system-ui, sans-serif`;

          // Show only the latest 2 confirmed breaks to keep the chart clean
          const recentEvents = this.events.slice(-2);
          for (const ev of recentEvents) {
            const tSwing = this.timeMap.get(ev.swing_date);
            const tBreak = this.timeMap.get(ev.break_date);
            if (!tSwing || !tBreak) continue;

            const xs = timeScale.timeToCoordinate(tSwing);
            const xb = timeScale.timeToCoordinate(tBreak);
            const yLevel = series.priceToCoordinate(ev.level_price);
            if (xs === null || xb === null || yLevel === null) continue;

            const isBull = ev.direction === "bullish";
            const col = isBull ? "#10b981" : "#f43f5e";

            // Subtle dashed ray from swing to breakout
            ctx.save();
            ctx.strokeStyle = col;
            ctx.lineWidth = 1 * hr;
            ctx.setLineDash([3 * hr, 3 * hr]);
            ctx.beginPath();
            ctx.moveTo(xs * hr, yLevel * vr);
            ctx.lineTo(xb * hr, yLevel * vr);
            ctx.stroke();
            ctx.restore();

            // Minimal discreet badge at breakout
            const text = `${ev.event_type} ${isBull ? "↑" : "↓"}`;
            const tw = ctx.measureText(text).width + 8 * hr;
            const th = 13 * vr;
            const tx = xb * hr - tw / 2;
            const ty = isBull ? yLevel * vr - th - 3 * vr : yLevel * vr + 3 * vr;

            ctx.fillStyle = isBull ? "#059669" : "#e11d48";
            ctx.beginPath();
            ctx.roundRect?.(tx, ty, tw, th, 3 * hr);
            ctx.fill();

            ctx.fillStyle = "#ffffff";
            ctx.textBaseline = "middle";
            ctx.fillText(text, tx + 4 * hr, ty + th / 2);
          }
        }

        // 4. Draw Swing Points (HH1, HH2, HL1, HL2, etc.) at straight line anchors
        // Only rendered when Structure (BOS) is enabled to keep Clean View pristine
        if (this.showStructure && this.swings.length > 0) {
          ctx.font = `bold ${8 * vr}px ui-sans-serif, system-ui, sans-serif`;
          ctx.textBaseline = "middle";

          // Show recent swings so HH1, HH2, HL1, HL2 are visible at the straight line anchors
          const recentSwings = this.swings.slice(-16);
          for (const sw of recentSwings) {
            const t = this.timeMap.get(sw.date);
            if (!t) continue;
            const x = timeScale.timeToCoordinate(t);
            const y = series.priceToCoordinate(sw.price);
            if (x === null || y === null) continue;

            const isHigh = sw.kind === "high";
            const isBull = sw.classification === "HH" || sw.classification === "HL";
            const col = isBull ? "#059669" : "#e11d48";

            const tag = sw.label || sw.classification;
            const tw = ctx.measureText(tag).width + 6 * hr;
            const th = 12 * vr;
            const tx = x * hr - tw / 2;
            const ty = isHigh ? y * vr - th - 5 * vr : y * vr + 5 * vr;

            ctx.fillStyle = `${col}18`;
            ctx.strokeStyle = `${col}99`;
            ctx.lineWidth = hr;
            ctx.beginPath();
            ctx.roundRect?.(tx, ty, tw, th, 2 * hr);
            ctx.fill();
            ctx.stroke();

            ctx.fillStyle = col;
            ctx.fillText(tag, tx + 3 * hr, ty + th / 2);
          }
        }

        // 5. Draw Liquidity Sweeps
        if (this.showLiquidity && this.sweeps.length > 0) {
          ctx.font = `bold ${8 * vr}px ui-sans-serif, system-ui, sans-serif`;

          // Sort descending by sweep_index so the latest confirmed sweeps appear
          const sortedSweeps = [...this.sweeps].sort((a, b) => (b.sweep_index ?? 0) - (a.sweep_index ?? 0));
          const recentSweeps = sortedSweeps.slice(0, 3);

          for (const sw of recentSweeps) {
            const t = this.timeMap.get(sw.sweep_date);
            if (!t) continue;
            const x = timeScale.timeToCoordinate(t);
            const y = series.priceToCoordinate(sw.extreme_price);
            const yLevel = series.priceToCoordinate(sw.level_price);
            if (x === null || y === null) continue;

            // Draw dashed horizontal line at the swept level price
            if (yLevel !== null) {
              ctx.save();
              ctx.setLineDash([3 * hr, 3 * hr]);
              ctx.strokeStyle = sw.reclaimed ? "#eab308cc" : "#f97316cc";
              ctx.lineWidth = 1.2 * hr;
              ctx.beginPath();
              ctx.moveTo((x - 28) * hr, yLevel * vr);
              ctx.lineTo((x + 28) * hr, yLevel * vr);
              ctx.stroke();
              ctx.restore();
            }

            const isLowSweep = sw.level_label.includes("L"); // e.g. PDL, PWL, EQL, ORL
            const badgeText = `⚡ ${sw.level_label} ${sw.reclaimed ? "SWEEP" : "BREAK"}`;
            const tw = ctx.measureText(badgeText).width + 8 * hr;
            const th = 13 * vr;
            const tx = x * hr - tw / 2;
            const ty = isLowSweep ? y * vr + 4 * vr : y * vr - th - 4 * vr;

            ctx.fillStyle = sw.reclaimed ? "#fef08a" : "#ffedd5";
            ctx.strokeStyle = sw.reclaimed ? "#ca8a04" : "#ea580c";
            ctx.lineWidth = 1 * hr;
            ctx.beginPath();
            ctx.roundRect?.(tx, ty, tw, th, 3 * hr);
            ctx.fill();
            ctx.stroke();

            ctx.fillStyle = sw.reclaimed ? "#854d0e" : "#9a3412";
            ctx.textBaseline = "middle";
            ctx.fillText(badgeText, tx + 4 * hr, ty + th / 2);
          }
        }

        ctx.restore();
      });
    };

    return [{ zOrder: () => "top", renderer: () => ({ draw }) }];
  }
}

/** Institutional Candlestick Terminal */
export function CandleChart({
  bars,
  levels = [],
  zones = [],
  plans = [],
  marketStructure,
  trendlines = [],
  sweeps = [],
  indicators,
  regime,
  decision,
  mtf,
  selectedSensitivity = 2,
  onSensitivityChange,
}: {
  bars: Bar[];
  levels?: Level[];
  zones?: Zone[];
  plans?: { label: string; plan: RiskPlan }[];
  marketStructure?: { swings: SwingPoint[]; events: StructureEvent[]; sensitivity?: number };
  trendlines?: TrendlineData[];
  sweeps?: LiquiditySweepData[];
  indicators?: IndicatorData;
  regime?: RegimeData;
  decision?: DecisionData;
  mtf?: MTFReportData;
  selectedSensitivity?: number;
  onSensitivityChange?: (s: number) => void;
}) {
  const wrap = useRef<HTMLDivElement>(null);
  const box = useRef<HTMLDivElement>(null);
  const chart = useRef<{
    api: IChartApi;
    candles: ISeriesApi<"Candlestick">;
    volume: ISeriesApi<"Histogram">;
    vwap: ISeriesApi<"Line">;
    ema9: ISeriesApi<"Line">;
    ema20: ISeriesApi<"Line">;
    ema50: ISeriesApi<"Line">;
    ema200: ISeriesApi<"Line">;
    lines: IPriceLine[];
    overlays: InstitutionalOverlays;
  }>(null);

  const shownKey = useRef("");
  const [hover, setHover] = useState<number | null>(null);

  // Preset & Layer Toggles (Default: Clean Mode with important things only)
  const [mode, setMode] = useState<"clean" | "pro">("clean");
  const [showLevels, setShowLevels] = useState(true);
  const [showStructure, setShowStructure] = useState(false);
  const [showEMAs, setShowEMAs] = useState(true);
  const [showChannels, setShowChannels] = useState(true);
  const [showMomentum, setShowMomentum] = useState(false);

  const intraday = (bars[0]?.date.length ?? 0) > 10;

  // Time mapping
  const data = useMemo(
    () => bars.map((b) => ({ ...b, time: toTime(b.date) })).filter((b, i, all) => i === 0 || b.time > all[i - 1].time),
    [bars],
  );

  const timeMap = useMemo(() => {
    const map = new Map<string, Time>();
    for (const b of data) {
      map.set(b.date, b.time);
    }
    return map;
  }, [data]);

  // Initialize Chart
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
      upColor: "#10b981",
      downColor: "#f43f5e",
      wickUpColor: "#10b981",
      wickDownColor: "#f43f5e",
      borderVisible: true,
      borderUpColor: "#10b981",
      borderDownColor: "#f43f5e",
    });

    const volume = api.addSeries(HistogramSeries, {
      priceScaleId: "",
      priceFormat: { type: "volume" },
      lastValueVisible: false,
      priceLineVisible: false,
    });
    volume.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });

    // Key Reference: VWAP (Institutional Benchmark)
    const vwap = api.addSeries(LineSeries, {
      color: "#8b5cf6",
      lineWidth: 2,
      title: "VWAP",
      priceLineVisible: false,
      lastValueVisible: true,
      crosshairMarkerVisible: false,
    });

    // Key Reference: EMA 20 (Dynamic Pullback Support/Resistance)
    const ema20 = api.addSeries(LineSeries, {
      color: "#f59e0b",
      lineWidth: 1,
      title: "EMA 20",
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    });

    // Optional Secondary EMAs (used in Pro mode)
    const ema9 = api.addSeries(LineSeries, {
      color: "#06b6d4",
      lineWidth: 1,
      title: "EMA 9",
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    });

    const ema50 = api.addSeries(LineSeries, {
      color: "#3b82f6",
      lineWidth: 1,
      title: "EMA 50",
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    });

    const ema200 = api.addSeries(LineSeries, {
      color: "#64748b",
      lineWidth: 1,
      title: "EMA 200",
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    });

    const onMove = (p: MouseEventParams) => setHover(p.logical ?? null);
    api.subscribeCrosshairMove(onMove);

    const overlays = new InstitutionalOverlays();
    candles.attachPrimitive(overlays);

    chart.current = {
      api,
      candles,
      volume,
      vwap,
      ema9,
      ema20,
      ema50,
      ema200,
      lines: [],
      overlays,
    };

    return () => {
      api.unsubscribeCrosshairMove(onMove);
      api.remove();
      chart.current = null;
    };
  }, []);

  // Set Candlestick & Volume Data
  useEffect(() => {
    const c = chart.current;
    if (!c) return;

    c.api.applyOptions({ timeScale: { timeVisible: intraday, secondsVisible: false } });
    c.candles.priceScale().applyOptions({
      scaleMargins: { top: 0.08, bottom: data.some((b) => b.volume) ? 0.22 : 0.05 },
    });

    c.candles.setData(
      data.map((b) => ({
        time: b.time,
        open: b.open ?? b.close,
        high: b.high ?? b.close,
        low: b.low ?? b.close,
        close: b.close,
      })),
    );

    c.volume.setData(
      data.map((b) => ({
        time: b.time,
        value: b.volume ?? 0,
        color: b.close >= (b.open ?? b.close) ? "#10b98118" : "#f43f5e18",
      })),
    );

    const key = `${data[0]?.date}|${intraday}`;
    if (data.length && key !== shownKey.current) {
      shownKey.current = key;
      c.api.timeScale().setVisibleLogicalRange({ from: data.length - 100, to: data.length + 8 });
    }
  }, [data, intraday]);

  // Set VWAP & EMAs Data (Clean Mode: VWAP + EMA 20 only)
  useEffect(() => {
    const c = chart.current;
    if (!c) return;

    // Key Reference: VWAP
    c.vwap.setData(
      showEMAs ? data.flatMap((b) => (b.vwap != null ? [{ time: b.time, value: b.vwap }] : [])) : [],
    );

    // Dynamic Moving Averages
    if (showEMAs && indicators) {
      const getLineData = (arr?: (number | null)[]) =>
        arr
          ? data.flatMap((b, i) =>
              arr[i] != null ? [{ time: b.time, value: arr[i]! }] : [],
            )
          : [];

      if (mode === "clean") {
        // Clean View: Only VWAP and EMA 20 for crystal-clear price action
        c.ema9.setData([]);
        c.ema20.setData(getLineData(indicators.ema20));
        c.ema50.setData([]);
        c.ema200.setData([]);
      } else {
        // Pro View: All EMAs
        c.ema9.setData(getLineData(indicators.ema9));
        c.ema20.setData(getLineData(indicators.ema20));
        c.ema50.setData(getLineData(indicators.ema50));
        c.ema200.setData(getLineData(indicators.ema200));
      }
    } else {
      c.ema9.setData([]);
      c.ema20.setData([]);
      c.ema50.setData([]);
      c.ema200.setData([]);
    }
  }, [data, showEMAs, indicators, mode]);

  // Set Overlays (S/R Zones & Trade Plans)
  useEffect(() => {
    const last = data.at(-1);
    const start = intraday ? data.find((b) => b.date.slice(0, 10) === last?.date.slice(0, 10)) : last;
    const curClose = last?.close ?? 0;

    let activeZones: Zone[] = [];
    if (showLevels) {
      if (mode === "clean") {
        // Clean Mode: Display ONLY the single nearest Support and Resistance zones
        const nearestRes = zones
          .filter((z) => z.low >= curClose || (z.low <= curClose && z.high >= curClose && z.side === "resistance"))
          .sort((a, b) => a.low - b.low)[0];

        const nearestSup = zones
          .filter((z) => z.high <= curClose || (z.low <= curClose && z.high >= curClose && z.side === "support"))
          .sort((a, b) => b.high - a.high)[0];

        activeZones = [nearestRes, nearestSup].filter(Boolean) as Zone[];
      } else {
        activeZones = zones;
      }
    }

    const zoneBoxes = activeZones.map((z) => ({
      low: z.low,
      high: z.high,
      color: zoneColor(z),
      right: true,
      text: `${z.side === "resistance" ? "Resistance" : "Support"} · ${num(z.low)}–${num(z.high)}`,
    }));

    const riskBoxes = plans.flatMap(({ label, plan: r }) => [
      {
        low: Math.min(r.entry, r.stop),
        high: Math.max(r.entry, r.stop),
        color: DOWN,
        right: true,
        text: `${label} · STOP ${num(r.stop)} · RISK ${inr(r.riskRs)}`,
      },
      ...(r.target === null
        ? []
        : [
            {
              low: Math.min(r.entry, r.target),
              high: Math.max(r.entry, r.target),
              color: UP,
              right: true,
              text: `TARGET ${num(r.target)} · ${r.rr}R`,
            },
          ]),
    ]);

    chart.current?.overlays.update(
      start ? [...zoneBoxes, ...riskBoxes] : [],
      start?.time ?? null,
      marketStructure?.swings ?? [],
      marketStructure?.events ?? [],
      trendlines,
      sweeps,
      timeMap,
      showStructure,
      showChannels,
      showLevels,
      mode,
    );
  }, [zones, plans, data, intraday, showLevels, showStructure, showChannels, marketStructure, trendlines, sweeps, timeMap, mode]);

  // Price Lines on the Scale (Clean Mode: Key Liquidity Levels PDH/PDL, PWH/PWL)
  useEffect(() => {
    const c = chart.current;
    if (!c) return;

    c.lines.forEach((l) => c.candles.removePriceLine(l));
    c.lines = [];

    if (!showLevels) return;

    if (mode === "clean") {
      // In Clean Mode, display only key institutional Liquidity Levels (PDH/BSL, PDL/SSL, PWH/PWL)
      const curClose = data.at(-1)?.close ?? 0;
      const maxDist = curClose * 0.035;

      const liquidityLabels = ["PDH", "PDL", "PWH", "PWL"];
      const matchedLevels = levels.filter(
        (l) => liquidityLabels.includes(l.label) && Math.abs(l.price - curClose) <= maxDist
      );

      const getLiquidityTitle = (label: string) => {
        if (label === "PDH") return "PDH · BSL";
        if (label === "PDL") return "PDL · SSL";
        if (label === "PWH") return "PWH · Weekly BSL";
        if (label === "PWL") return "PWL · Weekly SSL";
        return label;
      };

      for (const lv of matchedLevels) {
        c.lines.push(
          c.candles.createPriceLine({
            price: lv.price,
            color: levelColor(lv.label),
            lineWidth: 1,
            lineStyle: LineStyle.Dashed,
            axisLabelVisible: true,
            title: getLiquidityTitle(lv.label),
          })
        );
      }
    } else {
      // Pro Mode: Standard multi-level display
      const curClose = data.at(-1)?.close ?? 0;
      const maxDist = curClose * 0.03;

      const validLevels = levels.filter((lv) => Math.abs(lv.price - curClose) <= maxDist);
      const byPrice = new Map<number, string[]>();
      for (const l of validLevels) {
        if (l.label.startsWith("Entry") || !zones.some((z) => z.low <= l.price && l.price <= z.high)) {
          byPrice.set(l.price, [...(byPrice.get(l.price) ?? []), l.label]);
        }
      }

      c.lines = [...byPrice].map(([price, labels]) =>
        c.candles.createPriceLine({
          price,
          color: levelColor(labels[0]),
          lineWidth: 1,
          lineStyle: LineStyle.SparseDotted,
          axisLabelVisible: true,
          title: labels.join("/"),
        }),
      );
    }
  }, [levels, zones, plans, showLevels, mode, data]);

  const shown = data[hover !== null && hover >= 0 && hover < data.length ? hover : data.length - 1];
  const prev = shown && data[data.indexOf(shown) - 1];
  const op = shown ? shown.open ?? shown.close : 0;
  const change = shown ? shown.close - (prev?.close ?? op) : 0;

  return (
    <div ref={wrap} className="space-y-3 bg-white [&:fullscreen]:flex [&:fullscreen]:flex-col [&:fullscreen]:p-4">
      {/* 1. Trading Decision Dashboard (Phase 9 & Phase 7) */}
      <TradingDecisionDashboard
        decision={decision}
        regime={regime}
        mtf={mtf}
        symbol={bars.length ? "NIFTY" : "INDEX"}
        currentPrice={shown?.close}
      />

      {/* 2. Chart Header & Layer Controls Toolbar (Phase 8) */}
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 border-b border-zinc-100 pb-2 text-xs">
        {/* OHLC Bar Inspector */}
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[12px] text-zinc-700">
          {shown && (
            <>
              <span className="font-semibold text-zinc-900">
                {dayStr(shown.date)} {shown.date.slice(0, 4)}
                {intraday && ` · ${shown.date.slice(11)} IST`}
              </span>
              <span>O {num(op)}</span>
              <span>H {num(shown.high ?? shown.close)}</span>
              <span>L {num(shown.low ?? shown.close)}</span>
              <span className="font-semibold">C {num(shown.close)}</span>
              <span className={change >= 0 ? "text-emerald-700 font-semibold" : "text-rose-700 font-semibold"}>
                {change >= 0 ? "+" : ""}
                {num(change)} ({((change / ((prev?.close ?? op) || 1)) * 100).toFixed(2)}%)
              </span>
              {shown.vwap != null && (
                <span className="text-fuchsia-700 font-semibold">VWAP {num(shown.vwap)}</span>
              )}
            </>
          )}
        </div>

        {/* Preset Selector & Layer Controls */}
        <div className="flex flex-wrap items-center gap-2 font-sans text-xs">
          {/* Preset switch: Clean View vs Pro View */}
          <div className="flex items-center rounded-lg bg-zinc-100 p-0.5 border border-zinc-200">
            <button
              onClick={() => {
                setMode("clean");
                setShowStructure(false);
                setShowChannels(true);
                setShowMomentum(false);
                setShowLevels(true);
                setShowEMAs(true);
              }}
              className={`rounded-md px-2.5 py-1 text-[11px] font-semibold transition ${
                mode === "clean"
                  ? "bg-white text-zinc-900 shadow-sm"
                  : "text-zinc-600 hover:text-zinc-900"
              }`}
              title="Clean readable chart: Candles, VWAP, EMA 20, Trendlines, Liquidity Levels & Sweeps, and nearest S/R"
            >
              ✨ Clean View
            </button>
            <button
              onClick={() => {
                setMode("pro");
                setShowStructure(true);
                setShowChannels(true);
                setShowLevels(true);
                setShowEMAs(true);
              }}
              className={`rounded-md px-2.5 py-1 text-[11px] font-semibold transition ${
                mode === "pro"
                  ? "bg-white text-zinc-900 shadow-sm"
                  : "text-zinc-600 hover:text-zinc-900"
              }`}
              title="Detailed view: Multi-EMAs, Structure breaks (BOS/CHoCH), Trendlines, Sweeps, Oscillators"
            >
              🔬 Pro View
            </button>
          </div>

          <div className="h-4 w-[1px] bg-zinc-200" />

          {/* Dynamic EMAs / VWAP Toggle */}
          <button
            onClick={() => setShowEMAs(!showEMAs)}
            className={`rounded-md px-2 py-1 border text-[11px] font-medium transition ${
              showEMAs
                ? "bg-purple-50 text-purple-700 border-purple-200"
                : "text-zinc-500 hover:bg-zinc-50 border-zinc-200"
            }`}
            title="Toggle VWAP and EMA"
          >
            {mode === "clean" ? "VWAP & EMA 20" : "All EMAs + VWAP"}
          </button>

          {/* Key Liquidity Levels Toggle */}
          <button
            onClick={() => setShowLevels(!showLevels)}
            className={`rounded-md px-2 py-1 border text-[11px] font-medium transition ${
              showLevels
                ? "bg-amber-50 text-amber-700 border-amber-200"
                : "text-zinc-500 hover:bg-zinc-50 border-zinc-200"
            }`}
            title="Toggle Liquidity Levels (PDH/PDL, EQH/EQL) and Support/Resistance"
          >
            {mode === "clean" ? "Liquidity & S/R" : "All Key Levels"}
          </button>

          {/* Trendline & Channels Toggle */}
          <button
            onClick={() => setShowChannels(!showChannels)}
            className={`rounded-md px-2 py-1 border text-[11px] font-medium transition ${
              showChannels
                ? "bg-teal-50 text-teal-700 border-teal-200"
                : "text-zinc-500 hover:bg-zinc-50 border-zinc-200"
            }`}
            title="Toggle Trendlines and Price Channels"
          >
            Trendlines
          </button>

          {/* Market Structure Toggle */}
          <button
            onClick={() => setShowStructure(!showStructure)}
            className={`rounded-md px-2 py-1 border text-[11px] font-medium transition ${
              showStructure
                ? "bg-emerald-50 text-emerald-700 border-emerald-200"
                : "text-zinc-500 hover:bg-zinc-50 border-zinc-200"
            }`}
            title="Toggle Structure Breaks (BOS/CHoCH) and Swings"
          >
            Structure (BOS)
          </button>

          {/* Momentum Panel Toggle */}
          <button
            onClick={() => setShowMomentum(!showMomentum)}
            className={`rounded-md px-2 py-1 border text-[11px] font-medium transition ${
              showMomentum
                ? "bg-zinc-800 text-white border-zinc-800"
                : "text-zinc-500 hover:bg-zinc-50 border-zinc-200"
            }`}
            title="Toggle RSI, MACD, ADX oscillator subpanel"
          >
            Oscillators
          </button>

          {/* Swing Sensitivity Selector */}
          {onSensitivityChange && (
            <div className="flex items-center gap-1 border-l border-zinc-200 pl-1.5 ml-0.5">
              <span className="text-[10px] text-zinc-400 uppercase font-semibold">Swing:</span>
              {[2, 3, 5].map((s) => (
                <button
                  key={s}
                  onClick={() => onSensitivityChange(s)}
                  className={`rounded px-1.5 py-0.5 text-[10px] font-semibold border ${
                    selectedSensitivity === s
                      ? "bg-zinc-800 text-white border-zinc-800"
                      : "text-zinc-600 hover:bg-zinc-100 border-zinc-200"
                  }`}
                >
                  {s}b
                </button>
              ))}
            </div>
          )}

          {/* Fullscreen Button */}
          <button
            className="rounded border px-2 py-0.5 text-[11px] text-zinc-700 hover:bg-zinc-50 border-zinc-200 ml-auto"
            onClick={() => (document.fullscreenElement ? document.exitFullscreen() : wrap.current?.requestFullscreen())}
            title="Fullscreen"
          >
            ⛶
          </button>
        </div>
      </div>

      {/* 3. Primary Candlestick Chart Canvas */}
      <div className="relative h-[540px] w-full [:fullscreen_&]:h-auto [:fullscreen_&]:flex-1 rounded-lg border border-zinc-100 overflow-hidden shadow-xs">
        <div ref={box} className="absolute inset-0" />
        {!data.length && (
          <p className="absolute inset-0 grid place-items-center text-sm text-zinc-500">Loading candles…</p>
        )}
      </div>

      {/* 4. Momentum & Volatility Panel */}
      {showMomentum && (
        <MomentumPanel
          indicators={indicators}
          dates={data.map((b) => b.date)}
          hoverIndex={hover}
        />
      )}

      {/* Legend & Clean Guide */}
      <div className="flex flex-wrap items-center justify-between text-[11px] text-zinc-500 pt-1 border-t border-zinc-100">
        <div className="flex flex-wrap gap-x-4 gap-y-1">
          <span className="flex items-center gap-1.5 font-medium text-purple-700">
            <span className="inline-block w-3 h-0.5 bg-[#8b5cf6]" /> VWAP
          </span>
          <span className="flex items-center gap-1.5 font-medium text-amber-600">
            <span className="inline-block w-3 h-0.5 bg-[#f59e0b]" /> EMA 20
          </span>
          <span className="flex items-center gap-1.5 text-amber-700 font-medium">
            <span className="inline-block w-3 h-0.5 border-t border-dashed border-amber-600" /> Liquidity (PDH/PDL)
          </span>
          <span className="flex items-center gap-1.5 text-emerald-700 font-medium">
            <span className="inline-block w-3 h-0.5 bg-emerald-600" /> Trendline
          </span>
          <span className="flex items-center gap-1 text-amber-800">
            <span className="inline-block px-1 rounded text-[9px] bg-yellow-100 border border-yellow-300 font-semibold">⚡ Sweep</span> Liquidity Sweep
          </span>
          <span className="flex items-center gap-1.5 text-zinc-600">
            <span className="inline-block w-2.5 h-2.5 rounded-sm bg-emerald-600/15 border border-emerald-600/40" /> Nearest Support
          </span>
          <span className="flex items-center gap-1.5 text-zinc-600">
            <span className="inline-block w-2.5 h-2.5 rounded-sm bg-rose-600/15 border border-rose-600/40" /> Nearest Resistance
          </span>
          {mode === "pro" && (
            <>
              <span className="flex items-center gap-1 text-zinc-500">
                <span className="inline-block w-2.5 h-0.5 bg-[#06b6d4]" /> EMA 9
              </span>
              <span className="flex items-center gap-1 text-zinc-500">
                <span className="inline-block w-2.5 h-0.5 bg-[#3b82f6]" /> EMA 50
              </span>
              <span className="flex items-center gap-1 text-zinc-500">
                <span className="inline-block w-2.5 h-0.5 bg-[#64748b]" /> EMA 200
              </span>
              <span className="flex items-center gap-1 text-zinc-500">
                <span className="inline-block w-2.5 h-2.5 rounded-sm bg-emerald-600/30 border border-emerald-600" /> BOS
              </span>
            </>
          )}
        </div>
        <div className="font-mono text-[10px] text-zinc-400">
          Clean Mode Active · Non-Repainting · Zero Lookahead
        </div>
      </div>
    </div>
  );
}
