"""The drawdown ladder: what a deployment does as its equity falls from its peak.

The policy is set at approval (`risk_limits.drawdown`) and frozen with the deployment. Each level
names a drawdown from peak and an action; actions only get stronger up the ladder and are
cumulative, so a level also applies every action below it:

- warn: record the level change, nothing else
- reduce_size: new entries trade `size_factor` x the usual lots (at least 1 lot)
- stop_new_entries: entries are recorded as blocked; exits keep running
- pause: the deployment is paused when it reaches this level, optionally closing open positions first

MAX_DRAWDOWN_PCT adds a pause level no policy can remove. Levels step down only once drawdown is
below `restore_below_pct` (immediately when it is not set). Exits are never blocked.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.db.models import Deployment
from app.strategies.rules import Check

ACTIONS = ("warn", "reduce_size", "stop_new_entries", "pause")
SEVERITY = {a: i for i, a in enumerate(ACTIONS)}
BLOCKING = ("stop_new_entries", "pause")
ENTRIES_CHECK = "drawdown.entries_allowed"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DrawdownLevel(_Strict):
    at_pct: float = Field(gt=0, le=100)
    action: Literal["warn", "reduce_size", "stop_new_entries", "pause"]
    size_factor: float | None = Field(default=None, gt=0, lt=1)
    close_open_positions: bool = False

    @model_validator(mode="after")
    def _fields_for_action(self) -> DrawdownLevel:
        if (self.action == "reduce_size") != (self.size_factor is not None):
            raise ValueError("size_factor is required for reduce_size and only allowed there")
        if self.close_open_positions and self.action != "pause":
            raise ValueError("close_open_positions is only allowed on a pause level")
        return self


class DrawdownPolicy(_Strict):
    basis: Literal["mtm", "realized"] = "mtm"
    ladder: list[DrawdownLevel] = Field(min_length=1)
    restore_below_pct: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> DrawdownPolicy:
        for lower, upper in zip(self.ladder, self.ladder[1:]):
            if upper.at_pct <= lower.at_pct:
                raise ValueError("ladder at_pct values must increase")
            if SEVERITY[upper.action] < SEVERITY[lower.action]:
                raise ValueError(f"{upper.action} at {upper.at_pct:g}% is weaker than {lower.action} below it")
        if self.restore_below_pct is not None and self.restore_below_pct >= self.ladder[0].at_pct:
            raise ValueError("restore_below_pct must be below the first level")
        return self


def parse_policy(raw: Any, max_drawdown_pct: float) -> DrawdownPolicy:
    """Validate a policy; levels may not go past the MAX_DRAWDOWN_PCT pause, which always applies."""
    try:
        policy = DrawdownPolicy.model_validate(raw)
    except ValidationError as exc:
        err = exc.errors()[0]
        loc = ".".join(str(p) for p in err["loc"])
        raise ValueError(f"risk_limits.drawdown{'.' + loc if loc else ''}: {err['msg'].removeprefix('Value error, ')}")
    if max_drawdown_pct:
        for level in policy.ladder:
            if level.at_pct > max_drawdown_pct or (level.at_pct == max_drawdown_pct and level.action != "pause"):
                raise ValueError(
                    f"risk_limits.drawdown: {level.action} at {level.at_pct:g}% is at or past the "
                    f"MAX_DRAWDOWN_PCT pause ({max_drawdown_pct:g}%)"
                )
    return policy


def policy_for(deployment: Deployment) -> DrawdownPolicy | None:
    raw = deployment.risk_limits.get("drawdown")
    return DrawdownPolicy.model_validate(raw) if raw else None


def effective_ladder(policy: DrawdownPolicy | None, max_drawdown_pct: float) -> list[DrawdownLevel]:
    ladder = list(policy.ladder) if policy else []
    if max_drawdown_pct and not (ladder and ladder[-1].at_pct == max_drawdown_pct):
        ladder.append(DrawdownLevel(at_pct=max_drawdown_pct, action="pause"))
    return ladder


@dataclass
class LadderDecision:
    level: int  # 0 = normal, n = the first n levels apply
    previous: int
    action: str  # the strongest action in force, or "normal"
    at_pct: float | None
    drawdown_pct: float
    size_factor: float
    block_entries: bool
    pause: DrawdownLevel | None  # set only on the tick that reaches a pause level
    check: Check | None

    def summary(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "action": self.action,
            "at_pct": self.at_pct,
            "drawdown_pct": self.drawdown_pct,
            "size_factor": self.size_factor,
            "block_entries": self.block_entries,
        }


def decide(
    ladder: list[DrawdownLevel], restore_below_pct: float | None, drawdown_pct: float, previous: int
) -> LadderDecision:
    previous = min(previous, len(ladder))
    reached = sum(1 for level in ladder if drawdown_pct >= level.at_pct)
    if reached >= previous or restore_below_pct is None or drawdown_pct < restore_below_pct:
        level = reached
    else:
        level = previous  # recovering, but not yet below restore_below_pct
    active = ladder[:level]
    block = any(lvl.action in BLOCKING for lvl in active)
    entered = ladder[previous:level]
    blocking = [lvl.at_pct for lvl in ladder if lvl.action in BLOCKING]
    check = None
    if blocking:
        held = block and restore_below_pct is not None and drawdown_pct < blocking[0]
        threshold = restore_below_pct if held else blocking[0]
        check = Check(ENTRIES_CHECK, "drawdown_pct", "<", threshold, round(drawdown_pct, 4), not block)
    return LadderDecision(
        level=level,
        previous=previous,
        action=active[-1].action if active else "normal",
        at_pct=active[-1].at_pct if active else None,
        drawdown_pct=round(drawdown_pct, 4),
        size_factor=min((lvl.size_factor for lvl in active if lvl.size_factor is not None), default=1.0),
        block_entries=block,
        pause=next((lvl for lvl in reversed(entered) if lvl.action == "pause"), None),
        check=check,
    )


def scaled_lots(lots: int, size_factor: float) -> int:
    return lots if size_factor >= 1 else max(1, math.floor(lots * size_factor))
