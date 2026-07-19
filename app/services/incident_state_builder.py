"""Builders for durable incident lifecycle state records."""

from __future__ import annotations

from typing import Any

from app.models.alert import AlertEvent
from app.models.approval import ApprovalRequest
from app.models.change_execution import ChangeExecution
from app.models.incident import Incident
from app.models.incident_state import IncidentState
from app.models.report import DiagnosisReport
from app.services.incident_lifecycle import (
    alert_event_can_reopen_incident,
    incident_status_from_alert_status,
    is_alert_mutable_incident_status,
    manual_action_required_from_change_execution,
    status_from_change_execution,
)
from app.utils.structured_data import as_dict as _as_dict


def build_incident_state_from_state(
    *,
    state: dict[str, Any],
    status: str,
    status_reason: str = "",
    session_id: str = "",
) -> IncidentState:
    """Build lifecycle state from a LangGraph-like runtime state dict."""
    incident = _as_dict(state.get("incident"))
    report = _as_dict(state.get("report"))
    pending_approval = _as_dict(state.get("pending_approval"))
    risk = _as_dict(state.get("risk_assessment"))
    incident_id = str(incident.get("incident_id") or state.get("incident_id") or "")
    report_id = str(report.get("report_id") or "") or None
    approval_decision = _as_dict(report.get("approval_decision"))
    latest_approval_id = (
        str(pending_approval.get("approval_id") or approval_decision.get("approval_id") or "")
        or None
    )
    approval_status = (
        str(pending_approval.get("status") or "")
        or str(report.get("approval_status") or "")
        or "not_required"
    )
    manual_action_required = bool(
        pending_approval
        or report.get("manual_action_required")
        or risk.get("need_approval")
        or status in {"waiting_approval", "approval_approved", "approval_rejected"}
    )
    return IncidentState(
        incident_id=incident_id,
        status=status,
        status_reason=status_reason,
        title=str(report.get("title") or incident.get("title") or "AIOps incident"),
        service_name=str(
            report.get("service_name") or incident.get("service_name") or "unknown-service"
        ),
        severity=str(report.get("severity") or incident.get("severity") or "unknown"),
        environment=str(report.get("environment") or incident.get("environment") or "unknown"),
        summary=str(report.get("summary") or incident.get("symptom") or ""),
        root_cause=str(report.get("root_cause") or ""),
        trace_id=str(state.get("trace_id") or report.get("trace_id") or ""),
        session_id=str(session_id or state.get("session_id") or ""),
        report_id=report_id,
        approval_status=approval_status,
        latest_approval_id=latest_approval_id,
        manual_action_required=manual_action_required,
        metadata={
            "source": "aiops_state",
            "risk_policy": risk.get("policy"),
            "risk_level": risk.get("risk_level"),
            "node_name": state.get("node_name"),
        },
    )


def build_incident_state_from_incident(
    *,
    incident: Incident,
    session_id: str,
    trace_id: str,
    status: str = "diagnosing",
    status_reason: str = "",
) -> IncidentState:
    """Build lifecycle state from the structured incident input."""
    return IncidentState(
        incident_id=incident.incident_id,
        status=status,
        status_reason=status_reason,
        title=incident.title,
        service_name=incident.service_name,
        severity=incident.severity,
        environment=incident.environment,
        summary=incident.symptom,
        trace_id=trace_id,
        session_id=session_id,
        metadata={"source": "incident_input"},
    )


def build_incident_state_from_report(
    *,
    report: DiagnosisReport,
    status: str | None = None,
    status_reason: str = "",
    session_id: str = "",
) -> IncidentState:
    """Build lifecycle state from a diagnosis report."""
    metadata: dict[str, Any] = {"source": "diagnosis_report"}
    change_executions = [item for item in report.change_executions if isinstance(item, dict)]
    if change_executions:
        latest_execution = change_executions[-1]
        metadata.update(
            {
                "change_execution_id": latest_execution.get("change_execution_id"),
                "change_plan_id": latest_execution.get("change_plan_id"),
                "raw_status": latest_execution.get("status"),
            }
        )
    return IncidentState(
        incident_id=report.incident_id,
        status=status or report.status,
        status_reason=status_reason,
        title=report.title,
        service_name=report.service_name,
        severity=report.severity,
        environment=report.environment,
        summary=report.summary,
        root_cause=report.root_cause,
        trace_id=report.trace_id,
        session_id=session_id,
        report_id=report.report_id,
        approval_status=report.approval_status,
        latest_approval_id=str(report.approval_decision.get("approval_id") or "") or None,
        manual_action_required=report.manual_action_required,
        metadata=metadata,
    )


