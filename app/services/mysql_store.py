"""MySQL persistence for AIOps runtime state."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from threading import Lock
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
from app.services.mysql_runtime_import import import_mysql_runtime_state
from app.services.sql_safety import bind_markers
from app.services.store_maintenance import (
    cleanup_mysql_runtime_data,
    reset_mysql_runtime_data,
)
from app.services.store_schema import (
    ensure_mysql_approval_idempotency_columns as _ensure_mysql_approval_columns,
    ensure_mysql_change_execution_scope_unique_index as _ensure_mysql_change_scope,
    ensure_mysql_runtime_column_capacities as _ensure_mysql_column_capacities,
    initialize_mysql_store,
    require_mysql_transactional_runtime_tables as _require_mysql_transactional_tables,
)

_MYSQL_POOLS: dict[tuple[tuple[str, Any], ...], Any] = {}
_MYSQL_POOLS_LOCK = Lock()

class AIOpsMySQLStore:
    """Small PyMySQL-backed repository for trace, approval, and report state."""

    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or config.resolved_mysql_dsn
        if not self.dsn:
            raise ValueError("AIOPS_STORAGE_BACKEND=mysql requires MYSQL_DSN or MYSQL_HOST")
        self.connection_settings = _parse_mysql_dsn(self.dsn)
        self.storage_path = _redact_mysql_dsn(self.dsn)
        self.migration_warnings: list[str] = []
        initialize_mysql_store(self._connect)

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
            connection.begin()
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT change_execution_id FROM change_executions "
                        "WHERE change_execution_id = %s FOR UPDATE",
                        (execution.change_execution_id,),
                    )
                    existing = cursor.fetchone()
                    if existing is None:
                        cursor.execute(
                            """
                            INSERT INTO change_executions (
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
                    else:
                        cursor.execute(
                            """
                            UPDATE change_executions
                            SET change_plan_id = %s, approval_id = %s, incident_id = %s,
                                status = %s, mode = %s, created_at = %s,
                                updated_at = %s, payload = %s
                            WHERE change_execution_id = %s
                            """,
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
                            ),
                        )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

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
                    task_id, message_id, request_fingerprint, owner_id, skill, incident_id,
                    state, created_at, updated_at, payload
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        record.task_id,
                        record.message_id,
                        record.request_fingerprint,
                        record.owner_id,
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
                            or existing.owner_id != record.owner_id
                        ):
                            raise ValueError("A2A task ownership mismatch")
                        record.created_at = existing.created_at
                    cursor.execute(
                        """
                        INSERT INTO a2a_tasks (
                            task_id, message_id, request_fingerprint, owner_id, skill, incident_id,
                            state, created_at, updated_at, payload
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            message_id = VALUES(message_id),
                            request_fingerprint = VALUES(request_fingerprint),
                            owner_id = VALUES(owner_id),
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
                            record.owner_id,
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
        params.append(max(1, min(int(limit or 20), 100)))
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
        a2a_tasks: list[A2ATaskRecord],
        incident_states: list[IncidentState],
        diagnosis_reports: list[DiagnosisReport],
    ) -> dict[str, Any]:
        """Atomically import SQLite runtime rows without overwriting MySQL state."""
        return import_mysql_runtime_state(
            self._connect,
            alert_events=alert_events,
            trace_events=trace_events,
            approval_requests=approval_requests,
            change_executions=change_executions,
            aiops_sessions=aiops_sessions,
            a2a_tasks=a2a_tasks,
            incident_states=incident_states,
            diagnosis_reports=diagnosis_reports,
        )

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
        return reset_mysql_runtime_data(self._connect)

    def cleanup_older_than(self, *, keep_days: int, dry_run: bool = False) -> dict[str, Any]:
        """Delete runtime records older than the retention window."""
        return cleanup_mysql_runtime_data(
            self._connect,
            storage_path=self.storage_path,
            keep_days=keep_days,
            dry_run=dry_run,
        )

    def _ensure_approval_idempotency_columns(self, cursor: Any) -> None:
        _ensure_mysql_approval_columns(cursor)

    @staticmethod
    def _ensure_runtime_column_capacities(cursor: Any) -> None:
        _ensure_mysql_column_capacities(cursor)

    @staticmethod
    def _require_transactional_runtime_tables(cursor: Any) -> None:
        _require_mysql_transactional_tables(cursor)

    def _ensure_change_execution_scope_unique_index(self, cursor: Any) -> None:
        _ensure_mysql_change_scope(cursor)

    def _apply_mysql_approval_and_change_idempotency(self, cursor: Any) -> None:
        self._ensure_approval_idempotency_columns(cursor)
        self._ensure_change_execution_scope_unique_index(cursor)

    def _record_migration_warning(self, message: str) -> None:
        self.migration_warnings.append(message)
        logger.warning(message)

    def _connect(self):
        try:
            import pymysql
            from dbutils.pooled_db import PooledDB
            from pymysql.cursors import DictCursor
        except ImportError as exc:
            raise RuntimeError(
                "AIOPS_STORAGE_BACKEND=mysql requires pymysql; install project dependencies."
            ) from exc

        pool_key = tuple(sorted(self.connection_settings.items()))
        with _MYSQL_POOLS_LOCK:
            pool = _MYSQL_POOLS.get(pool_key)
            if pool is None:
                pool = PooledDB(
                    creator=pymysql,
                    mincached=1,
                    maxcached=config.mysql_pool_size,
                    maxconnections=config.mysql_pool_size + config.mysql_pool_max_overflow,
                    blocking=True,
                    ping=1,
                    **self.connection_settings,
                    autocommit=True,
                    cursorclass=DictCursor,
                )
                _MYSQL_POOLS[pool_key] = pool
        return pool.connection()


def close_mysql_pools() -> None:
    """Close process-wide MySQL pools during application shutdown."""

    with _MYSQL_POOLS_LOCK:
        pools = list(_MYSQL_POOLS.values())
        _MYSQL_POOLS.clear()
    for pool in pools:
        pool.close()


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
