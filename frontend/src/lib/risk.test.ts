import assert from "node:assert/strict";
import { test } from "node:test";

import { riskPlan } from "./risk.ts";

const zones = [
  { low: 990, high: 995 },
  { low: 1010, high: 1015 },
];
const levels = [{ price: 980 }, { price: 992 }, { price: 1030 }];

test("a long stops under the support zone below and targets the first resistance above", () => {
  assert.deepEqual(riskPlan(1000, 50, levels, zones, 0.001), {
    entry: 1000, stop: 989, target: 1010, risk: 11, riskRs: 550, reward: 10, rewardRs: 500, rr: 0.91,
  });
});

test("a short mirrors it, and an entry inside a zone leans on that zone", () => {
  const plan = riskPlan(1012, -10, levels, zones, 0.001);
  assert.equal(plan?.stop, 1016.01); // above the zone it sits in, not the 1030 level
  assert.equal(plan?.target, 995);
});

test("no target above, or nothing to lean on below", () => {
  assert.equal(riskPlan(1040, 1, levels, zones)?.target, null);
  assert.equal(riskPlan(970, 1, levels, zones), null);
});
