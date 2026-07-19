"""Read-model builders for durable AIOps diagnosis runs."""

from __future__ import annotations

from typing import Any

from app.integrations.base import public_adapter_failure_message
from app.models.aiops_session import AIOpsSessionSnapshot
from app.models.approval import ApprovalRequest
from app.models.report import DiagnosisReport
from app.models.trace import TraceEvent
from app.services.aiops_progress import build_progress_payload
from app.services.aiops_read_models.common import (
    build_approval_summary,
    build_run_links,
    build_run_trace_summary,
    latest_approval_request,
    latest_trace_event,
)
from app.services.incident_lifecycle import status_after_approved_run, status_metadata
from app.utils.redaction import redact_sensitive_data


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
    report_payload = _public_run_value(
        report.model_dump(mode="json") if report else snapshot.report
    )
    latest_event = latest_trace_event(events)
    latest_approval = latest_approval_request(approvals)
    progress = progress_for_snapshot(snapshot, status=status, report_payload=report_payload)

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
        "incident": _public_run_value(snapshot.incident),
        "input": _public_run_value(snapshot.input),
        "plan": _public_run_items(snapshot.plan),
        "current_plan": _public_run_items(snapshot.current_plan),
        "executed_steps": _public_run_items(snapshot.executed_steps),
        "past_steps": _public_run_items(snapshot.past_steps),
        "tool_call_records": _public_run_items(snapshot.tool_call_records),
        "gathered_evidence": _public_run_items(snapshot.gathered_evidence),
        "hypotheses": _public_strings(snapshot.hypotheses),
        "evidence_analysis": _public_run_value(snapshot.evidence_analysis),
        "risk_assessment": _public_run_value(snapshot.risk_assessment),
        "pending_approval": _public_run_value(snapshot.pending_approval),
        "change_plan": _public_run_value(snapshot.change_plan),
        "final_diagnosis": _public_run_value(snapshot.final_diagnosis),
        "remediation_suggestion": _public_run_value(snapshot.remediation_suggestion),
        "report_id": report.report_id if report else snapshot.final_report_id,
        "report": report_payload,
        "has_report": bool(report_payload),
        "progress": _public_run_value(progress),
        "progress_cursor": progress.get("cursor") or snapshot.progress_cursor,
        "progress_events": _public_run_items(snapshot.progress_events),
        "errors": _public_strings(snapshot.errors),
        "warnings": _public_strings(snapshot.warnings),
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
    progress = progress_for_snapshot(snapshot, status=status, report_payload=report_payload)
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
        "progress": progress,
        "progress_cursor": progress.get("cursor") or snapshot.progress_cursor,
        "links": build_run_links(snapshot.session_id, snapshot.incident_id),
    }


def progress_for_snapshot(
    snapshot: AIOpsSessionSnapshot,
    *,
    status: str | None = None,
    report_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return stored progress or derive a compatible snapshot progress payload."""
    resolved_status = status or snapshot.status or "running"
    if snapshot.progress:
        progress = dict(snapshot.progress)
        progress["status"] = resolved_status
        progress["phase"] = _phase_from_snapshot(snapshot, resolved_status)
        if report_payload:
            progress["report_status"] = str(
                report_payload.get("status") or progress.get("report_status") or resolved_status
            )
        return progress

    state = snapshot.to_state()
    if report_payload:
        state["report"] = report_payload
    return build_progress_payload(
        state,
        phase=_phase_from_snapshot(snapshot, resolved_status),
        node_name=snapshot.node_name or "workflow",
        cursor=snapshot.progress_cursor or f"{snapshot.session_id}:snapshot",
        status=resolved_status,
        message=f"Recovered AIOps run at node={snapshot.node_name or 'workflow'}",
    )


def _phase_from_snapshot(snapshot: AIOpsSessionSnapshot, status: str) -> str:
    if status in {"failed"}:
        return "error"
    if status in {
        "completed",
        "approval_resumed",
        "approval_rejected",
        "approval_cancelled",
        "blocked",
        "escalated",
    }:
        return "complete"
    if status == "waiting_approval" or snapshot.pending_approval:
        return "approval"
    if status == "resume_running":
        return "reporting"
    return {
        "planner": "planning",
        "executor": "executing",
        "replanner": "replanning",
        "report_generator": "reporting",
    }.get(snapshot.node_name or "", "workflow")


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


def _public_run_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_public_run_value(item) for item in items]


def _public_strings(items: list[str]) -> list[str]:
    return [str(redact_sensitive_data(item)) for item in items]


def _public_run_value(value: Any) -> Any:
    redacted = redact_sensitive_data(value)
    if isinstance(redacted, list):
        return [_public_run_value(item) for item in redacted]
    if not isinstance(redacted, dict):
        return redacted
    sanitized = {key: _public_run_value(item) for key, item in redacted.items()}
    sanitized.pop("endpoint", None)
    if "partial_errors" in sanitized:
        sanitized["partial_errors"] = _public_partial_errors(sanitized.get("partial_errors"))
    if "fallback_errors" in sanitized:
        sanitized["fallback_errors"] = _public_partial_errors(sanitized.get("fallback_errors"))
    if "processlist_sample" in sanitized:
        sanitized["processlist_sample"] = _public_processlist_sample(
            sanitized.get("processlist_sample")
        )
    raw_data = sanitized.get("raw_data")
    if isinstance(raw_data, dict):
        sanitized["raw_data"] = _public_run_value(raw_data)
    output = sanitized.get("output")
    if isinstance(output, dict):
        sanitized["output"] = _public_run_value(output)
    return sanitized


def _public_processlist_sample(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
        {
            "Command": item.get("Command"),
            "Time": item.get("Time"),
            "State": item.get("State"),
            "has_statement": bool(item.get("Info") or item.get("has_statement")),
        }
        for item in value[:5]
        if isinstance(item, dict)
    ]


def _public_partial_errors(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    errors: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        error_type = str(item.get("error_type") or "adapter_error")
        errors.append(
            {
                key: item.get(key)
                for key in ("query", "command", "tool_name", "source")
                if item.get(key) is not None
            }
            | {
                "error_type": error_type,
                "error_message": public_adapter_failure_message(error_type),
            }
        )
    return errors


def effective_run_status(
    snapshot: AIOpsSessionSnapshot,
    report: DiagnosisReport | None,
    approvals: list[ApprovalRequest],
) -> str:
    """Resolve the user-facing status for a diagnosis run."""
    status = snapshot.status or "running"
    if status == "failed":
        return status
    if any(approval.status == "pending" for approval in approvals):
        return "waiting_approval"
    if status == "resume_running":
        return status

    latest_approval = latest_approval_request(approvals)
    approval_status = latest_approval.status if latest_approval else ""
    report_status = report.status if report and report.status else ""
    if status == "running" and not report_status and not approval_status:
        return status
    if approval_status == "approved":
        return status_after_approved_run(report_status)
    if approval_status == "rejected":
        return "approval_rejected"
    if approval_status == "cancelled":
        return "approval_cancelled"

    if report_status:
        return report_status
    return status
