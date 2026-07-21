"""Atomic import of runtime state into the MySQL AIOps store."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from app.models.a2a import A2ATaskRecord
from app.models.aiops_session import AIOpsSessionSnapshot
from app.models.alert import AlertEvent
from app.models.approval import ApprovalRequest
from app.models.change_execution import ChangeExecution
from app.models.incident_state import IncidentState
from app.models.report import DiagnosisReport
from app.models.trace import TraceEvent
from app.services.sql_safety import trusted_identifier


def import_mysql_runtime_state(
    connect: Callable[[], Any],
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
    """Atomically import runtime rows without overwriting existing MySQL state."""
    imported = dict.fromkeys(
        (
            "alert_events",
            "trace_events",
            "approval_requests",
            "change_executions",
            "aiops_sessions",
            "a2a_tasks",
            "incident_states",
            "diagnosis_reports",
        ),
        0,
    )
    with connect() as connection:
        connection.begin()
        try:
            with connection.cursor() as cursor:
                conflicts = _find_runtime_import_conflicts(
                    cursor,
                    alert_events=alert_events,
                    trace_events=trace_events,
                    approval_requests=approval_requests,
                    change_executions=change_executions,
                    aiops_sessions=aiops_sessions,
                    a2a_tasks=a2a_tasks,
                    incident_states=incident_states,
                    diagnosis_reports=diagnosis_reports,
                )
                if any(conflicts.values()):
                    connection.rollback()
                    return {**imported, "conflicts": conflicts}
                groups = (
                    ("alert_events", alert_events, _insert_alert_event),
                    ("trace_events", trace_events, _insert_trace_event),
                    ("approval_requests", approval_requests, _insert_approval_request),
                    ("change_executions", change_executions, _insert_change_execution),
                    ("aiops_sessions", aiops_sessions, _insert_aiops_session),
                    ("a2a_tasks", a2a_tasks, _insert_a2a_task),
                    ("incident_states", incident_states, _insert_incident_state),
                    ("diagnosis_reports", diagnosis_reports, _insert_report),
                )
                for table, records, insert in groups:
                    for record in records:
                        imported[table] += insert(cursor, record)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return {**imported, "conflicts": dict.fromkeys(imported, 0)}


def _find_runtime_import_conflicts(
    cursor: Any,
    *,
    alert_events: list[AlertEvent],
    trace_events: list[TraceEvent],
    approval_requests: Sequence[ApprovalRequest | tuple[ApprovalRequest, str | None]],
    change_executions: list[ChangeExecution],
    aiops_sessions: list[AIOpsSessionSnapshot],
    a2a_tasks: list[A2ATaskRecord],
    incident_states: list[IncidentState],
    diagnosis_reports: list[DiagnosisReport],
) -> dict[str, int]:
    conflicts = dict.fromkeys(
        (
            "alert_events",
            "trace_events",
            "approval_requests",
            "change_executions",
            "aiops_sessions",
            "a2a_tasks",
            "incident_states",
            "diagnosis_reports",
        ),
        0,
    )
    for table, key_column, records in (
        ("alert_events", "fingerprint", alert_events),
        ("trace_events", "event_id", trace_events),
        ("aiops_sessions", "session_id", aiops_sessions),
        ("a2a_tasks", "task_id", a2a_tasks),
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
                "task_id",
                "incident_id",
                "report_id",
            },
        )
        for record in records:
            cursor.execute(
                f"SELECT payload FROM {table} WHERE {key_column} = %s",  # nosec B608
                (getattr(record, key_column),),
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
               OR (%s IS NOT NULL AND pending_idempotency_key = %s)
            """,
            (request.approval_id, idempotency_key, idempotency_key),
        )
        if any(
            _load_payload(row) != request.model_dump(mode="json")
            or str(row.get("idempotency_key") or "") != str(idempotency_key or "")
            for row in cursor.fetchall()
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
        if any(
            _load_payload(row) != execution.model_dump(mode="json")
            for row in cursor.fetchall()
        ):
            conflicts["change_executions"] += 1
    return conflicts


def _insert_alert_event(cursor: Any, event: AlertEvent) -> int:
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


def _insert_trace_event(cursor: Any, event: TraceEvent) -> int:
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


def _insert_approval_request(
    cursor: Any,
    approval_record: ApprovalRequest | tuple[ApprovalRequest, str | None],
) -> int:
    request, idempotency_key = _approval_import_record(approval_record)
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
            (request.decided_at or request.created_at).isoformat(),
            request.decided_at.isoformat() if request.decided_at else None,
            _dump_model(request),
        ),
    )
    return int(cursor.rowcount or 0)


def _insert_change_execution(cursor: Any, execution: ChangeExecution) -> int:
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


def _insert_aiops_session(cursor: Any, snapshot: AIOpsSessionSnapshot) -> int:
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


def _insert_a2a_task(cursor: Any, record: A2ATaskRecord) -> int:
    cursor.execute(
        """
        INSERT INTO a2a_tasks (
            task_id, message_id, request_fingerprint, owner_id, skill, incident_id,
            state, created_at, updated_at, payload
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE task_id = VALUES(task_id)
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
    return int(cursor.rowcount or 0)


def _insert_incident_state(cursor: Any, state: IncidentState) -> int:
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


def _insert_report(cursor: Any, report: DiagnosisReport) -> int:
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


def _approval_import_record(
    value: ApprovalRequest | tuple[ApprovalRequest, str | None],
) -> tuple[ApprovalRequest, str | None]:
    if isinstance(value, tuple):
        return value
    key = str(value.metadata.get("idempotency_key") or "").strip() or None
    return value, key


def _dump_model(model: Any) -> str:
    import json

    return json.dumps(model.model_dump(mode="json"), ensure_ascii=False, default=str)


def _load_payload(row: dict[str, Any]) -> dict[str, Any]:
    import json

    payload = json.loads(str(row["payload"]))
    return payload if isinstance(payload, dict) else {}
