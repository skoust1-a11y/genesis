from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.orm import ORMExecuteState, Session

from app.db.base import Base, LiveBase


class AppendOnlyViolation(RuntimeError):
    pass


def _is_append_only(cls: type) -> bool:
    return bool(getattr(cls, "__append_only__", False))


def _blocks_delete(cls: type) -> bool:
    return _is_append_only(cls) or bool(getattr(cls, "__no_delete__", False))


def _block_update(mapper, connection, target) -> None:
    if _is_append_only(type(target)):
        raise AppendOnlyViolation(f"{mapper.local_table.name} is append-only")


def _block_delete(mapper, connection, target) -> None:
    if _blocks_delete(type(target)):
        raise AppendOnlyViolation(f"{mapper.local_table.name} rows cannot be deleted")


for _base in (Base, LiveBase):
    event.listen(_base, "before_update", _block_update, propagate=True)
    event.listen(_base, "before_delete", _block_delete, propagate=True)


@event.listens_for(Session, "do_orm_execute")
def _block_bulk_writes(state: ORMExecuteState) -> None:
    if not (state.is_update or state.is_delete):
        return
    mapper = state.bind_mapper
    if mapper is None:
        return
    cls = mapper.class_
    if state.is_update and _is_append_only(cls):
        raise AppendOnlyViolation(f"{mapper.local_table.name} is append-only")
    if state.is_delete and _blocks_delete(cls):
        raise AppendOnlyViolation(f"{mapper.local_table.name} rows cannot be deleted")
