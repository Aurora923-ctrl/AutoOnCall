"""Shared status and IncidentState lifecycle rules for AIOps workflows."""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime
from typing import Any

from app.models.alert import AlertEvent
from app.models.incident_state import IncidentState
from app.services.policies.lifecycle import (
    ACTIVE_DIAGNOSIS_STATUSES,
    AI_OBJECT_STATUSES,
    AIOPS_RUN_FILTER_STATUSES,
    ALERT_AUTO_DIAGNOSIS_ACTIVE_STATUSES,
    ALERT_AUTO_DIAGNOSIS_FAILURE_SOURCE,
    ALERT_MUTABLE_INCIDENT_STATUSES,
    APPROVAL_LIFECYCLE_STATUSES,
    CHANGE_LIFECYCLE_STATUSES,
    HARD_TERMINAL_INCIDENT_STATUSES,
    REPORT_ONLY_STATUSES,
    REPORT_SAFE_INSUFFICIENT_EVIDENCE_STATUSES,
    REPORT_SAFE_POLICY_BOUNDARY_STATUSES,
    incident_status_from_report_status,
    incident_status_from_runtime_status,
    manual_action_required_from_change_execution,
    manual_result_source_statuses,
    report_requires_manual_action,
    snapshot_status_from_event,
    status_after_approved_run,
    status_from_change_execution,
    status_from_manual_result,
    terminal_event_status,
    trace_status_from_manual_result,
)

__all__ = [
    "AI_OBJECT_STATUSES",
    "AIOPS_RUN_FILTER_STATUSES",
    "REPORT_SAFE_INSUFFICIENT_EVIDENCE_STATUSES",
    "REPORT_SAFE_POLICY_BOUNDARY_STATUSES",
    "incident_status_from_report_status",
    "incident_status_from_runtime_status",
    "manual_action_required_from_change_execution",
    "manual_result_source_statuses",
    "report_requires_manual_action",
    "snapshot_status_from_event",
    "status_after_approved_run",
    "status_from_change_execution",
    "status_from_manual_result",
    "terminal_event_status",
    "trace_status_from_manual_result",
]

