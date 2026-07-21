"""Canonical Incident, session, report, and change lifecycle rules."""

from __future__ import annotations

from typing import Any

CHANGE_LIFECYCLE_STATUSES = frozenset(
    {
        "change_prechecking",
        "change_dry_run",
        "change_validated",
        "sandbox_validated",
        "waiting_manual_execution",
        "change_executing_sandbox",
        "observing",
        "partial_success",
        "recovery_pending",
        "resolved",
        "rollback_recommended",
        "rolled_back",
        "rollback_failed",
        "precheck_failed",
        "dry_run_failed",
        "escalated",
    }
)

REPORT_ONLY_STATUSES = frozenset(
    {
        "completed",
        "incomplete",
        "degraded",
        "needs_human",
        "waiting_approval",
        "approval_approved",
        "approval_rejected",
        "approval_resumed",
        "blocked",
        "failed",
    }
)

AI_OBJECT_STATUSES = frozenset(
    {
        *REPORT_ONLY_STATUSES,
        "approval_cancelled",
        "rollback_recommended",
        "partial_success",
        "recovery_pending",
        "rolled_back",
        "rollback_failed",
        "precheck_failed",
        "dry_run_failed",
        "manual_result_required",
        "manual_result_recorded",
        "dry_run_completed",
        "closed",
    }
)

AIOPS_RUN_FILTER_STATUSES = [
    "running",
    "resume_running",
    "completed",
    "waiting_approval",
    "approval_approved",
    "approval_rejected",
    "approval_cancelled",
    "approval_resumed",
    "change_validated",
    "waiting_manual_execution",
    "resolved",
    "rollback_recommended",
    "partial_success",
    "recovery_pending",
    "rolled_back",
    "rollback_failed",
    "precheck_failed",
    "dry_run_failed",
    "failed",
    "blocked",
    "escalated",
]

POST_APPROVAL_RUN_STATUSES = frozenset(
    {
        "approval_resumed",
        "change_validated",
        "resolved",
        "rollback_recommended",
        "precheck_running",
        "precheck_failed",
        "dry_run_running",
        "dry_run_completed",
        "dry_run_failed",
        "waiting_manual_execution",
        "manual_result_required",
        "manual_execution_recorded",
        "manual_result_recorded",
        "sandbox_executing",
        "sandbox_validated",
        "observing",
        "partial_success",
        "recovery_pending",
        "rolled_back",
        "rollback_failed",
        "escalated",
        "closed",
        "failed",
    }
)

ALERT_MUTABLE_INCIDENT_STATUSES = frozenset(
    {"created", "investigating", "alert_firing", "alert_resolved", "resolved"}
)
ACTIVE_DIAGNOSIS_STATUSES = frozenset(
    {
        "created",
        "investigating",
        "diagnosing",
        "running",
        "planning",
        "executing",
        "alert_firing",
        "alert_resolved",
    }
)
ALERT_AUTO_DIAGNOSIS_ACTIVE_STATUSES = frozenset({"running", "queued"})
ALERT_AUTO_DIAGNOSIS_FAILURE_SOURCE = "alert_auto_diagnosis"
APPROVAL_LIFECYCLE_STATUSES = frozenset(
    {
        "waiting_approval",
        "approval_approved",
        "approval_rejected",
        "approval_cancelled",
        "approval_resumed",
    }
)
HARD_TERMINAL_INCIDENT_STATUSES = frozenset(
    {
        "completed",
        "incomplete",
        "degraded",
        "needs_human",
        "approval_rejected",
        "approval_cancelled",
        "blocked",
        "failed",
        "escalated",
        "resolved",
        "closed",
        "precheck_failed",
        "dry_run_failed",
    }
)


def snapshot_status_from_event(event: dict[str, Any]) -> str:
    """Map streamed workflow events to durable session snapshot states."""
    event_type = event.get("type")
    if event_type == "approval_required":
        return "waiting_approval"
    if event_type == "report":
        structured_report = event.get("structured_report") or {}
        if isinstance(structured_report, dict) and structured_report.get("status"):
            return str(structured_report["status"])
        return "completed"
    if event_type == "error":
        return "failed"
    return "running"


def incident_status_from_runtime_status(status: str) -> str:
    """Normalize runtime and report statuses into an Incident status."""
    if status in {"running", "planning", "executing"}:
        return "diagnosing"
    if status in {
        "resume_running",
        "waiting_approval",
        "approval_approved",
        "approval_rejected",
        "approval_resumed",
        "blocked",
        "escalated",
        "failed",
        "completed",
    }:
        return status
    if status.startswith("approval_"):
        return status
    return status or "diagnosing"


