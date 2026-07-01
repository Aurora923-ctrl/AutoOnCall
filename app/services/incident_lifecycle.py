"""Shared status and IncidentState lifecycle rules for AIOps workflows."""

from __future__ import annotations

from typing import Any

from app.models.incident_state import IncidentState

CHANGE_LIFECYCLE_STATUSES = {
    "change_prechecking",
    "change_dry_run",
    "change_validated",
    "sandbox_validated",
    "waiting_manual_execution",
    "change_executing_sandbox",
    "observing",
    "resolved",
    "rollback_recommended",
    "precheck_failed",
    "dry_run_failed",
    "escalated",
}

REPORT_ONLY_STATUSES = {
    "completed",
    "waiting_approval",
    "approval_approved",
    "approval_rejected",
    "approval_resumed",
    "blocked",
    "failed",
}

AI_OBJECT_STATUSES = {
    *REPORT_ONLY_STATUSES,
    "approval_cancelled",
    "rollback_recommended",
    "precheck_failed",
    "dry_run_failed",
    "manual_result_required",
    "manual_result_recorded",
    "dry_run_completed",
    "closed",
}

AIOPS_RUN_FILTER_STATUSES = [
    "running",
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
    "precheck_failed",
    "dry_run_failed",
    "failed",
    "blocked",
    "escalated",
]

ALERT_FIRING_STATUSES = {"firing", "active", "triggered"}
ALERT_RESOLVED_STATUSES = {"resolved", "inactive", "ok", "closed"}
PRODUCTION_ENVIRONMENT_NAMES = {"prod", "production", "prd", "线上", "生产"}
ALERT_MUTABLE_INCIDENT_STATUSES = {
    "created",
    "investigating",
    "alert_firing",
    "alert_resolved",
    "resolved",
}

STATUS_METADATA: dict[str, dict[str, Any]] = {
    "investigating": {
        "label": "排查中",
        "tone": "warning",
        "phase": "diagnosis",
        "terminal": False,
    },
    "diagnosing": {
        "label": "诊断中",
        "tone": "warning",
        "phase": "diagnosis",
        "terminal": False,
    },
    "running": {
        "label": "运行中",
        "tone": "warning",
        "phase": "diagnosis",
        "terminal": False,
    },
    "planning": {
        "label": "规划中",
        "tone": "warning",
        "phase": "diagnosis",
        "terminal": False,
    },
    "executing": {
        "label": "执行诊断",
        "tone": "warning",
        "phase": "diagnosis",
        "terminal": False,
    },
    "completed": {
        "label": "诊断完成",
        "tone": "success",
        "phase": "report",
        "terminal": True,
    },
    "waiting_approval": {
        "label": "等待人工审批",
        "tone": "warning",
        "phase": "approval",
        "terminal": False,
    },
    "approval_approved": {
        "label": "审批已通过",
        "tone": "success",
        "phase": "approval",
        "terminal": False,
    },
    "approval_rejected": {
        "label": "审批已拒绝",
        "tone": "error",
        "phase": "approval",
        "terminal": True,
    },
    "approval_cancelled": {
        "label": "审批已取消",
        "tone": "neutral",
        "phase": "approval",
        "terminal": True,
    },
    "approval_resumed": {
        "label": "审批后已恢复诊断",
        "tone": "success",
        "phase": "diagnosis",
        "terminal": True,
    },
    "blocked": {
        "label": "已阻断",
        "tone": "error",
        "phase": "risk_control",
        "terminal": True,
    },
    "failed": {
        "label": "失败",
        "tone": "error",
        "phase": "diagnosis",
        "terminal": True,
    },
    "escalated": {
        "label": "已升级人工",
        "tone": "error",
        "phase": "human_handoff",
        "terminal": True,
    },
    "change_prechecking": {
        "label": "变更预检查",
        "tone": "warning",
        "phase": "change",
        "terminal": False,
    },
    "change_dry_run": {
        "label": "变更 Dry Run",
        "tone": "warning",
        "phase": "change",
        "terminal": False,
    },
    "change_validated": {
        "label": "变更已校验",
        "tone": "success",
        "phase": "change",
        "terminal": True,
    },
    "waiting_manual_execution": {
        "label": "等待人工执行",
        "tone": "warning",
        "phase": "change",
        "terminal": False,
    },
    "change_executing_sandbox": {
        "label": "沙箱执行中",
        "tone": "warning",
        "phase": "change",
        "terminal": False,
    },
    "observing": {
        "label": "观察中",
        "tone": "warning",
        "phase": "change",
        "terminal": False,
    },
    "resolved": {
        "label": "已恢复",
        "tone": "success",
        "phase": "closed",
        "terminal": True,
    },
    "closed": {
        "label": "已关闭",
        "tone": "success",
        "phase": "closed",
        "terminal": True,
    },
    "rollback_recommended": {
        "label": "建议回滚",
        "tone": "error",
        "phase": "change",
        "terminal": False,
    },
    "precheck_failed": {
        "label": "预检查失败",
        "tone": "error",
        "phase": "change",
        "terminal": True,
    },
    "dry_run_failed": {
        "label": "Dry Run 失败",
        "tone": "error",
        "phase": "change",
        "terminal": True,
    },
    "manual_result_required": {
        "label": "等待人工结果",
        "tone": "warning",
        "phase": "change",
        "terminal": False,
    },
    "manual_result_recorded": {
        "label": "人工结果已记录",
        "tone": "success",
        "phase": "change",
        "terminal": False,
    },
    "alert_firing": {
        "label": "告警触发中",
        "tone": "error",
        "phase": "alert",
        "terminal": False,
    },
    "alert_resolved": {
        "label": "告警已恢复",
        "tone": "success",
        "phase": "alert",
        "terminal": True,
    },
}


