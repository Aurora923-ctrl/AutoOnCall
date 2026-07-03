"""MySQL persistence for AIOps runtime state."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

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
        "SELECT COUNT(*) AS count FROM alert_events WHERE updated_at < %s",
        "DELETE FROM alert_events WHERE updated_at < %s",
    ),
    "trace_events": (
        "SELECT COUNT(*) AS count FROM trace_events WHERE created_at < %s",
        "DELETE FROM trace_events WHERE created_at < %s",
    ),
    "approval_requests": (
        "SELECT COUNT(*) AS count FROM approval_requests WHERE created_at < %s",
        "DELETE FROM approval_requests WHERE created_at < %s",
    ),
    "diagnosis_reports": (
        "SELECT COUNT(*) AS count FROM diagnosis_reports WHERE created_at < %s",
        "DELETE FROM diagnosis_reports WHERE created_at < %s",
    ),
    "change_executions": (
        "SELECT COUNT(*) AS count FROM change_executions WHERE created_at < %s",
        "DELETE FROM change_executions WHERE created_at < %s",
    ),
    "aiops_sessions": (
        "SELECT COUNT(*) AS count FROM aiops_sessions WHERE updated_at < %s",
        "DELETE FROM aiops_sessions WHERE updated_at < %s",
    ),
    "incident_states": (
        "SELECT COUNT(*) AS count FROM incident_states WHERE updated_at < %s",
        "DELETE FROM incident_states WHERE updated_at < %s",
    ),
}


class AIOpsMySQLStore:
    """Small PyMySQL-backed repository for trace, approval, and report state."""

    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or config.resolved_mysql_dsn
        if not self.dsn:
            raise ValueError("AIOPS_STORAGE_BACKEND=mysql requires MYSQL_DSN or MYSQL_HOST")
        self.connection_settings = _parse_mysql_dsn(self.dsn)
        self.storage_path = _redact_mysql_dsn(self.dsn)
        self.migration_warnings: list[str] = []
        self._initialize()

    def save_alert_event(self, event: AlertEvent) -> None:
        """Persist the latest state of one normalized alert event."""
        payload = _dump_model(event)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO alert_events (
                        fingerprint, incident_id, source, status, service_name,
                        severity, environment, starts_at, updated_at, payload
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        incident_id = VALUES(incident_id),
                        source = VALUES(source),
                        status = VALUES(status),
                        service_name = VALUES(service_name),
                        severity = VALUES(severity),
                        environment = VALUES(environment),
                        starts_at = VALUES(starts_at),
                        updated_at = VALUES(updated_at),
                        payload = VALUES(payload)
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
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM alert_events WHERE fingerprint = %s",
                    (fingerprint,),
                )
                row = cursor.fetchone()
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
            clauses.append("status = %s")
            params.append(status)
        if service_name is not None:
            clauses.append("service_name = %s")
            params.append(service_name)

        query = "SELECT payload FROM alert_events"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC, id DESC LIMIT %s"
        params.append(normalized_limit)

        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, params)
                rows = cursor.fetchall()
        return [AlertEvent.model_validate(_load_payload(row)) for row in rows]

    def save_trace_event(self, event: TraceEvent) -> None:
        """Persist a trace event."""
        payload = _dump_model(event)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO trace_events (
                        event_id, trace_id, incident_id, event_type, node_name,
                        step_id, tool_name, status, created_at, payload
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        trace_id = VALUES(trace_id),
                        incident_id = VALUES(incident_id),
                        event_type = VALUES(event_type),
                        node_name = VALUES(node_name),
                        step_id = VALUES(step_id),
                        tool_name = VALUES(tool_name),
                        status = VALUES(status),
                        created_at = VALUES(created_at),
                        payload = VALUES(payload)
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
            clauses.append("incident_id = %s")
            params.append(incident_id)
        if trace_id is not None:
            clauses.append("trace_id = %s")
            params.append(trace_id)
        if event_type is not None:
            clauses.append("event_type = %s")
            params.append(event_type)

        query = "SELECT payload FROM trace_events"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at ASC, id ASC"

        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, params)
                rows = cursor.fetchall()
        return [TraceEvent.model_validate(_load_payload(row)) for row in rows]

    def save_approval_request(self, request: ApprovalRequest) -> None:
        """Persist the latest state of one approval request."""
        payload = _dump_model(request)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO approval_requests (
                        approval_id, incident_id, status, risk_level, action,
                        created_at, decided_at, payload
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        incident_id = VALUES(incident_id),
                        status = VALUES(status),
                        risk_level = VALUES(risk_level),
                        action = VALUES(action),
                        created_at = VALUES(created_at),
                        decided_at = VALUES(decided_at),
                        payload = VALUES(payload)
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
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE approval_requests
                    SET
                        incident_id = %s,
                        status = %s,
                        risk_level = %s,
                        action = %s,
                        created_at = %s,
                        decided_at = %s,
                        payload = %s
                    WHERE approval_id = %s AND status = 'pending'
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
                return bool(cursor.rowcount == 1)

    def get_approval_request(self, approval_id: str) -> ApprovalRequest | None:
        """Return one approval request by id."""
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM approval_requests WHERE approval_id = %s",
                    (approval_id,),
                )
                row = cursor.fetchone()
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
            clauses.append("incident_id = %s")
            params.append(incident_id)
        if status is not None:
            clauses.append("status = %s")
            params.append(status)

        query = "SELECT payload FROM approval_requests"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at ASC, id ASC"

        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, params)
                rows = cursor.fetchall()
        return [ApprovalRequest.model_validate(_load_payload(row)) for row in rows]

    def save_change_execution(self, execution: ChangeExecution) -> None:
        """Persist the latest state of one safe change workflow."""
        payload = _dump_model(execution)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO change_executions (
                        change_execution_id, change_plan_id, approval_id, incident_id,
                        status, mode, created_at, updated_at, payload
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        change_plan_id = VALUES(change_plan_id),
                        approval_id = VALUES(approval_id),
                        incident_id = VALUES(incident_id),
                        status = VALUES(status),
                        mode = VALUES(mode),
                        created_at = VALUES(created_at),
                        updated_at = VALUES(updated_at),
                        payload = VALUES(payload)
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
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT payload FROM change_executions
                    WHERE incident_id = %s AND change_plan_id = %s AND approval_id = %s
                    ORDER BY created_at ASC, id ASC
                    LIMIT 1
                    """,
                    (execution.incident_id, execution.change_plan_id, execution.approval_id),
                )
                existing = cursor.fetchone()
                if existing is not None:
                    return ChangeExecution.model_validate(_load_payload(existing)), False

                cursor.execute(
                    """
                    INSERT IGNORE INTO change_executions (
                        change_execution_id, change_plan_id, approval_id, incident_id,
                        status, mode, created_at, updated_at, payload
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
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

                cursor.execute(
                    "SELECT payload FROM change_executions WHERE change_execution_id = %s",
                    (execution.change_execution_id,),
                )
                existing = cursor.fetchone()
                if existing is None:
                    cursor.execute(
                        """
                        SELECT payload FROM change_executions
                        WHERE incident_id = %s AND change_plan_id = %s AND approval_id = %s
                        ORDER BY created_at ASC, id ASC
                        LIMIT 1
                        """,
                        (execution.incident_id, execution.change_plan_id, execution.approval_id),
                    )
                    existing = cursor.fetchone()
                if existing is None:
                    raise RuntimeError(
                        "change execution creation conflicted but existing record was not found"
                    )
                return ChangeExecution.model_validate(_load_payload(existing)), False

    def get_change_execution(self, change_execution_id: str) -> ChangeExecution | None:
        """Return one safe change workflow by id."""
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM change_executions WHERE change_execution_id = %s",
                    (change_execution_id,),
                )
                row = cursor.fetchone()
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
            clauses.append("incident_id = %s")
            params.append(incident_id)
        if change_plan_id is not None:
            clauses.append("change_plan_id = %s")
            params.append(change_plan_id)

        query = "SELECT payload FROM change_executions"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at ASC, id ASC"

        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, params)
                rows = cursor.fetchall()
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
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO aiops_sessions (
                        session_id, incident_id, trace_id, status, node_name,
                        created_at, updated_at, payload
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        incident_id = VALUES(incident_id),
                        trace_id = VALUES(trace_id),
                        status = VALUES(status),
                        node_name = VALUES(node_name),
                        updated_at = VALUES(updated_at),
                        payload = VALUES(payload)
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
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM aiops_sessions WHERE session_id = %s",
                    (session_id,),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        return AIOpsSessionSnapshot.model_validate(_load_payload(row))

    def get_latest_aiops_session_snapshot(
        self,
        incident_id: str,
    ) -> AIOpsSessionSnapshot | None:
        """Return the newest durable diagnosis snapshot for one incident."""
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT payload
                    FROM aiops_sessions
                    WHERE incident_id = %s
                    ORDER BY updated_at DESC, id DESC
                    LIMIT 1
                    """,
                    (incident_id,),
                )
                row = cursor.fetchone()
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
            clauses.append("incident_id = %s")
            params.append(incident_id)

        query = "SELECT payload FROM aiops_sessions"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC, id DESC LIMIT %s"
        params.append(normalized_limit)

        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, params)
                rows = cursor.fetchall()
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
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO incident_states (
                        incident_id, status, service_name, severity, environment,
                        trace_id, session_id, approval_status, created_at, updated_at, payload
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        status = VALUES(status),
                        service_name = VALUES(service_name),
                        severity = VALUES(severity),
                        environment = VALUES(environment),
                        trace_id = VALUES(trace_id),
                        session_id = VALUES(session_id),
                        approval_status = VALUES(approval_status),
                        updated_at = VALUES(updated_at),
                        payload = VALUES(payload)
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
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM incident_states WHERE incident_id = %s",
                    (incident_id,),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        return IncidentState.model_validate(_load_payload(row))

    def list_incident_states(self) -> list[IncidentState]:
        """Return latest lifecycle states ordered by update time."""
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT payload
                    FROM incident_states
                    ORDER BY updated_at DESC, id DESC
                    """)
                rows = cursor.fetchall()
        return [IncidentState.model_validate(_load_payload(row)) for row in rows]

    def save_report(self, report: DiagnosisReport) -> None:
        """Persist a diagnosis report."""
        payload = _dump_model(report)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO diagnosis_reports (
                        report_id, incident_id, trace_id, created_at, payload
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        incident_id = VALUES(incident_id),
                        trace_id = VALUES(trace_id),
                        created_at = VALUES(created_at),
                        payload = VALUES(payload)
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
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT payload
                    FROM diagnosis_reports
                    WHERE incident_id = %s
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """,
                    (incident_id,),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        return DiagnosisReport.model_validate(_load_payload(row))

    def list_latest_reports(self) -> list[DiagnosisReport]:
        """Return the latest report per incident."""
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT dr.payload
                    FROM diagnosis_reports dr
                    JOIN (
                        SELECT incident_id, MAX(id) AS latest_id
                        FROM diagnosis_reports
                        GROUP BY incident_id
                    ) latest ON latest.latest_id = dr.id
                    ORDER BY dr.created_at DESC, dr.id DESC
                    """)
                rows = cursor.fetchall()
        return [DiagnosisReport.model_validate(_load_payload(row)) for row in rows]

    def cleanup_older_than(self, *, keep_days: int, dry_run: bool = False) -> dict[str, Any]:
        """Delete runtime records older than the retention window."""
        if keep_days < 1:
            raise ValueError("keep_days must be >= 1")

        cutoff = datetime.now(UTC) - timedelta(days=keep_days)
        cutoff_text = cutoff.isoformat()
        deleted: dict[str, int] = {}

        with self._connect() as connection:
            with connection.cursor() as cursor:
                for table, (count_sql, delete_sql) in _RETENTION_SQL.items():
                    cursor.execute(count_sql, (cutoff_text,))
                    deleted[table] = int(cursor.fetchone()["count"])
                    if not dry_run:
                        cursor.execute(delete_sql, (cutoff_text,))

        return {
            "backend": "mysql",
            "database": self.storage_path,
            "keep_days": keep_days,
            "cutoff": cutoff_text,
            "dry_run": dry_run,
            "deleted": deleted,
        }

    def _initialize(self) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
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
                    """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS trace_events (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        event_id VARCHAR(64) NOT NULL UNIQUE,
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
                    """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS approval_requests (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        approval_id VARCHAR(64) NOT NULL UNIQUE,
                        incident_id VARCHAR(128) NOT NULL,
                        status VARCHAR(32) NOT NULL,
                        risk_level VARCHAR(32) NOT NULL,
                        action VARCHAR(512) NOT NULL,
                        created_at VARCHAR(64) NOT NULL,
                        decided_at VARCHAR(64),
                        payload LONGTEXT NOT NULL,
                        INDEX idx_approval_requests_incident (incident_id, created_at),
                        INDEX idx_approval_requests_status (status, created_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS change_executions (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        change_execution_id VARCHAR(64) NOT NULL UNIQUE,
                        change_plan_id VARCHAR(64) NOT NULL,
                        approval_id VARCHAR(64) NOT NULL,
                        incident_id VARCHAR(128) NOT NULL,
                        status VARCHAR(32) NOT NULL,
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
                    """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS aiops_sessions (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        session_id VARCHAR(128) NOT NULL UNIQUE,
                        incident_id VARCHAR(128) NOT NULL,
                        trace_id VARCHAR(128) NOT NULL,
                        status VARCHAR(32) NOT NULL,
                        node_name VARCHAR(128) NOT NULL,
                        created_at VARCHAR(64) NOT NULL,
                        updated_at VARCHAR(64) NOT NULL,
                        payload LONGTEXT NOT NULL,
                        INDEX idx_aiops_sessions_incident (incident_id, updated_at),
                        INDEX idx_aiops_sessions_trace (trace_id, updated_at),
                        INDEX idx_aiops_sessions_status (status, updated_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS incident_states (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        incident_id VARCHAR(128) NOT NULL UNIQUE,
                        status VARCHAR(32) NOT NULL,
                        service_name VARCHAR(128) NOT NULL,
                        severity VARCHAR(32) NOT NULL,
                        environment VARCHAR(64) NOT NULL,
                        trace_id VARCHAR(128),
                        session_id VARCHAR(128),
                        approval_status VARCHAR(32) NOT NULL,
                        created_at VARCHAR(64) NOT NULL,
                        updated_at VARCHAR(64) NOT NULL,
                        payload LONGTEXT NOT NULL,
                        INDEX idx_incident_states_status (status, updated_at),
                        INDEX idx_incident_states_service (service_name, updated_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS diagnosis_reports (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        report_id VARCHAR(64) NOT NULL UNIQUE,
                        incident_id VARCHAR(128) NOT NULL,
                        trace_id VARCHAR(128) NOT NULL,
                        created_at VARCHAR(64) NOT NULL,
                        payload LONGTEXT NOT NULL,
                        INDEX idx_diagnosis_reports_incident (incident_id, created_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """)
                self._ensure_change_execution_scope_unique_index(cursor)

    def _ensure_change_execution_scope_unique_index(self, cursor: Any) -> None:
        """Add the business idempotency unique key to older MySQL tables when safe."""
        try:
            cursor.execute("""
                SELECT COUNT(*) AS index_count
                FROM information_schema.statistics
                WHERE table_schema = DATABASE()
                  AND table_name = 'change_executions'
                  AND index_name = 'uniq_change_executions_scope'
                """)
            row = cursor.fetchone() or {}
            if int(row.get("index_count") or 0) > 0:
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
            duplicate_row = cursor.fetchone() or {}
            duplicate_groups = int(duplicate_row.get("duplicate_groups") or 0)
            if duplicate_groups > 0:
                self._record_migration_warning(
                    "无法为 MySQL change_executions 创建业务幂等唯一索引，"
                    f"发现 {duplicate_groups} 组历史重复安全变更记录；"
                    "运行时将继续通过应用层预查避免新增重复。"
                )
                return

            cursor.execute("""
                ALTER TABLE change_executions
                ADD UNIQUE KEY uniq_change_executions_scope (
                    incident_id, change_plan_id, approval_id
                )
                """)
        except Exception as exc:
            self._record_migration_warning(
                "无法检查或创建 MySQL change_executions 业务幂等唯一索引；"
                "运行时将继续通过应用层预查避免新增重复。"
                f" error={exc}"
            )

    def _record_migration_warning(self, message: str) -> None:
        self.migration_warnings.append(message)
        logger.warning(message)

    def _connect(self):
        try:
            import pymysql
            from pymysql.cursors import DictCursor
        except ImportError as exc:
            raise RuntimeError(
                "AIOPS_STORAGE_BACKEND=mysql requires pymysql; install project dependencies."
            ) from exc

        return pymysql.connect(
            **self.connection_settings,
            autocommit=True,
            cursorclass=DictCursor,
        )


def _parse_mysql_dsn(dsn: str) -> dict[str, Any]:
    parsed = urlparse(dsn)
    if parsed.scheme not in {"mysql", "mysql+pymysql"}:
        raise ValueError("MySQL DSN must start with mysql:// or mysql+pymysql://")
    query = parse_qs(parsed.query)
    settings: dict[str, Any] = {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 3306,
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "database": unquote(parsed.path.lstrip("/")),
        "charset": query.get("charset", ["utf8mb4"])[0],
        "connect_timeout": int(
            float(query.get("connect_timeout", [config.mysql_timeout_seconds])[0])
        ),
        "read_timeout": int(float(query.get("read_timeout", [config.mysql_timeout_seconds])[0])),
        "write_timeout": int(float(query.get("write_timeout", [config.mysql_timeout_seconds])[0])),
    }
    if not settings["database"]:
        raise ValueError("MySQL DSN must include a database name")
    return settings


def _redact_mysql_dsn(dsn: str) -> str:
    parsed = urlparse(dsn)
    if parsed.password is None:
        return dsn
    username = parsed.username or ""
    auth = f"{username}:***@"
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{auth}{host}{port}{parsed.path}"


def _dump_model(model: Any) -> str:
    return json.dumps(model.model_dump(mode="json"), ensure_ascii=False, default=str)


def _load_payload(row: dict[str, Any]) -> dict[str, Any]:
    payload = json.loads(str(row["payload"]))
    return payload if isinstance(payload, dict) else {}