def build_incident_state_from_approval(
    *,
    approval: ApprovalRequest,
    status: str,
    status_reason: str = "",
) -> IncidentState:
    """Build lifecycle state from an approval request update."""
    return IncidentState(
        incident_id=approval.incident_id,
        status=status,
        status_reason=status_reason or approval.reason,
        title="AIOps incident",
        trace_id=str(approval.metadata.get("trace_id") or ""),
        session_id=str(approval.metadata.get("session_id") or ""),
        approval_status=approval.status,
        latest_approval_id=approval.approval_id,
        manual_action_required=True,
        metadata={
            "source": "approval",
            "action": approval.action,
            "risk_level": approval.risk_level,
            "tool_name": approval.tool_name,
        },
    )


def build_incident_state_from_change_execution(execution: ChangeExecution) -> IncidentState:
    """Build lifecycle state from a safe change workflow update."""
    return IncidentState(
        incident_id=execution.incident_id,
        status=status_from_change_execution(execution.status),
        status_reason=f"Safe change workflow status={execution.status}",
        trace_id=execution.trace_id,
        report_id=None,
        approval_status="approved",
        latest_approval_id=execution.approval_id,
        manual_action_required=manual_action_required_from_change_execution(
            execution.status,
            fallback=True,
        ),
        metadata={
            "source": "change_execution",
            "change_execution_id": execution.change_execution_id,
            "change_plan_id": execution.change_plan_id,
            "mode": execution.mode,
            "raw_status": execution.status,
        },
    )


def build_incident_state_from_alert(
    *,
    event: AlertEvent,
    incident: Incident,
    existing: IncidentState | None = None,
) -> IncidentState:
    """Build lifecycle state from a normalized external alert event."""
    desired_status = incident_status_from_alert_status(event.status)
    status_reason = (
        f"Alertmanager webhook status={event.status}, "
        f"alertname={event.alertname}, fingerprint={event.fingerprint}"
    )
    if (
        existing is not None
        and not is_alert_mutable_incident_status(existing.status)
        and not _alert_can_override_auto_diagnosis_failure(existing)
        and not alert_event_can_reopen_incident(existing, event)
    ):
        status = existing.status
        reason = existing.status_reason
        preserved_existing = True
    else:
        status = desired_status
        reason = status_reason
        preserved_existing = False
    preserved_state = existing if preserved_existing else None
    metadata = dict(existing.metadata if existing else {})
    metadata.update(
        {
            "source": event.source or "alertmanager",
            "alert_fingerprint": event.fingerprint,
            "alert_status": event.status,
            "alertname": event.alertname,
            "labels": event.labels,
            "annotations": event.annotations,
            "starts_at": event.starts_at.isoformat() if event.starts_at else "",
            "ends_at": event.ends_at.isoformat() if event.ends_at else "",
        }
    )
    if preserved_existing and existing is not None:
        metadata["preserved_incident_status"] = existing.status

    return IncidentState(
        incident_id=event.incident_id,
        status=status,
        status_reason=reason,
        title=preserved_state.title
        if preserved_state
        else f"{event.service_name} {event.alertname}",
        service_name=preserved_state.service_name if preserved_state else event.service_name,
        severity=preserved_state.severity if preserved_state else event.severity,
        environment=preserved_state.environment if preserved_state else event.environment,
        summary=preserved_state.summary if preserved_state else _alert_summary(event),
        root_cause=preserved_state.root_cause if preserved_state else "",
        trace_id=existing.trace_id if existing else "",
        session_id=existing.session_id if existing else "",
        report_id=existing.report_id if existing else None,
        approval_status=existing.approval_status if existing else "not_required",
        latest_approval_id=existing.latest_approval_id if existing else None,
        manual_action_required=existing.manual_action_required if existing else False,
        metadata=metadata,
    )


def _alert_can_override_auto_diagnosis_failure(existing: IncidentState) -> bool:
    """Allow alert lifecycle updates to recover a state failed only by auto diagnosis."""
    metadata = dict(existing.metadata or {})
    return existing.status == "failed" and metadata.get("alert_auto_diagnosis_status") == "failed"


def _alert_summary(event: AlertEvent) -> str:
    parts = [event.summary]
    if event.description and event.description != event.summary:
        parts.append(event.description)
    return "；".join(item for item in parts if item)
