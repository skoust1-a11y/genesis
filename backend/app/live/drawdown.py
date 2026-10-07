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

A floor is a fixed-rupee line under equity, on the same basis as the ladder:
- an entry is allowed only while equity is above the floor and the trade's max loss fits in the
  buffer left (equity minus floor);
- equity below the floor pauses the deployment, closing open positions unless told not to;
- `raise_once` lifts the floor a single time, once equity reaches `at_equity_inr`;
- with `resume_after_days`, the pause is a break: after that many trading days the deployment starts
  again by itself if equity is above the starting floor (a deposit tops it up), with the floor back
  at `floor_inr`. Without it, only a person can restart it.
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
FLOOR_CHECK = "drawdown.floor"
BUFFER_CHECK = "drawdown.floor_buffer"


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


class FloorRaise(_Strict):
    at_equity_inr: float = Field(gt=0)
    to_inr: float = Field(gt=0)

    @model_validator(mode="after")
    def _below(self) -> FloorRaise:
        if self.to_inr >= self.at_equity_inr:
            raise ValueError("to_inr must be below at_equity_inr")
        return self


class Floor(_Strict):
    floor_inr: float = Field(gt=0)
    raise_once: FloorRaise | None = None
    close_open_positions: bool = True
    resume_after_days: int | None = Field(default=None, ge=1, le=60)  # trading days

    @model_validator(mode="after")
    def _raised_above(self) -> Floor:
        if self.raise_once is not None and self.raise_once.to_inr <= self.floor_inr:
            raise ValueError("raise_once.to_inr must be above floor_inr")
        return self


class DrawdownPolicy(_Strict):
    basis: Literal["mtm", "realized"] = "mtm"
    ladder: list[DrawdownLevel] = Field(default_factory=list)
    restore_below_pct: float | None = Field(default=None, ge=0)
    floor: Floor | None = None

    @model_validator(mode="after")
    def _ordered(self) -> DrawdownPolicy:
        if not self.ladder and self.floor is None:
            raise ValueError("set a ladder, a floor, or both")
        for lower, upper in zip(self.ladder, self.ladder[1:]):
            if upper.at_pct <= lower.at_pct:
                raise ValueError("ladder at_pct values must increase")
            if SEVERITY[upper.action] < SEVERITY[lower.action]:
                raise ValueError(f"{upper.action} at {upper.at_pct:g}% is weaker than {lower.action} below it")
        if self.restore_below_pct is not None and (not self.ladder or self.restore_below_pct >= self.ladder[0].at_pct):
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


# The floor


def floor_in_force(floor: Floor, raised: bool) -> float:
    return floor.raise_once.to_inr if raised and floor.raise_once else floor.floor_inr


def floor_raise_due(floor: Floor, raised: bool, equity: float) -> bool:
    return floor.raise_once is not None and not raised and equity >= floor.raise_once.at_equity_inr


def floor_breached(floor_inr: float, equity: float) -> bool:
    """Below, not at: equity left exactly on the floor (say by a withdrawal) blocks entries but does not pause."""
    return equity < floor_inr


def floor_checks(floor_inr: float, equity: float, max_loss: float | None) -> list[Check]:
    """Entries need equity above the floor and the trade's max loss within the buffer left.
    An unknown max loss fails the buffer check."""
    equity, buffer = round(equity, 2), round(equity - floor_inr, 2)
    loss = round(max_loss, 2) if max_loss is not None else None
    return [
        Check(FLOOR_CHECK, "equity_inr", ">", floor_inr, equity, equity > floor_inr),
        Check(BUFFER_CHECK, "max_loss_inr", "<=", buffer, loss, loss is not None and loss <= buffer),
    ]
