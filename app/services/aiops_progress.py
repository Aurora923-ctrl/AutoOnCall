"""Stable progress payloads for AIOps diagnosis streams and recovery snapshots."""

from __future__ import annotations

from typing import Any

from app.services.aiops_state_utils import extract_incident_id
from app.utils.structured_data import as_dict, dict_list

MAX_PROGRESS_EVENTS = 20

_FAILED_TOOL_STATUSES = {"failed", "error", "blocked"}


def build_progress_payload(
    state: dict[str, Any] | None,
    *,
    phase: str,
    node_name: str,
    cursor: str,
    status: str = "running",
    current_tool: str | None = None,
    report_status: str | None = None,
    message: str = "",
) -> dict[str, Any]:
    """Build the stable progress contract shared by SSE and run-status recovery."""
    state = state or {}
    tool_calls = _dict_items(state.get("tool_call_records"))
    evidence = _dict_items(state.get("gathered_evidence"))
    risk_assessment = as_dict(state.get("risk_assessment"))
    report = as_dict(state.get("report"))

    payload = {
        "phase": phase,
        "node_name": node_name,
        "current_tool": current_tool or _current_tool(state, tool_calls),
        "tool_total": _tool_total(state),
        "tool_success_count": _tool_count(tool_calls, "success"),
        "tool_failed_count": _failed_tool_count(tool_calls),
        "evidence_count": len(evidence),
        "risk_policy": _risk_policy(state, risk_assessment),
        "report_status": report_status or _report_status(state, report),
        "cursor": cursor,
        "session_id": str(state.get("session_id") or ""),
        "status": status,
        "message": message,
        "incident_id": extract_incident_id(state),
        "trace_id": str(state.get("trace_id") or ""),
    }
    return payload


def build_progress_from_event(
    event_payload: dict[str, Any],
    state: dict[str, Any],
    *,
    node_name: str,
    cursor: str,
) -> dict[str, Any]:
    """Derive a progress payload from a node SSE event and merged graph state."""
    event_type = str(event_payload.get("type") or "")
    return build_progress_payload(
        state,
        phase=_phase_for_event(event_type, node_name),
        node_name=node_name,
        cursor=cursor,
        status=str(event_payload.get("status") or _status_for_event(event_type)),
        current_tool=_event_current_tool(event_payload),
        report_status=_event_report_status(event_payload, state),
        message=str(event_payload.get("message") or ""),
    )


def progress_event_payload(progress: dict[str, Any]) -> dict[str, Any]:
    """Return a standalone SSE progress event."""
    return {
        "type": "progress",
        "stage": progress.get("phase") or "progress",
        "status": progress.get("status") or "running",
        "message": progress.get("message") or "AIOps progress updated",
        **progress,
        "progress": dict(progress),
        "progress_cursor": progress.get("cursor") or "",
    }


def attach_progress(
    event_payload: dict[str, Any],
    progress: dict[str, Any],
) -> dict[str, Any]:
    """Attach progress metadata to an existing SSE event."""
    event_payload["progress"] = dict(progress)
    event_payload["progress_cursor"] = progress.get("cursor") or ""
    return event_payload


def state_with_progress(
    state: dict[str, Any],
    progress: dict[str, Any],
) -> dict[str, Any]:
    """Return state augmented with the latest progress and a bounded event tail."""
    progress_event = _compact_progress_event(progress)
    previous_events = _dict_items(state.get("progress_events"))
    return {
        **state,
        "progress": dict(progress),
        "progress_cursor": progress.get("cursor") or "",
        "progress_events": [*previous_events, progress_event][-MAX_PROGRESS_EVENTS:],
    }


def _phase_for_event(event_type: str, node_name: str) -> str:
    if event_type == "plan":
        return "planning"
    if event_type == "step_complete":
        return "executing"
    if event_type == "approval_required":
        return "approval"
    if event_type == "report":
        return "reporting"
    if event_type == "complete":
        return "complete"
    if event_type == "error":
        return "error"
    return {
        "planner": "planning",
        "executor": "executing",
        "replanner": "replanning",
        "workflow": "workflow",
    }.get(node_name, node_name or "progress")


