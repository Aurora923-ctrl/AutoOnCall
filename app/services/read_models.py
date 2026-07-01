"""Read-model builders for AIOps run and incident overview APIs."""

from __future__ import annotations

from collections import Counter
from typing import Any, cast

from app.models.aiops_session import AIOpsSessionSnapshot
from app.models.approval import ApprovalRequest
from app.models.incident_state import IncidentState
from app.models.report import DiagnosisReport
from app.models.trace import TraceEvent
from app.services.incident_lifecycle import status_metadata


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
        "plan_step_count": len(snapshot.plan or snapshot.current_plan),
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
    post_approval_statuses = {
        "approval_resumed",
        "resolved",
        "rollback_recommended",
        "precheck_failed",
        "dry_run_failed",
        "manual_result_required",
        "manual_result_recorded",
        "closed",
        "failed",
    }
    if approval_status == "approved":
        return report_status if report_status in post_approval_statuses else "approval_approved"
    if approval_status == "rejected":
        return "approval_rejected"
    if approval_status == "cancelled":
        return "approval_cancelled"

    if report_status:
        return report_status
    return status


def build_incident_overview(
    incident_id: str,
    report: DiagnosisReport | None,
    events: list[TraceEvent],
    approvals: list[ApprovalRequest],
    state: IncidentState | None = None,
) -> dict[str, Any]:
    """Build an API-friendly incident overview."""
    sorted_events = sorted(events, key=lambda item: item.created_at)
    sorted_approvals = sorted(approvals, key=lambda item: item.created_at)
    latest_event = sorted_events[-1] if sorted_events else None
    latest_approval = sorted_approvals[-1] if sorted_approvals else None
    trace_counts = Counter(event.event_type for event in events)
    trace_id = (
        report.trace_id
        if report and report.trace_id
        else latest_event.trace_id if latest_event else state.trace_id if state else ""
    )
    updated_at = latest_timestamp(report, latest_event, latest_approval, state)
    effective_approval_status = approval_status_from_approvals(approvals)
    effective_status = effective_incident_status(state, report, approvals)
    manual_action_required = (
        state.manual_action_required
        if state
        else report.manual_action_required if report else bool(approvals)
    ) or effective_status in {"waiting_approval", "approval_approved", "approval_rejected"}
    title = state.title if state else report.title if report else "AIOps incident"
    service_name = (
        state.service_name if state else report.service_name if report else "unknown-service"
    )
    severity = state.severity if state else report.severity if report else "unknown"
    environment = state.environment if state else report.environment if report else "unknown"
    summary = state.summary if state and state.summary else report.summary if report else ""
    root_cause = (
        state.root_cause if state and state.root_cause else report.root_cause if report else ""
    )

    return {
        "incident_id": incident_id,
        "trace_id": trace_id,
        "status": effective_status,
        "status_metadata": status_metadata(effective_status),
        "status_reason": state.status_reason if state else "",
        "title": title,
        "service_name": service_name,
        "severity": severity,
        "environment": environment,
        "summary": summary,
        "root_cause": root_cause,
        "manual_action_required": manual_action_required,
        "approval_status": (
            effective_approval_status
            if approvals
            else (
                state.approval_status
                if state
                else report.approval_status if report else "not_required"
            )
        ),
        "session_id": state.session_id if state else "",
        "lifecycle": state.model_dump(mode="json") if state else None,
        "trace_summary": {
            "event_count": len(events),
            "by_type": dict(trace_counts),
            "latest_event_type": latest_event.event_type if latest_event else "",
            "latest_event_status": latest_event.status if latest_event else "",
        },
        "approval_summary": build_approval_summary(approvals, latest_approval),
        "report": report.model_dump(mode="json") if report else None,
        "diagnosis_chain": build_diagnosis_chain(report, sorted_events, sorted_approvals),
        "links": {
            "trace": f"/api/incidents/{incident_id}/trace",
            "report": f"/api/incidents/{incident_id}/report",
            "approval": f"/api/incidents/{incident_id}/approval",
        },
        "updated_at": updated_at,
    }


def latest_timestamp(
    report: DiagnosisReport | None,
    latest_event: TraceEvent | None,
    latest_approval: ApprovalRequest | None,
    state: IncidentState | None = None,
) -> str:
    """Return the newest timestamp across the read-model sources."""
    timestamps = []
    if state:
        timestamps.append(state.updated_at)
    if report:
        timestamps.append(report.created_at)
    if latest_event:
        timestamps.append(latest_event.created_at)
    if latest_approval:
        timestamps.append(latest_approval.decided_at or latest_approval.created_at)
    return max(timestamps).isoformat() if timestamps else ""


def infer_status_from_approvals(approvals: list[ApprovalRequest]) -> str:
    """Infer an incident status when only approval records exist."""
    if any(approval.status == "pending" for approval in approvals):
        return "waiting_approval"
    if any(approval.status == "approved" for approval in approvals):
        return "approval_approved"
    if any(approval.status == "rejected" for approval in approvals):
        return "approval_rejected"
    if approvals:
        return "approval_decided"
    return "investigating"


