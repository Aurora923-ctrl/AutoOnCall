"""MySQL persistence for AIOps runtime state."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse
from uuid import uuid4

from loguru import logger

from app.config import config
from app.models.a2a import A2ATaskRecord
from app.models.aiops_session import AIOpsSessionSnapshot
from app.models.alert import AlertEvent
from app.models.approval import ApprovalRequest
from app.models.change_execution import ChangeExecution
from app.models.incident import Incident
from app.models.incident_state import IncidentState
from app.models.report import DiagnosisReport
from app.models.trace import TraceEvent
from app.services.incident_lifecycle import (
    alert_auto_diagnosis_claim_is_active,
    is_new_alert_generation,
    is_stale_alert_event,
    merge_incident_state,
)
from app.services.incident_state_builder import build_incident_state_from_alert
from app.services.sql_safety import bind_markers, trusted_identifier, trusted_table_statement

_RETENTION_SQL: dict[str, tuple[str, str]] = {
    "alert_events": (
        """
        SELECT COUNT(*) AS count FROM alert_events AS target
        WHERE target.updated_at < %s AND target.status = 'resolved'
          AND NOT EXISTS (
              SELECT 1 FROM incident_states AS state
              WHERE state.incident_id = target.incident_id
                AND state.status NOT IN (
                    'completed', 'incomplete', 'degraded', 'needs_human',
                    'approval_rejected', 'approval_cancelled', 'approval_resumed',
                    'blocked', 'failed', 'escalated', 'resolved', 'closed',
                    'precheck_failed', 'dry_run_failed', 'sandbox_validated',
                    'rolled_back', 'rollback_failed'
                )
          )
          AND NOT EXISTS (
              SELECT 1 FROM aiops_sessions AS session
              WHERE session.incident_id = target.incident_id
                AND session.status IN (
                    'running', 'planning', 'executing', 'resume_running',
                    'waiting_approval', 'approval_approved', 'change_validated',
                    'waiting_manual_execution', 'observing', 'partial_success',
                    'recovery_pending', 'rollback_recommended'
                )
          )
          AND NOT EXISTS (
              SELECT 1 FROM approval_requests AS approval
              WHERE approval.incident_id = target.incident_id
                AND approval.status = 'pending'
          )
          AND NOT EXISTS (
              SELECT 1 FROM change_executions AS execution
              WHERE execution.incident_id = target.incident_id
                AND execution.status NOT IN (
                    'precheck_failed', 'dry_run_failed', 'sandbox_validated',
                    'rolled_back', 'rollback_failed', 'closed', 'escalated'
                )
          )
        """,
        """
        DELETE target FROM alert_events AS target
        WHERE target.updated_at < %s AND target.status = 'resolved'
          AND NOT EXISTS (
              SELECT 1 FROM incident_states AS state
              WHERE state.incident_id = target.incident_id
                AND state.status NOT IN (
                    'completed', 'incomplete', 'degraded', 'needs_human',
                    'approval_rejected', 'approval_cancelled', 'approval_resumed',
                    'blocked', 'failed', 'escalated', 'resolved', 'closed',
                    'precheck_failed', 'dry_run_failed', 'sandbox_validated',
                    'rolled_back', 'rollback_failed'
                )
          )
          AND NOT EXISTS (
              SELECT 1 FROM aiops_sessions AS session
              WHERE session.incident_id = target.incident_id
                AND session.status IN (
                    'running', 'planning', 'executing', 'resume_running',
                    'waiting_approval', 'approval_approved', 'change_validated',
                    'waiting_manual_execution', 'observing', 'partial_success',
                    'recovery_pending', 'rollback_recommended'
                )
          )
          AND NOT EXISTS (
              SELECT 1 FROM approval_requests AS approval
              WHERE approval.incident_id = target.incident_id
                AND approval.status = 'pending'
          )
          AND NOT EXISTS (
              SELECT 1 FROM change_executions AS execution
              WHERE execution.incident_id = target.incident_id
                AND execution.status NOT IN (
                    'precheck_failed', 'dry_run_failed', 'sandbox_validated',
                    'rolled_back', 'rollback_failed', 'closed', 'escalated'
                )
          )
        """,
    ),
    "trace_events": (
        """
        SELECT COUNT(*) AS count FROM trace_events AS target
        WHERE target.created_at < %s
          AND NOT EXISTS (
              SELECT 1 FROM incident_states AS state
              WHERE state.incident_id = target.incident_id
                AND state.status NOT IN (
                    'completed', 'incomplete', 'degraded', 'needs_human',
                    'approval_rejected', 'approval_cancelled', 'approval_resumed',
                    'blocked', 'failed', 'escalated', 'resolved', 'closed',
                    'precheck_failed', 'dry_run_failed', 'sandbox_validated',
                    'rolled_back', 'rollback_failed'
                )
          )
          AND NOT EXISTS (
              SELECT 1 FROM aiops_sessions AS session
              WHERE session.incident_id = target.incident_id
                AND session.status IN (
                    'running', 'planning', 'executing', 'resume_running',
                    'waiting_approval', 'approval_approved'
                )
          )
          AND NOT EXISTS (
              SELECT 1 FROM approval_requests AS approval
              WHERE approval.incident_id = target.incident_id
                AND approval.status = 'pending'
          )
          AND NOT EXISTS (
              SELECT 1 FROM alert_events AS alert
              WHERE alert.incident_id = target.incident_id
                AND alert.status != 'resolved'
          )
          AND NOT EXISTS (
              SELECT 1 FROM change_executions AS execution
              WHERE execution.incident_id = target.incident_id
                AND execution.status NOT IN (
                    'precheck_failed', 'dry_run_failed', 'sandbox_validated',
                    'rolled_back', 'rollback_failed',
                    'closed', 'escalated'
                )
          )
        """,
        """
        DELETE target FROM trace_events AS target
        WHERE target.created_at < %s
          AND NOT EXISTS (
              SELECT 1 FROM incident_states AS state
              WHERE state.incident_id = target.incident_id
                AND state.status NOT IN (
                    'completed', 'incomplete', 'degraded', 'needs_human',
                    'approval_rejected', 'approval_cancelled', 'approval_resumed',
                    'blocked', 'failed', 'escalated', 'resolved', 'closed',
                    'precheck_failed', 'dry_run_failed', 'sandbox_validated',
                    'rolled_back', 'rollback_failed'
                )
          )
          AND NOT EXISTS (
              SELECT 1 FROM aiops_sessions AS session
              WHERE session.incident_id = target.incident_id
                AND session.status IN (
                    'running', 'planning', 'executing', 'resume_running',
                    'waiting_approval', 'approval_approved'
                )
          )
          AND NOT EXISTS (
              SELECT 1 FROM approval_requests AS approval
              WHERE approval.incident_id = target.incident_id
                AND approval.status = 'pending'
          )
          AND NOT EXISTS (
              SELECT 1 FROM alert_events AS alert
              WHERE alert.incident_id = target.incident_id
                AND alert.status != 'resolved'
          )
          AND NOT EXISTS (
              SELECT 1 FROM change_executions AS execution
              WHERE execution.incident_id = target.incident_id
                AND execution.status NOT IN (
                    'precheck_failed', 'dry_run_failed', 'sandbox_validated',
                    'rolled_back', 'rollback_failed',
                    'closed', 'escalated'
                )
          )
        """,
    ),
    "approval_requests": (
        """
        SELECT COUNT(*) AS count FROM approval_requests AS target
        WHERE target.updated_at < %s AND target.status != 'pending'
          AND NOT EXISTS (
              SELECT 1 FROM change_executions AS execution
              WHERE execution.approval_id = target.approval_id
                AND execution.status NOT IN (
                    'precheck_failed', 'dry_run_failed', 'sandbox_validated',
                    'rolled_back', 'rollback_failed',
                    'closed', 'escalated'
                )
          )
        """,
        """
        DELETE target FROM approval_requests AS target
        WHERE target.updated_at < %s AND target.status != 'pending'
          AND NOT EXISTS (
              SELECT 1 FROM change_executions AS execution
              WHERE execution.approval_id = target.approval_id
                AND execution.status NOT IN (
                    'precheck_failed', 'dry_run_failed', 'sandbox_validated',
                    'rolled_back', 'rollback_failed',
                    'closed', 'escalated'
                )
          )
        """,
    ),
    "diagnosis_reports": (
        """
        SELECT COUNT(*) AS count FROM diagnosis_reports AS target
        WHERE target.updated_at < %s
          AND NOT EXISTS (
              SELECT 1 FROM incident_states AS state
              WHERE state.incident_id = target.incident_id
                AND state.status NOT IN (
                    'completed', 'incomplete', 'degraded', 'needs_human',
                    'approval_rejected', 'approval_cancelled', 'approval_resumed',
                    'blocked', 'failed', 'escalated', 'resolved', 'closed',
                    'precheck_failed', 'dry_run_failed', 'sandbox_validated',
                    'rolled_back', 'rollback_failed'
                )
          )
          AND NOT EXISTS (
              SELECT 1 FROM aiops_sessions AS session
              WHERE session.incident_id = target.incident_id
                AND session.status IN (
                    'running', 'planning', 'executing', 'resume_running',
                    'waiting_approval', 'approval_approved'
                )
          )
          AND NOT EXISTS (
              SELECT 1 FROM approval_requests AS approval
              WHERE approval.incident_id = target.incident_id
                AND approval.status = 'pending'
          )
          AND NOT EXISTS (
              SELECT 1 FROM alert_events AS alert
              WHERE alert.incident_id = target.incident_id
                AND alert.status != 'resolved'
          )
          AND NOT EXISTS (
              SELECT 1 FROM change_executions AS execution
              WHERE execution.incident_id = target.incident_id
                AND execution.status NOT IN (
                    'precheck_failed', 'dry_run_failed', 'sandbox_validated',
                    'rolled_back', 'rollback_failed',
                    'closed', 'escalated'
                )
          )
        """,
        """
        DELETE target FROM diagnosis_reports AS target
        WHERE target.updated_at < %s
          AND NOT EXISTS (
              SELECT 1 FROM incident_states AS state
              WHERE state.incident_id = target.incident_id
                AND state.status NOT IN (
                    'completed', 'incomplete', 'degraded', 'needs_human',
                    'approval_rejected', 'approval_cancelled', 'approval_resumed',
                    'blocked', 'failed', 'escalated', 'resolved', 'closed',
                    'precheck_failed', 'dry_run_failed', 'sandbox_validated',
                    'rolled_back', 'rollback_failed'
                )
          )
          AND NOT EXISTS (
              SELECT 1 FROM aiops_sessions AS session
              WHERE session.incident_id = target.incident_id
                AND session.status IN (
                    'running', 'planning', 'executing', 'resume_running',
                    'waiting_approval', 'approval_approved'
                )
          )
          AND NOT EXISTS (
              SELECT 1 FROM approval_requests AS approval
              WHERE approval.incident_id = target.incident_id
                AND approval.status = 'pending'
          )
          AND NOT EXISTS (
              SELECT 1 FROM alert_events AS alert
              WHERE alert.incident_id = target.incident_id
                AND alert.status != 'resolved'
          )
          AND NOT EXISTS (
              SELECT 1 FROM change_executions AS execution
              WHERE execution.incident_id = target.incident_id
                AND execution.status NOT IN (
                    'precheck_failed', 'dry_run_failed', 'sandbox_validated',
                    'rolled_back', 'rollback_failed',
                    'closed', 'escalated'
                )
          )
        """,
    ),
    "change_executions": (
        """
        SELECT COUNT(*) AS count FROM change_executions
        WHERE updated_at < %s
          AND status IN (
              'precheck_failed', 'dry_run_failed', 'sandbox_validated',
              'rolled_back', 'rollback_failed',
              'closed', 'escalated'
          )
        """,
        """
        DELETE FROM change_executions
        WHERE updated_at < %s
          AND status IN (
              'precheck_failed', 'dry_run_failed', 'sandbox_validated',
              'rolled_back', 'rollback_failed',
              'closed', 'escalated'
          )
        """,
    ),
    "aiops_sessions": (
        """
        SELECT COUNT(*) AS count FROM aiops_sessions
        WHERE updated_at < %s
          AND status NOT IN (
              'running', 'planning', 'executing', 'resume_running',
              'waiting_approval', 'approval_approved'
          )
          AND NOT EXISTS (
              SELECT 1 FROM change_executions AS execution
              WHERE execution.incident_id = aiops_sessions.incident_id
                AND execution.status NOT IN (
                    'precheck_failed', 'dry_run_failed', 'sandbox_validated',
                    'rolled_back', 'rollback_failed',
                    'closed', 'escalated'
                )
          )
        """,
        """
        DELETE FROM aiops_sessions
        WHERE updated_at < %s
          AND status NOT IN (
              'running', 'planning', 'executing', 'resume_running',
              'waiting_approval', 'approval_approved'
          )
          AND NOT EXISTS (
              SELECT 1 FROM change_executions AS execution
              WHERE execution.incident_id = aiops_sessions.incident_id
                AND execution.status NOT IN (
                    'precheck_failed', 'dry_run_failed', 'sandbox_validated',
                    'rolled_back', 'rollback_failed',
                    'closed', 'escalated'
                )
          )
        """,
    ),
    "incident_states": (
        """
        SELECT COUNT(*) AS count FROM incident_states
        WHERE updated_at < %s
          AND status IN (
              'completed', 'incomplete', 'degraded', 'needs_human',
              'approval_rejected', 'approval_cancelled', 'approval_resumed',
              'blocked', 'failed', 'escalated', 'resolved', 'closed',
              'precheck_failed', 'dry_run_failed', 'sandbox_validated',
              'rolled_back', 'rollback_failed'
          )
          AND NOT EXISTS (
              SELECT 1 FROM change_executions AS execution
              WHERE execution.incident_id = incident_states.incident_id
                AND execution.status NOT IN (
                    'precheck_failed', 'dry_run_failed', 'sandbox_validated',
                    'rolled_back', 'rollback_failed',
                    'closed', 'escalated'
                )
          )
        """,
        """
        DELETE FROM incident_states
        WHERE updated_at < %s
          AND status IN (
              'completed', 'incomplete', 'degraded', 'needs_human',
              'approval_rejected', 'approval_cancelled', 'approval_resumed',
              'blocked', 'failed', 'escalated', 'resolved', 'closed',
              'precheck_failed', 'dry_run_failed', 'sandbox_validated',
              'rolled_back', 'rollback_failed'
          )
          AND NOT EXISTS (
              SELECT 1 FROM change_executions AS execution
              WHERE execution.incident_id = incident_states.incident_id
                AND execution.status NOT IN (
                    'precheck_failed', 'dry_run_failed', 'sandbox_validated',
                    'rolled_back', 'rollback_failed',
                    'closed', 'escalated'
                )
          )
        """,
    ),
}
_RUNTIME_RESET_SQL = (
    (
        "change_executions",
        "SELECT COUNT(*) AS count FROM change_executions",
        "DELETE FROM change_executions",
    ),
    (
        "approval_requests",
        "SELECT COUNT(*) AS count FROM approval_requests",
        "DELETE FROM approval_requests",
    ),
    (
        "diagnosis_reports",
        "SELECT COUNT(*) AS count FROM diagnosis_reports",
        "DELETE FROM diagnosis_reports",
    ),
    ("trace_events", "SELECT COUNT(*) AS count FROM trace_events", "DELETE FROM trace_events"),
    (
        "aiops_sessions",
        "SELECT COUNT(*) AS count FROM aiops_sessions",
        "DELETE FROM aiops_sessions",
    ),
    ("a2a_tasks", "SELECT COUNT(*) AS count FROM a2a_tasks", "DELETE FROM a2a_tasks"),
    (
        "incident_states",
        "SELECT COUNT(*) AS count FROM incident_states",
        "DELETE FROM incident_states",
    ),
    ("alert_events", "SELECT COUNT(*) AS count FROM alert_events", "DELETE FROM alert_events"),
)
_CHANGE_EXECUTION_TERMINAL_STATUSES = (
    "precheck_failed",
    "dry_run_failed",
    "sandbox_validated",
    "rolled_back",
    "rollback_failed",
    "closed",
    "escalated",
)


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
        with self._connect() as connection:
            with connection.cursor() as cursor:
                self._save_alert_event(cursor, event)

    def persist_alert_ingestion(
        self,
        event: AlertEvent,
        incident: Incident,
    ) -> tuple[AlertEvent, IncidentState, bool, str | None, bool, bool]:
        """Atomically upsert one alert and its IncidentState projection."""
        with self._connect() as connection:
            connection.begin()
            try:
                with connection.cursor() as cursor:
                    inserted = self._insert_alert_event_if_absent(cursor, event)
                    cursor.execute(
                        "SELECT payload FROM alert_events WHERE fingerprint = %s FOR UPDATE",
                        (event.fingerprint,),
                    )
                    alert_row = cursor.fetchone()
                    if alert_row is None:
                        raise RuntimeError("alert row missing after idempotent insert")
                    existing_alert = AlertEvent.model_validate(_load_payload(alert_row))
                    created = inserted
                    previous_status = None if created else existing_alert.status
                    stale_ignored = bool(
                        not created and is_stale_alert_event(existing_alert, event)
                    )
                    reopened = bool(
                        not created
                        and not stale_ignored
                        and is_new_alert_generation(existing_alert, event)
                    )

                    cursor.execute(
                        "SELECT payload FROM incident_states WHERE incident_id = %s FOR UPDATE",
                        (incident.incident_id,),
                    )
                    state_row = cursor.fetchone()
                    existing_state = (
                        IncidentState.model_validate(_load_payload(state_row))
                        if state_row is not None
                        else None
                    )

                    stored_event = (
                        existing_alert if stale_ignored and existing_alert is not None else event
                    )
                    if not created:
                        stored_event.created_at = existing_alert.created_at
                    if not stale_ignored:
                        stored_event.updated_at = datetime.now(UTC)

                    incident_state = build_incident_state_from_alert(
                        event=stored_event,
                        incident=incident,
                        existing=existing_state,
                    )
                    if existing_state is not None:
                        incident_state = merge_incident_state(existing_state, incident_state)

                    if stale_ignored and existing_state is not None:
                        connection.commit()
                        return (
                            stored_event,
                            existing_state,
                            created,
                            previous_status,
                            stale_ignored,
                            reopened,
                        )
                    if not stale_ignored:
                        self._save_alert_event(cursor, stored_event)
                    self._save_incident_state(cursor, incident_state)
                connection.commit()
                return (
                    stored_event,
                    incident_state,
                    created,
                    previous_status,
                    stale_ignored,
                    reopened,
                )
            except Exception:
                connection.rollback()
                raise

    def claim_alert_auto_diagnosis(self, incident_id: str) -> str | None:
        """Claim one alert diagnosis across all MySQL workers."""
        with self._connect() as connection:
            connection.begin()
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT payload FROM incident_states WHERE incident_id = %s FOR UPDATE",
                        (incident_id,),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        connection.rollback()
                        return None
                    state = IncidentState.model_validate(_load_payload(row))
                    metadata = dict(state.metadata or {})
                    now = datetime.now(UTC)
                    if alert_auto_diagnosis_claim_is_active(
                        metadata,
                        now=now,
                        lease_seconds=config.alert_auto_diagnosis_timeout_seconds,
                    ):
                        connection.rollback()
                        return None
                    claim_token = uuid4().hex
                    metadata.update(
                        {
                            "alert_auto_diagnosis_status": "running",
                            "alert_auto_diagnosis_error": "",
                            "alert_auto_diagnosis_claimed_at": now.isoformat(),
                            "alert_auto_diagnosis_claim_token": claim_token,
                        }
                    )
                    self._save_incident_state(
                        cursor,
                        state.model_copy(update={"metadata": metadata}),
                    )
                connection.commit()
                return claim_token
            except Exception:
                connection.rollback()
                raise

    def release_alert_auto_diagnosis(self, incident_id: str, claim_token: str) -> None:
        """Release a process-wide alert diagnosis claim after the task exits."""
        with self._connect() as connection:
            connection.begin()
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT payload FROM incident_states WHERE incident_id = %s FOR UPDATE",
                        (incident_id,),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        connection.rollback()
                        return
                    state = IncidentState.model_validate(_load_payload(row))
                    metadata = dict(state.metadata or {})
                    if (
                        metadata.get("alert_auto_diagnosis_status") != "running"
                        or metadata.get("alert_auto_diagnosis_claim_token") != claim_token
                    ):
                        connection.rollback()
                        return
                    metadata["alert_auto_diagnosis_status"] = "idle"
                    metadata["alert_auto_diagnosis_claimed_at"] = ""
                    metadata["alert_auto_diagnosis_claim_token"] = ""
                    self._save_incident_state(
                        cursor,
                        state.model_copy(update={"metadata": metadata}),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

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
        """Persist an immutable trace event idempotently."""
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
                    ON DUPLICATE KEY UPDATE event_id = VALUES(event_id)
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
                if cursor.rowcount == 0:
                    cursor.execute(
                        "SELECT payload FROM trace_events WHERE event_id = %s",
                        (event.event_id,),
                    )
                    row = cursor.fetchone()
                    if row is None or _load_payload(row) != event.model_dump(mode="json"):
                        raise ValueError(
                            f"Trace event {event.event_id} already exists with different payload"
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
        updated_at = (request.decided_at or request.created_at).isoformat()
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO approval_requests (
                        approval_id, incident_id, status, risk_level, action,
                        created_at, updated_at, decided_at, payload
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        incident_id = IF(status = VALUES(status), VALUES(incident_id), incident_id),
                        risk_level = IF(status = VALUES(status), VALUES(risk_level), risk_level),
                        action = IF(status = VALUES(status), VALUES(action), action),
                        created_at = IF(status = VALUES(status), VALUES(created_at), created_at),
                        updated_at = IF(status = VALUES(status), VALUES(updated_at), updated_at),
                        decided_at = IF(status = VALUES(status), VALUES(decided_at), decided_at),
                        payload = IF(status = VALUES(status), VALUES(payload), payload)
                    """,
                    (
                        request.approval_id,
                        request.incident_id,
                        request.status,
                        request.risk_level,
                        request.action,
                        request.created_at.isoformat(),
                        updated_at,
                        request.decided_at.isoformat() if request.decided_at else None,
                        payload,
                    ),
                )

    def create_approval_request_once(
        self,
        request: ApprovalRequest,
        *,
        idempotency_key: str,
    ) -> tuple[ApprovalRequest, bool]:
        """Create one pending approval for an idempotency key."""
        payload = _dump_model(request)
        with self._connect() as connection:
            connection.begin()
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT IGNORE INTO approval_requests (
                            approval_id, incident_id, status, risk_level, action,
                            idempotency_key, created_at, updated_at, decided_at, payload
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            request.approval_id,
                            request.incident_id,
                            request.status,
                            request.risk_level,
                            request.action,
                            idempotency_key,
                            request.created_at.isoformat(),
                            request.created_at.isoformat(),
                            request.decided_at.isoformat() if request.decided_at else None,
                            payload,
                        ),
                    )
                    created = cursor.rowcount == 1
                    if created:
                        connection.commit()
                        return request, True

                    cursor.execute(
                        """
                        SELECT payload FROM approval_requests
                        WHERE approval_id = %s
                           OR pending_idempotency_key = %s
                        ORDER BY CASE WHEN approval_id = %s THEN 0 ELSE 1 END
                        LIMIT 1
                        FOR UPDATE
                        """,
                        (request.approval_id, idempotency_key, request.approval_id),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise RuntimeError(
                            "approval creation conflicted but existing record was not found"
                        )
                    existing = ApprovalRequest.model_validate(_load_payload(row))
                    if existing.approval_id == request.approval_id and existing != request:
                        raise ValueError(
                            f"Approval {request.approval_id} already exists and cannot be replaced"
                        )
                connection.commit()
                return existing, False
            except Exception:
                connection.rollback()
                raise

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
                        updated_at = %s,
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
                        (request.decided_at or datetime.now(UTC)).isoformat(),
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

    def save_change_execution_if_status(
        self,
        execution: ChangeExecution,
        *,
        expected_statuses: set[str],
    ) -> bool:
        """Update one safe change workflow only from an expected current status."""
        normalized_statuses = sorted(
            {str(status).strip() for status in expected_statuses if str(status).strip()}
        )
        if not normalized_statuses:
            return False
        payload = _dump_model(execution)
        placeholders = bind_markers(len(normalized_statuses), "%s")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    UPDATE change_executions
                    SET
                        change_plan_id = %s,
                        approval_id = %s,
                        incident_id = %s,
                        status = %s,
                        mode = %s,
                        created_at = %s,
                        updated_at = %s,
                        payload = %s
                    WHERE change_execution_id = %s
                      AND status IN ({placeholders})
                    """,  # nosec B608 -- only bind markers from bind_markers are interpolated.
                    (
                        execution.change_plan_id,
                        execution.approval_id,
                        execution.incident_id,
                        execution.status,
                        execution.mode,
                        execution.created_at.isoformat(),
                        execution.updated_at.isoformat(),
                        payload,
                        execution.change_execution_id,
                        *normalized_statuses,
                    ),
                )
                return bool(cursor.rowcount == 1)

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
        snapshot.updated_at = now
        with self._connect() as connection:
            connection.begin()
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT payload FROM aiops_sessions WHERE session_id = %s FOR UPDATE",
                        (snapshot.session_id,),
                    )
                    row = cursor.fetchone()
                    if row is not None:
                        existing = AIOpsSessionSnapshot.model_validate(_load_payload(row))
                        snapshot.created_at = existing.created_at
                    payload = _dump_model(snapshot)
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
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def save_aiops_session_snapshot_with_incident(
        self,
        snapshot: AIOpsSessionSnapshot,
        incident_state: IncidentState,
    ) -> None:
        """Persist a session snapshot and incident projection in one transaction."""
        now = datetime.now(UTC)
        snapshot.updated_at = now
        incident_state.updated_at = now
        with self._connect() as connection:
            connection.begin()
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT payload FROM aiops_sessions WHERE session_id = %s FOR UPDATE",
                        (snapshot.session_id,),
                    )
                    row = cursor.fetchone()
                    if row is not None:
                        existing = AIOpsSessionSnapshot.model_validate(_load_payload(row))
                        snapshot.created_at = existing.created_at
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
                            _dump_model(snapshot),
                        ),
                    )
                    cursor.execute(
                        "SELECT payload FROM incident_states WHERE incident_id = %s FOR UPDATE",
                        (incident_state.incident_id,),
                    )
                    row = cursor.fetchone()
                    if row is not None:
                        incident_state = merge_incident_state(
                            IncidentState.model_validate(_load_payload(row)),
                            incident_state,
                        )
                    self._save_incident_state(cursor, incident_state)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def create_aiops_session_snapshot_with_incident(
        self,
        snapshot: AIOpsSessionSnapshot,
        incident_state: IncidentState,
    ) -> bool:
        """Create a session snapshot and incident projection atomically."""
        now = datetime.now(UTC)
        snapshot.updated_at = now
        incident_state.updated_at = now
        with self._connect() as connection:
            connection.begin()
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT IGNORE INTO aiops_sessions (
                            session_id, incident_id, trace_id, status, node_name,
                            created_at, updated_at, payload
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            snapshot.session_id,
                            snapshot.incident_id,
                            snapshot.trace_id,
                            snapshot.status,
                            snapshot.node_name,
                            snapshot.created_at.isoformat(),
                            snapshot.updated_at.isoformat(),
                            _dump_model(snapshot),
                        ),
                    )
                    if cursor.rowcount != 1:
                        connection.rollback()
                        return False
                    self._save_incident_state(cursor, incident_state)
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise

    def update_aiops_session_snapshot_with_incident_if_status(
        self,
        snapshot: AIOpsSessionSnapshot,
        incident_state: IncidentState,
        *,
        expected_statuses: set[str],
    ) -> bool:
        """Transition a session and incident projection in one transaction."""
        normalized = sorted({str(item).strip() for item in expected_statuses if str(item).strip()})
        if not normalized:
            return False
        now = datetime.now(UTC)
        snapshot.updated_at = now
        incident_state.updated_at = now
        placeholders = bind_markers(len(normalized), "%s")
        with self._connect() as connection:
            connection.begin()
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT payload FROM aiops_sessions WHERE session_id = %s FOR UPDATE",
                        (snapshot.session_id,),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        connection.rollback()
                        return False
                    existing = AIOpsSessionSnapshot.model_validate(_load_payload(row))
                    if existing.status not in normalized:
                        connection.rollback()
                        return False
                    snapshot.created_at = existing.created_at
                    cursor.execute(
                        f"""
                        UPDATE aiops_sessions
                        SET incident_id = %s, trace_id = %s, status = %s, node_name = %s,
                            updated_at = %s, payload = %s
                        WHERE session_id = %s AND status IN ({placeholders})
                        """,  # nosec B608 -- only bind markers from bind_markers are interpolated.
                        (
                            snapshot.incident_id,
                            snapshot.trace_id,
                            snapshot.status,
                            snapshot.node_name,
                            snapshot.updated_at.isoformat(),
                            _dump_model(snapshot),
                            snapshot.session_id,
                            *normalized,
                        ),
                    )
                    if cursor.rowcount != 1:
                        connection.rollback()
                        return False
                    cursor.execute(
                        "SELECT payload FROM incident_states WHERE incident_id = %s FOR UPDATE",
                        (incident_state.incident_id,),
                    )
                    row = cursor.fetchone()
                    if row is not None:
                        incident_state = merge_incident_state(
                            IncidentState.model_validate(_load_payload(row)),
                            incident_state,
                        )
                    self._save_incident_state(cursor, incident_state)
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise

    def create_aiops_session_snapshot(self, snapshot: AIOpsSessionSnapshot) -> bool:
        """Insert the first snapshot for a diagnosis session without overwriting."""
        now = datetime.now(UTC)
        snapshot.updated_at = now
        payload = _dump_model(snapshot)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT IGNORE INTO aiops_sessions (
                        session_id, incident_id, trace_id, status, node_name,
                        created_at, updated_at, payload
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
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
                return int(cursor.rowcount or 0) == 1

    def update_aiops_session_snapshot_if_status(
        self,
        snapshot: AIOpsSessionSnapshot,
        *,
        expected_statuses: set[str],
    ) -> bool:
        """Update one snapshot only when its current status matches."""
        normalized_statuses = sorted(
            {str(status).strip() for status in expected_statuses if str(status).strip()}
        )
        if not normalized_statuses:
            return False

        now = datetime.now(UTC)
        snapshot.updated_at = now
        placeholders = bind_markers(len(normalized_statuses), "%s")
        with self._connect() as connection:
            connection.begin()
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT payload FROM aiops_sessions WHERE session_id = %s FOR UPDATE",
                        (snapshot.session_id,),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        connection.rollback()
                        return False
                    existing = AIOpsSessionSnapshot.model_validate(_load_payload(row))
                    if existing.status not in normalized_statuses:
                        connection.rollback()
                        return False

                    snapshot.created_at = existing.created_at
                    payload = _dump_model(snapshot)
                    cursor.execute(
                        f"""
                        UPDATE aiops_sessions
                        SET
                            incident_id = %s,
                            trace_id = %s,
                            status = %s,
                            node_name = %s,
                            updated_at = %s,
                            payload = %s
                        WHERE session_id = %s
                          AND status IN ({placeholders})
                        """,  # nosec B608 -- only bind markers from bind_markers are interpolated.
                        (
                            snapshot.incident_id,
                            snapshot.trace_id,
                            snapshot.status,
                            snapshot.node_name,
                            snapshot.updated_at.isoformat(),
                            payload,
                            snapshot.session_id,
                            *normalized_statuses,
                        ),
                    )
                    updated = bool(cursor.rowcount == 1)
                connection.commit()
                return updated
            except Exception:
                connection.rollback()
                raise

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
        offset: int = 0,
    ) -> list[AIOpsSessionSnapshot]:
        """List durable diagnosis session snapshots by recent update time."""
        normalized_limit = max(1, min(int(limit or 20), 100))
        normalized_offset = max(int(offset or 0), 0)
        clauses = []
        params: list[object] = []
        if incident_id:
            clauses.append("incident_id = %s")
            params.append(incident_id)

        query = "SELECT payload FROM aiops_sessions"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC, id DESC LIMIT %s OFFSET %s"
        params.extend([normalized_limit, normalized_offset])

        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, params)
                rows = cursor.fetchall()
        return [AIOpsSessionSnapshot.model_validate(_load_payload(row)) for row in rows]

    def create_a2a_task_record(self, record: A2ATaskRecord) -> bool:
        """Insert an A2A task ownership record without overwriting."""
        now = datetime.now(UTC)
        record.updated_at = now
        payload = _dump_model(record)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT IGNORE INTO a2a_tasks (
                        task_id, message_id, request_fingerprint, skill, incident_id,
                        state, created_at, updated_at, payload
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        record.task_id,
                        record.message_id,
                        record.request_fingerprint,
                        record.skill,
                        record.incident_id,
                        record.state,
                        record.created_at.isoformat(),
                        record.updated_at.isoformat(),
                        payload,
                    ),
                )
                return int(cursor.rowcount or 0) == 1

    def save_a2a_task_record(self, record: A2ATaskRecord) -> None:
        """Update one durable A2A task record."""
        now = datetime.now(UTC)
        record.updated_at = now
        with self._connect() as connection:
            connection.begin()
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT payload FROM a2a_tasks WHERE task_id = %s FOR UPDATE",
                        (record.task_id,),
                    )
                    row = cursor.fetchone()
                    if row is not None:
                        existing = A2ATaskRecord.model_validate(_load_payload(row))
                        if (
                            existing.message_id != record.message_id
                            or existing.request_fingerprint != record.request_fingerprint
                        ):
                            raise ValueError("A2A task ownership mismatch")
                        record.created_at = existing.created_at
                    cursor.execute(
                        """
                        INSERT INTO a2a_tasks (
                            task_id, message_id, request_fingerprint, skill, incident_id,
                            state, created_at, updated_at, payload
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            message_id = VALUES(message_id),
                            request_fingerprint = VALUES(request_fingerprint),
                            skill = VALUES(skill),
                            incident_id = VALUES(incident_id),
                            state = VALUES(state),
                            updated_at = VALUES(updated_at),
                            payload = VALUES(payload)
                        """,
                        (
                            record.task_id,
                            record.message_id,
                            record.request_fingerprint,
                            record.skill,
                            record.incident_id,
                            record.state,
                            record.created_at.isoformat(),
                            record.updated_at.isoformat(),
                            _dump_model(record),
                        ),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def get_a2a_task_record(self, task_id: str) -> A2ATaskRecord | None:
        """Return one durable A2A task record."""
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM a2a_tasks WHERE task_id = %s",
                    (task_id,),
                )
                row = cursor.fetchone()
        return A2ATaskRecord.model_validate(_load_payload(row)) if row is not None else None

    def list_a2a_task_records(
        self,
        *,
        incident_id: str | None = None,
        limit: int = 20,
        owner_id: str = "",
    ) -> list[A2ATaskRecord]:
        """Return recent durable A2A task records."""
        query = "SELECT payload FROM a2a_tasks"
        params: list[Any] = []
        clauses: list[str] = []
        if incident_id is not None:
            clauses.append("incident_id = %s")
            params.append(incident_id)
        if owner_id:
            clauses.append("owner_id = %s")
            params.append(owner_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC, id DESC LIMIT %s"
        params.append(limit)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, params)
                rows = cursor.fetchall()
        return [A2ATaskRecord.model_validate(_load_payload(row)) for row in rows]

    def save_incident_state(self, state: IncidentState) -> None:
        """Persist the latest lifecycle state for one incident."""
        with self._connect() as connection:
            connection.begin()
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT payload FROM incident_states WHERE incident_id = %s FOR UPDATE",
                        (state.incident_id,),
                    )
                    row = cursor.fetchone()
                    if row is not None:
                        state = merge_incident_state(
                            IncidentState.model_validate(_load_payload(row)),
                            state,
                        )
                    self._save_incident_state(cursor, state)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

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

    @staticmethod
    def _insert_alert_event_if_absent(cursor: Any, event: AlertEvent) -> bool:
        payload = _dump_model(event)
        cursor.execute(
            """
            INSERT INTO alert_events (
                fingerprint, incident_id, source, status, service_name,
                severity, environment, starts_at, updated_at, payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE fingerprint = VALUES(fingerprint)
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
        return int(cursor.rowcount or 0) == 1

    @staticmethod
    def _save_alert_event(cursor: Any, event: AlertEvent) -> None:
        payload = _dump_model(event)
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

    @staticmethod
    def _save_incident_state(cursor: Any, state: IncidentState) -> None:
        state.updated_at = datetime.now(UTC)
        payload = _dump_model(state)
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

    def save_report(self, report: DiagnosisReport) -> None:
        """Persist a diagnosis report."""
        payload = _dump_model(report)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO diagnosis_reports (
                        report_id, incident_id, trace_id, created_at, updated_at, payload
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        incident_id = VALUES(incident_id),
                        trace_id = VALUES(trace_id),
                        created_at = VALUES(created_at),
                        updated_at = VALUES(updated_at),
                        payload = VALUES(payload)
                    """,
                    (
                        report.report_id,
                        report.incident_id,
                        report.trace_id,
                        report.created_at.isoformat(),
                        datetime.now(UTC).isoformat(),
                        payload,
                    ),
                )

    def save_report_with_incident(
        self,
        report: DiagnosisReport,
        incident_state: IncidentState,
    ) -> None:
        """Persist a report and its IncidentState projection atomically."""
        now = datetime.now(UTC)
        incident_state.updated_at = now
        with self._connect() as connection:
            connection.begin()
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO diagnosis_reports (
                            report_id, incident_id, trace_id, created_at, updated_at, payload
                        )
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            incident_id = VALUES(incident_id),
                            trace_id = VALUES(trace_id),
                            created_at = VALUES(created_at),
                            updated_at = VALUES(updated_at),
                            payload = VALUES(payload)
                        """,
                        (
                            report.report_id,
                            report.incident_id,
                            report.trace_id,
                            report.created_at.isoformat(),
                            now.isoformat(),
                            _dump_model(report),
                        ),
                    )
                    cursor.execute(
                        "SELECT payload FROM incident_states WHERE incident_id = %s FOR UPDATE",
                        (incident_state.incident_id,),
                    )
                    row = cursor.fetchone()
                    if row is not None:
                        incident_state = merge_incident_state(
                            IncidentState.model_validate(_load_payload(row)),
                            incident_state,
                        )
                    self._save_incident_state(cursor, incident_state)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def import_runtime_state(
        self,
        *,
        alert_events: list[AlertEvent],
        trace_events: list[TraceEvent],
        approval_requests: Sequence[ApprovalRequest | tuple[ApprovalRequest, str | None]],
        change_executions: list[ChangeExecution],
        aiops_sessions: list[AIOpsSessionSnapshot],
        incident_states: list[IncidentState],
        diagnosis_reports: list[DiagnosisReport],
    ) -> dict[str, Any]:
        """Atomically import SQLite runtime rows without overwriting MySQL state."""
        imported = {
            "alert_events": 0,
            "trace_events": 0,
            "approval_requests": 0,
            "change_executions": 0,
            "aiops_sessions": 0,
            "incident_states": 0,
            "diagnosis_reports": 0,
        }
        with self._connect() as connection:
            connection.begin()
            try:
                with connection.cursor() as cursor:
                    conflicts = self._find_runtime_import_conflicts(
                        cursor,
                        alert_events=alert_events,
                        trace_events=trace_events,
                        approval_requests=approval_requests,
                        change_executions=change_executions,
                        aiops_sessions=aiops_sessions,
                        incident_states=incident_states,
                        diagnosis_reports=diagnosis_reports,
                    )
                    if any(conflicts.values()):
                        connection.rollback()
                        return {**imported, "conflicts": conflicts}
                    for alert_event in alert_events:
                        imported["alert_events"] += self._insert_alert_event_for_import(
                            cursor, alert_event
                        )
                    for trace_event in trace_events:
                        imported["trace_events"] += self._insert_trace_event_for_import(
                            cursor, trace_event
                        )
                    for approval_record in approval_requests:
                        imported["approval_requests"] += self._insert_approval_request_for_import(
                            cursor, approval_record
                        )
                    for execution in change_executions:
                        imported["change_executions"] += self._insert_change_execution_for_import(
                            cursor, execution
                        )
                    for snapshot in aiops_sessions:
                        imported["aiops_sessions"] += self._insert_aiops_session_for_import(
                            cursor, snapshot
                        )
                    for state in incident_states:
                        imported["incident_states"] += self._insert_incident_state_for_import(
                            cursor, state
                        )
                    for report in diagnosis_reports:
                        imported["diagnosis_reports"] += self._insert_report_for_import(
                            cursor, report
                        )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {**imported, "conflicts": dict.fromkeys(imported, 0)}

    def _find_runtime_import_conflicts(
        self,
        cursor: Any,
        *,
        alert_events: list[AlertEvent],
        trace_events: list[TraceEvent],
        approval_requests: Sequence[ApprovalRequest | tuple[ApprovalRequest, str | None]],
        change_executions: list[ChangeExecution],
        aiops_sessions: list[AIOpsSessionSnapshot],
        incident_states: list[IncidentState],
        diagnosis_reports: list[DiagnosisReport],
    ) -> dict[str, int]:
        """Count target rows that share an identity but contain different state."""
        conflicts = {
            "alert_events": 0,
            "trace_events": 0,
            "approval_requests": 0,
            "change_executions": 0,
            "aiops_sessions": 0,
            "incident_states": 0,
            "diagnosis_reports": 0,
        }
        for table, key_column, records in (
            ("alert_events", "fingerprint", alert_events),
            ("trace_events", "event_id", trace_events),
            ("aiops_sessions", "session_id", aiops_sessions),
            ("incident_states", "incident_id", incident_states),
            ("diagnosis_reports", "report_id", diagnosis_reports),
        ):
            table = trusted_identifier(table, allowed=conflicts)
            key_column = trusted_identifier(
                key_column,
                allowed={
                    "fingerprint",
                    "event_id",
                    "session_id",
                    "incident_id",
                    "report_id",
                },
            )
            for record in records:
                key = getattr(record, key_column)
                cursor.execute(
                    f"SELECT payload FROM {table} WHERE {key_column} = %s",  # nosec B608
                    (key,),
                )
                row = cursor.fetchone()
                if row is not None and _load_payload(row) != record.model_dump(mode="json"):
                    conflicts[table] += 1

        for approval_record in approval_requests:
            request, idempotency_key = _approval_import_record(approval_record)
            cursor.execute(
                """
                SELECT payload, idempotency_key
                FROM approval_requests
                WHERE approval_id = %s
                   OR (
                       %s IS NOT NULL
                       AND pending_idempotency_key = %s
                   )
                """,
                (request.approval_id, idempotency_key, idempotency_key),
            )
            rows = cursor.fetchall()
            if any(
                _load_payload(row) != request.model_dump(mode="json")
                or str(row.get("idempotency_key") or "") != str(idempotency_key or "")
                for row in rows
            ):
                conflicts["approval_requests"] += 1

        for execution in change_executions:
            cursor.execute(
                """
                SELECT payload FROM change_executions
                WHERE change_execution_id = %s
                   OR (
                       incident_id = %s
                       AND change_plan_id = %s
                       AND approval_id = %s
                   )
                """,
                (
                    execution.change_execution_id,
                    execution.incident_id,
                    execution.change_plan_id,
                    execution.approval_id,
                ),
            )
            rows = cursor.fetchall()
            if any(_load_payload(row) != execution.model_dump(mode="json") for row in rows):
                conflicts["change_executions"] += 1
        return conflicts

    @staticmethod
    def _insert_alert_event_for_import(cursor: Any, event: AlertEvent) -> int:
        cursor.execute(
            """
            INSERT INTO alert_events (
                fingerprint, incident_id, source, status, service_name,
                severity, environment, starts_at, updated_at, payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE fingerprint = VALUES(fingerprint)
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
                _dump_model(event),
            ),
        )
        return int(cursor.rowcount or 0)

    @staticmethod
    def _insert_trace_event_for_import(cursor: Any, event: TraceEvent) -> int:
        cursor.execute(
            """
            INSERT INTO trace_events (
                event_id, trace_id, incident_id, event_type, node_name,
                step_id, tool_name, status, created_at, payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE event_id = VALUES(event_id)
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
                _dump_model(event),
            ),
        )
        return int(cursor.rowcount or 0)

    @staticmethod
    def _insert_approval_request_for_import(
        cursor: Any,
        approval_record: ApprovalRequest | tuple[ApprovalRequest, str | None],
    ) -> int:
        request, idempotency_key = _approval_import_record(approval_record)
        updated_at = (request.decided_at or request.created_at).isoformat()
        cursor.execute(
            """
            INSERT INTO approval_requests (
                approval_id, incident_id, status, risk_level, action,
                idempotency_key, created_at, updated_at, decided_at, payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE approval_id = VALUES(approval_id)
            """,
            (
                request.approval_id,
                request.incident_id,
                request.status,
                request.risk_level,
                request.action,
                idempotency_key,
                request.created_at.isoformat(),
                updated_at,
                request.decided_at.isoformat() if request.decided_at else None,
                _dump_model(request),
            ),
        )
        return int(cursor.rowcount or 0)

    @staticmethod
    def _insert_change_execution_for_import(
        cursor: Any,
        execution: ChangeExecution,
    ) -> int:
        cursor.execute(
            """
            INSERT INTO change_executions (
                change_execution_id, change_plan_id, approval_id, incident_id,
                status, mode, created_at, updated_at, payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE change_execution_id = change_execution_id
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
                _dump_model(execution),
            ),
        )
        return int(cursor.rowcount or 0)

    @staticmethod
    def _insert_aiops_session_for_import(
        cursor: Any,
        snapshot: AIOpsSessionSnapshot,
    ) -> int:
        cursor.execute(
            """
            INSERT INTO aiops_sessions (
                session_id, incident_id, trace_id, status, node_name,
                created_at, updated_at, payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE session_id = VALUES(session_id)
            """,
            (
                snapshot.session_id,
                snapshot.incident_id,
                snapshot.trace_id,
                snapshot.status,
                snapshot.node_name,
                snapshot.created_at.isoformat(),
                snapshot.updated_at.isoformat(),
                _dump_model(snapshot),
            ),
        )
        return int(cursor.rowcount or 0)

    @staticmethod
    def _insert_incident_state_for_import(
        cursor: Any,
        state: IncidentState,
    ) -> int:
        cursor.execute(
            """
            INSERT INTO incident_states (
                incident_id, status, service_name, severity, environment,
                trace_id, session_id, approval_status, created_at, updated_at, payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE incident_id = VALUES(incident_id)
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
                _dump_model(state),
            ),
        )
        return int(cursor.rowcount or 0)

    @staticmethod
    def _insert_report_for_import(cursor: Any, report: DiagnosisReport) -> int:
        cursor.execute(
            """
            INSERT INTO diagnosis_reports (
                report_id, incident_id, trace_id, created_at, updated_at, payload
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE report_id = VALUES(report_id)
            """,
            (
                report.report_id,
                report.incident_id,
                report.trace_id,
                report.created_at.isoformat(),
                report.created_at.isoformat(),
                _dump_model(report),
            ),
        )
        return int(cursor.rowcount or 0)

    def get_report(self, report_id: str) -> DiagnosisReport | None:
        """Return one report by its stable identifier."""
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM diagnosis_reports WHERE report_id = %s",
                    (report_id,),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        return DiagnosisReport.model_validate(_load_payload(row))

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
                    SELECT current.payload
                    FROM diagnosis_reports AS current
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM diagnosis_reports AS newer
                        WHERE newer.incident_id = current.incident_id
                          AND (
                              newer.created_at > current.created_at
                              OR (
                                  newer.created_at = current.created_at
                                  AND newer.id > current.id
                              )
                          )
                    )
                    ORDER BY current.created_at DESC, current.id DESC
                    """)
                rows = cursor.fetchall()
        return [DiagnosisReport.model_validate(_load_payload(row)) for row in rows]

    def reset_runtime_data(self) -> dict[str, int]:
        """Delete all AIOps runtime records while preserving the database schema."""
        deleted: dict[str, int] = {}
        with self._connect() as connection:
            connection.begin()
            try:
                with connection.cursor() as cursor:
                    for table, count_sql, delete_sql in _RUNTIME_RESET_SQL:
                        cursor.execute(count_sql)
                        deleted[table] = int(cursor.fetchone()["count"])
                        cursor.execute(delete_sql)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return deleted

    def cleanup_older_than(self, *, keep_days: int, dry_run: bool = False) -> dict[str, Any]:
        """Delete runtime records older than the retention window."""
        if keep_days < 1:
            raise ValueError("keep_days must be >= 1")

        cutoff = datetime.now(UTC) - timedelta(days=keep_days)
        cutoff_text = cutoff.isoformat()
        deleted: dict[str, int] = {}

        with self._connect() as connection:
            try:
                if not dry_run:
                    connection.begin()
                with connection.cursor() as cursor:
                    eligible_incidents = self._select_retention_eligible_incidents(
                        cursor,
                        cutoff_text=cutoff_text,
                    )
                    if dry_run:
                        deleted = self._count_retention_incidents(cursor, eligible_incidents)
                    else:
                        deleted = self._delete_retention_incidents(cursor, eligible_incidents)
                if not dry_run:
                    connection.commit()
            except Exception:
                if not dry_run:
                    connection.rollback()
                raise

        return {
            "backend": "mysql",
            "database": self.storage_path,
            "keep_days": keep_days,
            "cutoff": cutoff_text,
            "dry_run": dry_run,
            "deleted": deleted,
        }

    @staticmethod
    def _select_retention_eligible_incidents(
        cursor: Any,
        *,
        cutoff_text: str,
    ) -> list[str]:
        terminal_placeholders = bind_markers(
            len(_CHANGE_EXECUTION_TERMINAL_STATUSES),
            "%s",
        )
        cursor.execute(
            f"""
            SELECT incident_id
            FROM (
                SELECT incident_id FROM alert_events
                UNION SELECT incident_id FROM trace_events
                UNION SELECT incident_id FROM approval_requests
                UNION SELECT incident_id FROM change_executions
                UNION SELECT incident_id FROM aiops_sessions
                UNION SELECT incident_id FROM incident_states
                UNION SELECT incident_id FROM diagnosis_reports
            ) AS incidents
            WHERE NOT EXISTS (
                SELECT 1 FROM alert_events AS alert
                WHERE alert.incident_id = incidents.incident_id
                  AND (alert.status != 'resolved' OR alert.updated_at >= %s)
            )
              AND NOT EXISTS (
                SELECT 1 FROM trace_events AS trace
                WHERE trace.incident_id = incidents.incident_id
                  AND trace.created_at >= %s
            )
              AND NOT EXISTS (
                SELECT 1 FROM approval_requests AS approval
                WHERE approval.incident_id = incidents.incident_id
                  AND (approval.status = 'pending' OR approval.updated_at >= %s)
            )
              AND NOT EXISTS (
                SELECT 1 FROM change_executions AS execution
                WHERE execution.incident_id = incidents.incident_id
                  AND (
                      execution.status NOT IN ({terminal_placeholders})
                      OR execution.updated_at >= %s
                  )
            )
              AND NOT EXISTS (
                SELECT 1 FROM aiops_sessions AS session
                WHERE session.incident_id = incidents.incident_id
                  AND (
                      session.status IN (
                          'running', 'planning', 'executing', 'resume_running',
                          'waiting_approval', 'approval_approved', 'change_validated',
                          'waiting_manual_execution', 'observing', 'partial_success',
                          'recovery_pending', 'rollback_recommended'
                      )
                      OR session.updated_at >= %s
                  )
            )
              AND NOT EXISTS (
                SELECT 1 FROM incident_states AS state
                WHERE state.incident_id = incidents.incident_id
                  AND (
                      state.status NOT IN (
                          'completed', 'incomplete', 'degraded', 'needs_human',
                          'approval_rejected', 'approval_cancelled', 'approval_resumed',
                          'blocked', 'failed', 'escalated', 'resolved', 'closed',
                          'precheck_failed', 'dry_run_failed', 'sandbox_validated',
                          'rolled_back', 'rollback_failed'
                      )
                      OR state.updated_at >= %s
                  )
            )
              AND NOT EXISTS (
                SELECT 1 FROM diagnosis_reports AS report
                WHERE report.incident_id = incidents.incident_id
                  AND report.updated_at >= %s
            )
            ORDER BY incident_id
            """,  # nosec B608 -- only bind markers from bind_markers are interpolated.
            (
                cutoff_text,
                cutoff_text,
                cutoff_text,
                *_CHANGE_EXECUTION_TERMINAL_STATUSES,
                cutoff_text,
                cutoff_text,
                cutoff_text,
                cutoff_text,
            ),
        )
        return [str(row["incident_id"]) for row in cursor.fetchall()]

    @staticmethod
    def _count_retention_incidents(cursor: Any, incident_ids: list[str]) -> dict[str, int]:
        counts = dict.fromkeys(_RETENTION_SQL, 0)
        if not incident_ids:
            return counts
        for table in _RETENTION_SQL:
            cursor.execute(
                trusted_table_statement(
                    "SELECT_COUNT",
                    table=table,
                    allowed_tables=_RETENTION_SQL,
                    value_count=len(incident_ids),
                    marker="%s",
                ),
                incident_ids,
            )
            counts[table] = int(cursor.fetchone()["count"])
        return counts

    @staticmethod
    def _delete_retention_incidents(cursor: Any, incident_ids: list[str]) -> dict[str, int]:
        deleted = dict.fromkeys(_RETENTION_SQL, 0)
        if not incident_ids:
            return deleted
        deletion_order = (
            "change_executions",
            "approval_requests",
            "diagnosis_reports",
            "trace_events",
            "aiops_sessions",
            "incident_states",
            "alert_events",
        )
        for table in deletion_order:
            cursor.execute(
                trusted_table_statement(
                    "DELETE",
                    table=table,
                    allowed_tables=deletion_order,
                    value_count=len(incident_ids),
                    marker="%s",
                ),
                incident_ids,
            )
            deleted[table] = int(cursor.rowcount or 0)
        return deleted

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
                    """)
                cursor.execute("""
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
                    """)
                self._ensure_approval_idempotency_columns(cursor)
                cursor.execute("""
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
                    """)
                cursor.execute("""
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
                    """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS a2a_tasks (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        task_id VARCHAR(128) NOT NULL UNIQUE,
                        message_id VARCHAR(256) NOT NULL,
                        request_fingerprint VARCHAR(64) NOT NULL,
                        skill VARCHAR(128) NOT NULL,
                        incident_id VARCHAR(128) NOT NULL,
                        state VARCHAR(64) NOT NULL,
                        created_at VARCHAR(64) NOT NULL,
                        updated_at VARCHAR(64) NOT NULL,
                        payload LONGTEXT NOT NULL,
                        INDEX idx_a2a_tasks_message (message_id, updated_at),
                        INDEX idx_a2a_tasks_incident (incident_id, updated_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """)
                cursor.execute("""
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
                    """)
                cursor.execute("""
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
                    """)
                self._ensure_change_execution_scope_unique_index(cursor)
                self._ensure_retention_timestamp_columns(cursor)
                self._ensure_runtime_column_capacities(cursor)
                self._require_transactional_runtime_tables(cursor)

    def _ensure_approval_idempotency_columns(self, cursor: Any) -> None:
        """Add approval idempotency columns and unique key to older MySQL tables."""
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
            row = cursor.fetchone() or {}
            if int(row.get("index_count") or 0) == 0:
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

    def _ensure_retention_timestamp_columns(self, cursor: Any) -> None:
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
        cursor.execute(
            "UPDATE diagnosis_reports SET updated_at = created_at WHERE updated_at IS NULL"
        )
        cursor.execute("ALTER TABLE diagnosis_reports MODIFY updated_at VARCHAR(64) NOT NULL")

    @staticmethod
    def _ensure_runtime_column_capacities(cursor: Any) -> None:
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
            if required not in {64, 80, 1000}:
                raise RuntimeError(f"unsupported runtime column capacity: {required}")
            # Identifiers and capacity are selected from code-owned allowlists above.
            cursor.execute(  # nosec B608
                f"ALTER TABLE {table} MODIFY {column} VARCHAR({required}) NOT NULL"
            )

    @staticmethod
    def _require_transactional_runtime_tables(cursor: Any) -> None:
        """Fail closed when runtime tables cannot participate in transactions."""
        runtime_tables = (
            "alert_events",
            "trace_events",
            "approval_requests",
            "change_executions",
            "aiops_sessions",
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
            """,  # nosec B608 -- only bind markers from bind_markers are interpolated.
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

    def _ensure_change_execution_scope_unique_index(self, cursor: Any) -> None:
        """Require the business idempotency unique key for concurrent creation."""
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


def _approval_import_record(
    value: ApprovalRequest | tuple[ApprovalRequest, str | None],
) -> tuple[ApprovalRequest, str | None]:
    if isinstance(value, tuple):
        return value
    key = str(value.metadata.get("idempotency_key") or "").strip() or None
    return value, key