def _status_for_event(event_type: str) -> str:
    if event_type == "complete":
        return "completed"
    if event_type == "error":
        return "failed"
    if event_type == "approval_required":
        return "waiting_approval"
    return "running"


def _event_current_tool(event_payload: dict[str, Any]) -> str:
    step = event_payload.get("current_step")
    if isinstance(step, dict):
        return str(step.get("tool_name") or "")
    records = _dict_items(event_payload.get("tool_call_records"))
    if records:
        return str(records[-1].get("tool_name") or "")
    approval = as_dict(event_payload.get("pending_approval"))
    if approval:
        return str(approval.get("tool_name") or approval.get("action") or "")
    return ""


def _event_report_status(event_payload: dict[str, Any], state: dict[str, Any]) -> str:
    report = as_dict(event_payload.get("structured_report")) or as_dict(state.get("report"))
    if report:
        return str(report.get("status") or "generated")
    if event_payload.get("type") == "report":
        return "generated"
    if event_payload.get("type") == "approval_required":
        return "waiting_approval"
    return _report_status(state, {})


def _current_tool(state: dict[str, Any], tool_calls: list[dict[str, Any]]) -> str:
    current_plan = _dict_items(state.get("current_plan"))
    if current_plan:
        return str(current_plan[0].get("tool_name") or "")
    if tool_calls:
        return str(tool_calls[-1].get("tool_name") or "")
    executed_steps = _dict_items(state.get("executed_steps"))
    if executed_steps:
        return str(executed_steps[-1].get("tool_name") or "")
    return ""


def _tool_total(state: dict[str, Any]) -> int:
    past_steps = list(state.get("past_steps") or [])
    current_plan = _dict_items(state.get("current_plan"))
    legacy_plan = list(state.get("plan") or [])
    remaining_count = len(current_plan) if current_plan else len(legacy_plan)
    tool_calls = _dict_items(state.get("tool_call_records"))
    executed_steps = _dict_items(state.get("executed_steps"))
    return max(
        len(past_steps) + remaining_count,
        len(tool_calls),
        len(executed_steps),
    )


def _tool_count(tool_calls: list[dict[str, Any]], status: str) -> int:
    return sum(1 for call in tool_calls if str(call.get("status") or "").lower() == status)


def _failed_tool_count(tool_calls: list[dict[str, Any]]) -> int:
    return sum(
        1 for call in tool_calls if str(call.get("status") or "").lower() in _FAILED_TOOL_STATUSES
    )


def _risk_policy(state: dict[str, Any], risk_assessment: dict[str, Any]) -> str:
    if risk_assessment:
        return str(risk_assessment.get("policy") or "unknown")
    if state.get("pending_approval"):
        return "approval_required"
    return "allow"


def _report_status(state: dict[str, Any], report: dict[str, Any]) -> str:
    if report:
        return str(report.get("status") or "generated")
    if state.get("pending_approval"):
        return "waiting_approval"
    if state.get("response"):
        return "generated"
    return "not_started"


def _compact_progress_event(progress: dict[str, Any]) -> dict[str, Any]:
    return {
        "cursor": progress.get("cursor") or "",
        "phase": progress.get("phase") or "",
        "node_name": progress.get("node_name") or "",
        "status": progress.get("status") or "",
        "current_tool": progress.get("current_tool") or "",
        "tool_total": progress.get("tool_total") or 0,
        "tool_success_count": progress.get("tool_success_count") or 0,
        "tool_failed_count": progress.get("tool_failed_count") or 0,
        "evidence_count": progress.get("evidence_count") or 0,
        "risk_policy": progress.get("risk_policy") or "",
        "report_status": progress.get("report_status") or "",
        "message": progress.get("message") or "",
    }


def _dict_items(value: Any) -> list[dict[str, Any]]:
    return dict_list(value, wrap_scalars=False)