def status_metadata(status: str) -> dict[str, Any]:
    """Return frontend-friendly metadata for a lifecycle status."""
    normalized = status or "unknown"
    metadata = STATUS_METADATA.get(
        normalized,
        {
            "label": normalized,
            "tone": "neutral",
            "phase": "unknown",
            "terminal": False,
        },
    )
    return {"status": normalized, **metadata}


def status_catalog(statuses: list[str] | None = None) -> list[dict[str, Any]]:
    """Return ordered lifecycle metadata for frontend controls."""
    selected = statuses or list(STATUS_METADATA)
    return [status_metadata(status) for status in selected]


def infer_terminal_report_status(state: dict[str, Any]) -> str:
    """Infer the terminal report status from a LangGraph state snapshot."""
    risk_assessment = state.get("risk_assessment") or {}
    if state.get("pending_approval"):
        return "waiting_approval"
    if isinstance(risk_assessment, dict) and risk_assessment.get("policy") == "forbidden":
        return "blocked"
    if state.get("errors"):
        return "escalated"
    return "completed"


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
    """Normalize runtime/report statuses into an IncidentState status."""
    if status in {"running", "planning", "executing"}:
        return "diagnosing"
    if status in {
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


def incident_status_from_report_status(status: str) -> str:
    """Map a report status to the IncidentState lifecycle status."""
    return status if status in REPORT_ONLY_STATUSES else status or "completed"


def status_from_change_execution(status: str) -> str:
    """Map a ChangeExecution status to the Incident/Report lifecycle status."""
    if status == "precheck_running":
        return "change_prechecking"
    if status == "dry_run_running":
        return "change_dry_run"
    if status == "dry_run_completed":
        return "change_validated"
    if status == "sandbox_validated":
        return "change_validated"
    if status == "waiting_manual_execution":
        return "waiting_manual_execution"
    if status == "sandbox_executing":
        return "change_executing_sandbox"
    if status == "manual_execution_recorded":
        return "observing"
    if status == "closed":
        return "resolved"
    if status == "rollback_recommended":
        return "rollback_recommended"
    if status in {"precheck_failed", "dry_run_failed", "escalated"}:
        return status
    return status or "change_pending"


def manual_action_required_from_change_execution(status: str, *, fallback: bool) -> bool:
    """Return whether a change execution still needs human action."""
    if not status:
        return fallback
    return status not in {
        "closed",
        "dry_run_completed",
        "sandbox_validated",
        "dry_run_failed",
        "precheck_failed",
    }


def is_production_environment(value: Any) -> bool:
    """Return True for canonical production environment names used across AIOps."""
    return str(value or "").strip().lower() in PRODUCTION_ENVIRONMENT_NAMES


def normalize_alert_status(value: Any) -> str:
    """Normalize external alert statuses into the canonical alert lifecycle."""
    status = str(value or "firing").strip().lower()
    if status in ALERT_RESOLVED_STATUSES:
        return "resolved"
    if status in ALERT_FIRING_STATUSES:
        return "firing"
    return "firing"


def incident_status_from_alert_status(status: str) -> str:
    """Map an alert status to the durable IncidentState lifecycle."""
    return "resolved" if normalize_alert_status(status) == "resolved" else "alert_firing"


def is_alert_mutable_incident_status(status: str) -> bool:
    """Return whether alert webhooks may still own this IncidentState status."""
    return status in ALERT_MUTABLE_INCIDENT_STATUSES


def is_change_lifecycle_state(state: IncidentState) -> bool:
    """Return True when an IncidentState belongs to the safe-change lifecycle."""
    metadata = state.metadata or {}
    return (
        metadata.get("source") == "change_execution"
        or bool(metadata.get("change_execution_id"))
        or state.status in CHANGE_LIFECYCLE_STATUSES
    )


def merge_incident_state(existing: IncidentState, update: IncidentState) -> IncidentState:
    """Merge an IncidentState update without regressing a live change lifecycle."""
    metadata = dict(existing.metadata or {})
    metadata.update(update.metadata or {})
    preserve_existing_status = should_preserve_existing_lifecycle_status(existing, update)
    replace_manual_action = is_change_lifecycle_state(update)
    return update.model_copy(
        update={
            "created_at": existing.created_at,
            "status": existing.status if preserve_existing_status else update.status,
            "status_reason": (
                existing.status_reason if preserve_existing_status else update.status_reason
            ),
            "title": update.title if update.title != "AIOps incident" else existing.title,
            "service_name": (
                update.service_name
                if update.service_name != "unknown-service"
                else existing.service_name
            ),
            "severity": update.severity if update.severity != "unknown" else existing.severity,
            "environment": (
                update.environment if update.environment != "unknown" else existing.environment
            ),
            "summary": update.summary or existing.summary,
            "root_cause": update.root_cause or existing.root_cause,
            "trace_id": update.trace_id or existing.trace_id,
            "session_id": update.session_id or existing.session_id,
            "report_id": update.report_id or existing.report_id,
            "approval_status": (
                existing.approval_status if preserve_existing_status else update.approval_status
            ),
            "latest_approval_id": (
                existing.latest_approval_id
                if preserve_existing_status
                else update.latest_approval_id or existing.latest_approval_id
            ),
            "manual_action_required": merged_manual_action_required(
                existing=existing,
                update=update,
                preserve_existing_status=preserve_existing_status,
                replace_manual_action=replace_manual_action,
            ),
            "metadata": metadata,
        }
    )


def should_preserve_existing_lifecycle_status(
    existing: IncidentState,
    update: IncidentState,
) -> bool:
    """Keep a live change workflow from being pulled back by an older report."""
    return (
        is_change_lifecycle_state(existing)
        and not is_change_lifecycle_state(update)
        and update.status in REPORT_ONLY_STATUSES
    )


def merged_manual_action_required(
    *,
    existing: IncidentState,
    update: IncidentState,
    preserve_existing_status: bool,
    replace_manual_action: bool,
) -> bool:
    """Merge manual-action flags using the same lifecycle precedence as status."""
    if preserve_existing_status:
        return existing.manual_action_required
    if replace_manual_action:
        return update.manual_action_required
    return update.manual_action_required or existing.manual_action_required