def approval_status_from_approvals(approvals: list[ApprovalRequest]) -> str:
    """Return the effective approval status for a group of approval requests."""
    if any(approval.status == "pending" for approval in approvals):
        return "pending"
    if approvals:
        latest = latest_approval_request(approvals)
        return latest.status if latest else "not_required"
    return "not_required"


def effective_incident_status(
    state: IncidentState | None,
    report: DiagnosisReport | None,
    approvals: list[ApprovalRequest],
) -> str:
    """Resolve the user-facing status for an incident overview."""
    if state is not None:
        return state.status
    approval_status = approval_status_from_approvals(approvals)
    if approval_status == "approved":
        return "approval_approved"
    if approval_status == "rejected":
        return "approval_rejected"
    if approval_status == "pending":
        return "waiting_approval"
    return report.status if report else infer_status_from_approvals(approvals)


def build_diagnosis_chain(
    report: DiagnosisReport | None,
    events: list[TraceEvent],
    approvals: list[ApprovalRequest],
) -> dict[str, Any]:
    """Return a frontend-friendly explanation chain for one incident."""
    report_payload = report.model_dump(mode="json") if report else {}
    evidence = report_payload.get("evidence") or []
    tool_calls = report_payload.get("tool_calls") or []
    return {
        "plan": extract_plan_steps(events, report_payload),
        "steps": extract_execution_steps(events),
        "tool_calls": tool_calls,
        "dependency_signals": report_payload.get("dependency_signals")
        or extract_dependency_signals(
            evidence,
            tool_calls,
        ),
        "evidence": evidence,
        "confirmed_facts": report_payload.get("confirmed_facts") or [],
        "inferred_conclusions": report_payload.get("inferred_conclusions") or [],
        "hypothesis_ranking": report_payload.get("hypothesis_ranking") or [],
        "selected_root_cause_id": report_payload.get("selected_root_cause_id") or "",
        "change_plan": report_payload.get("change_plan") or {},
        "uncertainties": report_payload.get("uncertainties") or [],
        "next_steps": report_payload.get("next_steps") or [],
        "confidence": report_payload.get("confidence", 0.0),
        "confidence_reason": report_payload.get("confidence_reason", ""),
        "data_sources": summarize_data_sources(evidence, tool_calls),
        "approvals": [approval.model_dump(mode="json") for approval in approvals],
    }


def extract_plan_steps(
    events: list[TraceEvent], report_payload: dict[str, Any]
) -> list[dict[str, Any]]:
    """Extract the planned steps from trace events or report timeline."""
    for event in events:
        if event.node_name != "planner":
            continue
        raw_plan = _as_list(event.tool_result) or _as_list(event.metadata.get("current_plan"))
        if raw_plan:
            return [compact_plan_step(item) for item in raw_plan]
    timeline = _as_list(report_payload.get("timeline"))
    return [
        {
            "step_id": str(item.get("step_id") or ""),
            "tool_name": str(item.get("tool_name") or item.get("node_name") or ""),
            "purpose": str(item.get("summary") or ""),
            "status": str(item.get("status") or "unknown"),
        }
        for item in timeline
        if isinstance(item, dict) and item.get("step_id")
    ]


def extract_execution_steps(events: list[TraceEvent]) -> list[dict[str, Any]]:
    """Extract execution timeline rows from trace events."""
    steps = []
    for event in events:
        if event.event_type not in {"node", "tool_call", "risk_decision", "approval_request"}:
            continue
        steps.append(
            {
                "event_id": event.event_id,
                "event_type": event.event_type,
                "node_name": event.node_name,
                "step_id": event.step_id,
                "tool_name": event.tool_name,
                "status": event.status,
                "summary": event.output_summary or event.error_message or event.input_summary,
                "data_source": event.metadata.get("data_source", "unknown"),
                "latency_ms": event.latency_ms,
                "created_at": event.created_at.isoformat(),
            }
        )
    return steps


def compact_plan_step(item: Any) -> dict[str, Any]:
    """Normalize a raw plan step into the compact read-model shape."""
    if not isinstance(item, dict):
        return {"purpose": str(item), "status": "pending"}
    return {
        "step_id": str(item.get("step_id") or ""),
        "tool_name": str(item.get("tool_name") or "manual_analysis"),
        "purpose": str(item.get("purpose") or item.get("expected_evidence") or ""),
        "expected_evidence": str(item.get("expected_evidence") or ""),
        "risk_level": str(item.get("risk_level") or "low"),
        "status": str(item.get("status") or "pending"),
    }


