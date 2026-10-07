"""Does the LLM's opinion improve on the rules? Measured on closed outcomes only.

Every opinion is joined to what its entry actually did: the live trade for the lots that were
sent, plus the phantom trade for lots the LLM removed in veto mode. That gives two results per
opinion:

- `rules_net`: what the rules alone would have made (all the lots the rules proposed);
- `llm_net`: what following the opinion makes. In shadow mode it is the rules result scaled to the
  opinion's lots (charges are close to linear in lots); in veto mode it is what was actually traded.

The difference is zero for takes and errors, so the evidence comes from skips and reductions. No
verdict is given until there are `opinion_eval_min_samples` of them.
"""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select

from app.config import Settings
from app.db.models import LiveTrade, PhantomTrade
from app.live.store import LiveStore

DECISIONS = ("take", "reduce", "skip", "error")
BOOTSTRAP_ITERATIONS = 2000
CONFIDENCE = 0.95
METHOD = (
    "Closed outcomes only. rules = all lots the rules proposed (live trade plus phantom trade for removed lots); "
    "with_llm = shadow opinions scaled to the opinion's lots, veto opinions as actually traded. "
    f"Difference CI: {CONFIDENCE:.0%} percentile bootstrap of the mean over skip and reduce opinions."
)


@dataclass
class Outcome:
    opinion_id: str
    trade_intent_id: str
    mode: str
    decision: str  # take | reduce | skip | error
    rules_lots: int
    llm_lots: int
    closed_at: datetime
    rules_net: float
    llm_net: float

    @property
    def difference(self) -> float:
        return self.llm_net - self.rules_net


def outcomes(store: LiveStore, deployment_id: str | None = None) -> tuple[list[Outcome], int]:
    """Closed outcomes in close order, and the number of opinions still waiting for an exit."""
    opinions = [o for o in store.opinions(deployment_id) if o.trade_intent_id is not None]
    ids = [o.trade_intent_id for o in opinions]
    trades = {
        t.open_intent_id: t for t in store.session.scalars(select(LiveTrade).where(LiveTrade.open_intent_id.in_(ids)))
    }
    phantoms = {
        p.trade_intent_id: p
        for p in store.session.scalars(select(PhantomTrade).where(PhantomTrade.trade_intent_id.in_(ids)))
    }
    closed, pending = [], 0
    for o in opinions:
        trade, phantom = trades.get(o.trade_intent_id), phantoms.get(o.trade_intent_id)
        removed = o.rules_lots - o.final_lots
        if (o.final_lots > 0 and trade is None) or (removed > 0 and phantom is None):
            pending += 1
            continue
        actual = float(trade.net) if trade else 0.0
        rules_net = actual + (float(phantom.net) if phantom else 0.0)
        if o.status != "ok":
            decision, llm_lots, llm_net = "error", o.rules_lots, rules_net
        elif o.mode == "veto":
            decision, llm_lots, llm_net = o.decision, o.final_lots, actual
        else:
            decision, llm_lots = o.decision, o.lots if o.lots is not None else o.rules_lots
            llm_net = rules_net * llm_lots / o.rules_lots if o.rules_lots else 0.0
        stamps = [t.closed_at for t in (trade, phantom) if t is not None]
        closed.append(
            Outcome(o.id, o.trade_intent_id, o.mode, decision, o.rules_lots, llm_lots, max(stamps), rules_net, llm_net)
        )
    closed.sort(key=lambda x: (x.closed_at, x.opinion_id))
    return closed, pending


def max_drawdown(values: list[float]) -> float:
    equity, peak, worst = 0.0, 0.0, 0.0
    for v in values:
        equity += v
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return worst


def bootstrap_mean_ci(
    values: list[float], iterations: int = BOOTSTRAP_ITERATIONS, seed: int = 0
) -> tuple[float, float]:
    """Percentile bootstrap interval for the mean. Seeded, so the same data gives the same interval."""
    if not values:
        return 0.0, 0.0
    rng, n = random.Random(seed), len(values)
    means = sorted(sum(rng.choices(values, k=n)) / n for _ in range(iterations))
    tail = (1 - CONFIDENCE) / 2
    return means[int(tail * iterations)], means[min(iterations - 1, int((1 - tail) * iterations))]


def _group(rows: list[Outcome]) -> dict[str, Any]:
    nets = [r.rules_net for r in rows]
    return {
        "count": len(rows),
        "rules_net": round(sum(nets), 2),
        "mean_rules_net": round(sum(nets) / len(nets), 2) if nets else None,
        "win_rate": round(sum(1 for n in nets if n > 0) / len(nets), 4) if nets else None,
        "max_drawdown": round(max_drawdown(nets), 2),
    }


def summarize(rows: list[Outcome], min_samples: int) -> dict[str, Any]:
    interventions = [r for r in rows if r.decision in ("skip", "reduce")]
    diffs = [r.difference for r in interventions]
    rules_total = llm_total = 0.0
    curve = []
    for r in rows:
        rules_total += r.rules_net
        llm_total += r.llm_net
        curve.append(
            {"closed_at": r.closed_at.isoformat(), "rules": round(rules_total, 2), "with_llm": round(llm_total, 2)}
        )

    mean = sum(diffs) / len(diffs) if diffs else None
    low, high = bootstrap_mean_ci(diffs)
    if len(diffs) < min_samples:
        verdict = None
    elif low > 0:
        verdict = "veto_helps"
    elif high < 0:
        verdict = "veto_hurts"
    else:
        verdict = "inconclusive"
    return {
        "closed": len(rows),
        "interventions": len(diffs),
        "min_samples": min_samples,
        "by_decision": {d: _group([r for r in rows if r.decision == d]) for d in DECISIONS},
        "rules": {"net": round(rules_total, 2), "max_drawdown": round(max_drawdown([r.rules_net for r in rows]), 2)},
        "with_llm": {"net": round(llm_total, 2), "max_drawdown": round(max_drawdown([r.llm_net for r in rows]), 2)},
        "difference": {
            "total": round(sum(diffs), 2),
            "mean_per_intervention": round(mean, 2) if mean is not None else None,
            "ci95": [round(low, 2), round(high, 2)] if diffs else None,
            "bootstrap_iterations": BOOTSTRAP_ITERATIONS,
        },
        "equity_curve": curve,
        "verdict": verdict,
        "verdict_reason": (
            f"needs {min_samples - len(diffs)} more closed skip or reduce opinions" if verdict is None else None
        ),
    }


def evaluate(store: LiveStore, settings: Settings, deployment_id: str | None = None) -> dict[str, Any]:
    rows, pending = outcomes(store, deployment_id)
    rolling = rows[-settings.opinion_eval_window :] if settings.opinion_eval_window > 0 else rows
    return {
        "deployment_id": deployment_id,
        "pending": pending,
        "method": METHOD,
        **summarize(rows, settings.opinion_eval_min_samples),
        "rolling": {
            "window": settings.opinion_eval_window,
            **{k: v for k, v in summarize(rolling, 0).items() if k not in ("equity_curve", "min_samples")},
        },
        "outcomes": [
            asdict(r) | {"closed_at": r.closed_at.isoformat(), "difference": round(r.difference, 2)} for r in rows
        ],
    }


def rolling_is_negative(evaluation: dict[str, Any]) -> bool:
    """The opinion cost money over the recent window: the trigger for switching veto back to shadow."""
    rolling = evaluation["rolling"]
    return rolling["interventions"] > 0 and rolling["difference"]["total"] < 0
