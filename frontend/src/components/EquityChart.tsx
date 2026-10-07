type Series = { label: string; color: string; points: { x: string; y: number }[] };

/** A dependency-free line chart for equity curves. */
export function EquityChart({ series, height = 220 }: { series: Series[]; height?: number }) {
  const all = series.flatMap((s) => s.points.map((p) => p.y));
  if (!all.length) return <p className="text-sm text-zinc-500">No equity points yet.</p>;
  const width = 720;
  const pad = { top: 10, right: 10, bottom: 24, left: 80 };
  const min = Math.min(...all);
  const max = Math.max(...all);
  const span = max - min || 1;
  const longest = Math.max(...series.map((s) => s.points.length));
  const x = (i: number) => pad.left + (i / Math.max(longest - 1, 1)) * (width - pad.left - pad.right);
  const y = (v: number) => pad.top + (1 - (v - min) / span) * (height - pad.top - pad.bottom);
  const labels = series.find((s) => s.points.length === longest)?.points ?? [];
  const ticks = [min, min + span / 2, max];

  return (
    <div className="space-y-2">
      <svg viewBox={`0 0 ${width} ${height}`} className="w-full" role="img" aria-label="Equity curve">
        {ticks.map((t) => (
          <g key={t}>
            <line x1={pad.left} x2={width - pad.right} y1={y(t)} y2={y(t)} stroke="#e4e4e7" />
            <text x={pad.left - 6} y={y(t) + 4} textAnchor="end" fontSize="11" fill="#71717a">
              {Math.round(t).toLocaleString("en-IN")}
            </text>
          </g>
        ))}
        {labels.length > 0 && (
          <>
            <text x={pad.left} y={height - 6} fontSize="11" fill="#71717a">
              {labels[0].x}
            </text>
            <text x={width - pad.right} y={height - 6} textAnchor="end" fontSize="11" fill="#71717a">
              {labels[labels.length - 1].x}
            </text>
          </>
        )}
        {series.map((s) => (
          <polyline
            key={s.label}
            fill="none"
            stroke={s.color}
            strokeWidth="1.75"
            points={s.points.map((p, i) => `${x(i)},${y(p.y)}`).join(" ")}
          />
        ))}
      </svg>
      <div className="flex gap-4 text-xs text-zinc-600">
        {series.map((s) => (
          <span key={s.label} className="flex items-center gap-1">
            <span className="inline-block h-2 w-4 rounded" style={{ background: s.color }} />
            {s.label}
          </span>
        ))}
      </div>
    </div>
  );
}
