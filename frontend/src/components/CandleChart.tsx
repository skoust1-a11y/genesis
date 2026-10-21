"use client";

import {
  AreaSeries,
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  LineStyle,
  type IChartApi,
  type IPriceLine,
  type IPrimitivePaneRenderer,
  type IPrimitivePaneView,
  type ISeriesApi,
  type ISeriesPrimitive,
  type Logical,
  type MouseEventParams,
  type SeriesAttachedParameter,
  type Time,
  type UTCTimestamp,
  createChart,
} from "lightweight-charts";
import { useEffect, useMemo, useRef, useState } from "react";

import { inr } from "@/lib/api";
import type { RiskPlan } from "@/lib/risk";
import type { IndicatorData } from "@/components/MomentumPanel";

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
  side?: "high" | "low";
  line_type?: "ascending_support" | "descending_resistance" | "ascending_resistance" | "descending_support" | string;
  color?: string;
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
  touch_count?: number;
  body_violations?: number;
  quality_score?: number;
  apex_index?: number | null;
  apex_price?: number | null;
};

export type TrendlineStyleConfig = {
  type: "ascending_support" | "descending_resistance" | "ascending_resistance" | "descending_support";
  label: string;
  color: string;
  badgeBg: string;
  badgeBorder: string;
  badgeText: string;
};

export function getTrendlineStyle(tl: TrendlineData): TrendlineStyleConfig {
  const isHigh =
    tl.side === "high"
      ? true
      : tl.side === "low"
      ? false
      : (tl.line_type ? tl.line_type.includes("resistance") : false) ||
        (tl.label ? tl.label.includes("Resistance") : false) ||
        tl.channel_low != null ||
        tl.kind === "descending";

  if (isHigh) {
    if (tl.kind === "descending") {
      return {
        type: "descending_resistance",
        label: "Descending Resistance",
        color: tl.color || "#f43f5e",
        badgeBg: "#ffe4e6f4",
        badgeBorder: "#f43f5e",
        badgeText: "#9f1239",
      };
    } else {
      return {
        type: "ascending_resistance",
        label: "Ascending Resistance",
        color: tl.color || "#f59e0b",
        badgeBg: "#fef3c7f4",
        badgeBorder: "#f59e0b",
        badgeText: "#92400e",
      };
    }
  } else {
    if (tl.kind === "ascending") {
      return {
        type: "ascending_support",
        label: "Ascending Support",
        color: tl.color || "#10b981",
        badgeBg: "#dcfce7f4",
        badgeBorder: "#10b981",
        badgeText: "#065f46",
      };
    } else {
      return {
        type: "descending_support",
        label: "Descending Support",
        color: tl.color || "#0284c7",
        badgeBg: "#e0f2fef4",
        badgeBorder: "#0284c7",
        badgeText: "#0369a1",
      };
    }
  }
}

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

const UP = "#089981";
const DOWN = "#f23645";
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const dayStr = (d: string) => `${d.slice(8, 10)} ${MONTHS[Number(d.slice(5, 7)) - 1]}`;
const num = (v: number) => v.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

const levelColor = (l: string, isDark: boolean) =>
  l.startsWith("Entry")
    ? "#a855f7"
    : l.startsWith("PDH") ||
      l.startsWith("PWH") ||
      l.startsWith("PMH") ||
      l.startsWith("52WH") ||
      l === "DH" ||
      l.startsWith("Res") ||
      l.startsWith("Flip Res") ||
      /^R[1-4]/.test(l)
    ? isDark ? "#f87171" : "#e11d48"
    : l.startsWith("PDL") ||
      l.startsWith("PWL") ||
      l.startsWith("PML") ||
      l.startsWith("52WL") ||
      l === "DL" ||
      l.startsWith("Sup") ||
      l.startsWith("Flip Sup") ||
      /^S[1-4]/.test(l)
    ? isDark ? "#34d399" : "#059669"
    : /^(EQH)/.test(l)
    ? "#fbbf24"
    : /^(EQL)/.test(l)
    ? "#34d399"
    : /^(BSL)/.test(l)
    ? isDark ? "#f87171" : "#e11d48"
    : /^(SSL)/.test(l)
    ? isDark ? "#34d399" : "#059669"
    : l === "P"
    ? "#818cf8"
    : isDark ? "#94a3b8" : "#64748b";

