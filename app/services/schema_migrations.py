"""Version tracking for the runtime SQLite and MySQL schemas."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

SCHEMA_MIGRATIONS_TABLE = "schema_migrations"


@dataclass(frozen=True)
class SchemaMigration:
    """One ordered, idempotent runtime schema migration."""

    version: int
    name: str
    apply: Callable[[Any], None]


def apply_sqlite_migrations(
    connection: Any,
    migrations: Sequence[SchemaMigration],
) -> None:
    """Apply pending SQLite migrations and persist their versions atomically."""

    connection.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """)
    rows = connection.execute("SELECT version, name FROM schema_migrations").fetchall()
    applied = {int(row["version"]): str(row["name"]) for row in rows}
    for migration in sorted(migrations, key=lambda item: item.version):
        applied_name = applied.get(migration.version)
        if applied_name is not None:
            if applied_name != migration.name:
                raise RuntimeError(
                    f"schema migration version {migration.version} is already recorded as "
                    f"{applied_name!r}, not {migration.name!r}"
                )
            continue
        migration.apply(connection)
        connection.execute(
            "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
            (migration.version, migration.name),
        )


def apply_mysql_migrations(
    cursor: Any,
    migrations: Sequence[SchemaMigration],
) -> None:
    """Apply pending MySQL migrations and persist their versions."""

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version BIGINT PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
    cursor.execute("SELECT version, name FROM schema_migrations")
    applied = {
        int(row.get("version") or 0): str(row.get("name") or "") for row in cursor.fetchall()
    }
    for migration in sorted(migrations, key=lambda item: item.version):
        applied_name = applied.get(migration.version)
        if applied_name is not None:
            if applied_name != migration.name:
                raise RuntimeError(
                    f"schema migration version {migration.version} is already recorded as "
                    f"{applied_name!r}, not {migration.name!r}"
                )
            continue
        migration.apply(cursor)
        cursor.execute(
            "INSERT INTO schema_migrations(version, name) VALUES (%s, %s)",
            (migration.version, migration.name),
        )
