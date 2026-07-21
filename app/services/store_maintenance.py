"""Runtime retention and reset operations shared by AIOps stores."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.services.policies.retention_policy import (
    A2A_TASK_RETENTION_ACTIVE_SQL,
    CHANGE_EXECUTION_TERMINAL_STATUSES,
    INCIDENT_RETENTION_TERMINAL_SQL,
    SESSION_RETENTION_ACTIVE_SQL,
)
from app.services.sql_safety import bind_markers, trusted_table_statement

RUNTIME_TABLES = (
    "change_executions",
    "approval_requests",
    "diagnosis_reports",
    "trace_events",
    "aiops_sessions",
    "a2a_tasks",
    "incident_states",
    "alert_events",
)
RETENTION_TABLES = RUNTIME_TABLES


def reset_sqlite_runtime_data(
    connect: Callable[[], Any],
) -> dict[str, int]:
    """Delete all SQLite runtime records while preserving the schema."""
    deleted: dict[str, int] = {}
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        for table in RUNTIME_TABLES:
            row = connection.execute(
                trusted_table_statement(
                    "SELECT_ALL_COUNT",
                    table=table,
                    allowed_tables=RUNTIME_TABLES,
                )
            ).fetchone()
            deleted[table] = int(row[0])
            connection.execute(
                trusted_table_statement(
                    "DELETE_ALL",
                    table=table,
                    allowed_tables=RUNTIME_TABLES,
                )
            )
    return deleted


def cleanup_sqlite_runtime_data(
    connect: Callable[[], Any],
    *,
    database_path: str | Path,
    keep_days: int,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Delete SQLite incidents wholly outside the retention window."""
    cutoff_text = _retention_cutoff(keep_days)
    with connect() as connection:
        if not dry_run:
            connection.execute("BEGIN IMMEDIATE")
        incident_ids = _select_sqlite_retention_incidents(connection, cutoff_text)
        deleted = (
            _count_sqlite_retention_incidents(connection, incident_ids)
            if dry_run
            else _delete_sqlite_retention_incidents(connection, incident_ids)
        )
        if not dry_run:
            try:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.OperationalError:
                pass
    return {
        "database_path": str(database_path),
        "keep_days": keep_days,
        "cutoff": cutoff_text,
        "dry_run": dry_run,
        "deleted": deleted,
    }


