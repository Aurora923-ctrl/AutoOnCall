"""Regression tests for Incident lifecycle transition guards."""

import pytest

from app.models.incident_state import IncidentState
from app.services.incident_lifecycle import (
    incident_status_from_runtime_status,
    is_production_environment,
    merge_incident_state,
    status_metadata,
)


def test_production_environment_recognizes_qualified_names_without_prefix_false_positives() -> None:
    for environment in (
        "prod-cn",
        "production-us",
        "prd_east",
        "prod.cluster-a",
        "生产-华东",
    ):
        assert is_production_environment(environment) is True

    for environment in ("product", "productionlike", "preprod", "non-prod", "staging"):
        assert is_production_environment(environment) is False


def test_terminal_incident_rejects_implicit_runtime_regression() -> None:
    existing = IncidentState(
        incident_id="inc-terminal",
        status="completed",
        status_reason="Diagnosis report saved: completed",
        title="confirmed title",
        service_name="order-service",
        severity="P1",
        environment="prod",
        summary="confirmed summary",
        root_cause="confirmed root cause",
        trace_id="trace-completed",
        report_id="report-completed",
        metadata={"source": "diagnosis_report"},
    )
    late_update = IncidentState(
        incident_id="inc-terminal",
        status="diagnosing",
        status_reason="late runtime snapshot",
        title="stale title",
        service_name="wrong-service",
        severity="P3",
        environment="test",
        summary="stale summary",
        trace_id="trace-stale",
        metadata={"source": "aiops_state"},
    )

    merged = merge_incident_state(existing, late_update)

    assert merged.status == "completed"
    assert merged.status_reason == "Diagnosis report saved: completed"
    assert merged.title == "confirmed title"
    assert merged.service_name == "order-service"
    assert merged.summary == "confirmed summary"
    assert merged.root_cause == "confirmed root cause"
    assert merged.trace_id == "trace-completed"
    assert merged.report_id == "report-completed"
    assert merged.metadata["ignored_status_update"] == "diagnosing"
    assert merged.metadata["ignored_status_update_source"] == "aiops_state"


def test_resolved_alert_can_reopen_only_with_explicit_alert_generation() -> None:
    existing = IncidentState(
        incident_id="inc-reopen",
        status="resolved",
        status_reason="alert resolved",
        metadata={
            "source": "alertmanager",
            "alert_status": "resolved",
            "starts_at": "2026-06-30T10:00:00+00:00",
        },
    )
    reopen = IncidentState(
        incident_id="inc-reopen",
        status="alert_firing",
        status_reason="new alert generation",
        metadata={
            "source": "alertmanager",
            "alert_status": "firing",
            "starts_at": "2026-06-30T10:09:00+00:00",
        },
    )

    merged = merge_incident_state(existing, reopen)

    assert merged.status == "alert_firing"
    assert merged.status_reason == "new alert generation"


def test_diagnosis_resolved_incident_can_reopen_for_new_alert_generation() -> None:
    existing = IncidentState(
        incident_id="inc-report-reopen",
        status="resolved",
        status_reason="change workflow closed",
        metadata={
            "source": "diagnosis_report",
            "alert_status": "resolved",
            "starts_at": "2026-06-30T10:00:00+00:00",
        },
    )
    reopen = IncidentState(
        incident_id="inc-report-reopen",
        status="alert_firing",
        status_reason="new alert generation",
        metadata={
            "source": "alertmanager",
            "alert_status": "firing",
            "starts_at": "2026-06-30T10:09:00+00:00",
        },
    )

    assert merge_incident_state(existing, reopen).status == "alert_firing"


