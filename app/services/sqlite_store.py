"""SQLite persistence for AIOps runtime state."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger

from app.config import config
from app.models.aiops_session import AIOpsSessionSnapshot
from app.models.alert import AlertEvent
from app.models.approval import ApprovalRequest
from app.models.change_execution import ChangeExecution
from app.models.incident_state import IncidentState
from app.models.report import DiagnosisReport
from app.models.trace import TraceEvent
from app.services.incident_lifecycle import merge_incident_state

_RETENTION_SQL: dict[str, tuple[str, str]] = {
    "alert_events": (
        "SELECT COUNT(*) FROM alert_events WHERE updated_at < ?",
        "DELETE FROM alert_events WHERE updated_at < ?",
    ),
    "trace_events": (
        "SELECT COUNT(*) FROM trace_events WHERE created_at < ?",
        "DELETE FROM trace_events WHERE created_at < ?",
    ),
    "approval_requests": (
        "SELECT COUNT(*) FROM approval_requests WHERE created_at < ?",
        "DELETE FROM approval_requests WHERE created_at < ?",
    ),
    "diagnosis_reports": (
        "SELECT COUNT(*) FROM diagnosis_reports WHERE created_at < ?",
        "DELETE FROM diagnosis_reports WHERE created_at < ?",
    ),
    "change_executions": (
        "SELECT COUNT(*) FROM change_executions WHERE created_at < ?",
        "DELETE FROM change_executions WHERE created_at < ?",
    ),
    "aiops_sessions": (
        "SELECT COUNT(*) FROM aiops_sessions WHERE updated_at < ?",
        "DELETE FROM aiops_sessions WHERE updated_at < ?",
    ),
    "incident_states": (
        "SELECT COUNT(*) FROM incident_states WHERE updated_at < ?",
        "DELETE FROM incident_states WHERE updated_at < ?",
    ),
}


def resolve_sqlite_path(storage_path: str | Path | None = None) -> Path:
    """Resolve a runtime storage path to a SQLite database path."""
    if storage_path is None:
        return Path(config.aiops_sqlite_path)

    path = Path(storage_path)
    if path.suffix.lower() == ".jsonl":
        return path.with_suffix(".db")
    return path


class AIOpsSQLiteStore:
    """Small SQLite repository for trace, approval, and report state."""

    def __init__(self, database_path: str | Path | None = None):
        self.database_path = resolve_sqlite_path(database_path)
        self.migration_warnings: list[str] = []
        self._initialize()

    def save_alert_event(self, event: AlertEvent) -> None:
        """Persist the latest state of one normalized alert event."""
        payload = _dump_model(event)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO alert_events (
                    fingerprint, incident_id, source, status, service_name,
                    severity, environment, starts_at, updated_at, payload
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    incident_id = excluded.incident_id,
                    source = excluded.source,
                    status = excluded.status,
                    service_name = excluded.service_name,
                    severity = excluded.severity,
                    environment = excluded.environment,
                    starts_at = excluded.starts_at,
                    updated_at = excluded.updated_at,
                    payload = excluded.payload
                """,
                (
                    event.fingerprint,
                    event.incident_id,
                    event.source,
                    event.status,
                    event.service_name,
                    event.severity,
                    event.environment,
                    event.starts_at.isoformat() if event.starts_at else None,
                    event.updated_at.isoformat(),
                    payload,
                ),
            )

    def get_alert_event(self, fingerprint: str) -> AlertEvent | None:
        """Return one normalized alert by fingerprint."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM alert_events WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
        if row is None:
            return None
        return AlertEvent.model_validate(_load_payload(row))

    def list_alert_events(
        self,
        *,
        status: str | None = None,
        service_name: str | None = None,
        limit: int = 50,
    ) -> list[AlertEvent]:
        """List normalized alert events by recent update time."""
        normalized_limit = max(1, min(int(limit or 50), 200))
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if service_name is not None:
            clauses.append("service_name = ?")
            params.append(service_name)

        query = "SELECT payload FROM alert_events"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC, rowid DESC LIMIT ?"
        params.append(normalized_limit)

        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [AlertEvent.model_validate(_load_payload(row)) for row in rows]

    def save_trace_event(self, event: TraceEvent) -> None:
        """Persist a trace event."""
        payload = _dump_model(event)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO trace_events (
                    event_id, trace_id, incident_id, event_type, node_name,
                    step_id, tool_name, status, created_at, payload
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    trace_id = excluded.trace_id,
                    incident_id = excluded.incident_id,
                    event_type = excluded.event_type,
                    node_name = excluded.node_name,
                    step_id = excluded.step_id,
                    tool_name = excluded.tool_name,
                    status = excluded.status,
                    created_at = excluded.created_at,
                    payload = excluded.payload
                """,
                (
                    event.event_id,
                    event.trace_id,
                    event.incident_id,
                    event.event_type,
                    event.node_name,
                    event.step_id,
                    event.tool_name,
                    event.status,
                    event.created_at.isoformat(),
                    payload,
                ),
            )

    def list_trace_events(
        self,
        *,
        incident_id: str | None = None,
        trace_id: str | None = None,
        event_type: str | None = None,
    ) -> list[TraceEvent]:
        """List trace events filtered by incident, trace, or type."""
        clauses: list[str] = []
        params: list[Any] = []
        if incident_id is not None:
            clauses.append("incident_id = ?")
            params.append(incident_id)
        if trace_id is not None:
            clauses.append("trace_id = ?")
            params.append(trace_id)
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type)

        query = "SELECT payload FROM trace_events"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at ASC, rowid ASC"

        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [TraceEvent.model_validate(_load_payload(row)) for row in rows]

    def save_approval_request(self, request: ApprovalRequest) -> None:
        """Persist the latest state of one approval request."""
        payload = _dump_model(request)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO approval_requests (
                    approval_id, incident_id, status, risk_level, action,
                    created_at, decided_at, payload
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(approval_id) DO UPDATE SET
                    incident_id = excluded.incident_id,
                    status = excluded.status,
                    risk_level = excluded.risk_level,
                    action = excluded.action,
                    created_at = excluded.created_at,
                    decided_at = excluded.decided_at,
                    payload = excluded.payload
                """,
                (
                    request.approval_id,
                    request.incident_id,
                    request.status,
                    request.risk_level,
                    request.action,
                    request.created_at.isoformat(),
                    request.decided_at.isoformat() if request.decided_at else None,
                    payload,
                ),
            )

    def save_approval_decision_if_pending(self, request: ApprovalRequest) -> bool:
        """Persist an approval decision only while the request is still pending."""
        payload = _dump_model(request)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE approval_requests
                SET
                    incident_id = ?,
                    status = ?,
                    risk_level = ?,
                    action = ?,
                    created_at = ?,
                    decided_at = ?,
                    payload = ?
                WHERE approval_id = ? AND status = 'pending'
                """,
                (
                    request.incident_id,
                    request.status,
                    request.risk_level,
                    request.action,
                    request.created_at.isoformat(),
                    request.decided_at.isoformat() if request.decided_at else None,
                    payload,
                    request.approval_id,
                ),
            )
            return cursor.rowcount == 1

    def get_approval_request(self, approval_id: str) -> ApprovalRequest | None:
        """Return one approval request by id."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM approval_requests WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
        if row is None:
            return None
        return ApprovalRequest.model_validate(_load_payload(row))

    def list_approval_requests(
        self,
        *,
        incident_id: str | None = None,
        status: str | None = None,
    ) -> list[ApprovalRequest]:
        """List approval requests filtered by incident and status."""
        clauses: list[str] = []
        params: list[Any] = []
        if incident_id is not None:
            clauses.append("incident_id = ?")
            params.append(incident_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)

        query = "SELECT payload FROM approval_requests"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at ASC, rowid ASC"

        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [ApprovalRequest.model_validate(_load_payload(row)) for row in rows]

    def save_change_execution(self, execution: ChangeExecution) -> None:
        """Persist the latest state of one safe change workflow."""
        payload = _dump_model(execution)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO change_executions (
                    change_execution_id, change_plan_id, approval_id, incident_id,
                    status, mode, created_at, updated_at, payload
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(change_execution_id) DO UPDATE SET
                    change_plan_id = excluded.change_plan_id,
                    approval_id = excluded.approval_id,
                    incident_id = excluded.incident_id,
                    status = excluded.status,
                    mode = excluded.mode,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    payload = excluded.payload
                """,
                (
                    execution.change_execution_id,
                    execution.change_plan_id,
                    execution.approval_id,
                    execution.incident_id,
                    execution.status,
                    execution.mode,
                    execution.created_at.isoformat(),
                    execution.updated_at.isoformat(),
                    payload,
                ),
            )

    def create_change_execution_once(
        self,
        execution: ChangeExecution,
    ) -> tuple[ChangeExecution, bool]:
        """Create a safe change workflow once and return an existing row on conflict."""
        payload = _dump_model(execution)
        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT payload FROM change_executions
                WHERE incident_id = ? AND change_plan_id = ? AND approval_id = ?
                ORDER BY created_at ASC, rowid ASC
                LIMIT 1
                """,
                (execution.incident_id, execution.change_plan_id, execution.approval_id),
            ).fetchone()
            if existing is not None:
                return ChangeExecution.model_validate(_load_payload(existing)), False

            cursor = connection.execute(
                """
                INSERT INTO change_executions (
                    change_execution_id, change_plan_id, approval_id, incident_id,
                    status, mode, created_at, updated_at, payload
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (
                    execution.change_execution_id,
                    execution.change_plan_id,
                    execution.approval_id,
                    execution.incident_id,
                    execution.status,
                    execution.mode,
                    execution.created_at.isoformat(),
                    execution.updated_at.isoformat(),
                    payload,
                ),
            )
            if cursor.rowcount == 1:
                return execution, True

            existing = connection.execute(
                "SELECT payload FROM change_executions WHERE change_execution_id = ?",
                (execution.change_execution_id,),
            ).fetchone()
            if existing is None:
                existing = connection.execute(
                    """
                    SELECT payload FROM change_executions
                    WHERE incident_id = ? AND change_plan_id = ? AND approval_id = ?
                    ORDER BY created_at ASC, rowid ASC
                    LIMIT 1
                    """,
                    (execution.incident_id, execution.change_plan_id, execution.approval_id),
                ).fetchone()
            if existing is None:
                raise RuntimeError(
                    "change execution creation conflicted but existing record was not found"
                )
            return ChangeExecution.model_validate(_load_payload(existing)), False

    def get_change_execution(self, change_execution_id: str) -> ChangeExecution | None:
        """Return one safe change workflow by id."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM change_executions WHERE change_execution_id = ?",
                (change_execution_id,),
            ).fetchone()
        if row is None:
            return None
        return ChangeExecution.model_validate(_load_payload(row))

    def list_change_executions(
        self,
        *,
        incident_id: str | None = None,
        change_plan_id: str | None = None,
    ) -> list[ChangeExecution]:
        """List safe change workflows filtered by incident or plan."""
        clauses: list[str] = []
        params: list[Any] = []
        if incident_id is not None:
            clauses.append("incident_id = ?")
            params.append(incident_id)
        if change_plan_id is not None:
            clauses.append("change_plan_id = ?")
            params.append(change_plan_id)

        query = "SELECT payload FROM change_executions"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at ASC, rowid ASC"

        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [ChangeExecution.model_validate(_load_payload(row)) for row in rows]

    def save_aiops_session_snapshot(self, snapshot: AIOpsSessionSnapshot) -> None:
        """Persist the latest durable snapshot for one diagnosis session."""
        now = datetime.now(UTC)
        existing = self.get_aiops_session_snapshot(snapshot.session_id)
        if existing is not None:
            snapshot.created_at = existing.created_at
        snapshot.updated_at = now
        payload = _dump_model(snapshot)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO aiops_sessions (
                    session_id, incident_id, trace_id, status, node_name,
                    created_at, updated_at, payload
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    incident_id = excluded.incident_id,
                    trace_id = excluded.trace_id,
                    status = excluded.status,
                    node_name = excluded.node_name,
                    updated_at = excluded.updated_at,
                    payload = excluded.payload
                """,
                (
                    snapshot.session_id,
                    snapshot.incident_id,
                    snapshot.trace_id,
                    snapshot.status,
                    snapshot.node_name,
                    snapshot.created_at.isoformat(),
                    snapshot.updated_at.isoformat(),
                    payload,
                ),
            )

    def get_aiops_session_snapshot(self, session_id: str) -> AIOpsSessionSnapshot | None:
        """Return the latest durable snapshot for one diagnosis session."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM aiops_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return AIOpsSessionSnapshot.model_validate(_load_payload(row))

    def get_latest_aiops_session_snapshot(
        self,
        incident_id: str,
    ) -> AIOpsSessionSnapshot | None:
        """Return the newest durable diagnosis snapshot for one incident."""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT payload
                FROM aiops_sessions
                WHERE incident_id = ?
                ORDER BY updated_at DESC, rowid DESC
                LIMIT 1
                """,
                (incident_id,),
            ).fetchone()
        if row is None:
            return None
        return AIOpsSessionSnapshot.model_validate(_load_payload(row))

    def list_aiops_session_snapshots(
        self,
        *,
        incident_id: str | None = None,
        limit: int = 20,
    ) -> list[AIOpsSessionSnapshot]:
        """List durable diagnosis session snapshots by recent update time."""
        normalized_limit = max(1, min(int(limit or 20), 100))
        clauses = []
        params: list[object] = []
        if incident_id:
            clauses.append("incident_id = ?")
            params.append(incident_id)

        query = "SELECT payload FROM aiops_sessions"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC, rowid DESC LIMIT ?"
        params.append(normalized_limit)

        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [AIOpsSessionSnapshot.model_validate(_load_payload(row)) for row in rows]

    def save_incident_state(self, state: IncidentState) -> None:
        """Persist the latest lifecycle state for one incident."""
        existing = self.get_incident_state(state.incident_id)
        if existing is not None:
            state = merge_incident_state(existing, state)
        now = datetime.now(UTC)
        state.updated_at = now
        payload = _dump_model(state)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO incident_states (
                    incident_id, status, service_name, severity, environment,
                    trace_id, session_id, approval_status, created_at, updated_at, payload
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(incident_id) DO UPDATE SET
                    status = excluded.status,
                    service_name = excluded.service_name,
                    severity = excluded.severity,
                    environment = excluded.environment,
                    trace_id = excluded.trace_id,
                    session_id = excluded.session_id,
                    approval_status = excluded.approval_status,
                    updated_at = excluded.updated_at,
                    payload = excluded.payload
                """,
                (
                    state.incident_id,
                    state.status,
                    state.service_name,
                    state.severity,
                    state.environment,
                    state.trace_id,
                    state.session_id,
                    state.approval_status,
                    state.created_at.isoformat(),
                    state.updated_at.isoformat(),
                    payload,
                ),
            )

    def get_incident_state(self, incident_id: str) -> IncidentState | None:
        """Return the latest lifecycle state for one incident."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM incident_states WHERE incident_id = ?",
                (incident_id,),
            ).fetchone()
        if row is None:
            return None
        return IncidentState.model_validate(_load_payload(row))

    def list_incident_states(self) -> list[IncidentState]:
        """Return latest lifecycle states ordered by update time."""
        with self._connect() as connection:
            rows = connection.execute("""
                SELECT payload
                FROM incident_states
                ORDER BY updated_at DESC, rowid DESC
                """).fetchall()
        return [IncidentState.model_validate(_load_payload(row)) for row in rows]

    def save_report(self, report: DiagnosisReport) -> None:
        """Persist a diagnosis report."""
        payload = _dump_model(report)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO diagnosis_reports (
                    report_id, incident_id, trace_id, created_at, payload
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(report_id) DO UPDATE SET
                    incident_id = excluded.incident_id,
                    trace_id = excluded.trace_id,
                    created_at = excluded.created_at,
                    payload = excluded.payload
                """,
                (
                    report.report_id,
                    report.incident_id,
                    report.trace_id,
                    report.created_at.isoformat(),
                    payload,
                ),
            )

    def get_latest_report(self, incident_id: str) -> DiagnosisReport | None:
        """Return the latest report for one incident."""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT payload
                FROM diagnosis_reports
                WHERE incident_id = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT 1
                """,
                (incident_id,),
            ).fetchone()
        if row is None:
            return None
        return DiagnosisReport.model_validate(_load_payload(row))

    def list_latest_reports(self) -> list[DiagnosisReport]:
        """Return the latest report per incident."""
        with self._connect() as connection:
            rows = connection.execute("""
                SELECT payload
                FROM diagnosis_reports
                WHERE rowid IN (
                    SELECT MAX(rowid)
                    FROM diagnosis_reports
                    GROUP BY incident_id
                )
                ORDER BY created_at DESC, rowid DESC
                """).fetchall()
        return [DiagnosisReport.model_validate(_load_payload(row)) for row in rows]

    def cleanup_older_than(self, *, keep_days: int, dry_run: bool = False) -> dict[str, Any]:
        """Delete runtime records older than the retention window."""
        if keep_days < 1:
            raise ValueError("keep_days must be >= 1")

        cutoff = datetime.now(UTC) - timedelta(days=keep_days)
        cutoff_text = cutoff.isoformat()
        deleted: dict[str, int] = {}

        with self._connect() as connection:
            for table, (count_sql, delete_sql) in _RETENTION_SQL.items():
                count = connection.execute(count_sql, (cutoff_text,)).fetchone()[0]
                deleted[table] = int(count)
                if not dry_run:
                    connection.execute(delete_sql, (cutoff_text,))
            if not dry_run:
                try:
                    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.OperationalError:
                    pass

        return {
            "database_path": str(self.database_path),
            "keep_days": keep_days,
            "cutoff": cutoff_text,
            "dry_run": dry_run,
            "deleted": deleted,
        }

    def _initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
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
                    created_at TEXT NOT NULL,
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
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_diagnosis_reports_incident
                    ON diagnosis_reports(incident_id, created_at);
                """)
            try:
                connection.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS uniq_change_executions_scope
                    ON change_executions(incident_id, change_plan_id, approval_id)
                    """)
            except sqlite3.IntegrityError as exc:
                duplicate_groups = self._count_change_execution_scope_duplicates(connection)
                self._record_migration_warning(
                    "无法为 change_executions 创建业务幂等唯一索引，"
                    f"发现 {duplicate_groups} 组历史重复安全变更记录；"
                    "运行时将继续通过应用层预查避免新增重复。"
                    f" error={exc}"
                )
            except sqlite3.OperationalError as exc:
                self._record_migration_warning(
                    "无法检查或创建 change_executions 业务幂等唯一索引；"
                    "运行时将继续通过应用层预查避免新增重复。"
                    f" error={exc}"
                )

    def _count_change_execution_scope_duplicates(self, connection: sqlite3.Connection) -> int:
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

    def _record_migration_warning(self, message: str) -> None:
        self.migration_warnings.append(message)
        logger.warning(message)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.execute("PRAGMA busy_timeout=5000")
        connection.row_factory = sqlite3.Row
        return connection


def _dump_model(model: Any) -> str:
    return json.dumps(model.model_dump(mode="json"), ensure_ascii=False, default=str)


def _load_payload(row: sqlite3.Row) -> dict[str, Any]:
    payload = json.loads(str(row["payload"]))
    return payload if isinstance(payload, dict) else {}
