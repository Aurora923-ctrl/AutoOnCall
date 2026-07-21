"""Schema initialization and compatibility upgrades for AIOps stores."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.services.schema_migrations import (
    SchemaMigration,
    apply_mysql_migrations,
    apply_sqlite_migrations,
)
from app.services.sql_safety import bind_markers, trusted_identifier

SQLITE_RUNTIME_SCHEMA = """
    CREATE TABLE IF NOT EXISTS alert_events (
        fingerprint TEXT PRIMARY KEY,
        incident_id TEXT NOT NULL,
        source TEXT NOT NULL,
        status TEXT NOT NULL,
        service_name TEXT NOT NULL,
        severity TEXT NOT NULL,
        environment TEXT NOT NULL,
        starts_at TEXT,
        updated_at TEXT NOT NULL,
        payload TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_alert_events_incident
        ON alert_events(incident_id, updated_at);
    CREATE INDEX IF NOT EXISTS idx_alert_events_status
        ON alert_events(status, updated_at);
    CREATE INDEX IF NOT EXISTS idx_alert_events_service
        ON alert_events(service_name, updated_at);

    CREATE TABLE IF NOT EXISTS trace_events (
        event_id TEXT PRIMARY KEY,
        trace_id TEXT NOT NULL,
        incident_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        node_name TEXT NOT NULL,
        step_id TEXT,
        tool_name TEXT,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        payload TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_trace_events_incident
        ON trace_events(incident_id, created_at);
    CREATE INDEX IF NOT EXISTS idx_trace_events_trace
        ON trace_events(trace_id, created_at);
    CREATE INDEX IF NOT EXISTS idx_trace_events_type
        ON trace_events(event_type, created_at);

    CREATE TABLE IF NOT EXISTS approval_requests (
        approval_id TEXT PRIMARY KEY,
        incident_id TEXT NOT NULL,
        status TEXT NOT NULL,
        risk_level TEXT NOT NULL,
        action TEXT NOT NULL,
        idempotency_key TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        decided_at TEXT,
        payload TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_approval_requests_incident
        ON approval_requests(incident_id, created_at);
    CREATE INDEX IF NOT EXISTS idx_approval_requests_status
        ON approval_requests(status, created_at);

    CREATE TABLE IF NOT EXISTS change_executions (
        change_execution_id TEXT PRIMARY KEY,
        change_plan_id TEXT NOT NULL,
        approval_id TEXT NOT NULL,
        incident_id TEXT NOT NULL,
        status TEXT NOT NULL,
        mode TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        payload TEXT NOT NULL,
        UNIQUE(incident_id, change_plan_id, approval_id)
    );
    CREATE INDEX IF NOT EXISTS idx_change_executions_incident
        ON change_executions(incident_id, created_at);
    CREATE INDEX IF NOT EXISTS idx_change_executions_plan
        ON change_executions(change_plan_id, created_at);

    CREATE TABLE IF NOT EXISTS aiops_sessions (
        session_id TEXT PRIMARY KEY,
        incident_id TEXT NOT NULL,
        trace_id TEXT NOT NULL,
        status TEXT NOT NULL,
        node_name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        payload TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_aiops_sessions_incident
        ON aiops_sessions(incident_id, updated_at);
    CREATE INDEX IF NOT EXISTS idx_aiops_sessions_trace
        ON aiops_sessions(trace_id, updated_at);
    CREATE INDEX IF NOT EXISTS idx_aiops_sessions_status
        ON aiops_sessions(status, updated_at);

    CREATE TABLE IF NOT EXISTS a2a_tasks (
        task_id TEXT PRIMARY KEY,
        message_id TEXT NOT NULL,
        request_fingerprint TEXT NOT NULL,
        owner_id TEXT NOT NULL DEFAULT '',
        skill TEXT NOT NULL,
        incident_id TEXT NOT NULL,
        state TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        payload TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_a2a_tasks_message
        ON a2a_tasks(message_id, updated_at);
    CREATE INDEX IF NOT EXISTS idx_a2a_tasks_incident
        ON a2a_tasks(incident_id, updated_at);
    CREATE INDEX IF NOT EXISTS idx_a2a_tasks_owner
        ON a2a_tasks(owner_id, updated_at);

    CREATE TABLE IF NOT EXISTS incident_states (
        incident_id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        service_name TEXT NOT NULL,
        severity TEXT NOT NULL,
        environment TEXT NOT NULL,
        trace_id TEXT,
        session_id TEXT,
        approval_status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        payload TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_incident_states_status
        ON incident_states(status, updated_at);
    CREATE INDEX IF NOT EXISTS idx_incident_states_service
        ON incident_states(service_name, updated_at);

    CREATE TABLE IF NOT EXISTS diagnosis_reports (
        report_id TEXT PRIMARY KEY,
        incident_id TEXT NOT NULL,
        trace_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        payload TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_diagnosis_reports_incident
        ON diagnosis_reports(incident_id, created_at);
"""

MYSQL_RUNTIME_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS alert_events (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        fingerprint VARCHAR(128) NOT NULL UNIQUE,
        incident_id VARCHAR(128) NOT NULL,
        source VARCHAR(64) NOT NULL,
        status VARCHAR(32) NOT NULL,
        service_name VARCHAR(128) NOT NULL,
        severity VARCHAR(32) NOT NULL,
        environment VARCHAR(64) NOT NULL,
        starts_at VARCHAR(64),
        updated_at VARCHAR(64) NOT NULL,
        payload LONGTEXT NOT NULL,
        INDEX idx_alert_events_incident (incident_id, updated_at),
        INDEX idx_alert_events_status (status, updated_at),
        INDEX idx_alert_events_service (service_name, updated_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS trace_events (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        event_id VARCHAR(128) NOT NULL UNIQUE,
        trace_id VARCHAR(128) NOT NULL,
        incident_id VARCHAR(128) NOT NULL,
        event_type VARCHAR(64) NOT NULL,
        node_name VARCHAR(128) NOT NULL,
        step_id VARCHAR(128),
        tool_name VARCHAR(128),
        status VARCHAR(32) NOT NULL,
        created_at VARCHAR(64) NOT NULL,
        payload LONGTEXT NOT NULL,
        INDEX idx_trace_events_incident (incident_id, created_at),
        INDEX idx_trace_events_trace (trace_id, created_at),
        INDEX idx_trace_events_type (event_type, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS approval_requests (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        approval_id VARCHAR(128) NOT NULL UNIQUE,
        incident_id VARCHAR(128) NOT NULL,
        status VARCHAR(32) NOT NULL,
        risk_level VARCHAR(32) NOT NULL,
        action VARCHAR(1000) NOT NULL,
        idempotency_key VARCHAR(128),
        pending_idempotency_key VARCHAR(128)
            GENERATED ALWAYS AS (
                CASE WHEN status = 'pending' THEN idempotency_key ELSE NULL END
            ) STORED,
        created_at VARCHAR(64) NOT NULL,
        updated_at VARCHAR(64) NOT NULL,
        decided_at VARCHAR(64),
        payload LONGTEXT NOT NULL,
        UNIQUE KEY uniq_pending_approval_idempotency (pending_idempotency_key),
        INDEX idx_approval_requests_incident (incident_id, created_at),
        INDEX idx_approval_requests_status (status, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS change_executions (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        change_execution_id VARCHAR(128) NOT NULL UNIQUE,
        change_plan_id VARCHAR(128) NOT NULL,
        approval_id VARCHAR(128) NOT NULL,
        incident_id VARCHAR(128) NOT NULL,
        status VARCHAR(64) NOT NULL,
        mode VARCHAR(32) NOT NULL,
        created_at VARCHAR(64) NOT NULL,
        updated_at VARCHAR(64) NOT NULL,
        payload LONGTEXT NOT NULL,
        UNIQUE KEY uniq_change_executions_scope (
            incident_id, change_plan_id, approval_id
        ),
        INDEX idx_change_executions_incident (incident_id, created_at),
        INDEX idx_change_executions_plan (change_plan_id, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS aiops_sessions (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        session_id VARCHAR(128) NOT NULL UNIQUE,
        incident_id VARCHAR(128) NOT NULL,
        trace_id VARCHAR(128) NOT NULL,
        status VARCHAR(64) NOT NULL,
        node_name VARCHAR(128) NOT NULL,
        created_at VARCHAR(64) NOT NULL,
        updated_at VARCHAR(64) NOT NULL,
        payload LONGTEXT NOT NULL,
        INDEX idx_aiops_sessions_incident (incident_id, updated_at),
        INDEX idx_aiops_sessions_trace (trace_id, updated_at),
        INDEX idx_aiops_sessions_status (status, updated_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS a2a_tasks (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        task_id VARCHAR(128) NOT NULL UNIQUE,
        message_id VARCHAR(256) NOT NULL,
        request_fingerprint VARCHAR(64) NOT NULL,
        owner_id VARCHAR(128) NOT NULL DEFAULT '',
        skill VARCHAR(128) NOT NULL,
        incident_id VARCHAR(128) NOT NULL,
        state VARCHAR(64) NOT NULL,
        created_at VARCHAR(64) NOT NULL,
        updated_at VARCHAR(64) NOT NULL,
        payload LONGTEXT NOT NULL,
        INDEX idx_a2a_tasks_message (message_id, updated_at),
        INDEX idx_a2a_tasks_incident (incident_id, updated_at),
        INDEX idx_a2a_tasks_owner (owner_id, updated_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS incident_states (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        incident_id VARCHAR(128) NOT NULL UNIQUE,
        status VARCHAR(64) NOT NULL,
        service_name VARCHAR(128) NOT NULL,
        severity VARCHAR(32) NOT NULL,
        environment VARCHAR(80) NOT NULL,
        trace_id VARCHAR(128),
        session_id VARCHAR(128),
        approval_status VARCHAR(64) NOT NULL,
        created_at VARCHAR(64) NOT NULL,
        updated_at VARCHAR(64) NOT NULL,
        payload LONGTEXT NOT NULL,
        INDEX idx_incident_states_status (status, updated_at),
        INDEX idx_incident_states_service (service_name, updated_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS diagnosis_reports (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        report_id VARCHAR(128) NOT NULL UNIQUE,
        incident_id VARCHAR(128) NOT NULL,
        trace_id VARCHAR(128) NOT NULL,
        created_at VARCHAR(64) NOT NULL,
        updated_at VARCHAR(64) NOT NULL,
        payload LONGTEXT NOT NULL,
        INDEX idx_diagnosis_reports_incident (incident_id, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
)


def initialize_sqlite_store(database_path: str | Path, connect: Callable[[], Any]) -> None:
    """Create and upgrade the SQLite runtime schema."""
    Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    with connect() as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        try:
            connection.executescript(SQLITE_RUNTIME_SCHEMA)
        except sqlite3.OperationalError as exc:
            if "no such column: owner_id" not in str(exc):
                raise
            _apply_sqlite_a2a_task_ownership(connection)
            connection.executescript(SQLITE_RUNTIME_SCHEMA)
        _ensure_sqlite_report_timestamp(connection)
        apply_sqlite_migrations(
            connection,
            [
                SchemaMigration(1, "runtime_schema_baseline", lambda _connection: None),
                SchemaMigration(
                    2,
                    "approval_and_change_idempotency",
                    _apply_sqlite_approval_and_change_idempotency,
                ),
                SchemaMigration(3, "a2a_task_ownership", _apply_sqlite_a2a_task_ownership),
            ],
        )


def initialize_mysql_store(connect: Callable[[], Any]) -> None:
    """Create and upgrade the MySQL runtime schema."""
    with connect() as connection:
        with connection.cursor() as cursor:
            for statement in MYSQL_RUNTIME_SCHEMA:
                cursor.execute(statement)
            ensure_mysql_retention_timestamp_columns(cursor)
            ensure_mysql_runtime_column_capacities(cursor)
            require_mysql_transactional_runtime_tables(cursor)
            apply_mysql_migrations(
                cursor,
                [
                    SchemaMigration(1, "runtime_schema_baseline", lambda _cursor: None),
                    SchemaMigration(
                        2,
                        "approval_and_change_idempotency",
                        apply_mysql_approval_and_change_idempotency,
                    ),
                    SchemaMigration(3, "a2a_task_ownership", apply_mysql_a2a_task_ownership),
                ],
            )


def _ensure_sqlite_report_timestamp(connection: sqlite3.Connection) -> None:
    columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(diagnosis_reports)").fetchall()
    }
    if "updated_at" not in columns:
        connection.execute("ALTER TABLE diagnosis_reports ADD COLUMN updated_at TEXT")
        connection.execute(
            "UPDATE diagnosis_reports SET updated_at = created_at WHERE updated_at IS NULL"
        )


def _apply_sqlite_approval_and_change_idempotency(
    connection: sqlite3.Connection,
) -> None:
    columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(approval_requests)").fetchall()
    }
    if "idempotency_key" not in columns:
        connection.execute("ALTER TABLE approval_requests ADD COLUMN idempotency_key TEXT")
    if "updated_at" not in columns:
        connection.execute("ALTER TABLE approval_requests ADD COLUMN updated_at TEXT")
        connection.execute(
            "UPDATE approval_requests SET updated_at = "
            "COALESCE(decided_at, created_at) WHERE updated_at IS NULL"
        )
    connection.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uniq_pending_approval_idempotency
        ON approval_requests(idempotency_key)
        WHERE status = 'pending' AND idempotency_key IS NOT NULL
        """)
    try:
        connection.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uniq_change_executions_scope
            ON change_executions(incident_id, change_plan_id, approval_id)
            """)
    except sqlite3.IntegrityError as exc:
        duplicate_groups = _count_sqlite_change_execution_scope_duplicates(connection)
        raise RuntimeError(
            "SQLite change_executions contains "
            f"{duplicate_groups} duplicate business-scope groups"
        ) from exc
    except sqlite3.OperationalError as exc:
        raise RuntimeError(
            "SQLite change_executions schema cannot enforce business-scope idempotency"
        ) from exc


def _count_sqlite_change_execution_scope_duplicates(
    connection: sqlite3.Connection,
) -> int:
    row = connection.execute("""
        SELECT COUNT(*) AS duplicate_groups
        FROM (
            SELECT 1
            FROM change_executions
            GROUP BY incident_id, change_plan_id, approval_id
            HAVING COUNT(*) > 1
        )
        """).fetchone()
    return int(row["duplicate_groups"] or 0) if row is not None else 0


def _apply_sqlite_a2a_task_ownership(connection: sqlite3.Connection) -> None:
    columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(a2a_tasks)").fetchall()
    }
    if "owner_id" not in columns:
        connection.execute(
            "ALTER TABLE a2a_tasks ADD COLUMN owner_id TEXT NOT NULL DEFAULT ''"
        )
    rows = connection.execute(
        "SELECT task_id, payload FROM a2a_tasks WHERE owner_id = ''"
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(str(row["payload"]))
            owner_id = str(payload.get("owner_id") or "") if isinstance(payload, dict) else ""
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid A2A task payload while migrating owner_id: {row['task_id']}"
            ) from exc
        if owner_id:
            connection.execute(
                "UPDATE a2a_tasks SET owner_id = ? WHERE task_id = ?",
                (owner_id, row["task_id"]),
            )
    connection.execute("""
        CREATE INDEX IF NOT EXISTS idx_a2a_tasks_owner
        ON a2a_tasks(owner_id, updated_at)
        """)


def ensure_mysql_approval_idempotency_columns(cursor: Any) -> None:
    """Add approval idempotency columns and unique key to older tables."""
    try:
        cursor.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = 'approval_requests'
              AND column_name IN (
                  'idempotency_key', 'pending_idempotency_key', 'updated_at'
              )
            """)
        columns = {str(row.get("column_name") or "") for row in cursor.fetchall()}
        if "idempotency_key" not in columns:
            cursor.execute(
                "ALTER TABLE approval_requests ADD COLUMN idempotency_key VARCHAR(128)"
            )
        if "pending_idempotency_key" not in columns:
            cursor.execute("""
                ALTER TABLE approval_requests
                ADD COLUMN pending_idempotency_key VARCHAR(128)
                GENERATED ALWAYS AS (
                    CASE WHEN status = 'pending' THEN idempotency_key ELSE NULL END
                ) STORED
                """)
        if "updated_at" not in columns:
            cursor.execute("ALTER TABLE approval_requests ADD COLUMN updated_at VARCHAR(64)")
            cursor.execute(
                "UPDATE approval_requests SET updated_at = "
                "COALESCE(decided_at, created_at) WHERE updated_at IS NULL"
            )
            cursor.execute(
                "ALTER TABLE approval_requests MODIFY updated_at VARCHAR(64) NOT NULL"
            )
        cursor.execute("""
            SELECT COUNT(*) AS index_count
            FROM information_schema.statistics
            WHERE table_schema = DATABASE()
              AND table_name = 'approval_requests'
              AND index_name = 'uniq_pending_approval_idempotency'
            """)
        if int((cursor.fetchone() or {}).get("index_count") or 0) == 0:
            cursor.execute("""
                ALTER TABLE approval_requests
                ADD UNIQUE KEY uniq_pending_approval_idempotency (
                    pending_idempotency_key
                )
                """)
    except Exception as exc:
        raise RuntimeError(
            "MySQL approval schema is incompatible with runtime idempotency requirements"
        ) from exc


def apply_mysql_approval_and_change_idempotency(cursor: Any) -> None:
    ensure_mysql_approval_idempotency_columns(cursor)
    ensure_mysql_change_execution_scope_unique_index(cursor)


def apply_mysql_a2a_task_ownership(cursor: Any) -> None:
    """Add the persisted A2A owner used by authorization filters."""
    cursor.execute("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = 'a2a_tasks'
          AND column_name = 'owner_id'
        """)
    if cursor.fetchone() is None:
        cursor.execute(
            "ALTER TABLE a2a_tasks "
            "ADD COLUMN owner_id VARCHAR(128) NOT NULL DEFAULT '' AFTER request_fingerprint"
        )
    cursor.execute("SELECT task_id, payload FROM a2a_tasks WHERE owner_id = '' FOR UPDATE")
    for row in cursor.fetchall():
        try:
            payload = json.loads(str(row.get("payload") or ""))
            owner_id = str(payload.get("owner_id") or "") if isinstance(payload, dict) else ""
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "Invalid A2A task payload while migrating owner_id: "
                f"{row.get('task_id') or '<unknown>'}"
            ) from exc
        if owner_id:
            cursor.execute(
                "UPDATE a2a_tasks SET owner_id = %s WHERE task_id = %s",
                (owner_id, row["task_id"]),
            )
    cursor.execute("""
        SELECT COUNT(*) AS index_count
        FROM information_schema.statistics
        WHERE table_schema = DATABASE()
          AND table_name = 'a2a_tasks'
          AND index_name = 'idx_a2a_tasks_owner'
        """)
    if int((cursor.fetchone() or {}).get("index_count") or 0) == 0:
        cursor.execute(
            "ALTER TABLE a2a_tasks ADD INDEX idx_a2a_tasks_owner (owner_id, updated_at)"
        )


def ensure_mysql_retention_timestamp_columns(cursor: Any) -> None:
    """Backfill timestamps required for retention based on last mutation."""
    cursor.execute("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = 'diagnosis_reports'
          AND column_name = 'updated_at'
        """)
    if cursor.fetchone() is not None:
        return
    cursor.execute("ALTER TABLE diagnosis_reports ADD COLUMN updated_at VARCHAR(64)")
    cursor.execute("UPDATE diagnosis_reports SET updated_at = created_at WHERE updated_at IS NULL")
    cursor.execute("ALTER TABLE diagnosis_reports MODIFY updated_at VARCHAR(64) NOT NULL")


def ensure_mysql_runtime_column_capacities(cursor: Any) -> None:
    """Keep MySQL columns at least as wide as their Pydantic contracts."""
    required_lengths = {
        ("approval_requests", "action"): 1000,
        ("aiops_sessions", "status"): 64,
        ("incident_states", "status"): 64,
        ("incident_states", "environment"): 80,
        ("incident_states", "approval_status"): 64,
    }
    cursor.execute("""
        SELECT table_name, column_name, character_maximum_length
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND (
              (table_name = 'approval_requests' AND column_name = 'action')
              OR (table_name = 'aiops_sessions' AND column_name = 'status')
              OR (
                  table_name = 'incident_states'
                  AND column_name IN ('status', 'environment', 'approval_status')
              )
          )
        """)
    actual = {
        (str(row.get("table_name") or ""), str(row.get("column_name") or "")): int(
            row.get("character_maximum_length") or 0
        )
        for row in cursor.fetchall()
    }
    missing = sorted(set(required_lengths) - set(actual))
    if missing:
        formatted = ", ".join(f"{table}.{column}" for table, column in missing)
        raise RuntimeError(f"MySQL runtime schema is missing required columns: {formatted}")
    for (table, column), required in required_lengths.items():
        if actual[(table, column)] >= required:
            continue
        table = trusted_identifier(
            table,
            allowed={"approval_requests", "aiops_sessions", "incident_states"},
        )
        column = trusted_identifier(
            column,
            allowed={"action", "status", "environment", "approval_status"},
        )
        cursor.execute(  # nosec B608 -- identifiers and capacities are code-owned.
            f"ALTER TABLE {table} MODIFY {column} VARCHAR({required}) NOT NULL"
        )


def require_mysql_transactional_runtime_tables(cursor: Any) -> None:
    """Fail closed when runtime tables cannot participate in transactions."""
    runtime_tables = (
        "alert_events",
        "trace_events",
        "approval_requests",
        "change_executions",
        "aiops_sessions",
        "a2a_tasks",
        "incident_states",
        "diagnosis_reports",
    )
    placeholders = bind_markers(len(runtime_tables), "%s")
    cursor.execute(
        f"""
        SELECT table_name, engine
        FROM information_schema.tables
        WHERE table_schema = DATABASE()
          AND table_name IN ({placeholders})
        """,  # nosec B608 -- only generated bind markers are interpolated.
        runtime_tables,
    )
    engines = {
        str(row.get("table_name") or ""): str(row.get("engine") or "").upper()
        for row in cursor.fetchall()
    }
    missing = sorted(set(runtime_tables) - set(engines))
    non_transactional = sorted(table for table, engine in engines.items() if engine != "INNODB")
    if missing or non_transactional:
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if non_transactional:
            details.append("non_innodb=" + ",".join(non_transactional))
        raise RuntimeError(
            "MySQL runtime schema cannot guarantee atomic writes: " + "; ".join(details)
        )


def ensure_mysql_change_execution_scope_unique_index(cursor: Any) -> None:
    """Require the business idempotency unique key for concurrent creation."""
    try:
        cursor.execute("""
            SELECT COUNT(*) AS index_count
            FROM information_schema.statistics
            WHERE table_schema = DATABASE()
              AND table_name = 'change_executions'
              AND index_name = 'uniq_change_executions_scope'
            """)
        if int((cursor.fetchone() or {}).get("index_count") or 0) > 0:
            return
        cursor.execute("""
            SELECT COUNT(*) AS duplicate_groups
            FROM (
                SELECT incident_id, change_plan_id, approval_id
                FROM change_executions
                GROUP BY incident_id, change_plan_id, approval_id
                HAVING COUNT(*) > 1
            ) duplicate_scope
            """)
        duplicate_groups = int((cursor.fetchone() or {}).get("duplicate_groups") or 0)
        if duplicate_groups > 0:
            raise RuntimeError(
                "MySQL change_executions contains "
                f"{duplicate_groups} duplicate business-scope groups"
            )
        cursor.execute("""
            ALTER TABLE change_executions
            ADD UNIQUE KEY uniq_change_executions_scope (
                incident_id, change_plan_id, approval_id
            )
            """)
    except Exception as exc:
        raise RuntimeError(
            "MySQL change_executions schema cannot enforce business-scope idempotency"
        ) from exc