ALERT_FIRING_STATUSES = {"firing", "active", "triggered"}
ALERT_RESOLVED_STATUSES = {"resolved", "inactive", "ok", "closed"}
PRODUCTION_ENVIRONMENT_NAMES = {"prod", "production", "prd", "线上", "生产"}
PRODUCTION_ENVIRONMENT_PATTERN = re.compile(
    r"^(?:prod|production|prd)(?:$|[-_.:/\s].+)",
    re.IGNORECASE,
)
CHINESE_PRODUCTION_ENVIRONMENT_PATTERN = re.compile(r"^(?:线上|生产)(?:$|[-_.:/\s].+)")
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
    "resume_running": {
        "label": "审批恢复中",
        "tone": "warning",
        "phase": "approval",
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
    "partial_success": {
        "label": "部分完成",
        "tone": "warning",
        "phase": "change",
        "terminal": False,
    },
    "recovery_pending": {
        "label": "恢复待确认",
        "tone": "warning",
        "phase": "change",
        "terminal": False,
    },
    "rolled_back": {
        "label": "回滚已完成",
        "tone": "warning",
        "phase": "change",
        "terminal": True,
    },
    "rollback_failed": {
        "label": "回滚失败",
        "tone": "error",
        "phase": "change",
        "terminal": True,
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


def is_production_environment(value: Any) -> bool:
    """Return True for canonical production names and qualified production variants."""
    environment = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    if environment in PRODUCTION_ENVIRONMENT_NAMES:
        return True
    return bool(
        PRODUCTION_ENVIRONMENT_PATTERN.fullmatch(environment)
        or CHINESE_PRODUCTION_ENVIRONMENT_PATTERN.fullmatch(environment)
    )


def normalize_alert_status(value: Any, *, strict: bool = False) -> str:
    """Normalize external alert statuses into the canonical alert lifecycle."""
    status = str(value or "firing").strip().lower()
    if status in ALERT_RESOLVED_STATUSES:
        return "resolved"
    if status in ALERT_FIRING_STATUSES:
        return "firing"
    if strict:
        raise ValueError(f"unsupported alert status: {status or '<empty>'}")
    return "firing"


def incident_status_from_alert_status(status: str) -> str:
    """Map an alert status to the durable IncidentState lifecycle."""
    return "resolved" if normalize_alert_status(status) == "resolved" else "alert_firing"


def is_alert_mutable_incident_status(status: str) -> bool:
    """Return whether alert webhooks may still own this IncidentState status."""
    return status in ALERT_MUTABLE_INCIDENT_STATUSES


def is_stale_alert_event(existing: AlertEvent, incoming: AlertEvent) -> bool:
    """Return whether an incoming alert belongs to an older lifecycle generation."""
    existing_start = _as_utc(existing.starts_at)
    incoming_start = _as_utc(incoming.starts_at)
    if existing_start is not None and incoming_start is not None:
        if incoming_start != existing_start:
            return incoming_start < existing_start

        if existing.status == "resolved" and incoming.status == "firing":
            return True
        if existing.status == incoming.status == "resolved":
            existing_end = _as_utc(existing.ends_at)
            incoming_end = _as_utc(incoming.ends_at)
            return bool(existing_end and incoming_end and incoming_end < existing_end)
        return False

    existing_time = _alert_lifecycle_time(existing)
    incoming_time = _alert_lifecycle_time(incoming)
    return bool(existing_time and incoming_time and incoming_time < existing_time)


def is_new_alert_generation(existing: AlertEvent, incoming: AlertEvent) -> bool:
    """Return whether the incoming firing event starts a newer alert occurrence."""
    existing_start = _as_utc(existing.starts_at)
    incoming_start = _as_utc(incoming.starts_at)
    return bool(
        incoming.status == "firing"
        and existing_start is not None
        and incoming_start is not None
        and incoming_start > existing_start
    )


def alert_event_can_reopen_incident(existing: IncidentState, event: AlertEvent) -> bool:
    """Return whether a newer firing occurrence may reopen a terminal Incident."""
    existing_start = _parse_metadata_datetime((existing.metadata or {}).get("starts_at"))
    incoming_start = _as_utc(event.starts_at)
    return bool(
        event.status == "firing"
        and existing_start is not None
        and incoming_start is not None
        and incoming_start > existing_start
    )


def alert_auto_diagnosis_claim_is_active(
    metadata: dict[str, Any],
    *,
    now: datetime,
    lease_seconds: float,
) -> bool:
    """Return whether a durable diagnosis claim is still within its lease."""
    if metadata.get("alert_auto_diagnosis_status") not in ALERT_AUTO_DIAGNOSIS_ACTIVE_STATUSES:
        return False
    claimed_at = _parse_metadata_datetime(metadata.get("alert_auto_diagnosis_claimed_at"))
    if claimed_at is None:
        return False
    return (now - claimed_at).total_seconds() < lease_seconds


def _alert_lifecycle_time(event: AlertEvent) -> datetime | None:
    value = event.ends_at if event.status == "resolved" and event.ends_at else event.starts_at
    return _as_utc(value)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


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
    rejected_transition = not is_incident_status_transition_allowed(existing, update)
    preserve_existing_status = rejected_transition or should_preserve_existing_lifecycle_status(
        existing,
        update,
    )
    if rejected_transition:
        incoming_source = str((update.metadata or {}).get("source") or "unknown")
        metadata = dict(existing.metadata or {})
        metadata.update(
            {
                "source": (existing.metadata or {}).get("source") or incoming_source,
                "ignored_status_update": update.status,
                "ignored_status_update_source": incoming_source,
            }
        )
    replace_manual_action = is_change_lifecycle_state(update)
    return update.model_copy(
        update={
            "created_at": existing.created_at,
            "status": existing.status if preserve_existing_status else update.status,
            "status_reason": (
                existing.status_reason if preserve_existing_status else update.status_reason
            ),
            "title": (
                existing.title
                if preserve_existing_status
                else update.title
                if update.title != "AIOps incident"
                else existing.title
            ),
            "service_name": (
                existing.service_name
                if preserve_existing_status
                else (
                    update.service_name
                    if update.service_name != "unknown-service"
                    else existing.service_name
                )
            ),
            "severity": (
                existing.severity
                if preserve_existing_status
                else update.severity
                if update.severity != "unknown"
                else existing.severity
            ),
            "environment": (
                existing.environment
                if preserve_existing_status
                else update.environment
                if update.environment != "unknown"
                else existing.environment
            ),
            "summary": existing.summary
            if preserve_existing_status
            else update.summary or existing.summary,
            "root_cause": (
                existing.root_cause
                if preserve_existing_status
                else update.root_cause or existing.root_cause
            ),
            "trace_id": (
                existing.trace_id
                if preserve_existing_status
                else update.trace_id or existing.trace_id
            ),
            "session_id": (
                existing.session_id
                if preserve_existing_status
                else update.session_id or existing.session_id
            ),
            "report_id": (
                existing.report_id
                if preserve_existing_status
                else update.report_id or existing.report_id
            ),
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


def is_incident_status_transition_allowed(
    existing: IncidentState,
    update: IncidentState,
) -> bool:
    """Reject implicit reopen attempts from hard terminal Incident states."""
    if existing.status == update.status:
        return True
    if existing.status in APPROVAL_LIFECYCLE_STATUSES and (
        update.status in ACTIVE_DIAGNOSIS_STATUSES
        or (
            update.status == "failed"
            and (update.metadata or {}).get("source") == ALERT_AUTO_DIAGNOSIS_FAILURE_SOURCE
        )
    ):
        return False
    if is_change_lifecycle_state(existing) and not is_change_lifecycle_state(update):
        if existing.status in HARD_TERMINAL_INCIDENT_STATUSES and _is_explicit_alert_reopen(
            existing,
            update,
        ):
            return True
        return False
    if existing.status not in HARD_TERMINAL_INCIDENT_STATUSES:
        return True
    return _is_explicit_alert_reopen(existing, update) or _is_alert_failure_recovery(
        existing,
        update,
    )


def _is_explicit_alert_reopen(existing: IncidentState, update: IncidentState) -> bool:
    existing_metadata = existing.metadata or {}
    update_metadata = update.metadata or {}
    existing_start = _parse_metadata_datetime(existing_metadata.get("starts_at"))
    update_start = _parse_metadata_datetime(update_metadata.get("starts_at"))
    return (
        update_metadata.get("source") == "alertmanager"
        and update_metadata.get("alert_status") == "firing"
        and existing_start is not None
        and update_start is not None
        and update_start > existing_start
    )


def _is_alert_failure_recovery(existing: IncidentState, update: IncidentState) -> bool:
    existing_metadata = existing.metadata or {}
    update_metadata = update.metadata or {}
    return (
        existing.status == "failed"
        and existing_metadata.get("alert_auto_diagnosis_status") == "failed"
        and update_metadata.get("source") == "alertmanager"
        and update.status in {"alert_firing", "resolved"}
    )


def _parse_metadata_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return _as_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return None


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
