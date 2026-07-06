"""Read-model builders for durable AIOps diagnosis runs."""

from __future__ import annotations

from typing import Any

from app.models.aiops_session import AIOpsSessionSnapshot
from app.models.approval import ApprovalRequest
from app.models.report import DiagnosisReport
from app.models.trace import TraceEvent
from app.services.aiops_read_models.common import (
    build_approval_summary,
    build_run_links,
    build_run_trace_summary,
    latest_approval_request,
    latest_trace_event,
)
from app.services.incident_lifecycle import status_after_approved_run, status_metadata


def build_aiops_run_status(
    snapshot: AIOpsSessionSnapshot,
    *,
    events: list[TraceEvent],
    approvals: list[ApprovalRequest],
    report: DiagnosisReport | None,
) -> dict[str, Any]:
    """Build the detailed durable diagnosis run read model."""
    incident_id = snapshot.incident_id
    status = effective_run_status(snapshot, report, approvals)
    report_payload = report.model_dump(mode="json") if report else snapshot.report
    latest_event = latest_trace_event(events)
    latest_approval = latest_approval_request(approvals)

    return {
        "available": True,
        "diagnosis_run_id": snapshot.session_id,
        "session_id": snapshot.session_id,
        "incident_id": incident_id,
        "trace_id": snapshot.trace_id,
        "status": status,
        "status_metadata": status_metadata(status),
        "node_name": snapshot.node_name,
        "started_at": snapshot.created_at.isoformat(),
        "updated_at": snapshot.updated_at.isoformat(),
        "incident": snapshot.incident,
        "input": snapshot.input,
        "plan": snapshot.plan,
        "current_plan": snapshot.current_plan,
        "executed_steps": snapshot.executed_steps,
        "past_steps": snapshot.past_steps,
        "tool_call_records": snapshot.tool_call_records,
        "gathered_evidence": snapshot.gathered_evidence,
        "hypotheses": snapshot.hypotheses,
        "evidence_analysis": snapshot.evidence_analysis,
        "risk_assessment": snapshot.risk_assessment,
        "pending_approval": snapshot.pending_approval,
        "change_plan": snapshot.change_plan,
        "final_diagnosis": snapshot.final_diagnosis,
        "remediation_suggestion": snapshot.remediation_suggestion,
        "report_id": report.report_id if report else snapshot.final_report_id,
        "report": report_payload,
        "has_report": bool(report_payload),
        "errors": snapshot.errors,
        "warnings": snapshot.warnings,
        "trace_summary": build_run_trace_summary(events, latest_event),
        "approval_summary": build_approval_summary(approvals, latest_approval),
        "links": build_run_links(snapshot.session_id, incident_id),
    }


def build_aiops_run_summary(
    snapshot: AIOpsSessionSnapshot,
    *,
    approvals: list[ApprovalRequest],
    report: DiagnosisReport | None,
) -> dict[str, Any]:
    """Build the compact diagnosis run summary used by history views."""
    incident = snapshot.incident or {}
    title = str(incident.get("title") or snapshot.incident_id)
    service_name = str(incident.get("service_name") or "unknown-service")
    severity = str(incident.get("severity") or "unknown")
    environment = str(incident.get("environment") or "unknown")
    symptom = str(incident.get("symptom") or "")
    latest_approval = latest_approval_request(approvals)
    approval_summary = build_approval_summary(approvals, latest_approval)
    status = effective_run_status(snapshot, report, approvals)
    report_payload = report.model_dump(mode="json") if report else snapshot.report or {}
    report_id = (
        report.report_id
        if report
        else snapshot.final_report_id or str(report_payload.get("report_id") or "")
    )
    pending_approval = snapshot.pending_approval or None
    approval_status = (
        str(approval_summary.get("status") or "not_required")
        if approvals
        else str((pending_approval or {}).get("status") or "not_required")
    )
    has_pending_approval = any(approval.status == "pending" for approval in approvals)
    if not approvals:
        has_pending_approval = pending_approval is not None

    return {
        "diagnosis_run_id": snapshot.session_id,
        "session_id": snapshot.session_id,
        "incident_id": snapshot.incident_id,
        "trace_id": snapshot.trace_id,
        "status": status,
        "status_metadata": status_metadata(status),
        "node_name": snapshot.node_name,
        "title": title,
        "service_name": service_name,
        "severity": severity,
        "environment": environment,
        "summary": symptom or snapshot.final_diagnosis or snapshot.input,
        "started_at": snapshot.created_at.isoformat(),
        "updated_at": snapshot.updated_at.isoformat(),
        "approval_status": approval_status,
        "has_pending_approval": has_pending_approval,
        "has_report": bool(report_payload or report_id),
        "report_id": report_id or None,
        "plan_step_count": planned_step_count(snapshot),
        "completed_step_count": len(snapshot.past_steps),
        "evidence_count": len(snapshot.gathered_evidence),
        "tool_call_count": len(snapshot.tool_call_records),
        "error_count": len(snapshot.errors),
        "warning_count": len(snapshot.warnings),
        "links": build_run_links(snapshot.session_id, snapshot.incident_id),
    }


