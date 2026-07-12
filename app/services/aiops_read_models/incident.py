"""Incident overview and diagnosis-chain read models."""

from __future__ import annotations

from collections import Counter
from typing import Any

from app.models.approval import ApprovalRequest
from app.models.incident_state import IncidentState
from app.models.report import DiagnosisReport
from app.models.trace import TraceEvent
from app.services.aiops_read_models.common import (
    _as_list,
    approval_status_from_approvals,
    build_approval_summary,
    effective_incident_status,
    latest_timestamp,
)
from app.services.incident_lifecycle import status_metadata


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
    """Advanced trace/message-queue dependency cards are not part of the mainline."""
    return []
