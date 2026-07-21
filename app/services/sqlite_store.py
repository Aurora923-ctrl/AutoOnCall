"""SQLite persistence for AIOps runtime state."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
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
from app.services.sql_safety import bind_markers
from app.services.store_maintenance import (
    cleanup_sqlite_runtime_data,
    reset_sqlite_runtime_data,
)
from app.services.store_schema import initialize_sqlite_store


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
        initialize_sqlite_store(self.database_path, self._connect)

    def save_alert_event(self, event: AlertEvent) -> None:
        """Persist the latest state of one normalized alert event."""
        with self._connect() as connection:
            self._save_alert_event(connection, event)

    def persist_alert_ingestion(
        self,
        event: AlertEvent,
        incident: Incident,
    ) -> tuple[AlertEvent, IncidentState, bool, str | None, bool, bool]:
        """Atomically upsert one alert and its IncidentState projection."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            alert_row = connection.execute(
                "SELECT payload FROM alert_events WHERE fingerprint = ?",
                (event.fingerprint,),
            ).fetchone()
            existing_alert = (
                AlertEvent.model_validate(_load_payload(alert_row))
                if alert_row is not None
                else None
            )
            created = existing_alert is None
            previous_status = existing_alert.status if existing_alert is not None else None
            stale_ignored = bool(
                existing_alert is not None and is_stale_alert_event(existing_alert, event)
            )
            reopened = bool(
                existing_alert is not None
                and not stale_ignored
                and is_new_alert_generation(existing_alert, event)
            )

            state_row = connection.execute(
                "SELECT payload FROM incident_states WHERE incident_id = ?",
                (incident.incident_id,),
            ).fetchone()
            existing_state = (
                IncidentState.model_validate(_load_payload(state_row))
                if state_row is not None
                else None
            )

            stored_event = existing_alert if stale_ignored and existing_alert is not None else event
            if existing_alert is not None:
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
                return (
                    stored_event,
                    existing_state,
                    created,
                    previous_status,
                    stale_ignored,
                    reopened,
                )
            if not stale_ignored:
                self._save_alert_event(connection, stored_event)
            self._save_incident_state(connection, incident_state)
            return (
                stored_event,
                incident_state,
                created,
                previous_status,
                stale_ignored,
                reopened,
            )

    def claim_alert_auto_diagnosis(self, incident_id: str) -> str | None:
        """Claim one alert diagnosis across all SQLite workers."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM incident_states WHERE incident_id = ?",
                (incident_id,),
            ).fetchone()
            if row is None:
                return None
            state = IncidentState.model_validate(_load_payload(row))
            metadata = dict(state.metadata or {})
            now = datetime.now(UTC)
            if alert_auto_diagnosis_claim_is_active(
                metadata,
                now=now,
                lease_seconds=config.alert_auto_diagnosis_timeout_seconds,
            ):
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
                connection,
                state.model_copy(update={"metadata": metadata}),
            )
            return claim_token

    def release_alert_auto_diagnosis(self, incident_id: str, claim_token: str) -> None:
        """Release a process-wide alert diagnosis claim after the task exits."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM incident_states WHERE incident_id = ?",
                (incident_id,),
            ).fetchone()
            if row is None:
                return
            state = IncidentState.model_validate(_load_payload(row))
            metadata = dict(state.metadata or {})
            if (
                metadata.get("alert_auto_diagnosis_status") != "running"
                or metadata.get("alert_auto_diagnosis_claim_token") != claim_token
            ):
                return
            metadata["alert_auto_diagnosis_status"] = "idle"
            metadata["alert_auto_diagnosis_claimed_at"] = ""
            metadata["alert_auto_diagnosis_claim_token"] = ""
            self._save_incident_state(
                connection,
                state.model_copy(update={"metadata": metadata}),
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
        """Persist an immutable trace event idempotently."""
        payload = _dump_model(event)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO trace_events (
                    event_id, trace_id, incident_id, event_type, node_name,
                    step_id, tool_name, status, created_at, payload
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO NOTHING
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
                row = connection.execute(
                    "SELECT payload FROM trace_events WHERE event_id = ?",
                    (event.event_id,),
                ).fetchone()
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
        updated_at = (request.decided_at or request.created_at).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO approval_requests (
                    approval_id, incident_id, status, risk_level, action,
                    created_at, updated_at, decided_at, payload
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(approval_id) DO UPDATE SET
                    incident_id = excluded.incident_id,
                    risk_level = excluded.risk_level,
                    action = excluded.action,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    decided_at = excluded.decided_at,
                    payload = excluded.payload
                WHERE approval_requests.status = excluded.status
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
            connection.execute("BEGIN IMMEDIATE")
            existing_row = connection.execute(
                """
                SELECT payload FROM approval_requests
                WHERE idempotency_key = ? AND status = 'pending'
                """,
                (idempotency_key,),
            ).fetchone()
            if existing_row is not None:
                return ApprovalRequest.model_validate(_load_payload(existing_row)), False

            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO approval_requests (
                    approval_id, incident_id, status, risk_level, action,
                    idempotency_key, created_at, updated_at, decided_at, payload
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            if cursor.rowcount == 1:
                return request, True

            conflict_row = connection.execute(
                """
                SELECT payload FROM approval_requests
                WHERE approval_id = ?
                   OR (idempotency_key = ? AND status = 'pending')
                ORDER BY CASE WHEN approval_id = ? THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (request.approval_id, idempotency_key, request.approval_id),
            ).fetchone()
            if conflict_row is None:
                raise RuntimeError("approval creation conflicted but existing record was not found")
            existing = ApprovalRequest.model_validate(_load_payload(conflict_row))
            if existing.approval_id == request.approval_id and existing != request:
                raise ValueError(
                    f"Approval {request.approval_id} already exists and cannot be replaced"
                )
            return existing, False

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
                    updated_at = ?,
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
                    (request.decided_at or datetime.now(UTC)).isoformat(),
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
        placeholders = bind_markers(len(normalized_statuses), "?")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                f"""
                UPDATE change_executions
                SET
                    change_plan_id = ?,
                    approval_id = ?,
                    incident_id = ?,
                    status = ?,
                    mode = ?,
                    created_at = ?,
                    updated_at = ?,
                    payload = ?
                WHERE change_execution_id = ?
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
            return cursor.rowcount == 1

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
        snapshot.updated_at = now
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM aiops_sessions WHERE session_id = ?",
                (snapshot.session_id,),
            ).fetchone()
            if row is not None:
                existing = AIOpsSessionSnapshot.model_validate(_load_payload(row))
                snapshot.created_at = existing.created_at
            payload = _dump_model(snapshot)
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
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM aiops_sessions WHERE session_id = ?",
                (snapshot.session_id,),
            ).fetchone()
            if row is not None:
                existing = AIOpsSessionSnapshot.model_validate(_load_payload(row))
                snapshot.created_at = existing.created_at
            payload = _dump_model(snapshot)
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
            row = connection.execute(
                "SELECT payload FROM incident_states WHERE incident_id = ?",
                (incident_state.incident_id,),
            ).fetchone()
            if row is not None:
                incident_state = merge_incident_state(
                    IncidentState.model_validate(_load_payload(row)),
                    incident_state,
                )
            self._save_incident_state(connection, incident_state)

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
            connection.execute("BEGIN IMMEDIATE")
            payload = _dump_model(snapshot)
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO aiops_sessions (
                    session_id, incident_id, trace_id, status, node_name,
                    created_at, updated_at, payload
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
            if int(cursor.rowcount or 0) != 1:
                return False
            self._save_incident_state(connection, incident_state)
            return True

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
        placeholders = bind_markers(len(normalized), "?")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM aiops_sessions WHERE session_id = ?",
                (snapshot.session_id,),
            ).fetchone()
            if row is None:
                return False
            existing = AIOpsSessionSnapshot.model_validate(_load_payload(row))
            if existing.status not in normalized:
                return False
            snapshot.created_at = existing.created_at
            cursor = connection.execute(
                f"""
                UPDATE aiops_sessions
                SET incident_id = ?, trace_id = ?, status = ?, node_name = ?,
                    updated_at = ?, payload = ?
                WHERE session_id = ? AND status IN ({placeholders})
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
                return False
            row = connection.execute(
                "SELECT payload FROM incident_states WHERE incident_id = ?",
                (incident_state.incident_id,),
            ).fetchone()
            if row is not None:
                incident_state = merge_incident_state(
                    IncidentState.model_validate(_load_payload(row)),
                    incident_state,
                )
            self._save_incident_state(connection, incident_state)
            return True

    def create_aiops_session_snapshot(self, snapshot: AIOpsSessionSnapshot) -> bool:
        """Insert the first snapshot for a diagnosis session without overwriting."""
        now = datetime.now(UTC)
        snapshot.updated_at = now
        payload = _dump_model(snapshot)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO aiops_sessions (
                    session_id, incident_id, trace_id, status, node_name,
                    created_at, updated_at, payload
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
        placeholders = bind_markers(len(normalized_statuses), "?")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM aiops_sessions WHERE session_id = ?",
                (snapshot.session_id,),
            ).fetchone()
            if row is None:
                return False
            existing = AIOpsSessionSnapshot.model_validate(_load_payload(row))
            if existing.status not in normalized_statuses:
                return False

            snapshot.created_at = existing.created_at
            payload = _dump_model(snapshot)
            cursor = connection.execute(
                f"""
                UPDATE aiops_sessions
                SET
                    incident_id = ?,
                    trace_id = ?,
                    status = ?,
                    node_name = ?,
                    updated_at = ?,
                    payload = ?
                WHERE session_id = ?
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
            return cursor.rowcount == 1

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
        offset: int = 0,
    ) -> list[AIOpsSessionSnapshot]:
        """List durable diagnosis session snapshots by recent update time."""
        normalized_limit = max(1, min(int(limit or 20), 100))
        normalized_offset = max(int(offset or 0), 0)
        clauses = []
        params: list[object] = []
        if incident_id:
            clauses.append("incident_id = ?")
            params.append(incident_id)

        query = "SELECT payload FROM aiops_sessions"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC, rowid DESC LIMIT ? OFFSET ?"
        params.extend([normalized_limit, normalized_offset])

        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [AIOpsSessionSnapshot.model_validate(_load_payload(row)) for row in rows]

    def create_a2a_task_record(self, record: A2ATaskRecord) -> bool:
        """Insert an A2A task ownership record without overwriting."""
        now = datetime.now(UTC)
        record.updated_at = now
        payload = _dump_model(record)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO a2a_tasks (
                    task_id, message_id, request_fingerprint, owner_id, skill, incident_id,
                    state, created_at, updated_at, payload
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM a2a_tasks WHERE task_id = ?",
                (record.task_id,),
            ).fetchone()
            if row is not None:
                existing = A2ATaskRecord.model_validate(_load_payload(row))
                if (
                    existing.message_id != record.message_id
                    or existing.request_fingerprint != record.request_fingerprint
                    or existing.owner_id != record.owner_id
                ):
                    raise ValueError("A2A task ownership mismatch")
                record.created_at = existing.created_at
            connection.execute(
                """
                INSERT INTO a2a_tasks (
                    task_id, message_id, request_fingerprint, owner_id, skill, incident_id,
                    state, created_at, updated_at, payload
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    message_id = excluded.message_id,
                    request_fingerprint = excluded.request_fingerprint,
                    owner_id = excluded.owner_id,
                    skill = excluded.skill,
                    incident_id = excluded.incident_id,
                    state = excluded.state,
                    updated_at = excluded.updated_at,
                    payload = excluded.payload
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

    def get_a2a_task_record(self, task_id: str) -> A2ATaskRecord | None:
        """Return one durable A2A task record."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM a2a_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
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
            clauses.append("incident_id = ?")
            params.append(incident_id)
        if owner_id:
            clauses.append("owner_id = ?")
            params.append(owner_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC, rowid DESC LIMIT ?"
        params.append(max(1, min(int(limit or 20), 100)))
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [A2ATaskRecord.model_validate(_load_payload(row)) for row in rows]

    def save_incident_state(self, state: IncidentState) -> None:
        """Persist the latest lifecycle state for one incident."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM incident_states WHERE incident_id = ?",
                (state.incident_id,),
            ).fetchone()
            if row is not None:
                state = merge_incident_state(
                    IncidentState.model_validate(_load_payload(row)),
                    state,
                )
            self._save_incident_state(connection, state)

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

    @staticmethod
    def _save_alert_event(connection: sqlite3.Connection, event: AlertEvent) -> None:
        payload = _dump_model(event)
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

    @staticmethod
    def _save_incident_state(
        connection: sqlite3.Connection,
        state: IncidentState,
    ) -> None:
        state.updated_at = datetime.now(UTC)
        payload = _dump_model(state)
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

    def save_report(self, report: DiagnosisReport) -> None:
        """Persist a diagnosis report."""
        payload = _dump_model(report)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO diagnosis_reports (
                    report_id, incident_id, trace_id, created_at, updated_at, payload
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(report_id) DO UPDATE SET
                    incident_id = excluded.incident_id,
                    trace_id = excluded.trace_id,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    payload = excluded.payload
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
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO diagnosis_reports (
                    report_id, incident_id, trace_id, created_at, updated_at, payload
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(report_id) DO UPDATE SET
                    incident_id = excluded.incident_id,
                    trace_id = excluded.trace_id,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    payload = excluded.payload
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
            row = connection.execute(
                "SELECT payload FROM incident_states WHERE incident_id = ?",
                (incident_state.incident_id,),
            ).fetchone()
            if row is not None:
                incident_state = merge_incident_state(
                    IncidentState.model_validate(_load_payload(row)),
                    incident_state,
                )
            self._save_incident_state(connection, incident_state)

    def get_report(self, report_id: str) -> DiagnosisReport | None:
        """Return one report by its stable identifier."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM diagnosis_reports WHERE report_id = ?",
                (report_id,),
            ).fetchone()
        if row is None:
            return None
        return DiagnosisReport.model_validate(_load_payload(row))

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
                              AND newer.rowid > current.rowid
                          )
                      )
                )
                ORDER BY current.created_at DESC, current.rowid DESC
                """).fetchall()
        return [DiagnosisReport.model_validate(_load_payload(row)) for row in rows]

    def reset_runtime_data(self) -> dict[str, int]:
        """Delete all AIOps runtime records while preserving the database schema."""
        return reset_sqlite_runtime_data(self._connect)

    def cleanup_older_than(self, *, keep_days: int, dry_run: bool = False) -> dict[str, Any]:
        """Delete runtime records older than the retention window."""
        return cleanup_sqlite_runtime_data(
            self._connect,
            database_path=self.database_path,
            keep_days=keep_days,
            dry_run=dry_run,
        )

    def _record_migration_warning(self, message: str) -> None:
        self.migration_warnings.append(message)
        logger.warning(message)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30)
        try:
            connection.execute("PRAGMA busy_timeout=5000")
            connection.row_factory = sqlite3.Row
            with connection:
                yield connection
        finally:
            connection.close()


def _dump_model(model: Any) -> str:
    return json.dumps(model.model_dump(mode="json"), ensure_ascii=False, default=str)


def _load_payload(row: sqlite3.Row) -> dict[str, Any]:
    payload = json.loads(str(row["payload"]))
    return payload if isinstance(payload, dict) else {}