def summarize_data_sources(
    evidence: list[Any],
    tool_calls: list[Any],
) -> dict[str, Any]:
    """Summarize backend data sources used by evidence and tool calls."""
    counter: Counter[str] = Counter()
    has_mock = False
    has_not_configured = False
    for item in evidence + tool_calls:
        if not isinstance(item, dict):
            continue
        source = str(item.get("data_source") or "unknown")
        counter[source] += 1
        has_mock = has_mock or source == "mock"
        has_not_configured = has_not_configured or source == "not_configured"
    return {
        "by_source": dict(counter),
        "has_mock": has_mock,
        "has_not_configured": has_not_configured,
    }


def extract_dependency_signals(
    evidence: list[Any],
    tool_calls: list[Any],
) -> list[dict[str, Any]]:
    """Build dependency-signal cards from trace and message-queue tool calls."""
    evidence_by_step_tool = {
        (str(item.get("step_id") or ""), str(item.get("source_tool") or "")): item
        for item in evidence
        if isinstance(item, dict)
        and str(item.get("evidence_type") or "") in {"trace", "message_queue"}
    }
    signals: list[dict[str, Any]] = []
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        tool_name = str(call.get("tool_name") or "")
        if tool_name not in {"query_traces", "query_message_queue_status"}:
            continue
        evidence_item = evidence_by_step_tool.get((str(call.get("step_id") or ""), tool_name), {})
        signals.append(
            {
                "step_id": call.get("step_id", ""),
                "tool_name": tool_name,
                "domain": "tracing" if tool_name == "query_traces" else "message_queue",
                "backend": call.get("data_source") or evidence_item.get("data_source") or "unknown",
                "status": call.get("status", "unknown"),
                "data_source": call.get("data_source", "unknown"),
                "latency_ms": call.get("latency_ms", 0.0),
                "summary": call.get("output_summary")
                or evidence_item.get("summary")
                or call.get("error_message")
                or "",
                "stance": evidence_item.get("stance", "neutral"),
                "confidence": evidence_item.get("confidence", 0.0),
                "confidence_reason": evidence_item.get("confidence_reason", ""),
            }
        )
    return signals


def build_run_trace_summary(
    events: list[TraceEvent],
    latest_event: TraceEvent | None,
) -> dict[str, Any]:
    """Build a compact trace summary for a run detail payload."""
    counts = Counter(event.event_type for event in events)
    return {
        "event_count": len(events),
        "by_type": dict(counts),
        "latest_event_type": latest_event.event_type if latest_event else "",
        "latest_event_status": latest_event.status if latest_event else "",
        "latest": latest_event.model_dump(mode="json") if latest_event else None,
    }


def list_run_trace_events(snapshot: AIOpsSessionSnapshot, trace_service: Any) -> list[TraceEvent]:
    """Load trace events for one run, preferring trace_id and falling back to incident_id."""
    if snapshot.trace_id and snapshot.trace_id != "trace-unknown":
        events = trace_service.list_events(trace_id=snapshot.trace_id)
        if events:
            return cast(list[TraceEvent], events)
    return cast(list[TraceEvent], trace_service.list_events(incident_id=snapshot.incident_id))


def build_approval_summary(
    approvals: list[ApprovalRequest],
    latest_approval: ApprovalRequest | None = None,
) -> dict[str, Any]:
    """Build a shared approval summary for run and incident read models."""
    latest = latest_approval or latest_approval_request(approvals)
    counts = Counter(approval.status for approval in approvals)
    if any(approval.status == "pending" for approval in approvals):
        effective_status = "pending"
    elif latest:
        effective_status = latest.status
    else:
        effective_status = "not_required"
    return {
        "total": len(approvals),
        "status": effective_status,
        "by_status": dict(counts),
        "latest": latest.model_dump(mode="json") if latest else None,
    }


def latest_trace_event(events: list[TraceEvent]) -> TraceEvent | None:
    """Return the newest trace event by creation time."""
    if not events:
        return None
    return sorted(events, key=lambda event: event.created_at)[-1]


def latest_approval_request(approvals: list[ApprovalRequest]) -> ApprovalRequest | None:
    """Return the newest approval by decision time or creation time."""
    if not approvals:
        return None
    return sorted(approvals, key=lambda approval: approval.decided_at or approval.created_at)[-1]


def build_run_links(session_id: str, incident_id: str) -> dict[str, str]:
    """Return API links for one run and its incident-scoped resources."""
    links = {"run": f"/api/aiops/runs/{session_id}"}
    if is_known_incident_id(incident_id):
        links.update(
            {
                "incident": f"/api/incidents/{incident_id}",
                "trace": f"/api/incidents/{incident_id}/trace",
                "report": f"/api/incidents/{incident_id}/report",
                "approval": f"/api/incidents/{incident_id}/approval",
                "changes": f"/api/incidents/{incident_id}/changes",
            }
        )
    return links


def is_known_incident_id(incident_id: str | None) -> bool:
    """Return True when a run has a durable incident identity."""
    return bool(incident_id and incident_id != "incident-unknown")


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []
