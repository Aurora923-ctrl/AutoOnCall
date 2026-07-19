"""Pure helpers used by the AIOps orchestration service."""

from typing import Any

from app.models.incident import Incident
from app.models.trace import TraceEvent
from app.services.aiops_prompt_builder import (
    build_incident_diagnosis_input,
    format_raw_alert_for_prompt,
)
from app.services.aiops_state_utils import extract_incident_id
from app.services.incident_lifecycle import (
    incident_status_from_runtime_status,
    infer_terminal_report_status,
    snapshot_status_from_event,
    terminal_event_status,
)

ADDITIVE_STATE_FIELDS = {
    "past_steps",
    "executed_steps",
    "tool_call_records",
    "gathered_evidence",
    "progress_events",
    "errors",
    "warnings",
}


def _attach_trace_event(event_payload: dict[str, Any], trace_event: TraceEvent) -> dict[str, Any]:
    """Add trace metadata to an SSE event without changing its original shape."""
    event_payload["trace_id"] = trace_event.trace_id
    event_payload["trace_event_id"] = trace_event.event_id
    event_payload["trace_event"] = trace_event.model_dump(mode="json")
    return event_payload


def _build_fallback_final_response(state: dict[str, Any]) -> str:
    """Build a non-empty final response when the graph ends without response."""
    report = state.get("report") or {}
    if isinstance(report, dict) and report.get("markdown"):
        return str(report["markdown"])

    incident = state.get("incident") or {}
    if isinstance(incident, dict):
        incident_id = incident.get("incident_id") or "unknown"
        service_name = incident.get("service_name") or "unknown-service"
        symptom = incident.get("symptom") or state.get("input") or "未提供故障现象"
    else:
        incident_id = getattr(incident, "incident_id", "unknown")
        service_name = getattr(incident, "service_name", "unknown-service")
        symptom = getattr(incident, "symptom", None) or state.get("input") or "未提供故障现象"

    pending_approval = state.get("pending_approval")
    past_steps = state.get("past_steps") or []
    errors = state.get("errors") or []
    warnings = state.get("warnings") or []

    if pending_approval:
        return (
            "# AIOps 诊断已暂停，等待人工审批\n\n"
            f"- 事件：{incident_id}\n"
            f"- 服务：{service_name}\n"
            f"- 现象：{symptom}\n"
            f"- 已执行步骤数：{len(past_steps)}\n"
            "- 状态：检测到需要人工审批的动作，自动执行已暂停。\n"
        )

    error_block = ""
    if errors:
        error_preview = "; ".join(str(error) for error in errors[:3])
        error_block = f"\n- 已记录错误：{error_preview}\n"
    warning_block = ""
    if warnings:
        warning_preview = "; ".join(str(warning) for warning in warnings[:3])
        warning_block = f"\n- 已记录运行告警：{warning_preview}\n"

    return (
        "# AIOps 诊断流程已结束\n\n"
        f"- 事件：{incident_id}\n"
        f"- 服务：{service_name}\n"
        f"- 现象：{symptom}\n"
        f"- 已执行步骤数：{len(past_steps)}\n"
        "- 状态：流程结束时未生成最终诊断报告，请结合 Trace 和已采集证据继续排查。\n"
        f"{error_block}"
        f"{warning_block}"
    )


def _extract_incident_id(state: dict[str, Any]) -> str:
    """Compatibility wrapper for older imports from this helper module."""
    return extract_incident_id(state)


def _infer_terminal_report_status(state: dict[str, Any]) -> str:
    """Infer a report status for graph terminal states that missed Replanner finalization."""
    return infer_terminal_report_status(state)


def _snapshot_status_from_event(event: dict[str, Any]) -> str:
    """Map streamed workflow events to durable session snapshot states."""
    return snapshot_status_from_event(event)


def _incident_status_from_runtime_status(status: str) -> str:
    """Normalize runtime/report statuses into incident lifecycle statuses."""
    return incident_status_from_runtime_status(status)


def _terminal_event_status(event: dict[str, Any]) -> str:
    """Derive the legacy terminal status from the structured report contract."""
    return terminal_event_status(event)


def _merge_checkpoint_with_node_output(
    checkpoint_state: dict[str, Any],
    node_output: dict[str, Any],
) -> dict[str, Any]:
    """Merge LangGraph node deltas into a durable snapshot without losing additive fields."""
    merged = dict(checkpoint_state or {})
    for key, value in node_output.items():
        if key in {"session_id", "trace_id", "incident_id"} and key in merged:
            # These identifiers belong to the run, not to an individual node delta.
            continue
        if key == "incident" and isinstance(merged.get("incident"), dict):
            existing_incident = dict(merged["incident"])
            incoming_incident = value if isinstance(value, dict) else {}
            existing_id = str(existing_incident.get("incident_id") or "")
            incoming_id = str(incoming_incident.get("incident_id") or "")
            if existing_id and incoming_id and existing_id != incoming_id:
                continue
        if key not in ADDITIVE_STATE_FIELDS or not isinstance(value, list):
            merged[key] = value
            continue

        existing = merged.get(key)
        if not isinstance(existing, list):
            merged[key] = value
        elif _list_endswith(existing, value):
            merged[key] = existing
        else:
            merged[key] = [*existing, *value]
    return merged


def _list_endswith(values: list[Any], suffix: list[Any]) -> bool:
    if not suffix:
        return True
    if len(suffix) > len(values):
        return False
    return values[-len(suffix) :] == suffix


def _build_incident_diagnosis_input(base_task: str, incident: Incident | None) -> str:
    """Render the structured incident into the planner-facing diagnosis request."""
    return build_incident_diagnosis_input(base_task, incident)


def _format_raw_alert_for_prompt(raw_alert: dict[str, Any], max_chars: int = 4000) -> str:
    """Serialize raw alert fields for planning while keeping the prompt bounded."""
    return format_raw_alert_for_prompt(raw_alert, max_chars=max_chars)
