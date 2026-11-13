from __future__ import annotations

from typing import Any

from sqlalchemy import inspect


def row_to_dict(obj: Any, exclude: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        attr.key: getattr(obj, attr.key)
        for attr in inspect(obj).mapper.column_attrs
        if attr.key not in exclude
    }
