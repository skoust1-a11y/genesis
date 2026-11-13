"use client";

import { useMemo, useState } from "react";

export type IndicatorData = {
  ema9?: (number | null)[];
  ema20?: (number | null)[];
  ema50?: (number | null)[];
  ema200?: (number | null)[];
  rsi14?: (number | null)[];
  macd?: (number | null)[];
  signal?: (number | null)[];
  hist?: (number | null)[];
  adx?: (number | null)[];
  plus_di?: (number | null)[];
  minus_di?: (number | null)[];
  atr14?: number[];
  rvol?: number[];
};

export function MomentumPanel({
  indicators,
  dates = [],
  hoverIndex,
}: {
  indicators?: IndicatorData;
  dates?: string[];
  hoverIndex?: number | null;
}) {
  const [tab, setTab] = useState<"rsi" | "macd" | "adx" | "atr">("rsi");

  const idx = hoverIndex !== null && hoverIndex !== undefined && hoverIndex >= 0
    ? hoverIndex
    : (dates.length ? dates.length - 1 : 0);

  const rsi = indicators?.rsi14;
  const macd = indicators?.macd;
  const signal = indicators?.signal;
  const hist = indicators?.hist;
  const adx = indicators?.adx;
  const pdi = indicators?.plus_di;
  const mdi = indicators?.minus_di;
  const atr = indicators?.atr14;
  const rvol = indicators?.rvol;

  const currentRsi = rsi && rsi[idx] != null ? rsi[idx]! : null;
  const currentMacd = macd && macd[idx] != null ? macd[idx]! : null;
  const currentSignal = signal && signal[idx] != null ? signal[idx]! : null;
  const currentHist = hist && hist[idx] != null ? hist[idx]! : null;
  const currentAdx = adx && adx[idx] != null ? adx[idx]! : null;
  const currentPdi = pdi && pdi[idx] != null ? pdi[idx]! : null;
  const currentMdi = mdi && mdi[idx] != null ? mdi[idx]! : null;
  const currentAtr = atr && atr[idx] != null ? atr[idx] : null;
  const currentRvol = rvol && rvol[idx] != null ? rvol[idx] : null;

  // Slice last 80 points for smooth SVG rendering
  const sliceLen = 80;
  const startIdx = Math.max(0, dates.length - sliceLen);

  // SVG dimensions
  const width = 800;
  const height = 90;
  const padTop = 10;
  const padBottom = 16;
  const plotH = height - padTop - padBottom;

  // 1. RSI Path
  const rsiPath = useMemo(() => {
    if (!rsi || !rsi.length) return "";
    const subset = rsi.slice(startIdx);
    if (!subset.length) return "";
    const n = subset.length;
    const pts: string[] = [];
    subset.forEach((val, i) => {
      if (val === null) return;
      const x = (i / (n - 1 || 1)) * width;
      // RSI scale 0 to 100
      const y = padTop + (1.0 - val / 100.0) * plotH;
      pts.push(`${x.toFixed(1)},${y.toFixed(1)}`);
    });
    return pts.length ? `M ${pts.join(" L ")}` : "";
  }, [rsi, startIdx, width, plotH]);

  // 2. MACD Paths
  const { macdPath, signalPath, histBars } = useMemo(() => {
    if (!macd || !signal || !hist) return { macdPath: "", signalPath: "", histBars: [] };
    const subMacd = macd.slice(startIdx);
    const subSig = signal.slice(startIdx);
    const subHist = hist.slice(startIdx);
    const n = subMacd.length;
    if (!n) return { macdPath: "", signalPath: "", histBars: [] };

    const validVals = [...subMacd, ...subSig, ...subHist].filter((v): v is number => v !== null);
    const maxVal = Math.max(1.0, ...validVals.map(Math.abs));
    const zeroY = padTop + plotH / 2;

    const mPts: string[] = [];
    const sPts: string[] = [];
    const bars: { x: number; y: number; w: number; h: number; color: string }[] = [];
    const barW = Math.max(2, (width / n) * 0.7);

    for (let i = 0; i < n; i++) {
      const x = (i / (n - 1 || 1)) * width;
      const mv = subMacd[i];
      const sv = subSig[i];
      const hv = subHist[i];

      if (mv !== null) {
        const y = zeroY - (mv / maxVal) * (plotH / 2);
        mPts.push(`${x.toFixed(1)},${y.toFixed(1)}`);
      }
      if (sv !== null) {
        const y = zeroY - (sv / maxVal) * (plotH / 2);
        sPts.push(`${x.toFixed(1)},${y.toFixed(1)}`);
      }
      if (hv !== null) {
        const h = Math.abs(hv / maxVal) * (plotH / 2);
        const y = hv >= 0 ? zeroY - h : zeroY;
        bars.push({
          x: x - barW / 2,
          y,
          w: barW,
          h: Math.max(1, h),
          color: hv >= 0 ? "#10b981" : "#f43f5e",
        });
      }
    }

    return {
      macdPath: mPts.length ? `M ${mPts.join(" L ")}` : "",
      signalPath: sPts.length ? `M ${sPts.join(" L ")}` : "",
      histBars: bars,
    };
  }, [macd, signal, hist, startIdx, width, plotH]);

  // 3. ADX Paths
  const { adxPath, pdiPath, mdiPath } = useMemo(() => {
    if (!adx || !pdi || !mdi) return { adxPath: "", pdiPath: "", mdiPath: "" };
    const subAdx = adx.slice(startIdx);
    const subPdi = pdi.slice(startIdx);
    const subMdi = mdi.slice(startIdx);
    const n = subAdx.length;
    if (!n) return { adxPath: "", pdiPath: "", mdiPath: "" };

    const toY = (v: number) => padTop + (1.0 - Math.min(100, Math.max(0, v)) / 80.0) * plotH;

    const aPts: string[] = [];
    const pPts: string[] = [];
    const mPts: string[] = [];

    for (let i = 0; i < n; i++) {
      const x = (i / (n - 1 || 1)) * width;
      const av = subAdx[i];
      const pv = subPdi[i];
      const mv = subMdi[i];
      if (av !== null) aPts.push(`${x.toFixed(1)},${toY(av).toFixed(1)}`);
      if (pv !== null) pPts.push(`${x.toFixed(1)},${toY(pv).toFixed(1)}`);
      if (mv !== null) mPts.push(`${x.toFixed(1)},${toY(mv).toFixed(1)}`);
    }

    return {
      adxPath: aPts.length ? `M ${aPts.join(" L ")}` : "",
      pdiPath: pPts.length ? `M ${pPts.join(" L ")}` : "",
      mdiPath: mPts.length ? `M ${mPts.join(" L ")}` : "",
    };
  }, [adx, pdi, mdi, startIdx, width, plotH]);

  return (
    <div className="rounded-lg border border-zinc-200 bg-white p-2.5 space-y-2">
      {/* Subpanel Header & Tab Switcher */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-100 pb-1.5 text-xs">
        <div className="flex items-center gap-1">
          <span className="font-semibold text-zinc-500 uppercase text-[10px] tracking-wider mr-1">
            Momentum Panel:
          </span>
          <button
            onClick={() => setTab("rsi")}
            className={`rounded px-2 py-0.5 text-[11px] font-medium transition ${
              tab === "rsi" ? "bg-zinc-900 text-white" : "text-zinc-600 hover:bg-zinc-100"
            }`}
          >
            RSI (14) {currentRsi !== null && <span className="font-mono">({currentRsi.toFixed(1)})</span>}
          </button>
          <button
            onClick={() => setTab("macd")}
            className={`rounded px-2 py-0.5 text-[11px] font-medium transition ${
              tab === "macd" ? "bg-zinc-900 text-white" : "text-zinc-600 hover:bg-zinc-100"
            }`}
          >
            MACD (12, 26, 9)
          </button>
          <button
            onClick={() => setTab("adx")}
            className={`rounded px-2 py-0.5 text-[11px] font-medium transition ${
              tab === "adx" ? "bg-zinc-900 text-white" : "text-zinc-600 hover:bg-zinc-100"
            }`}
          >
            ADX (14) {currentAdx !== null && <span className="font-mono">({currentAdx.toFixed(1)})</span>}
          </button>
          <button
            onClick={() => setTab("atr")}
            className={`rounded px-2 py-0.5 text-[11px] font-medium transition ${
              tab === "atr" ? "bg-zinc-900 text-white" : "text-zinc-600 hover:bg-zinc-100"
            }`}
          >
            ATR & Volatility
          </button>
        </div>

        {/* Status Callout */}
        <div className="font-mono text-[11px] text-zinc-600 flex items-center gap-3">
          {tab === "rsi" && currentRsi !== null && (
            <span>
              Status:{" "}
              <strong className={currentRsi >= 60 ? "text-emerald-700" : currentRsi <= 40 ? "text-rose-700" : "text-zinc-700"}>
                {currentRsi >= 70
                  ? "Overbought (Potential Exhaustion)"
                  : currentRsi <= 30
                  ? "Oversold (Potential Bounce)"
                  : currentRsi >= 55
                  ? "Bullish Momentum"
                  : currentRsi <= 45
                  ? "Bearish Momentum"
                  : "Neutral Zone"}
              </strong>
            </span>
          )}

          {tab === "macd" && currentMacd !== null && (
            <span>
              MACD: <span className="text-blue-600">{currentMacd.toFixed(2)}</span> · Signal:{" "}
              <span className="text-amber-600">{currentSignal?.toFixed(2)}</span> · Hist:{" "}
              <span className={currentHist && currentHist >= 0 ? "text-emerald-600" : "text-rose-600"}>
                {currentHist?.toFixed(2)}
              </span>
            </span>
          )}

          {tab === "adx" && currentAdx !== null && (
            <span>
              ADX:{" "}
              <strong className={currentAdx >= 25 ? "text-purple-700" : "text-zinc-500"}>
                {currentAdx.toFixed(1)} ({currentAdx >= 25 ? "Strong Trend" : "Weak / Ranging"})
              </strong>
              {currentPdi !== null && currentMdi !== null && (
                <span className="ml-2 font-mono text-[10px]">
                  (+DI: <span className="text-emerald-600 font-semibold">{currentPdi.toFixed(1)}</span> · -DI:{" "}
                  <span className="text-rose-600 font-semibold">{currentMdi.toFixed(1)}</span>)
                </span>
              )}
            </span>
          )}

          {tab === "atr" && currentAtr !== null && (
            <span>
              ATR (14): <strong>{currentAtr.toFixed(2)} pts</strong> · RVOL:{" "}
              <strong>{currentRvol ? `${currentRvol.toFixed(2)}x` : "1.0x"}</strong>
            </span>
          )}
        </div>
      </div>

      {/* SVG Canvas */}
      <div className="relative h-[95px] w-full bg-zinc-50/50 rounded border border-zinc-100 overflow-hidden">
        <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" className="w-full h-full">
          {/* Grid lines */}
          <line x1="0" y1={padTop} x2={width} y2={padTop} stroke="#e4e4e7" strokeDasharray="3,3" />
          <line x1="0" y1={padTop + plotH / 2} x2={width} y2={padTop + plotH / 2} stroke="#e4e4e7" strokeDasharray="2,2" />
          <line x1="0" y1={padTop + plotH} x2={width} y2={padTop + plotH} stroke="#e4e4e7" strokeDasharray="3,3" />

          {tab === "rsi" && (
            <>
              {/* Overbought 70, Oversold 30 lines */}
              <line
                x1="0"
                y1={padTop + 0.3 * plotH}
                x2={width}
                y2={padTop + 0.3 * plotH}
                stroke="#f43f5e"
                strokeDasharray="4,4"
                strokeWidth="1"
              />
              <text x={width - 40} y={padTop + 0.3 * plotH - 3} fill="#f43f5e" fontSize="9" fontFamily="sans-serif">
                70 OB
              </text>

              <line
                x1="0"
                y1={padTop + 0.7 * plotH}
                x2={width}
                y2={padTop + 0.7 * plotH}
                stroke="#10b981"
                strokeDasharray="4,4"
                strokeWidth="1"
              />
              <text x={width - 40} y={padTop + 0.7 * plotH - 3} fill="#10b981" fontSize="9" fontFamily="sans-serif">
                30 OS
              </text>

              <path d={rsiPath} fill="none" stroke="#6366f1" strokeWidth="2" strokeLinecap="round" />
            </>
          )}

          {tab === "macd" && (
            <>
              {/* Histogram bars */}
              {histBars.map((b, i) => (
                <rect key={i} x={b.x} y={b.y} width={b.w} height={b.h} fill={b.color} opacity="0.65" />
              ))}
              {/* Zero line */}
              <line x1="0" y1={padTop + plotH / 2} x2={width} y2={padTop + plotH / 2} stroke="#a1a1aa" strokeWidth="1" />
              {/* MACD & Signal lines */}
              <path d={macdPath} fill="none" stroke="#2563eb" strokeWidth="1.8" />
              <path d={signalPath} fill="none" stroke="#f59e0b" strokeWidth="1.5" strokeDasharray="3,2" />
            </>
          )}

          {tab === "adx" && (
            <>
              {/* 25 Trend line */}
              <line
                x1="0"
                y1={padTop + (1.0 - 25.0 / 80.0) * plotH}
                x2={width}
                y2={padTop + (1.0 - 25.0 / 80.0) * plotH}
                stroke="#a855f7"
                strokeDasharray="4,4"
                strokeWidth="1"
              />
              <text
                x={width - 55}
                y={padTop + (1.0 - 25.0 / 80.0) * plotH - 3}
                fill="#a855f7"
                fontSize="9"
                fontFamily="sans-serif"
              >
                25 Trend
              </text>

              <path d={pdiPath} fill="none" stroke="#10b981" strokeWidth="1.5" />
              <path d={mdiPath} fill="none" stroke="#ef4444" strokeWidth="1.5" />
              <path d={adxPath} fill="none" stroke="#8b5cf6" strokeWidth="2.2" />
            </>
          )}

          {tab === "atr" && (
            <>
              <text x="15" y={padTop + 25} fill="#3f3f46" fontSize="12" fontFamily="sans-serif" fontWeight="600">
                ATR (14): {currentAtr?.toFixed(2)} pts
              </text>
              <text x="15" y={padTop + 45} fill="#71717a" fontSize="11" fontFamily="sans-serif">
                Expected 5m Bar Range: ±{(currentAtr ? currentAtr * 0.7 : 10).toFixed(1)} to ±{(currentAtr || 15).toFixed(1)} pts
              </text>
              <text x="15" y={padTop + 65} fill="#71717a" fontSize="11" fontFamily="sans-serif">
                Relative Volume (RVOL): {currentRvol ? `${currentRvol.toFixed(2)}x` : "1.0x"} of 20-period volume SMA
              </text>
            </>
          )}
        </svg>
      </div>
    </div>
  );
}
