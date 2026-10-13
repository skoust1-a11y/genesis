export type RiskPlan = {
  entry: number;
  stop: number;
  target: number | null;
  risk: number; // points from entry to stop
  riskRs: number;
  reward: number | null;
  rewardRs: number | null;
  rr: number | null;
};

const round = (v: number) => Math.round(v * 100) / 100;

/** Stop just past the nearest zone or level on the losing side of `entry`; target at the first one on the winning
 * side. `qty` is signed units (positive = long). `buffer` (a fraction of entry) keeps the stop clear of the level
 * itself, where stops cluster and get run. Null when there is nothing on the losing side to lean on. */
export function riskPlan(
  entry: number,
  qty: number,
  levels: { price: number }[],
  zones: { low: number; high: number }[],
  buffer = 0.0005,
): RiskPlan | null {
  const bands = [
    ...zones,
    ...levels.filter((l) => !zones.some((z) => z.low <= l.price && l.price <= z.high)).map((l) => ({ low: l.price, high: l.price })),
  ];
  const long = qty > 0;
  // A long leans on the bottom of the support beneath it; a short on the top of the resistance above it.
  const losing = long ? bands.filter((b) => b.low < entry).map((b) => b.low) : bands.filter((b) => b.high > entry).map((b) => b.high);
  const winning = long ? bands.filter((b) => b.low > entry).map((b) => b.low) : bands.filter((b) => b.high < entry).map((b) => b.high);
  if (!losing.length || qty === 0) return null;
  const pad = entry * buffer;
  const stop = round(long ? Math.max(...losing) - pad : Math.min(...losing) + pad);
  const target = winning.length ? (long ? Math.min(...winning) : Math.max(...winning)) : null;
  const risk = round(Math.abs(entry - stop));
  const reward = target === null ? null : round(Math.abs(target - entry));
  return {
    entry, stop, target, risk, riskRs: round(risk * Math.abs(qty)), reward,
    rewardRs: reward === null ? null : round(reward * Math.abs(qty)), rr: reward === null ? null : round(reward / risk),
  };
}