def terminal_event_status(event: dict[str, Any]) -> str:
    """Derive the terminal workflow status from a streamed event payload."""
    structured_report = event.get("structured_report") or {}
    if isinstance(structured_report, dict) and structured_report.get("status"):
        return str(structured_report["status"])
    risk_assessment = event.get("risk_assessment") or {}
    if event.get("pending_approval"):
        return "waiting_approval"
    if isinstance(risk_assessment, dict) and risk_assessment.get("policy") == "forbidden":
        return "blocked"
    if event.get("errors"):
        return "escalated"
    if event.get("type") == "error":
        return "failed"
    return "completed"


def status_after_approved_run(report_status: str) -> str:
    """Resolve a diagnosis run status after approval is granted."""
    return report_status if report_status in POST_APPROVAL_RUN_STATUSES else "approval_approved"


CHANGE_EXECUTION_REPORT_STATUS = {
    "precheck_running": "change_prechecking",
    "dry_run_running": "change_dry_run",
    "dry_run_completed": "change_validated",
    "sandbox_validated": "change_validated",
    "waiting_manual_execution": "waiting_manual_execution",
    "sandbox_executing": "change_executing_sandbox",
    "manual_execution_recorded": "observing",
    "closed": "resolved",
}

CHANGE_EXECUTION_NO_MANUAL_ACTION_STATUSES = frozenset(
    {
        "closed",
        "dry_run_completed",
        "sandbox_validated",
        "rolled_back",
        "dry_run_failed",
        "precheck_failed",
    }
)

MANUAL_RESULT_EXECUTION_STATUS = {
    "succeeded": "closed",
    "failed": "rollback_recommended",
    "partial": "partial_success",
    "recovery_pending": "recovery_pending",
    "rolled_back": "rolled_back",
    "rollback_failed": "rollback_failed",
}

MANUAL_RESULT_SOURCE_STATUSES = {
    "succeeded": frozenset({"waiting_manual_execution", "recovery_pending"}),
    "failed": frozenset(
        {"waiting_manual_execution", "partial_success", "recovery_pending"}
    ),
    "partial": frozenset({"waiting_manual_execution"}),
    "recovery_pending": frozenset(
        {
            "waiting_manual_execution",
            "partial_success",
            "rollback_recommended",
            "rolled_back",
            "rollback_failed",
        }
    ),
    "rolled_back": frozenset(
        {
            "waiting_manual_execution",
            "partial_success",
            "rollback_recommended",
            "rollback_failed",
        }
    ),
    "rollback_failed": frozenset(
        {"waiting_manual_execution", "partial_success", "rollback_recommended"}
    ),
}

REPORT_MANUAL_ACTION_STATUSES = frozenset(
    {
        "waiting_approval",
        "blocked",
        "escalated",
        "degraded",
        "needs_human",
        "incomplete",
    }
)
REPORT_SAFE_POLICY_BOUNDARY_STATUSES = frozenset(
    {"waiting_approval", "blocked", "needs_human"}
)
REPORT_SAFE_INSUFFICIENT_EVIDENCE_STATUSES = frozenset(
    {"degraded", "needs_human", "incomplete"}
)


def incident_status_from_report_status(status: str) -> str:
    """Map a report status to the Incident lifecycle."""
    return status if status in REPORT_ONLY_STATUSES else status or "completed"


def status_from_change_execution(status: str) -> str:
    """Map a ChangeExecution status to the Incident and report lifecycle."""
    if status in {"precheck_failed", "dry_run_failed", "escalated"}:
        return status
    if status in {
        "partial_success",
        "recovery_pending",
        "rollback_recommended",
        "rolled_back",
        "rollback_failed",
    }:
        return status
    return CHANGE_EXECUTION_REPORT_STATUS.get(status, status or "change_pending")


def manual_action_required_from_change_execution(status: str, *, fallback: bool) -> bool:
    """Return whether a change execution still needs human action."""
    if not status:
        return fallback
    return status not in CHANGE_EXECUTION_NO_MANUAL_ACTION_STATUSES


def report_requires_manual_action(
    status: str,
    *,
    has_pending_approval: bool = False,
    risk_requires_approval: bool = False,
    risk_forbidden: bool = False,
) -> bool:
    """Return whether a report requires a human decision or action."""
    return (
        has_pending_approval
        or risk_requires_approval
        or risk_forbidden
        or status in REPORT_MANUAL_ACTION_STATUSES
    )


def status_from_manual_result(status: str) -> str:
    """Map an operator-recorded result to the ChangeExecution lifecycle."""
    return MANUAL_RESULT_EXECUTION_STATUS[status]


def manual_result_source_statuses(status: str) -> frozenset[str]:
    """Return ChangeExecution states from which a manual result may be recorded."""
    return MANUAL_RESULT_SOURCE_STATUSES[status]


def trace_status_from_manual_result(status: str) -> str:
    """Map an operator-recorded result to its Trace status."""
    if status in {"succeeded", "rolled_back"}:
        return "success"
    if status == "recovery_pending":
        return "waiting"
    return "failed"