def test_waiting_approval_rejects_late_diagnosis_snapshot() -> None:
    existing = IncidentState(
        incident_id="inc-approval",
        status="waiting_approval",
        status_reason="human approval is pending",
        title="approved incident title",
        trace_id="trace-current",
        session_id="session-current",
        approval_status="pending",
        latest_approval_id="approval-current",
        manual_action_required=True,
        metadata={"source": "approval"},
    )
    late_snapshot = IncidentState(
        incident_id="inc-approval",
        status="diagnosing",
        status_reason="late planner snapshot",
        title="stale title",
        trace_id="trace-stale",
        session_id="session-stale",
        metadata={"source": "aiops_state"},
    )

    merged = merge_incident_state(existing, late_snapshot)

    assert merged.status == "waiting_approval"
    assert merged.status_reason == "human approval is pending"
    assert merged.title == "approved incident title"
    assert merged.trace_id == "trace-current"
    assert merged.session_id == "session-current"
    assert merged.approval_status == "pending"
    assert merged.latest_approval_id == "approval-current"
    assert merged.manual_action_required is True


def test_waiting_approval_rejects_late_diagnosis_failure() -> None:
    existing = IncidentState(
        incident_id="inc-approval-failure",
        status="waiting_approval",
        status_reason="human approval is pending",
        title="approved incident title",
        service_name="order-service",
        summary="confirmed summary",
        root_cause="confirmed root cause",
        trace_id="trace-current",
        approval_status="pending",
        latest_approval_id="approval-current",
        manual_action_required=True,
        metadata={"source": "approval"},
    )
    late_failure = IncidentState(
        incident_id="inc-approval-failure",
        status="failed",
        status_reason="late auto diagnosis failure",
        title="stale title",
        service_name="wrong-service",
        summary="stale summary",
        trace_id="trace-stale",
        metadata={"source": "alert_auto_diagnosis"},
    )

    merged = merge_incident_state(existing, late_failure)

    assert merged.status == "waiting_approval"
    assert merged.title == "approved incident title"
    assert merged.summary == "confirmed summary"
    assert merged.root_cause == "confirmed root cause"
    assert merged.trace_id == "trace-current"


def test_resume_running_has_explicit_non_terminal_lifecycle_metadata() -> None:
    metadata = status_metadata("resume_running")

    assert metadata["phase"] == "approval"
    assert metadata["terminal"] is False
    assert incident_status_from_runtime_status("resume_running") == "resume_running"


def test_active_change_lifecycle_rejects_late_diagnosis_snapshot() -> None:
    existing = IncidentState(
        incident_id="inc-change",
        status="change_dry_run",
        status_reason="safe change dry run is active",
        trace_id="trace-change",
        approval_status="approved",
        manual_action_required=True,
        metadata={
            "source": "change_execution",
            "change_execution_id": "chgexec-1",
        },
    )
    late_snapshot = IncidentState(
        incident_id="inc-change",
        status="diagnosing",
        status_reason="late executor snapshot",
        trace_id="trace-stale",
        metadata={"source": "aiops_state"},
    )

    merged = merge_incident_state(existing, late_snapshot)

    assert merged.status == "change_dry_run"
    assert merged.status_reason == "safe change dry run is active"
    assert merged.trace_id == "trace-change"
    assert merged.approval_status == "approved"
    assert merged.manual_action_required is True


@pytest.mark.parametrize(
    ("existing_status", "incoming_status"),
    [
        ("completed", "waiting_approval"),
        ("closed", "diagnosing"),
        ("approval_rejected", "diagnosing"),
        ("resolved", "failed"),
        ("failed", "waiting_approval"),
    ],
)
def test_hard_terminal_incident_rejects_implicit_status_changes(
    existing_status: str,
    incoming_status: str,
) -> None:
    existing = IncidentState(
        incident_id="inc-terminal-matrix",
        status=existing_status,
        status_reason="terminal state",
        title="confirmed title",
        metadata={"source": "diagnosis_report"},
    )
    update = IncidentState(
        incident_id="inc-terminal-matrix",
        status=incoming_status,
        status_reason="late update",
        title="stale title",
        metadata={"source": "aiops_state"},
    )

    merged = merge_incident_state(existing, update)

    assert merged.status == existing_status
    assert merged.status_reason == "terminal state"
    assert merged.title == "confirmed title"
