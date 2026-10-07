from __future__ import annotations

PG_FUNCTION = """
CREATE OR REPLACE FUNCTION genesis_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only (% blocked)', TG_TABLE_NAME, TG_OP;
END;
$$ LANGUAGE plpgsql
"""

PG_NO_DELETE_FUNCTION = """
CREATE OR REPLACE FUNCTION genesis_no_delete() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% rows cannot be deleted', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql
"""


def append_only_sql(dialect: str, table: str) -> list[str]:
    if dialect == "sqlite":
        return [
            f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END",
            f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END",
        ]
    if dialect == "postgresql":
        return [
            PG_FUNCTION,
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION genesis_append_only()",
        ]
    raise NotImplementedError(f"append-only triggers not implemented for {dialect}")


def no_delete_sql(dialect: str, table: str) -> list[str]:
    if dialect == "sqlite":
        return [
            f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{table} rows cannot be deleted'); END",
        ]
    if dialect == "postgresql":
        return [
            PG_NO_DELETE_FUNCTION,
            f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION genesis_no_delete()",
        ]
    raise NotImplementedError(f"no-delete triggers not implemented for {dialect}")


def drop_triggers_sql(dialect: str, table: str) -> list[str]:
    if dialect == "sqlite":
        return [
            f"DROP TRIGGER IF EXISTS {table}_no_update",
            f"DROP TRIGGER IF EXISTS {table}_no_delete",
        ]
    return [
        f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}",
        f"DROP TRIGGER IF EXISTS {table}_no_delete ON {table}",
    ]