def reset_mysql_runtime_data(
    connect: Callable[[], Any],
) -> dict[str, int]:
    """Delete all MySQL runtime records in one transaction."""
    deleted: dict[str, int] = {}
    with connect() as connection:
        connection.begin()
        try:
            with connection.cursor() as cursor:
                for table in RUNTIME_TABLES:
                    cursor.execute(
                        trusted_table_statement(
                            "SELECT_ALL_COUNT",
                            table=table,
                            allowed_tables=RUNTIME_TABLES,
                        )
                    )
                    deleted[table] = int(cursor.fetchone()["count"])
                    cursor.execute(
                        trusted_table_statement(
                            "DELETE_ALL",
                            table=table,
                            allowed_tables=RUNTIME_TABLES,
                        )
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return deleted


def cleanup_mysql_runtime_data(
    connect: Callable[[], Any],
    *,
    storage_path: str,
    keep_days: int,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Delete MySQL incidents wholly outside the retention window."""
    cutoff_text = _retention_cutoff(keep_days)
    with connect() as connection:
        try:
            if not dry_run:
                connection.begin()
            with connection.cursor() as cursor:
                incident_ids = _select_mysql_retention_incidents(cursor, cutoff_text)
                deleted = (
                    _count_mysql_retention_incidents(cursor, incident_ids)
                    if dry_run
                    else _delete_mysql_retention_incidents(cursor, incident_ids)
                )
            if not dry_run:
                connection.commit()
        except Exception:
            if not dry_run:
                connection.rollback()
            raise
    return {
        "backend": "mysql",
        "database": storage_path,
        "keep_days": keep_days,
        "cutoff": cutoff_text,
        "dry_run": dry_run,
        "deleted": deleted,
    }


def _retention_cutoff(keep_days: int) -> str:
    if keep_days < 1:
        raise ValueError("keep_days must be >= 1")
    return (datetime.now(UTC) - timedelta(days=keep_days)).isoformat()


def _select_sqlite_retention_incidents(
    connection: sqlite3.Connection,
    cutoff_text: str,
) -> list[str]:
    placeholders = bind_markers(len(CHANGE_EXECUTION_TERMINAL_STATUSES), "?")
    rows = connection.execute(
        _retention_eligible_sql(marker="?", terminal_placeholders=placeholders),
        _retention_params(cutoff_text),
    ).fetchall()
    return [str(row["incident_id"]) for row in rows]


def _select_mysql_retention_incidents(cursor: Any, cutoff_text: str) -> list[str]:
    placeholders = bind_markers(len(CHANGE_EXECUTION_TERMINAL_STATUSES), "%s")
    cursor.execute(
        _retention_eligible_sql(marker="%s", terminal_placeholders=placeholders),
        _retention_params(cutoff_text),
    )
    return [str(row["incident_id"]) for row in cursor.fetchall()]


def _retention_eligible_sql(*, marker: str, terminal_placeholders: str) -> str:
    return f"""
        SELECT incident_id
        FROM (
            SELECT incident_id FROM alert_events
            UNION SELECT incident_id FROM trace_events
            UNION SELECT incident_id FROM approval_requests
            UNION SELECT incident_id FROM change_executions
            UNION SELECT incident_id FROM aiops_sessions
            UNION SELECT incident_id FROM a2a_tasks
            UNION SELECT incident_id FROM incident_states
            UNION SELECT incident_id FROM diagnosis_reports
        ) AS incidents
        WHERE NOT EXISTS (
            SELECT 1 FROM alert_events AS alert
            WHERE alert.incident_id = incidents.incident_id
              AND (alert.status != 'resolved' OR alert.updated_at >= {marker})
        )
          AND NOT EXISTS (
            SELECT 1 FROM trace_events AS trace
            WHERE trace.incident_id = incidents.incident_id
              AND trace.created_at >= {marker}
        )
          AND NOT EXISTS (
            SELECT 1 FROM approval_requests AS approval
            WHERE approval.incident_id = incidents.incident_id
              AND (approval.status = 'pending' OR approval.updated_at >= {marker})
        )
          AND NOT EXISTS (
            SELECT 1 FROM change_executions AS execution
            WHERE execution.incident_id = incidents.incident_id
              AND (
                  execution.status NOT IN ({terminal_placeholders})
                  OR execution.updated_at >= {marker}
              )
        )
          AND NOT EXISTS (
            SELECT 1 FROM aiops_sessions AS session
            WHERE session.incident_id = incidents.incident_id
              AND (
                  session.status IN ({SESSION_RETENTION_ACTIVE_SQL})
                  OR session.updated_at >= {marker}
              )
        )
          AND NOT EXISTS (
            SELECT 1 FROM a2a_tasks AS task
            WHERE task.incident_id = incidents.incident_id
              AND (
                  task.state IN ({A2A_TASK_RETENTION_ACTIVE_SQL})
                  OR task.updated_at >= {marker}
              )
        )
          AND NOT EXISTS (
            SELECT 1 FROM incident_states AS state
            WHERE state.incident_id = incidents.incident_id
              AND (
                  state.status NOT IN ({INCIDENT_RETENTION_TERMINAL_SQL})
                  OR state.updated_at >= {marker}
              )
        )
          AND NOT EXISTS (
            SELECT 1 FROM diagnosis_reports AS report
            WHERE report.incident_id = incidents.incident_id
              AND report.updated_at >= {marker}
        )
        ORDER BY incident_id
        """  # nosec B608 -- all interpolated SQL fragments are code-owned.


def _retention_params(cutoff_text: str) -> tuple[str, ...]:
    return (
        cutoff_text,
        cutoff_text,
        cutoff_text,
        *CHANGE_EXECUTION_TERMINAL_STATUSES,
        cutoff_text,
        cutoff_text,
        cutoff_text,
        cutoff_text,
        cutoff_text,
    )


def _count_sqlite_retention_incidents(
    connection: sqlite3.Connection,
    incident_ids: list[str],
) -> dict[str, int]:
    counts = dict.fromkeys(RETENTION_TABLES, 0)
    if not incident_ids:
        return counts
    for table in reversed(RETENTION_TABLES):
        row = connection.execute(
            trusted_table_statement(
                "SELECT_COUNT",
                table=table,
                allowed_tables=RETENTION_TABLES,
                value_count=len(incident_ids),
                marker="?",
            ),
            incident_ids,
        ).fetchone()
        counts[table] = int(row[0])
    return counts


def _count_mysql_retention_incidents(
    cursor: Any,
    incident_ids: list[str],
) -> dict[str, int]:
    counts = dict.fromkeys(RETENTION_TABLES, 0)
    if not incident_ids:
        return counts
    for table in reversed(RETENTION_TABLES):
        cursor.execute(
            trusted_table_statement(
                "SELECT_COUNT",
                table=table,
                allowed_tables=RETENTION_TABLES,
                value_count=len(incident_ids),
                marker="%s",
            ),
            incident_ids,
        )
        counts[table] = int(cursor.fetchone()["count"])
    return counts


def _delete_sqlite_retention_incidents(
    connection: sqlite3.Connection,
    incident_ids: list[str],
) -> dict[str, int]:
    deleted = dict.fromkeys(RETENTION_TABLES, 0)
    if not incident_ids:
        return deleted
    for table in RETENTION_TABLES:
        cursor = connection.execute(
            trusted_table_statement(
                "DELETE",
                table=table,
                allowed_tables=RETENTION_TABLES,
                value_count=len(incident_ids),
                marker="?",
            ),
            incident_ids,
        )
        deleted[table] = int(cursor.rowcount or 0)
    return deleted


def _delete_mysql_retention_incidents(
    cursor: Any,
    incident_ids: list[str],
) -> dict[str, int]:
    deleted = dict.fromkeys(RETENTION_TABLES, 0)
    if not incident_ids:
        return deleted
    for table in RETENTION_TABLES:
        cursor.execute(
            trusted_table_statement(
                "DELETE",
                table=table,
                allowed_tables=RETENTION_TABLES,
                value_count=len(incident_ids),
                marker="%s",
            ),
            incident_ids,
        )
        deleted[table] = int(cursor.rowcount or 0)
    return deleted