def filter_aiops_run_summaries(
    items: list[dict[str, Any]],
    *,
    status: str | None = None,
    service_name: str | None = None,
) -> list[dict[str, Any]]:
    """Apply history-list filters after compact run summaries are built."""
    status_filter = (status or "").strip()
    service_filter = (service_name or "").strip().lower()
    filtered = items
    if status_filter:
        filtered = [item for item in filtered if str(item.get("status") or "") == status_filter]
    if service_filter:
        filtered = [
            item
            for item in filtered
            if service_filter in str(item.get("service_name") or "").lower()
        ]
    return filtered


def planned_step_count(snapshot: AIOpsSessionSnapshot) -> int:
    """Infer the original plan size from durable remaining and executed step state."""
    executed_identity_steps = (
        [*snapshot.executed_steps, *snapshot.past_steps]
        if snapshot.executed_steps
        else snapshot.past_steps
    )
    completed_count = len(snapshot.past_steps)
    executed_count = max(len(snapshot.executed_steps), completed_count)

    if snapshot.current_plan:
        if _plan_contains_executed_step(snapshot.current_plan, executed_identity_steps):
            return max(len(snapshot.current_plan), executed_count)
        return executed_count + len(snapshot.current_plan)

    if snapshot.plan:
        if _plan_contains_executed_step(snapshot.plan, executed_identity_steps):
            return max(len(snapshot.plan), executed_count)
        if executed_count:
            return executed_count + len(snapshot.plan)
        return len(snapshot.plan)

    return executed_count


def _plan_contains_executed_step(
    plan: list[dict[str, Any]],
    executed_steps: list[dict[str, Any]],
) -> bool:
    plan_identities = {_step_identity(item) for item in plan}
    plan_identities.discard("")
    if not plan_identities:
        return False
    return any(_step_identity(item) in plan_identities for item in executed_steps)


def _step_identity(step: Any) -> str:
    if not isinstance(step, dict):
        return str(step or "")

    if "step" in step and len(step) != 1:
        return _step_identity(step.get("step"))
    if "value" in step and len(step) == 1:
        return _step_identity(step.get("value"))

    step_id = step.get("step_id")
    if step_id:
        return f"step_id:{step_id}"

    tool_name = step.get("tool_name")
    purpose = step.get("purpose") or step.get("action") or step.get("summary")
    if tool_name or purpose:
        return f"tool:{tool_name}|purpose:{purpose}"

    return str(step or "")


def effective_run_status(
    snapshot: AIOpsSessionSnapshot,
    report: DiagnosisReport | None,
    approvals: list[ApprovalRequest],
) -> str:
    """Resolve the user-facing status for a diagnosis run."""
    status = snapshot.status or "running"
    if any(approval.status == "pending" for approval in approvals):
        return "waiting_approval"

    latest_approval = latest_approval_request(approvals)
    approval_status = latest_approval.status if latest_approval else ""
    report_status = report.status if report and report.status else ""
    if approval_status == "approved":
        return status_after_approved_run(report_status)
    if approval_status == "rejected":
        return "approval_rejected"
    if approval_status == "cancelled":
        return "approval_cancelled"

    if report_status:
        return report_status
    return status
