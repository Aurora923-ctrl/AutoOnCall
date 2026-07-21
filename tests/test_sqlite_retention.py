"""Tests for SQLite runtime retention cleanup."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from app.models.a2a import A2ATaskRecord
from app.models.aiops_session import AIOpsSessionSnapshot
from app.models.alert import AlertEvent
from app.models.approval import ApprovalRequest
from app.models.change_execution import ChangeExecution
from app.models.incident import new_model_id
from app.models.incident_state import IncidentState
from app.models.report import DiagnosisReport
from app.models.trace import TraceEvent
from app.services.sqlite_store import AIOpsSQLiteStore


def test_sqlite_store_cleanup_older_than_removes_old_runtime_records(tmp_path) -> None:
    store = AIOpsSQLiteStore(tmp_path / "aiops.db")
    old_time = datetime.now(UTC) - timedelta(days=30)
    recent_time = datetime.now(UTC)
    old_incident_id = new_model_id("inc-old")
    recent_incident_id = new_model_id("inc-new")

    old_event = TraceEvent(
        incident_id=old_incident_id,
        trace_id="trace-old",
        node_name="workflow",
        event_type="workflow_started",
        created_at=old_time,
    )
    recent_event = TraceEvent(
        incident_id=recent_incident_id,
        trace_id="trace-new",
        node_name="workflow",
        event_type="workflow_started",
        created_at=recent_time,
    )
    old_report = DiagnosisReport(
        incident_id=old_incident_id,
        trace_id="trace-old",
        created_at=old_time,
        markdown="# old",
        root_cause="old",
    )
    recent_report = DiagnosisReport(
        incident_id=recent_incident_id,
        trace_id="trace-new",
        created_at=recent_time,
        markdown="# new",
        root_cause="new",
    )

    store.save_trace_event(old_event)
    store.save_trace_event(recent_event)
    store.save_report(old_report)
    store.save_report(recent_report)
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE diagnosis_reports SET updated_at = ? WHERE report_id = ?",
            (old_time.isoformat(), old_report.report_id),
        )

    dry_run = store.cleanup_older_than(keep_days=14, dry_run=True)
    assert dry_run["deleted"]["trace_events"] == 1
    assert dry_run["deleted"]["diagnosis_reports"] == 1
    assert store.get_latest_report(old_incident_id) is not None

    result = store.cleanup_older_than(keep_days=14)
    assert result["deleted"]["trace_events"] == 1
    assert result["deleted"]["diagnosis_reports"] == 1
    assert store.list_trace_events(incident_id=old_incident_id) == []
    assert store.get_latest_report(old_incident_id) is None
    assert store.list_trace_events(incident_id=recent_incident_id)
    assert store.get_latest_report(recent_incident_id) is not None


def test_sqlite_store_cleanup_requires_positive_retention(tmp_path) -> None:
    store = AIOpsSQLiteStore(tmp_path / "aiops.db")

    with pytest.raises(ValueError, match="keep_days"):
        store.cleanup_older_than(keep_days=0)


def test_sqlite_retention_preserves_old_active_recovery_records(tmp_path) -> None:
    store = AIOpsSQLiteStore(tmp_path / "active-recovery.db")
    old_time = datetime.now(UTC) - timedelta(days=30)
    incident_id = "inc-active-retention"
    approval = ApprovalRequest(
        approval_id="apr-active-retention",
        incident_id=incident_id,
        action="manual mitigation",
        risk_level="high",
        status="pending",
        created_at=old_time,
    )
    session = AIOpsSessionSnapshot(
        session_id="session-active-retention",
        incident_id=incident_id,
        trace_id="trace-active-retention",
        status="waiting_approval",
        created_at=old_time,
        updated_at=old_time,
        pending_approval=approval.model_dump(mode="json"),
    )
    state = IncidentState(
        incident_id=incident_id,
        status="waiting_approval",
        created_at=old_time,
        updated_at=old_time,
    )
    firing_alert = AlertEvent(
        fingerprint="fp-active-retention",
        incident_id=incident_id,
        service_name="order-service",
        status="firing",
        created_at=old_time,
        updated_at=old_time,
    )
    execution = ChangeExecution(
        change_execution_id="chgexec-active-retention",
        change_plan_id="plan-active-retention",
        approval_id=approval.approval_id,
        incident_id=incident_id,
        status="waiting_manual_execution",
        created_at=old_time,
        updated_at=old_time,
    )

    store.save_approval_request(approval)
    store.save_aiops_session_snapshot(session)
    store.save_incident_state(state)
    store.save_alert_event(firing_alert)
    store.save_change_execution(execution)
    store.save_trace_event(
        TraceEvent(
            event_id="evt-active-retention",
            trace_id=session.trace_id,
            incident_id=incident_id,
            node_name="workflow",
            created_at=old_time,
        )
    )
    store.save_report(
        DiagnosisReport(
            report_id="rpt-active-retention",
            incident_id=incident_id,
            trace_id=session.trace_id,
            status="waiting_approval",
            created_at=old_time,
        )
    )

    result = store.cleanup_older_than(keep_days=14)

    assert result["deleted"]["trace_events"] == 0
    assert result["deleted"]["diagnosis_reports"] == 0
    assert result["deleted"]["approval_requests"] == 0
    assert result["deleted"]["aiops_sessions"] == 0
    assert result["deleted"]["incident_states"] == 0
    assert result["deleted"]["alert_events"] == 0
    assert result["deleted"]["change_executions"] == 0
    assert store.get_approval_request(approval.approval_id) is not None
    assert store.get_aiops_session_snapshot(session.session_id) is not None
    assert store.get_incident_state(incident_id) is not None
    assert store.get_alert_event(firing_alert.fingerprint) is not None
    assert store.get_change_execution(execution.change_execution_id) is not None
    assert store.list_trace_events(incident_id=incident_id)
    assert store.get_latest_report(incident_id) is not None


@pytest.mark.parametrize("execution_status", ["closed", "rolled_back", "rollback_failed"])
def test_sqlite_retention_removes_old_terminal_recovery_records(
    tmp_path,
    execution_status,
) -> None:
    store = AIOpsSQLiteStore(tmp_path / "terminal-recovery.db")
    old_time = datetime.now(UTC) - timedelta(days=30)
    incident_id = "inc-terminal-retention"
    approval = ApprovalRequest(
        approval_id="apr-terminal-retention",
        incident_id=incident_id,
        action="manual mitigation",
        risk_level="high",
        status="approved",
        created_at=old_time,
        decided_at=old_time,
    )
    session = AIOpsSessionSnapshot(
        session_id="session-terminal-retention",
        incident_id=incident_id,
        trace_id="trace-terminal-retention",
        status="completed",
        created_at=old_time,
        updated_at=old_time,
    )
    state = IncidentState(
        incident_id=incident_id,
        status="completed",
        created_at=old_time,
        updated_at=old_time,
    )
    resolved_alert = AlertEvent(
        fingerprint="fp-terminal-retention",
        incident_id=incident_id,
        service_name="order-service",
        status="resolved",
        created_at=old_time,
        updated_at=old_time,
    )
    execution = ChangeExecution(
        change_execution_id="chgexec-terminal-retention",
        change_plan_id="plan-terminal-retention",
        approval_id=approval.approval_id,
        incident_id=incident_id,
        status=execution_status,
        created_at=old_time,
        updated_at=old_time,
    )

    store.save_approval_request(approval)
    store.save_aiops_session_snapshot(session)
    store.save_incident_state(state)
    store.save_alert_event(resolved_alert)
    store.save_change_execution(execution)
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE aiops_sessions SET updated_at = ? WHERE session_id = ?",
            (old_time.isoformat(), session.session_id),
        )
        connection.execute(
            "UPDATE incident_states SET updated_at = ? WHERE incident_id = ?",
            (old_time.isoformat(), incident_id),
        )

    result = store.cleanup_older_than(keep_days=14)

    assert result["deleted"]["approval_requests"] == 1
    assert result["deleted"]["aiops_sessions"] == 1
    assert result["deleted"]["incident_states"] == 1
    assert result["deleted"]["alert_events"] == 1
    assert result["deleted"]["change_executions"] == 1


def test_sqlite_retention_preserves_dependencies_of_active_change_execution(tmp_path) -> None:
    store = AIOpsSQLiteStore(tmp_path / "active-change.db")
    old_time = datetime.now(UTC) - timedelta(days=30)
    incident_id = "inc-active-change-retention"
    approval = ApprovalRequest(
        approval_id="apr-active-change-retention",
        incident_id=incident_id,
        action="record manual mitigation",
        risk_level="high",
        status="approved",
        created_at=old_time,
        decided_at=old_time,
    )
    session = AIOpsSessionSnapshot(
        session_id="session-active-change-retention",
        incident_id=incident_id,
        trace_id="trace-active-change-retention",
        status="completed",
        created_at=old_time,
        updated_at=old_time,
    )
    state = IncidentState(
        incident_id=incident_id,
        status="completed",
        created_at=old_time,
        updated_at=old_time,
    )
    execution = ChangeExecution(
        change_execution_id="chgexec-active-change-retention",
        change_plan_id="plan-active-change-retention",
        approval_id=approval.approval_id,
        incident_id=incident_id,
        status="waiting_manual_execution",
        created_at=old_time,
        updated_at=old_time,
    )

    store.save_approval_request(approval)
    store.save_aiops_session_snapshot(session)
    store.save_incident_state(state)
    store.save_change_execution(execution)
    store.save_trace_event(
        TraceEvent(
            event_id="evt-active-change-retention",
            trace_id=session.trace_id,
            incident_id=incident_id,
            node_name="change_execution",
            created_at=old_time,
        )
    )
    store.save_report(
        DiagnosisReport(
            report_id="rpt-active-change-retention",
            incident_id=incident_id,
            trace_id=session.trace_id,
            status="waiting_manual_execution",
            created_at=old_time,
        )
    )
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE aiops_sessions SET updated_at = ? WHERE session_id = ?",
            (old_time.isoformat(), session.session_id),
        )
        connection.execute(
            "UPDATE incident_states SET updated_at = ? WHERE incident_id = ?",
            (old_time.isoformat(), incident_id),
        )

    result = store.cleanup_older_than(keep_days=14)

    assert result["deleted"]["approval_requests"] == 0
    assert result["deleted"]["aiops_sessions"] == 0
    assert result["deleted"]["incident_states"] == 0
    assert result["deleted"]["trace_events"] == 0
    assert result["deleted"]["diagnosis_reports"] == 0
    assert result["deleted"]["change_executions"] == 0
    assert store.get_approval_request(approval.approval_id) is not None
    assert store.get_aiops_session_snapshot(session.session_id) is not None
    assert store.get_incident_state(incident_id) is not None
    assert store.list_trace_events(incident_id=incident_id)
    assert store.get_latest_report(incident_id) is not None
    assert store.get_change_execution(execution.change_execution_id) is not None


def test_sqlite_retention_preserves_active_a2a_task_and_deletes_terminal_task(tmp_path) -> None:
    store = AIOpsSQLiteStore(tmp_path / "a2a-retention.db")
    old_time = datetime.now(UTC) - timedelta(days=30)
    active = A2ATaskRecord(
        task_id="task-active",
        message_id="msg-active",
        request_fingerprint="a" * 64,
        owner_id="alice",
        skill="diagnose_incident",
        incident_id="inc-a2a-active",
        state="TASK_STATE_WORKING",
        created_at=old_time,
        updated_at=old_time,
    )
    terminal = active.model_copy(
        update={
            "task_id": "task-terminal",
            "message_id": "msg-terminal",
            "request_fingerprint": "b" * 64,
            "incident_id": "inc-a2a-terminal",
            "state": "TASK_STATE_COMPLETED",
        }
    )
    store.create_a2a_task_record(active)
    store.create_a2a_task_record(terminal)
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE a2a_tasks SET updated_at = ?",
            (old_time.isoformat(),),
        )

    result = store.cleanup_older_than(keep_days=14)

    assert result["deleted"]["a2a_tasks"] == 1
    assert store.get_a2a_task_record(active.task_id) is not None
    assert store.get_a2a_task_record(terminal.task_id) is None
