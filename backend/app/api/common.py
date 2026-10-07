from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from fastapi import HTTPException

T = TypeVar("T")


def or_404(fn: Callable[..., T], *args, **kwargs) -> T:
    try:
        return fn(*args, **kwargs)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"not found: {exc.args[0] if exc.args else ''}") from exc


def or_400(fn: Callable[..., T], *args, **kwargs) -> T:
    try:
        return or_404(fn, *args, **kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
