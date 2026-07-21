"""Canonical retention eligibility status sets."""

CHANGE_EXECUTION_TERMINAL_STATUSES = (
    "precheck_failed",
    "dry_run_failed",
    "sandbox_validated",
    "rolled_back",
    "rollback_failed",
    "closed",
    "escalated",
)

SESSION_RETENTION_ACTIVE_STATUSES = (
    "running",
    "planning",
    "executing",
    "resume_running",
    "waiting_approval",
    "approval_approved",
    "change_validated",
    "waiting_manual_execution",
    "observing",
    "partial_success",
    "recovery_pending",
    "rollback_recommended",
)

A2A_TASK_RETENTION_ACTIVE_STATES = (
    "TASK_STATE_UNSPECIFIED",
    "TASK_STATE_SUBMITTED",
    "TASK_STATE_WORKING",
    "TASK_STATE_INPUT_REQUIRED",
    "TASK_STATE_AUTH_REQUIRED",
)

INCIDENT_RETENTION_TERMINAL_STATUSES = (
    "completed",
    "incomplete",
    "degraded",
    "needs_human",
    "approval_rejected",
    "approval_cancelled",
    "approval_resumed",
    "blocked",
    "failed",
    "escalated",
    "resolved",
    "closed",
    "precheck_failed",
    "dry_run_failed",
    "sandbox_validated",
    "rolled_back",
    "rollback_failed",
)


def sql_status_list(statuses: tuple[str, ...]) -> str:
    """Render a code-owned status tuple as a fixed SQL literal list."""
    if not statuses or any("'" in status for status in statuses):
        raise ValueError("retention statuses must be non-empty trusted literals")
    return ", ".join(f"'{status}'" for status in statuses)


SESSION_RETENTION_ACTIVE_SQL = sql_status_list(SESSION_RETENTION_ACTIVE_STATUSES)
A2A_TASK_RETENTION_ACTIVE_SQL = sql_status_list(A2A_TASK_RETENTION_ACTIVE_STATES)
INCIDENT_RETENTION_TERMINAL_SQL = sql_status_list(INCIDENT_RETENTION_TERMINAL_STATUSES)


def is_change_execution_retention_terminal(status: str) -> bool:
    return status in CHANGE_EXECUTION_TERMINAL_STATUSES


def is_session_retention_active(status: str) -> bool:
    return status in SESSION_RETENTION_ACTIVE_STATUSES


def is_incident_retention_terminal(status: str) -> bool:
    return status in INCIDENT_RETENTION_TERMINAL_STATUSES