const zoneColor = (z: Zone) => (z.side === "support" ? UP : z.side === "resistance" ? DOWN : "#71717a");

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
  private events: StructureEvent[] = [];
  private trendlines: TrendlineData[] = [];
  private sweeps: LiquiditySweepData[] = [];
  private timeMap: Map<string, Time> = new Map();
  private dates: string[] = [];
  private showStructure = false;
  private showTrendlines = true;
  private showZones = true;
  private showSweeps = true;
  private isDark = true;

  attached(p: SeriesAttachedParameter<Time>) {
    this.p = p;
  }

  detached() {
    this.p = null;
  }

  update(
    boxes: Box[],
    start: Time | null,
    events: StructureEvent[],
    trendlines: TrendlineData[],
    sweeps: LiquiditySweepData[],
    timeMap: Map<string, Time>,
    dates: string[],
    showStructure: boolean,
    showTrendlines: boolean,
    showZones: boolean,
    showSweeps: boolean,
    isDark: boolean
  ) {
    this.boxes = boxes;
    this.start = start;
    this.events = events;
    this.trendlines = trendlines;
    this.sweeps = sweeps;
    this.timeMap = timeMap;
    this.dates = dates;
    this.showStructure = showStructure;
    this.showTrendlines = showTrendlines;
    this.showZones = showZones;
    this.showSweeps = showSweeps;
    this.isDark = isDark;
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

        // 1. Draw S/R Zones & Risk/Reward Boxes (subtle, clean, spanning chart)
        if (this.showZones && this.boxes.length > 0) {
          ctx.font = `600 ${9 * vr}px ui-sans-serif, system-ui, sans-serif`;
          ctx.textBaseline = "middle";

          for (const b of this.boxes) {
            const top = series.priceToCoordinate(b.high);
            const bottom = series.priceToCoordinate(b.low);
            if (top === null || bottom === null) continue;

            const [x, y, w, h] = [
              0,
              top * vr,
              bitmapSize.width,
              Math.max(2 * vr, (bottom - top) * vr),
            ];

            // Ultra-soft translucent band
            ctx.fillStyle = `${b.color}0a`;
            ctx.fillRect(x, y, w - x, h);
            ctx.strokeStyle = `${b.color}30`;
            ctx.lineWidth = hr;
            ctx.setLineDash([3 * hr, 3 * hr]);
            ctx.strokeRect(x, y, w - x, h);
            ctx.setLineDash([]);

            // Neat tag pinned to the right edge
            const tw = ctx.measureText(b.text).width + 10 * hr;
            const th = 14 * vr;
            const tx = w - tw - 10 * hr;
            const ty = y + h / 2 - th / 2;

            ctx.fillStyle = this.isDark ? "#1e222df0" : "#fffffff0";
            ctx.beginPath();
            ctx.roundRect?.(tx, ty, tw, th, 3 * hr);
            ctx.fill();
            ctx.strokeStyle = `${b.color}40`;
            ctx.lineWidth = hr;
            ctx.stroke();

            ctx.fillStyle = b.color;
            ctx.fillText(b.text, tx + 5 * hr, ty + th / 2);
          }
        }

        // 2. Draw 3-point Non-Crossing Trendlines (Active & Historical)
        if (this.showTrendlines && this.trendlines.length > 0) {
          const linesToDraw = this.trendlines.filter(
            (tl) => (tl.touch_count ?? 3) >= 3
          );

          const totalBars = this.dates.length;

          const getXCoord = (index?: number | null, dateStr?: string | null): number | null => {
            if (index !== undefined && index !== null && index >= 0) {
              const coord = timeScale.logicalToCoordinate(index as Logical);
              if (coord !== null) return coord;
            }
            if (dateStr) {
              const t = this.timeMap.get(dateStr) ?? toTime(dateStr);
              if (t) {
                const coord = timeScale.timeToCoordinate(t);
                if (coord !== null) return coord;
              }
            }
            return null;
          };

          type LineCoords = {
            tl: TrendlineData;
            x1: number;
            y1: number;
            x2: number;
            y2: number;
            slopePx: number;
            extX: number;
            extY: number;
            xBreakPx: number | null;
          };

          const preparedLines: LineCoords[] = [];

          for (const tl of linesToDraw) {
            const x1 = getXCoord(tl.start_index, tl.start_date);
            const x2 = getXCoord(tl.end_index, tl.end_date);
            const y1 = series.priceToCoordinate(tl.start_price);
            const y2 = series.priceToCoordinate(tl.end_price);
            if (x1 === null || x2 === null || y1 === null || y2 === null) continue;

            const isLiveActive = !tl.is_broken && (totalBars - tl.end_index <= 80 || tl.end_index >= totalBars - 15);
            const xBreak = (tl.break_index != null || tl.break_date)
              ? getXCoord(tl.break_index, tl.break_date)
              : null;

            let extX: number;
            if (isLiveActive) {
              extX = bitmapSize.width;
            } else if (tl.is_broken && xBreak !== null && xBreak > x2) {
              extX = xBreak * hr + 4 * hr;
            } else {
              const avgBarPx = Math.abs(x2 - x1) / Math.max(tl.end_index - tl.start_index, 1);
              extX = x2 * hr + 16 * avgBarPx * hr;
            }

            if (tl.apex_index != null) {
              const xApex = getXCoord(tl.apex_index);
              if (xApex !== null && xApex * hr > x2 * hr) {
                extX = Math.min(extX, xApex * hr);
              }
            }

            // Skip lines completely outside the horizontal canvas window
            const minX = Math.min(x1 * hr, x2 * hr);
            const maxX = Math.max(x1 * hr, x2 * hr, extX);
            if (maxX < -50 || minX > bitmapSize.width + 50) continue;

            const slopePx = (y2 * vr - y1 * vr) / ((x2 - x1) * hr || 1);
            preparedLines.push({
              tl,
              x1: x1 * hr,
              y1: y1 * vr,
              x2: x2 * hr,
              y2: y2 * vr,
              slopePx,
              extX,
              extY: y2 * vr + slopePx * (extX - x2 * hr),
              xBreakPx: xBreak !== null ? xBreak * hr : null,
            });
          }

          // Enforce non-crossing forward rays on active forward lines (same-side only)
          for (let i = 0; i < preparedLines.length; i++) {
            const pA = preparedLines[i];
            for (let j = i + 1; j < preparedLines.length; j++) {
              const pB = preparedLines[j];
              // Top trendlines (resistance ceiling) and bottom trendlines (support floor) must NEVER truncate each other!
              if (pA.tl.side !== pB.tl.side) continue;

              const mDiff = pA.slopePx - pB.slopePx;
              if (Math.abs(mDiff) > 1e-4) {
                const cA = pA.y2 - pA.slopePx * pA.x2;
                const cB = pB.y2 - pB.slopePx * pB.x2;
                const xCross = (cB - cA) / mDiff;
                if (xCross > Math.max(pA.x2, pB.x2)) {
                  if (xCross < pA.extX) {
                    pA.extX = xCross;
                    pA.extY = pA.y2 + pA.slopePx * (pA.extX - pA.x2);
                  }
                  if (xCross < pB.extX) {
                    pB.extX = xCross;
                    pB.extY = pB.y2 + pB.slopePx * (pB.extX - pB.x2);
                  }
                }
              }
            }
          }

          // Render trendlines
          for (const p of preparedLines) {
            const { tl, x1, y1, x2, y2, extX, extY, xBreakPx } = p;
            const style = getTrendlineStyle(tl);
            const tlColor = style.color;

            // 1. Draw Primary Trendline
            ctx.save();
            ctx.strokeStyle = tl.is_broken ? `${tlColor}77` : tlColor;
            ctx.lineWidth = (tl.is_broken ? 1.2 : 1.8) * hr;
            if (tl.is_broken) {
              ctx.setLineDash([5 * hr, 3 * hr]);
            }
            ctx.beginPath();
            ctx.moveTo(x1, y1);
            ctx.lineTo(x2, y2);
            ctx.stroke();
            ctx.restore();

            if (extX > x2) {
              ctx.save();
              ctx.strokeStyle = tl.is_broken ? `${tlColor}44` : `${tlColor}99`;
              ctx.lineWidth = 1.1 * hr;
              ctx.setLineDash([3 * hr, 3 * hr]);
              ctx.beginPath();
              ctx.moveTo(x2, y2);
              ctx.lineTo(extX, extY);
              ctx.stroke();
              ctx.restore();

              if (tl.is_broken && xBreakPx !== null && xBreakPx > x2) {
                const breakY = y2 + p.slopePx * (xBreakPx - x2);
                ctx.save();
                ctx.fillStyle = tlColor;
                ctx.beginPath();
                ctx.arc(xBreakPx, breakY, 3 * hr, 0, Math.PI * 2);
                ctx.fill();
                ctx.restore();
              }
            }

            // 2. Draw Accompanying Parallel Companion Trendline (Bottom floor for Top line, or Top ceiling for Bottom line)
            if (tl.side === "high" && tl.channel_low != null) {
              const yChanCoord = series.priceToCoordinate(tl.channel_low);
              if (yChanCoord !== null) {
                const botY1 = yChanCoord * vr;
                const dy = botY1 - y1;
                const botY2 = y2 + dy;
                const botExtY = extY + dy;
                const botColor = tl.kind === "ascending" ? "#10b981" : "#0284c7";

                // Channel corridor shading
                ctx.save();
                ctx.fillStyle = this.isDark ? "rgba(56, 189, 248, 0.04)" : "rgba(14, 165, 233, 0.04)";
                ctx.beginPath();
                ctx.moveTo(x1, y1);
                ctx.lineTo(Math.max(x2, extX), extY);
                ctx.lineTo(Math.max(x2, extX), botExtY);
                ctx.lineTo(x1, botY1);
                ctx.closePath();
                ctx.fill();
                ctx.restore();

                // Bottom Trendline Floor
                ctx.save();
                ctx.strokeStyle = tl.is_broken ? `${botColor}77` : botColor;
                ctx.lineWidth = (tl.is_broken ? 1.2 : 1.8) * hr;
                if (tl.is_broken) {
                  ctx.setLineDash([5 * hr, 3 * hr]);
                }
                ctx.beginPath();
                ctx.moveTo(x1, botY1);
                ctx.lineTo(x2, botY2);
                ctx.stroke();
                ctx.restore();

                if (extX > x2) {
                  ctx.save();
                  ctx.strokeStyle = tl.is_broken ? `${botColor}44` : `${botColor}99`;
                  ctx.lineWidth = 1.1 * hr;
                  ctx.setLineDash([3 * hr, 3 * hr]);
                  ctx.beginPath();
                  ctx.moveTo(x2, botY2);
                  ctx.lineTo(extX, botExtY);
                  ctx.stroke();
                  ctx.restore();
                }
              }
            } else if (tl.side === "low" && tl.channel_high != null) {
              const yChanCoord = series.priceToCoordinate(tl.channel_high);
              if (yChanCoord !== null) {
                const topY1 = yChanCoord * vr;
                const dy = topY1 - y1;
                const topY2 = y2 + dy;
                const topExtY = extY + dy;
                const topColor = tl.kind === "descending" ? "#f43f5e" : "#f59e0b";

                // Channel corridor shading
                ctx.save();
                ctx.fillStyle = this.isDark ? "rgba(245, 158, 11, 0.04)" : "rgba(245, 158, 11, 0.04)";
                ctx.beginPath();
                ctx.moveTo(x1, y1);
                ctx.lineTo(Math.max(x2, extX), extY);
                ctx.lineTo(Math.max(x2, extX), topExtY);
                ctx.lineTo(x1, topY1);
                ctx.closePath();
                ctx.fill();
                ctx.restore();

                // Top Trendline Ceiling
                ctx.save();
                ctx.strokeStyle = tl.is_broken ? `${topColor}77` : topColor;
                ctx.lineWidth = (tl.is_broken ? 1.2 : 1.8) * hr;
                if (tl.is_broken) {
                  ctx.setLineDash([5 * hr, 3 * hr]);
                }
                ctx.beginPath();
                ctx.moveTo(x1, topY1);
                ctx.lineTo(x2, topY2);
                ctx.stroke();
                ctx.restore();

                if (extX > x2) {
                  ctx.save();
                  ctx.strokeStyle = tl.is_broken ? `${topColor}44` : `${topColor}99`;
                  ctx.lineWidth = 1.1 * hr;
                  ctx.setLineDash([3 * hr, 3 * hr]);
                  ctx.beginPath();
                  ctx.moveTo(x2, topY2);
                  ctx.lineTo(extX, topExtY);
                  ctx.stroke();
                  ctx.restore();
                }
              }
            }
          }
        }

        // 3. Draw BOS & CHoCH (Structure Breaks)
        if (this.showStructure && this.events.length > 0) {
          ctx.font = `600 ${8.5 * vr}px ui-sans-serif, system-ui, sans-serif`;
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

            ctx.save();
            ctx.strokeStyle = col;
            ctx.lineWidth = 1 * hr;
            ctx.setLineDash([3 * hr, 3 * hr]);
            ctx.beginPath();
            ctx.moveTo(xs * hr, yLevel * vr);
            ctx.lineTo(xb * hr, yLevel * vr);
            ctx.stroke();
            ctx.restore();

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

        // 4. Draw Liquidity Sweeps
        if (this.showSweeps && this.sweeps.length > 0) {
          ctx.font = `bold ${8 * vr}px ui-sans-serif, system-ui, sans-serif`;
          const sortedSweeps = [...this.sweeps].sort((a, b) => (b.sweep_index ?? 0) - (a.sweep_index ?? 0));
          const recentSweeps = sortedSweeps.slice(0, 3);

          for (const sw of recentSweeps) {
            const t = this.timeMap.get(sw.sweep_date);
            if (!t) continue;
            const x = timeScale.timeToCoordinate(t);
            const y = series.priceToCoordinate(sw.extreme_price);
            const yLevel = series.priceToCoordinate(sw.level_price);
            if (x === null || y === null) continue;

            if (yLevel !== null) {
              ctx.save();
              ctx.setLineDash([3 * hr, 3 * hr]);
              ctx.strokeStyle = sw.reclaimed ? "#eab308cc" : "#f97316cc";
              ctx.lineWidth = 1.1 * hr;
              ctx.beginPath();
              ctx.moveTo((x - 24) * hr, yLevel * vr);
              ctx.lineTo((x + 24) * hr, yLevel * vr);
              ctx.stroke();
              ctx.restore();
            }

            const isLowSweep = sw.level_label.includes("L");
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

export type ChartType = "candlestick" | "line" | "area";

export function CandleChart({
  bars,
  levels = [],
  zones = [],
  plans = [],
  marketStructure,
  trendlines = [],
  sweeps = [],
  indicators,
  symbol = "NIFTY",
  timeframe = "5m",
  onTimeframeChange,
  selectedSensitivity = 2,
  onSensitivityChange,
  theme: propTheme,
  onThemeChange,
}: {
  bars: Bar[];
  levels?: Level[];
  zones?: Zone[];
  plans?: { label: string; plan: RiskPlan }[];
  marketStructure?: { swings: SwingPoint[]; events: StructureEvent[]; sensitivity?: number };
  trendlines?: TrendlineData[];
  sweeps?: LiquiditySweepData[];
  indicators?: IndicatorData;
  symbol?: string;
  timeframe?: string;
  onTimeframeChange?: (tf: string) => void;
  selectedSensitivity?: number;
  onSensitivityChange?: (s: number) => void;
  theme?: "dark" | "light";
  onThemeChange?: (t: "dark" | "light") => void;
}) {
  const wrap = useRef<HTMLDivElement>(null);
  const box = useRef<HTMLDivElement>(null);
  const chart = useRef<{
    api: IChartApi;
    mainSeries: ISeriesApi<"Candlestick" | "Line" | "Area">;
    volume: ISeriesApi<"Histogram">;
    vwap: ISeriesApi<"Line">;
    ema20: ISeriesApi<"Line">;
    ema50: ISeriesApi<"Line">;
    ema200: ISeriesApi<"Line">;
    rsiSeries?: ISeriesApi<"Line">;
    macdHist?: ISeriesApi<"Histogram">;
    macdLine?: ISeriesApi<"Line">;
    signalLine?: ISeriesApi<"Line">;
    lines: IPriceLine[];
    overlays: InstitutionalOverlays;
  }>(null);

  const shownKey = useRef("");
  const lastLogicalRange = useRef<{ from: number; to: number } | null>(null);
  const [visibleLogicalRange, setVisibleLogicalRange] = useState<{ from: number; to: number } | null>(null);
  const [hover, setHover] = useState<number | null>(null);

  // TradingView Style Configuration State
  const [theme, setTheme] = useState<"dark" | "light">(() => propTheme || "dark");
  const [chartType, setChartType] = useState<ChartType>("candlestick");
  const [showIndicatorsMenu, setShowIndicatorsMenu] = useState(false);

  // Overlay Toggles (On Main Chart)
  const [showVwap, setShowVwap] = useState(true);
  const [showEma20, setShowEma20] = useState(true);
  const [showEma50_200, setShowEma50_200] = useState(false);
  const [showLevelsAndTrendlines, setShowLevelsAndTrendlines] = useState(true);
  const [showStructure, setShowStructure] = useState(false);
  const [showSweeps, setShowSweeps] = useState(true);

  // Separate Sub-Pane Toggles (Below Main Chart)
  const [showRsi, setShowRsi] = useState(false);
  const [showMacd, setShowMacd] = useState(false);

  const isDark = theme === "dark";
  const intraday = (bars[0]?.date.length ?? 0) > 10;

  // Persist Theme & Sync with prop
  useEffect(() => {
    if (propTheme) {
      setTheme(propTheme);
      return;
    }
    const saved = window.localStorage.getItem("tsl.chart.theme") as "dark" | "light" | null;
    if (saved === "dark" || saved === "light") {
      setTheme(saved);
    }
  }, [propTheme]);

  const setMode = (next: "dark" | "light") => {
    setTheme(next);
    window.localStorage.setItem("tsl.chart.theme", next);
    onThemeChange?.(next);
  };

  // Time mapping
  const data = useMemo(
    () => bars.map((b) => ({ ...b, time: toTime(b.date) })).filter((b, i, all) => i === 0 || b.time > all[i - 1].time),
    [bars]
  );

  const timeMap = useMemo(() => {
    const map = new Map<string, Time>();
    for (const b of data) {
      map.set(b.date, b.time);
    }
    return map;
  }, [data]);

  // Total active panes count determines the dynamic container height
  const paneCount = 1 + (showRsi ? 1 : 0) + (showMacd ? 1 : 0);
  const chartPixelHeight = paneCount === 3 ? 740 : paneCount === 2 ? 620 : 520;

  // Determine whether the active viewing range is increased (e.g. zoomed out, wide price span, higher timeframe)
  const { vMin, vMax, visiblePriceSpan, isRangeIncreased } = useMemo(() => {
    if (!data.length) return { vMin: 0, vMax: 0, visiblePriceSpan: 0, isRangeIncreased: false };

    let minP = 0;
    let maxP = 0;
    let span = 0;
    let barCount = 90;

    if (visibleLogicalRange) {
      const fromIdx = Math.max(0, Math.floor(visibleLogicalRange.from));
      const toIdx = Math.min(data.length - 1, Math.ceil(visibleLogicalRange.to));
      barCount = Math.max(1, visibleLogicalRange.to - visibleLogicalRange.from);
      if (toIdx >= fromIdx) {
        const slice = data.slice(fromIdx, toIdx + 1);
        if (slice.length > 0) {
          minP = Math.min(...slice.map((b) => b.low ?? b.close));
          maxP = Math.max(...slice.map((b) => b.high ?? b.close));
          span = maxP - minP;
        }
      }
    } else {
      const slice = data.slice(-90);
      minP = Math.min(...slice.map((b) => b.low ?? b.close));
      maxP = Math.max(...slice.map((b) => b.high ?? b.close));
      span = maxP - minP;
    }

    const curClose = data.at(-1)?.close ?? 20000;
    const increased =
      timeframe === "1h" ||
      timeframe === "1d" ||
      span >= Math.max(380, curClose * 0.016) ||
      barCount >= 120;

    return { vMin: minP, vMax: maxP, visiblePriceSpan: span, isRangeIncreased: increased };
  }, [data, visibleLogicalRange, timeframe]);

  // Initialize or Reconfigure Lightweight Chart when theme, chartType, or pane setup changes
  useEffect(() => {
    if (!box.current) return;

    // Capture current zoom range if available
    if (chart.current?.api) {
      try {
        lastLogicalRange.current = chart.current.api.timeScale().getVisibleLogicalRange();
      } catch {
        // ignore
      }
    }

    const darkBg = "#131722";
    const lightBg = "#ffffff";
    const darkGrid = "#1e222d";
    const lightGrid = "#f0f3fa";
    const darkText = "#9598a1";
    const lightText = "#4b5563";
    const darkBorder = "#2a2e39";
    const lightBorder = "#e5e7eb";

    const api = createChart(box.current, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: isDark ? darkBg : lightBg },
        textColor: isDark ? darkText : lightText,
        fontSize: 11,
      },
      grid: {
        vertLines: { color: isDark ? darkGrid : lightGrid },
        horzLines: { color: isDark ? darkGrid : lightGrid },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: {
          color: isDark ? "#434651" : "#9ca3af",
          labelBackgroundColor: isDark ? "#2962ff" : "#18181b",
        },
        horzLine: {
          color: isDark ? "#434651" : "#9ca3af",
          labelBackgroundColor: isDark ? "#2962ff" : "#18181b",
        },
      },
      rightPriceScale: {
        borderColor: isDark ? darkBorder : lightBorder,
        scaleMargins: { top: 0.08, bottom: 0.18 },
      },
      timeScale: {
        borderColor: isDark ? darkBorder : lightBorder,
        rightOffset: 8,
        minBarSpacing: 3,
        timeVisible: intraday,
        secondsVisible: false,
      },
      localization: { locale: "en-IN", priceFormatter: num },
    });

    // 1. Primary Series (Candles / Line / Area) on Pane 0
    let mainSeries: ISeriesApi<"Candlestick" | "Line" | "Area">;
    if (chartType === "candlestick") {
      mainSeries = api.addSeries(CandlestickSeries, {
        upColor: "#089981",
        downColor: "#f23645",
        wickUpColor: "#089981",
        wickDownColor: "#f23645",
        borderVisible: true,
        borderUpColor: "#089981",
        borderDownColor: "#f23645",
      }, 0);
    } else if (chartType === "line") {
      mainSeries = api.addSeries(LineSeries, {
        color: "#2962ff",
        lineWidth: 2,
        priceLineVisible: true,
        lastValueVisible: true,
      }, 0);
    } else {
      mainSeries = api.addSeries(AreaSeries, {
        topColor: "rgba(41, 98, 255, 0.28)",
        bottomColor: "rgba(41, 98, 255, 0.02)",
        lineColor: "#2962ff",
        lineWidth: 2,
      }, 0);
    }

    // 2. Volume Series at bottom of Pane 0
    const volume = api.addSeries(HistogramSeries, {
      priceScaleId: "",
      priceFormat: { type: "volume" },
      lastValueVisible: false,
      priceLineVisible: false,
    }, 0);
    volume.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });

    // 3. Dynamic Moving Averages Overlays on Pane 0
    const vwap = api.addSeries(LineSeries, {
      color: "#8b5cf6",
      lineWidth: 2,
      title: "VWAP",
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    }, 0);

    const ema20 = api.addSeries(LineSeries, {
      color: "#f59e0b",
      lineWidth: 1,
      title: "EMA 20",
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    }, 0);

    const ema50 = api.addSeries(LineSeries, {
      color: "#3b82f6",
      lineWidth: 1,
      title: "EMA 50",
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    }, 0);

    const ema200 = api.addSeries(LineSeries, {
      color: isDark ? "#94a3b8" : "#64748b",
      lineWidth: 1,
      title: "EMA 200",
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    }, 0);

    // 4. Attach Canvas Overlays Primitive (Trendlines, Zones, Sweeps)
    const overlays = new InstitutionalOverlays();
    mainSeries.attachPrimitive(overlays);

    // 5. Dedicated Sub-Panes (RSI & MACD)
    let currentPaneIndex = 1;
    let rsiSeries: ISeriesApi<"Line"> | undefined;
    let macdHist: ISeriesApi<"Histogram"> | undefined;
    let macdLine: ISeriesApi<"Line"> | undefined;
    let signalLine: ISeriesApi<"Line"> | undefined;

    if (showRsi) {
      const rsiPane = currentPaneIndex++;
      rsiSeries = api.addSeries(LineSeries, {
        color: "#a855f7",
        lineWidth: 2,
        priceFormat: { type: "custom", formatter: (p: number) => p.toFixed(1) },
        priceScaleId: "right",
        lastValueVisible: true,
        priceLineVisible: false,
      }, rsiPane);

      rsiSeries.createPriceLine({
        price: 70,
        color: isDark ? "#ef444466" : "#ef444488",
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: "70",
      });
      rsiSeries.createPriceLine({
        price: 30,
        color: isDark ? "#10b98166" : "#10b98188",
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: "30",
      });
      rsiSeries.createPriceLine({
        price: 50,
        color: isDark ? "#52525b44" : "#a1a1aa55",
        lineWidth: 1,
        lineStyle: LineStyle.Dotted,
        axisLabelVisible: false,
      });
    }

    if (showMacd) {
      const macdPane = currentPaneIndex++;
      macdHist = api.addSeries(HistogramSeries, {
        priceFormat: { type: "custom", formatter: (p: number) => p.toFixed(2) },
        priceScaleId: "right",
        lastValueVisible: false,
        priceLineVisible: false,
      }, macdPane);

      macdLine = api.addSeries(LineSeries, {
        color: "#2962ff",
        lineWidth: 1,
        priceFormat: { type: "custom", formatter: (p: number) => p.toFixed(2) },
        priceScaleId: "right",
        lastValueVisible: true,
        priceLineVisible: false,
      }, macdPane);

      signalLine = api.addSeries(LineSeries, {
        color: "#ff6d00",
        lineWidth: 1,
        priceFormat: { type: "custom", formatter: (p: number) => p.toFixed(2) },
        priceScaleId: "right",
        lastValueVisible: true,
        priceLineVisible: false,
      }, macdPane);

      macdLine.createPriceLine({
        price: 0,
        color: isDark ? "#52525b55" : "#a1a1aa66",
        lineWidth: 1,
        lineStyle: LineStyle.Dotted,
        axisLabelVisible: false,
      });
    }

    // Set Pane Heights
    const panes = api.panes();
    if (panes.length > 1) {
      const totalH = chartPixelHeight;
      const subPaneH = 125;
      const numSubPanes = panes.length - 1;
      const mainH = Math.max(280, totalH - subPaneH * numSubPanes);
      panes[0].setHeight(mainH);
      for (let i = 1; i < panes.length; i++) {
        panes[i].setHeight(subPaneH);
      }
    }

    const onMove = (p: MouseEventParams) => setHover(p.logical ?? null);
    api.subscribeCrosshairMove(onMove);

    chart.current = {
      api,
      mainSeries,
      volume,
      vwap,
      ema20,
      ema50,
      ema200,
      rsiSeries,
      macdHist,
      macdLine,
      signalLine,
      lines: [],
      overlays,
    };

    const onRangeChange = (newRange: { from: number; to: number } | null) => {
      if (newRange) {
        lastLogicalRange.current = newRange;
        setVisibleLogicalRange(newRange);
      }
    };
    api.timeScale().subscribeVisibleLogicalRangeChange(onRangeChange);

    return () => {
      api.timeScale().unsubscribeVisibleLogicalRangeChange(onRangeChange);
      api.unsubscribeCrosshairMove(onMove);
      api.remove();
      chart.current = null;
    };
  }, [theme, chartType, showRsi, showMacd]);

  // Update Candlestick, Line, or Area Data
  useEffect(() => {
    const c = chart.current;
    if (!c || !data.length) return;

    if (chartType === "candlestick") {
      (c.mainSeries as ISeriesApi<"Candlestick">).setData(
        data.map((b) => ({
          time: b.time,
          open: b.open ?? b.close,
          high: b.high ?? b.close,
          low: b.low ?? b.close,
          close: b.close,
        }))
      );
    } else {
      (c.mainSeries as ISeriesApi<"Line" | "Area">).setData(
        data.map((b) => ({
          time: b.time,
          value: b.close,
        }))
      );
    }

    // Volume histogram
    c.volume.setData(
      data.map((b) => ({
        time: b.time,
        value: b.volume ?? 0,
        color: b.close >= (b.open ?? b.close)
          ? (isDark ? "rgba(8, 153, 129, 0.22)" : "rgba(8, 153, 129, 0.18)")
          : (isDark ? "rgba(242, 54, 69, 0.22)" : "rgba(242, 54, 69, 0.18)"),
      }))
    );

    // Initial View Range or restore last logical range
    const key = `${data[0]?.date}|${intraday}|${data.length}`;
    if (key !== shownKey.current) {
      shownKey.current = key;
      if (lastLogicalRange.current) {
        c.api.timeScale().setVisibleLogicalRange(lastLogicalRange.current);
      } else {
        c.api.timeScale().setVisibleLogicalRange({ from: data.length - 90, to: data.length + 6 });
      }
    }
  }, [data, chartType, isDark, intraday]);

  // Update Overlays: VWAP & EMAs
  useEffect(() => {
    const c = chart.current;
    if (!c) return;

    // VWAP
    c.vwap.setData(
      showVwap ? data.flatMap((b) => (b.vwap != null ? [{ time: b.time, value: b.vwap }] : [])) : []
    );

    // Dynamic EMAs
    const getLineData = (arr?: (number | null)[]) =>
      arr
        ? data.flatMap((b, i) => (arr[i] != null ? [{ time: b.time, value: arr[i]! }] : []))
        : [];

    c.ema20.setData(showEma20 && indicators ? getLineData(indicators.ema20) : []);
    c.ema50.setData(showEma50_200 && indicators ? getLineData(indicators.ema50) : []);
    c.ema200.setData(showEma50_200 && indicators ? getLineData(indicators.ema200) : []);
  }, [data, showVwap, showEma20, showEma50_200, indicators]);

  // Update Sub-Panes Data: RSI & MACD
  useEffect(() => {
    const c = chart.current;
    if (!c || !indicators) return;

    // RSI
    if (c.rsiSeries && showRsi && indicators.rsi14) {
      c.rsiSeries.setData(
        data.flatMap((b, i) =>
          indicators.rsi14?.[i] != null ? [{ time: b.time, value: indicators.rsi14[i]! }] : []
        )
      );
    }

    // MACD
    if (showMacd && c.macdLine && c.signalLine && c.macdHist) {
      c.macdLine.setData(
        data.flatMap((b, i) =>
          indicators.macd?.[i] != null ? [{ time: b.time, value: indicators.macd[i]! }] : []
        )
      );
      c.signalLine.setData(
        data.flatMap((b, i) =>
          indicators.signal?.[i] != null ? [{ time: b.time, value: indicators.signal[i]! }] : []
        )
      );
      c.macdHist.setData(
        data.flatMap((b, i) => {
          const val = indicators.hist?.[i];
          if (val == null) return [];
          return [
            {
              time: b.time,
              value: val,
              color: val >= 0
                ? (isDark ? "rgba(8, 153, 129, 0.75)" : "rgba(8, 153, 129, 0.85)")
                : (isDark ? "rgba(242, 54, 69, 0.75)" : "rgba(242, 54, 69, 0.85)"),
            },
          ];
        })
      );
    }
  }, [data, indicators, showRsi, showMacd, isDark]);

  // Update Overlays: Zones, Sweeps, Trendlines
  useEffect(() => {
    const last = data.at(-1);
    const start = intraday ? data.find((b) => b.date.slice(0, 10) === last?.date.slice(0, 10)) : data[0];
    const curClose = last?.close ?? 0;

    const baseMin = vMin > 0 ? vMin : (data.length > 0 ? Math.min(...data.map((b) => b.low ?? b.close)) : 0);
    const baseMax = vMax > 0 ? vMax : (data.length > 0 ? Math.max(...data.map((b) => b.high ?? b.close)) : 0);
    const viewSpan = Math.max(baseMax - baseMin, 100);
    const pad = Math.max(viewSpan * 0.2, 80);
    const lowBound = baseMin - pad;
    const highBound = baseMax + pad;

    let activeZones: Zone[] = [];
    if (showLevelsAndTrendlines) {
      const candidateZones = zones.filter((z) => z.high >= lowBound && z.low <= highBound);

      if (isRangeIncreased) {
        // Range is increased -> Keep ONLY IMPORTANT zones (confluent 2+ levels or macro benchmark extremes)
        const majorKeys = ["52W", "PM", "PW", "PD", "DH", "DL", "P", "R1", "S1", "R2", "S2"];
        activeZones = candidateZones.filter((z) => {
          const isConfluent = z.labels && z.labels.length >= 2;
          const hasMajorKey = z.labels && z.labels.some((lbl) => majorKeys.some((k) => lbl.startsWith(k)));
          return isConfluent || hasMajorKey;
        });
      } else {
        // Normal range (e.g. 22800 to 23200) -> Show all structural zones in viewing window
        activeZones = candidateZones;
      }
    }

    const zoneBoxes = activeZones.map((z) => {
      const labelText = z.labels && z.labels.length > 0
        ? z.labels.slice(0, 3).join("/")
        : (z.side === "resistance" ? "Res" : "Sup");
      return {
        low: z.low,
        high: z.high,
        color: zoneColor(z),
        right: true,
        text: `${labelText} Zone · ${num(z.low)}–${num(z.high)}`,
      };
    });

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
      [...zoneBoxes, ...riskBoxes],
      start?.time ?? null,
      marketStructure?.events ?? [],
      trendlines,
      sweeps,
      timeMap,
      data.map((b) => b.date),
      showStructure,
      showLevelsAndTrendlines,
      showLevelsAndTrendlines,
      showSweeps,
      isDark
    );
  }, [zones, plans, data, intraday, showLevelsAndTrendlines, showStructure, showSweeps, marketStructure, trendlines, sweeps, timeMap, isDark, isRangeIncreased]);

  // Clean, Non-Colliding Horizontal Levels on Chart
  useEffect(() => {
    const c = chart.current;
    if (!c) return;

    c.lines.forEach((l) => c.mainSeries.removePriceLine(l));
    c.lines = [];

    if (!showLevelsAndTrendlines || data.length === 0) return;

    const baseMin = vMin > 0 ? vMin : (data.length > 0 ? Math.min(...data.map((b) => b.low ?? b.close)) : 0);
    const baseMax = vMax > 0 ? vMax : (data.length > 0 ? Math.max(...data.map((b) => b.high ?? b.close)) : 0);
    const viewSpan = Math.max(baseMax - baseMin, 100);
    const pad = Math.max(viewSpan * 0.15, 60);
    const lowBound = baseMin - pad;
    const highBound = baseMax + pad;

    const keyBenchmarks = ["DH", "DL", "PDH", "PDL", "PWH", "PWL", "PMH", "PML", "52WH", "52WL"];
    const majorPivots = ["P", "R1", "S1", "R2", "S2"];
    const minorIntraday = ["ORH", "ORL", "EQH", "EQL", "Res", "Sup", "BSL", "SSL", "R3", "S3", "R4", "S4"];

    // When range is increased, keep ONLY important benchmark and pivot levels
    const allowed = isRangeIncreased
      ? [...keyBenchmarks, ...majorPivots]
      : [...keyBenchmarks, ...majorPivots, ...minorIntraday];

    const filtered = levels.filter(
      (lv) => lv.price >= lowBound && lv.price <= highBound && allowed.some((p) => lv.label.startsWith(p))
    );

    const sorted = [...filtered].sort((a, b) => a.price - b.price);
    const clustered: { price: number; labels: string[] }[] = [];
    
    // When range is increased, cluster nearby lines with wider spacing so chart never crowds
    const minSpacing = isRangeIncreased
      ? Math.max(viewSpan * 0.045, 25)
      : Math.max(viewSpan * 0.025, 12);

    for (const lv of sorted) {
      const lastGroup = clustered[clustered.length - 1];
      if (lastGroup && Math.abs(lv.price - lastGroup.price) < minSpacing) {
        if (!lastGroup.labels.includes(lv.label)) {
          lastGroup.labels.push(lv.label);
        }
      } else {
        clustered.push({ price: lv.price, labels: [lv.label] });
      }
    }

    for (const item of clustered) {
      const primary = item.labels[0];
      const isKeyBenchmark = keyBenchmarks.some((p) => primary.startsWith(p));

      c.lines.push(
        c.mainSeries.createPriceLine({
          price: item.price,
          color: levelColor(primary, isDark),
          lineWidth: isKeyBenchmark ? 1 : 1,
          lineStyle: isKeyBenchmark ? LineStyle.Dashed : LineStyle.SparseDotted,
          axisLabelVisible: false,
          title: `${item.labels.slice(0, 2).join("/")} (${num(item.price)})`,
        })
      );
    }
  }, [levels, showLevelsAndTrendlines, data, isDark, isRangeIncreased]);

  // Hovered Bar & Values
  const shown = data[hover !== null && hover >= 0 && hover < data.length ? hover : data.length - 1];
  const prev = shown && data[data.indexOf(shown) - 1];
  const op = shown ? shown.open ?? shown.close : 0;
  const change = shown ? shown.close - (prev?.close ?? op) : 0;
  const changePct = ((change / ((prev?.close ?? op) || 1)) * 100).toFixed(2);
  const activeIdx = shown ? data.indexOf(shown) : data.length - 1;

  const currentRsi = indicators?.rsi14?.[activeIdx];
  const currentMacd = indicators?.macd?.[activeIdx];
  const currentSignal = indicators?.signal?.[activeIdx];
  const currentHist = indicators?.hist?.[activeIdx];

  const resetView = () => {
    if (chart.current?.api && data.length) {
      chart.current.api.timeScale().setVisibleLogicalRange({ from: data.length - 90, to: data.length + 6 });
    }
  };

  return (
    <div
      ref={wrap}
      className={`rounded-xl border transition-colors ${
        isDark ? "bg-[#131722] border-[#2a2e39] text-[#d1d4dc]" : "bg-white border-zinc-200 text-zinc-800"
      } p-3 space-y-2 [&:fullscreen]:p-4 [&:fullscreen]:flex [&:fullscreen]:flex-col`}
    >
      {/* 1. TradingView-Style Professional Header & Action Toolbar */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b pb-2 text-xs border-inherit">
        {/* Left: Symbol & Timeframe Quick Buttons & Chart Type */}
        <div className="flex flex-wrap items-center gap-1.5 sm:gap-2">
          {/* Symbol Tag */}
          <span className="font-bold text-sm tracking-wide px-1.5 py-0.5 rounded bg-blue-500/10 text-blue-500">
            {symbol}
          </span>

          {/* Timeframe Switcher */}
          {onTimeframeChange && (
            <div className={`flex items-center rounded-lg p-0.5 border ${isDark ? "bg-[#1e222d] border-[#2a2e39]" : "bg-zinc-100 border-zinc-200"}`}>
              {["1m", "5m", "15m", "60m", "1d"].map((tf) => {
                const label = tf === "60m" ? "1h" : tf === "1d" ? "1D" : tf;
                const active = timeframe === tf;
                return (
                  <button
                    key={tf}
                    onClick={() => onTimeframeChange(tf)}
                    className={`rounded px-2 py-0.5 text-[11px] font-semibold transition ${
                      active
                        ? isDark
                          ? "bg-[#2962ff] text-white shadow-xs"
                          : "bg-white text-zinc-900 shadow-xs"
                        : isDark
                        ? "text-[#787b86] hover:text-[#d1d4dc]"
                        : "text-zinc-600 hover:text-zinc-900"
                    }`}
                  >
                    {label}
                  </button>
                );
              })}
            </div>
          )}

          <div className={`h-4 w-[1px] ${isDark ? "bg-[#2a2e39]" : "bg-zinc-200"}`} />

          {/* Chart Type Selector */}
          <div className={`flex items-center rounded-lg p-0.5 border ${isDark ? "bg-[#1e222d] border-[#2a2e39]" : "bg-zinc-100 border-zinc-200"}`}>
            <button
              onClick={() => setChartType("candlestick")}
              title="Candlestick Chart"
              className={`rounded px-2 py-0.5 text-[11px] font-medium transition ${
                chartType === "candlestick"
                  ? isDark ? "bg-[#2962ff] text-white" : "bg-white text-zinc-900 shadow-xs"
                  : isDark ? "text-[#787b86] hover:text-[#d1d4dc]" : "text-zinc-600 hover:text-zinc-900"
              }`}
            >
              🕯️ Candles
            </button>
            <button
              onClick={() => setChartType("line")}
              title="Line Chart"
              className={`rounded px-2 py-0.5 text-[11px] font-medium transition ${
                chartType === "line"
                  ? isDark ? "bg-[#2962ff] text-white" : "bg-white text-zinc-900 shadow-xs"
                  : isDark ? "text-[#787b86] hover:text-[#d1d4dc]" : "text-zinc-600 hover:text-zinc-900"
              }`}
            >
              📈 Line
            </button>
            <button
              onClick={() => setChartType("area")}
              title="Area Chart"
              className={`rounded px-2 py-0.5 text-[11px] font-medium transition ${
                chartType === "area"
                  ? isDark ? "bg-[#2962ff] text-white" : "bg-white text-zinc-900 shadow-xs"
                  : isDark ? "text-[#787b86] hover:text-[#d1d4dc]" : "text-zinc-600 hover:text-zinc-900"
              }`}
            >
              🌊 Area
            </button>
          </div>
        </div>

        {/* Right: fx Indicators Menu, Quick Indicator Toggles, Theme & Fullscreen */}
        <div className="flex flex-wrap items-center gap-1.5 sm:gap-2">
          {/* Quick Overlay Chips */}
          <button
            onClick={() => setShowVwap(!showVwap)}
            className={`rounded px-2 py-0.5 text-[11px] font-semibold border transition ${
              showVwap
                ? "bg-purple-500/20 text-purple-400 border-purple-500/40"
                : isDark
                ? "text-[#787b86] border-[#2a2e39] hover:bg-[#1e222d]"
                : "text-zinc-500 border-zinc-200 hover:bg-zinc-100"
            }`}
          >
            VWAP
          </button>

          <button
            onClick={() => setShowEma20(!showEma20)}
            className={`rounded px-2 py-0.5 text-[11px] font-semibold border transition ${
              showEma20
                ? "bg-amber-500/20 text-amber-400 border-amber-500/40"
                : isDark
                ? "text-[#787b86] border-[#2a2e39] hover:bg-[#1e222d]"
                : "text-zinc-500 border-zinc-200 hover:bg-zinc-100"
            }`}
          >
            EMA 20
          </button>

          {/* Unified Indicator: Levels & Trendlines */}
          <button
            onClick={() => setShowLevelsAndTrendlines(!showLevelsAndTrendlines)}
            className={`flex items-center gap-1.5 rounded px-2.5 py-0.5 text-[11px] font-semibold border transition ${
              showLevelsAndTrendlines
                ? "bg-emerald-500/20 text-emerald-400 border-emerald-500/40 shadow-2xs"
                : isDark
                ? "text-[#787b86] border-[#2a2e39] hover:bg-[#1e222d]"
                : "text-zinc-500 border-zinc-200 hover:bg-zinc-100"
            }`}
            title="Toggle Unified Levels & Trendlines Indicator (Key Support/Resistance, Benchmarks, & 3-Point Trendlines)"
          >
            <span
              className={`inline-block h-2 w-2 rounded-full ${
                showLevelsAndTrendlines ? "bg-emerald-500 animate-pulse" : isDark ? "bg-[#787b86]" : "bg-zinc-300"
              }`}
            />
            <span>Levels & Trendlines</span>
          </button>

          {/* Dedicated Sub-Pane Toggles: RSI & MACD */}
          <button
            onClick={() => setShowRsi(!showRsi)}
            className={`flex items-center gap-1 rounded px-2 py-0.5 text-[11px] font-semibold border transition ${
              showRsi
                ? "bg-purple-600 text-white border-purple-600 shadow-xs"
                : isDark
                ? "text-[#787b86] border-[#2a2e39] hover:bg-[#1e222d]"
                : "text-zinc-500 border-zinc-200 hover:bg-zinc-100"
            }`}
            title="Toggle RSI Sub-Pane (Rendered in separate pane below chart)"
          >
            <span>RSI</span>
            {showRsi && <span className="text-[10px] opacity-75">✓</span>}
          </button>

          <button
            onClick={() => setShowMacd(!showMacd)}
            className={`flex items-center gap-1 rounded px-2 py-0.5 text-[11px] font-semibold border transition ${
              showMacd
                ? "bg-blue-600 text-white border-blue-600 shadow-xs"
                : isDark
                ? "text-[#787b86] border-[#2a2e39] hover:bg-[#1e222d]"
                : "text-zinc-500 border-zinc-200 hover:bg-zinc-100"
            }`}
            title="Toggle MACD Sub-Pane (Rendered in separate pane below chart)"
          >
            <span>MACD</span>
            {showMacd && <span className="text-[10px] opacity-75">✓</span>}
          </button>

          <div className={`h-4 w-[1px] ${isDark ? "bg-[#2a2e39]" : "bg-zinc-200"}`} />

          {/* Indicators Dropdown (TradingView fx Button) */}
          <div className="relative">
            <button
              onClick={() => setShowIndicatorsMenu(!showIndicatorsMenu)}
              className={`flex items-center gap-1 rounded px-2.5 py-0.5 text-[11px] font-bold border transition ${
                showIndicatorsMenu
                  ? "bg-[#2962ff] text-white border-[#2962ff]"
                  : isDark
                  ? "bg-[#1e222d] text-[#d1d4dc] border-[#2a2e39] hover:bg-[#2a2e39]"
                  : "bg-zinc-100 text-zinc-800 border-zinc-200 hover:bg-zinc-200"
              }`}
            >
              <span>ƒx Indicators</span>
              <span className="text-[9px]">▼</span>
            </button>

            {showIndicatorsMenu && (
              <>
                {/* Backdrop to close dropdown on click outside */}
                <div
                  className="fixed inset-0 z-40"
                  onClick={() => setShowIndicatorsMenu(false)}
                />

                <div
                  className="absolute right-0 top-full mt-2 rounded-xl border p-3 shadow-2xl text-xs z-50 transition-all select-none"
                  style={{
                    backgroundColor: isDark ? "#1e222d" : "#ffffff",
                    borderColor: isDark ? "#363a45" : "#d1d5db",
                    color: isDark ? "#d1d4dc" : "#111827",
                    width: "310px",
                    minWidth: "310px",
                    boxShadow: "0 20px 30px -5px rgba(0, 0, 0, 0.5), 0 8px 10px -6px rgba(0, 0, 0, 0.4)",
                    zIndex: 9999,
                  }}
                >
                  {/* Header */}
                  <div
                    className="flex items-center justify-between pb-2 mb-2 border-b"
                    style={{ borderColor: isDark ? "#2a2e39" : "#e5e7eb" }}
                  >
                    <span
                      className="font-bold text-[11px] uppercase tracking-wider"
                      style={{ color: isDark ? "#9598a1" : "#6b7280" }}
                    >
                      ƒx Indicators & Panes
                    </span>
                    <button
                      onClick={() => setShowIndicatorsMenu(false)}
                      className="rounded p-1 text-sm font-bold transition"
                      style={{ color: isDark ? "#9598a1" : "#6b7280" }}
                    >
                      ✕
                    </button>
                  </div>

                  {/* Section 0: Chart Background Theme Selector */}
                  <div
                    className="p-2 mb-3 rounded-lg border"
                    style={{
                      backgroundColor: isDark ? "#1e222d" : "#f8fafc",
                      borderColor: isDark ? "#2a2e39" : "#e2e8f0",
                    }}
                  >
                    <div
                      className="text-[10px] font-bold uppercase tracking-wider mb-1.5 flex items-center justify-between"
                      style={{ color: isDark ? "#93c5fd" : "#2563eb" }}
                    >
                      <span>Chart Background</span>
                      <span className="text-[10px] font-normal opacity-75">
                        {isDark ? "Dark (#131722)" : "Light (#ffffff)"}
                      </span>
                    </div>
                    <div className="grid grid-cols-2 gap-1.5">
                      <button
                        type="button"
                        onClick={() => setMode("dark")}
                        className={`flex items-center justify-center gap-1.5 py-1 px-2 rounded-md text-xs font-semibold border transition ${
                          isDark
                            ? "bg-[#2962ff] text-white border-blue-500 shadow-xs"
                            : "bg-transparent text-zinc-500 border-zinc-200 hover:bg-zinc-100"
                        }`}
                      >
                        <span>🌙</span>
                        <span>Dark</span>
                      </button>
                      <button
                        type="button"
                        onClick={() => setMode("light")}
                        className={`flex items-center justify-center gap-1.5 py-1 px-2 rounded-md text-xs font-semibold border transition ${
                          !isDark
                            ? "bg-white text-zinc-900 border-zinc-300 shadow-xs"
                            : "bg-transparent text-[#787b86] border-[#2a2e39] hover:bg-[#1e222d]"
                        }`}
                      >
                        <span>☀️</span>
                        <span>Light</span>
                      </button>
                    </div>
                  </div>

                  {/* Section 1: Separate Sub-Panes */}
                  <div className="space-y-1 mb-3">
                    <div
                      className="text-[10px] font-bold uppercase tracking-wider px-1 pt-1"
                      style={{ color: isDark ? "#818cf8" : "#4f46e5" }}
                    >
                      Separate Sub-Panes (Below Chart)
                    </div>

                    <label
                      className="flex items-center justify-between cursor-pointer p-1.5 rounded-lg transition"
                      style={{
                        backgroundColor: showRsi ? (isDark ? "#2a2e39" : "#f3f4f6") : "transparent",
                      }}
                    >
                      <span className="flex items-center gap-2">
                        <span className="h-2.5 w-2.5 rounded-full bg-purple-500 shrink-0" />
                        <div>
                          <span
                            className="block font-semibold text-xs leading-tight"
                            style={{ color: isDark ? "#ffffff" : "#111827" }}
                          >
                            RSI (14) Relative Strength
                          </span>
                          <span
                            className="block text-[10px] leading-tight"
                            style={{ color: isDark ? "#9598a1" : "#6b7280" }}
                          >
                            Momentum oscillator sub-pane (30/70 bands)
                          </span>
                        </div>
                      </span>
                      <input
                        type="checkbox"
                        checked={showRsi}
                        onChange={(e) => setShowRsi(e.target.checked)}
                        className="h-4 w-4 rounded accent-purple-600 cursor-pointer"
                      />
                    </label>

                    <label
                      className="flex items-center justify-between cursor-pointer p-1.5 rounded-lg transition"
                      style={{
                        backgroundColor: showMacd ? (isDark ? "#2a2e39" : "#f3f4f6") : "transparent",
                      }}
                    >
                      <span className="flex items-center gap-2">
                        <span className="h-2.5 w-2.5 rounded-full bg-blue-500 shrink-0" />
                        <div>
                          <span
                            className="block font-semibold text-xs leading-tight"
                            style={{ color: isDark ? "#ffffff" : "#111827" }}
                          >
                            MACD (12, 26, 9) Histogram
                          </span>
                          <span
                            className="block text-[10px] leading-tight"
                            style={{ color: isDark ? "#9598a1" : "#6b7280" }}
                          >
                            Trend momentum sub-pane with signal line
                          </span>
                        </div>
                      </span>
                      <input
                        type="checkbox"
                        checked={showMacd}
                        onChange={(e) => setShowMacd(e.target.checked)}
                        className="h-4 w-4 rounded accent-blue-600 cursor-pointer"
                      />
                    </label>
                  </div>

                  {/* Section 2: Chart Overlays */}
                  <div className="space-y-1">
                    <div
                      className="text-[10px] font-bold uppercase tracking-wider px-1 pt-1 border-t"
                      style={{
                        color: isDark ? "#34d399" : "#059669",
                        borderColor: isDark ? "#2a2e39" : "#e5e7eb",
                      }}
                    >
                      Chart Overlays (On Candles)
                    </div>

                    <label
                      className="flex items-center justify-between cursor-pointer p-1.5 rounded-lg transition"
                      style={{
                        backgroundColor: showLevelsAndTrendlines ? (isDark ? "#2a2e39" : "#f3f4f6") : "transparent",
                      }}
                    >
                      <span className="flex items-center gap-2">
                        <span className="h-2.5 w-2.5 rounded-full bg-emerald-500 shrink-0" />
                        <div>
                          <span
                            className="block font-semibold text-xs leading-tight"
                            style={{ color: isDark ? "#ffffff" : "#111827" }}
                          >
                            Levels & Trendlines
                          </span>
                          <span
                            className="block text-[10px] leading-tight"
                            style={{ color: isDark ? "#9598a1" : "#6b7280" }}
                          >
                            Key S/R, Benchmarks, & 3P Trendlines
                          </span>
                        </div>
                      </span>
                      <input
                        type="checkbox"
                        checked={showLevelsAndTrendlines}
                        onChange={(e) => setShowLevelsAndTrendlines(e.target.checked)}
                        className="h-4 w-4 rounded accent-emerald-600 cursor-pointer"
                      />
                    </label>

                    <label
                      className="flex items-center justify-between cursor-pointer p-1.5 rounded-lg transition"
                      style={{
                        backgroundColor: showVwap ? (isDark ? "#2a2e39" : "#f3f4f6") : "transparent",
                      }}
                    >
                      <span className="flex items-center gap-2">
                        <span className="h-2.5 w-2.5 rounded-full bg-purple-400 shrink-0" />
                        <div>
                          <span
                            className="block font-semibold text-xs leading-tight"
                            style={{ color: isDark ? "#ffffff" : "#111827" }}
                          >
                            VWAP Benchmark
                          </span>
                          <span
                            className="block text-[10px] leading-tight"
                            style={{ color: isDark ? "#9598a1" : "#6b7280" }}
                          >
                            Volume-Weighted Average Price
                          </span>
                        </div>
                      </span>
                      <input
                        type="checkbox"
                        checked={showVwap}
                        onChange={(e) => setShowVwap(e.target.checked)}
                        className="h-4 w-4 rounded accent-purple-600 cursor-pointer"
                      />
                    </label>

                    <label
                      className="flex items-center justify-between cursor-pointer p-1.5 rounded-lg transition"
                      style={{
                        backgroundColor: showEma20 ? (isDark ? "#2a2e39" : "#f3f4f6") : "transparent",
                      }}
                    >
                      <span className="flex items-center gap-2">
                        <span className="h-2.5 w-2.5 rounded-full bg-amber-500 shrink-0" />
                        <div>
                          <span
                            className="block font-semibold text-xs leading-tight"
                            style={{ color: isDark ? "#ffffff" : "#111827" }}
                          >
                            EMA 20 Pullback
                          </span>
                          <span
                            className="block text-[10px] leading-tight"
                            style={{ color: isDark ? "#9598a1" : "#6b7280" }}
                          >
                            Fast 20-period dynamic guide
                          </span>
                        </div>
                      </span>
                      <input
                        type="checkbox"
                        checked={showEma20}
                        onChange={(e) => setShowEma20(e.target.checked)}
                        className="h-4 w-4 rounded accent-amber-600 cursor-pointer"
                      />
                    </label>

                    <label
                      className="flex items-center justify-between cursor-pointer p-1.5 rounded-lg transition"
                      style={{
                        backgroundColor: showEma50_200 ? (isDark ? "#2a2e39" : "#f3f4f6") : "transparent",
                      }}
                    >
                      <span className="flex items-center gap-2">
                        <span className="h-2.5 w-2.5 rounded-full bg-blue-400 shrink-0" />
                        <div>
                          <span
                            className="block font-semibold text-xs leading-tight"
                            style={{ color: isDark ? "#ffffff" : "#111827" }}
                          >
                            EMA 50 & 200 Trend
                          </span>
                          <span
                            className="block text-[10px] leading-tight"
                            style={{ color: isDark ? "#9598a1" : "#6b7280" }}
                          >
                            Macro trend confirmation lines
                          </span>
                        </div>
                      </span>
                      <input
                        type="checkbox"
                        checked={showEma50_200}
                        onChange={(e) => setShowEma50_200(e.target.checked)}
                        className="h-4 w-4 rounded accent-blue-600 cursor-pointer"
                      />
                    </label>

                    <label
                      className="flex items-center justify-between cursor-pointer p-1.5 rounded-lg transition"
                      style={{
                        backgroundColor: showStructure ? (isDark ? "#2a2e39" : "#f3f4f6") : "transparent",
                      }}
                    >
                      <span className="flex items-center gap-2">
                        <span className="h-2.5 w-2.5 rounded-full bg-indigo-500 shrink-0" />
                        <div>
                          <span
                            className="block font-semibold text-xs leading-tight"
                            style={{ color: isDark ? "#ffffff" : "#111827" }}
                          >
                            BOS / CHoCH Structure
                          </span>
                          <span
                            className="block text-[10px] leading-tight"
                            style={{ color: isDark ? "#9598a1" : "#6b7280" }}
                          >
                            Break of structure & change of character
                          </span>
                        </div>
                      </span>
                      <input
                        type="checkbox"
                        checked={showStructure}
                        onChange={(e) => setShowStructure(e.target.checked)}
                        className="rounded accent-indigo-600 cursor-pointer"
                      />
                    </label>

                    <label
                      className="flex items-center justify-between cursor-pointer p-1.5 rounded-lg transition"
                      style={{
                        backgroundColor: showSweeps ? (isDark ? "#2a2e39" : "#f3f4f6") : "transparent",
                      }}
                    >
                      <span className="flex items-center gap-2">
                        <span className="h-2.5 w-2.5 rounded-full bg-yellow-500 shrink-0" />
                        <div>
                          <span
                            className="block font-semibold text-xs leading-tight"
                            style={{ color: isDark ? "#ffffff" : "#111827" }}
                          >
                            Liquidity Sweeps
                          </span>
                          <span
                            className="block text-[10px] leading-tight"
                            style={{ color: isDark ? "#9598a1" : "#6b7280" }}
                          >
                            Wick sweeps of PDH/PDL/Equal highs
                          </span>
                        </div>
                      </span>
                      <input
                        type="checkbox"
                        checked={showSweeps}
                        onChange={(e) => setShowSweeps(e.target.checked)}
                        className="rounded accent-yellow-600 cursor-pointer"
                      />
                    </label>
                  </div>
                </div>
              </>
            )}
          </div>

          {/* Reset Zoom Button */}
          <button
            onClick={resetView}
            className={`rounded px-2 py-0.5 text-[11px] font-semibold border transition ${
              isDark ? "text-[#787b86] border-[#2a2e39] hover:bg-[#1e222d] hover:text-[#d1d4dc]" : "text-zinc-600 border-zinc-200 hover:bg-zinc-100"
            }`}
            title="Reset Chart View & Fit Candles"
          >
            Auto
          </button>

          {/* Explicit Chart Background Theme Switcher (Dark / Light) */}
          <div
            className={`flex items-center rounded-lg p-0.5 border ${
              isDark ? "bg-[#1e222d] border-[#2a2e39]" : "bg-zinc-100 border-zinc-200"
            }`}
            title="Chart Background Theme"
          >
            <button
              type="button"
              onClick={() => setMode("dark")}
              className={`flex items-center gap-1 rounded px-2 py-0.5 text-[11px] font-semibold transition ${
                isDark
                  ? "bg-[#2962ff] text-white shadow-xs"
                  : "text-zinc-500 hover:text-zinc-900"
              }`}
              title="Set Chart Background to Dark Mode"
            >
              <span>🌙</span>
              <span className="hidden sm:inline">Dark</span>
            </button>
            <button
              type="button"
              onClick={() => setMode("light")}
              className={`flex items-center gap-1 rounded px-2 py-0.5 text-[11px] font-semibold transition ${
                !isDark
                  ? "bg-white text-zinc-900 shadow-xs"
                  : "text-[#787b86] hover:text-[#d1d4dc]"
              }`}
              title="Set Chart Background to Light Mode"
            >
              <span>☀️</span>
              <span className="hidden sm:inline">Light</span>
            </button>
          </div>

          {/* Fullscreen Button */}
          <button
            className={`rounded px-2 py-0.5 text-[11px] border transition ${
              isDark ? "text-[#787b86] border-[#2a2e39] hover:bg-[#1e222d] hover:text-[#d1d4dc]" : "text-zinc-600 border-zinc-200 hover:bg-zinc-100"
            }`}
            onClick={() => (document.fullscreenElement ? document.exitFullscreen() : wrap.current?.requestFullscreen())}
            title="Fullscreen Chart"
          >
            ⛶
          </button>
        </div>
      </div>

      {/* 2. Interactive OHLC Bar & Indicator HUD (TradingView Style) */}
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 font-mono text-[11px] px-1">
        {shown && (
          <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5">
            <span className={`font-semibold ${isDark ? "text-zinc-300" : "text-zinc-800"}`}>
              {dayStr(shown.date)} {shown.date.slice(0, 4)}
              {intraday && ` · ${shown.date.slice(11)} IST`}
            </span>
            <span>O <strong className={isDark ? "text-zinc-200" : "text-zinc-900"}>{num(op)}</strong></span>
            <span>H <strong className={isDark ? "text-zinc-200" : "text-zinc-900"}>{num(shown.high ?? shown.close)}</strong></span>
            <span>L <strong className={isDark ? "text-zinc-200" : "text-zinc-900"}>{num(shown.low ?? shown.close)}</strong></span>
            <span>C <strong className={isDark ? "text-zinc-200" : "text-zinc-900"}>{num(shown.close)}</strong></span>
            <span className={change >= 0 ? "text-emerald-500 font-semibold" : "text-rose-500 font-semibold"}>
              {change >= 0 ? "+" : ""}{num(change)} ({changePct}%)
            </span>

            {/* Indicator values on hover */}
            {showVwap && shown.vwap != null && (
              <span className="text-purple-400">
                VWAP <strong className="font-semibold">{num(shown.vwap)}</strong>
              </span>
            )}
            {showEma20 && indicators?.ema20?.[activeIdx] != null && (
              <span className="text-amber-400">
                EMA20 <strong className="font-semibold">{num(indicators.ema20[activeIdx]!)}</strong>
              </span>
            )}
            {showRsi && currentRsi != null && (
              <span className="text-purple-400">
                RSI <strong className="font-semibold">{currentRsi.toFixed(1)}</strong>
              </span>
            )}
            {showMacd && currentMacd != null && (
              <span className="text-blue-400">
                MACD <strong className="font-semibold">{currentMacd.toFixed(2)}</strong>
                {currentSignal != null && ` Sig ${currentSignal.toFixed(2)}`}
                {currentHist != null && ` Hist ${currentHist.toFixed(2)}`}
              </span>
            )}
          </div>
        )}

        <div className={`text-[10px] hidden sm:block ${isDark ? "text-[#787b86]" : "text-zinc-400"}`}>
          IST (UTC+5:30) · TradingView Engine · Non-Repainting
        </div>
      </div>

      {/* 3. Primary Multi-Pane Lightweight Chart Canvas */}
      <div
        className={`relative w-full [:fullscreen_&]:h-auto [:fullscreen_&]:flex-1 rounded-lg border overflow-hidden ${
          isDark ? "border-[#2a2e39]" : "border-zinc-200"
        }`}
        style={{ height: chartPixelHeight, minHeight: 460 }}
      >
        <div ref={box} className="w-full h-full" />
        {!data.length && (
          <div className="absolute inset-0 grid place-items-center text-sm text-zinc-500">
            Loading market candles…
          </div>
        )}
      </div>

      {/* 4. Chart Legend & Controls Summary */}
      <div className={`flex flex-wrap items-center justify-between text-[11px] pt-1 border-t ${isDark ? "border-[#2a2e39] text-[#787b86]" : "border-zinc-100 text-zinc-500"}`}>
        <div className="flex flex-wrap gap-x-3 gap-y-1">
          {showVwap && (
            <span className="flex items-center gap-1 text-purple-400 font-medium">
              <span className="inline-block w-2.5 h-0.5 bg-[#8b5cf6]" /> VWAP
            </span>
          )}
          {showEma20 && (
            <span className="flex items-center gap-1 text-amber-400 font-medium">
              <span className="inline-block w-2.5 h-0.5 bg-[#f59e0b]" /> EMA 20
            </span>
          )}
          {showEma50_200 && (
            <>
              <span className="flex items-center gap-1 text-blue-400 font-medium">
                <span className="inline-block w-2.5 h-0.5 bg-[#3b82f6]" /> EMA 50
              </span>
              <span className="flex items-center gap-1 text-slate-400 font-medium">
                <span className="inline-block w-2.5 h-0.5 bg-[#94a3b8]" /> EMA 200
              </span>
            </>
          )}
          {showLevelsAndTrendlines && (
            <>
              <span className="flex items-center gap-1 font-semibold text-emerald-400">
                📐 Levels & Trendlines
              </span>
              <span className="flex items-center gap-1 text-emerald-400">
                <span className="inline-block w-2.5 h-0.5 bg-[#10b981]" /> Support TL
              </span>
              <span className="flex items-center gap-1 text-rose-400">
                <span className="inline-block w-2.5 h-0.5 bg-[#f43f5e]" /> Resistance TL
              </span>
              <span className="flex items-center gap-1 text-amber-400">
                <span className="inline-block w-2.5 h-0.5 border-t border-dashed border-amber-500" /> Key S/R
              </span>
            </>
          )}
          {showRsi && (
            <span className="flex items-center gap-1 text-purple-400 font-medium">
              <span className="inline-block w-2 h-2 rounded-xs bg-purple-500/30 border border-purple-500" /> RSI Sub-Pane
            </span>
          )}
          {showMacd && (
            <span className="flex items-center gap-1 text-blue-400 font-medium">
              <span className="inline-block w-2 h-2 rounded-xs bg-blue-500/30 border border-blue-500" /> MACD Sub-Pane
            </span>
          )}
        </div>

        <div className="flex items-center gap-2">
          {onSensitivityChange && (
            <div className="flex items-center gap-1">
              <span className="text-[10px] uppercase font-semibold text-[#787b86]">Swing:</span>
              {[2, 3, 5].map((s) => (
                <button
                  key={s}
                  onClick={() => onSensitivityChange(s)}
                  className={`rounded px-1.5 py-0.2 text-[10px] font-semibold border ${
                    selectedSensitivity === s
                      ? "bg-[#2962ff] text-white border-[#2962ff]"
                      : isDark
                      ? "text-[#787b86] border-[#2a2e39] hover:bg-[#1e222d]"
                      : "text-zinc-600 border-zinc-200 hover:bg-zinc-100"
                  }`}
                >
                  {s}b
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
