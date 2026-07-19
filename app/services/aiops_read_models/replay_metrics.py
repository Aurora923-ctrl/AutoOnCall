"""Metrics, evidence, tooling, and report summaries for incident replay."""

from __future__ import annotations

from collections import Counter
from typing import Any

from app.models.approval import ApprovalRequest
from app.services.aiops_read_models.common import _as_list, _safe_float
from app.utils.redaction import redact_sensitive_data


def build_replay_metrics(
    *,
    timeline: list[dict[str, Any]],
    replanner_decisions: list[dict[str, Any]],
    report_payload: dict[str, Any],
    diagnosis_chain: dict[str, Any],
    approvals: list[ApprovalRequest],
    change_executions: list[dict[str, Any]],
    tooling: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build compact replay counters and latency numbers."""
    evidence = _as_list(report_payload.get("evidence"))
    tool_calls = _as_list(report_payload.get("tool_calls"))
    plan = _as_list(diagnosis_chain.get("plan"))
    if not plan:
        plan = [
            item
            for item in _as_list(report_payload.get("timeline"))
            if isinstance(item, dict) and item.get("step_id")
        ]
    latencies = []
    for item in timeline:
        latency = _safe_float(item.get("latency_ms"))
        if latency and latency > 0:
            latencies.append(latency)
    failed_events = [
        item for item in timeline if str(item.get("status") or "") in {"failed", "error", "blocked"}
    ]
    return {
        "trace_event_count": len(timeline),
        "plan_step_count": len(plan),
        "tool_call_count": int(
            (tooling or {}).get("total")
            or len([item for item in tool_calls if _actual_tool_invoked(item)])
            or sum(
                1
                for item in timeline
                if item.get("event_type") == "tool_call" and _actual_tool_invoked(item)
            )
        ),
        "tool_call_record_count": int(
            max(
                int((tooling or {}).get("audit_record_total") or 0),
                int((tooling or {}).get("report_record_count") or 0),
                int((tooling or {}).get("trace_record_count") or 0),
            )
            or len(tool_calls)
            or sum(1 for item in timeline if item.get("event_type") == "tool_call")
        ),
        "evidence_count": len(evidence),
        "approval_count": len(approvals),
        "change_execution_count": len(change_executions),
        "failed_event_count": len(failed_events),
        "replan_event_count": sum(1 for item in timeline if item.get("stage") == "replanner"),
        "replanner_decision_count": len(replanner_decisions),
        "total_latency_ms": round(sum(latencies), 2),
        "p95_latency_ms": replay_percentile(latencies, 0.95),
        "confidence": _safe_float(report_payload.get("confidence")) or 0.0,
    }


def build_replay_evidence_quality(evidence: list[Any]) -> dict[str, Any]:
    """Summarize evidence source quality for the replay sidebar."""
    by_type: Counter[str] = Counter()
    by_source: Counter[str] = Counter()
    by_stance: Counter[str] = Counter()
    confidences: list[float] = []
    usable_confidences: list[float] = []
    usable_count = 0
    trusted_usable_count = 0
    for item in evidence:
        if not isinstance(item, dict):
            continue
        by_type[str(item.get("evidence_type") or "unknown")] += 1
        by_source[str(item.get("data_source") or "unknown")] += 1
        by_stance[str(item.get("stance") or "neutral")] += 1
        confidence = _safe_float(item.get("confidence"))
        if confidence is not None:
            confidences.append(confidence)
        if _replay_evidence_is_usable(item):
            usable_count += 1
            if _replay_evidence_is_trusted(item):
                trusted_usable_count += 1
            if confidence is not None:
                usable_confidences.append(confidence)
    return {
        "total": sum(by_type.values()),
        "usable_count": usable_count,
        "trusted_usable_count": trusted_usable_count,
        "unusable_count": sum(by_type.values()) - usable_count,
        "by_type": dict(by_type),
        "by_source": dict(by_source),
        "by_stance": dict(by_stance),
        "average_confidence": (
            round(sum(usable_confidences) / len(usable_confidences), 3)
            if usable_confidences
            else 0.0
        ),
        "all_evidence_average_confidence": (
            round(sum(confidences) / len(confidences), 3) if confidences else 0.0
        ),
        "high_confidence_count": sum(1 for value in usable_confidences if value >= 0.8),
        "low_confidence_count": sum(1 for value in usable_confidences if value < 0.5),
        "has_mock": by_source.get("mock", 0) > 0,
        "has_not_configured": by_source.get("not_configured", 0) > 0,
    }


def _replay_evidence_is_usable(item: dict[str, Any]) -> bool:
    raw_data = item.get("raw_data")
    raw_payload = raw_data if isinstance(raw_data, dict) else {}
    if raw_payload.get("status") != "success":
        return False
    metadata = raw_payload.get("metadata")
    metadata_payload = metadata if isinstance(metadata, dict) else {}
    quality = metadata_payload.get("evidence_quality")
    quality_payload = quality if isinstance(quality, dict) else {}
    if quality_payload.get("usable", True) is False:
        return False
    return str(item.get("data_source") or "") not in {
        "failed",
        "not_configured",
        "manual_analysis",
        "llm_toolnode_fallback",
    }


def _replay_evidence_is_trusted(item: dict[str, Any]) -> bool:
    return str(item.get("data_source") or "") not in {
        "",
        "unknown",
        "mock",
        "failed",
        "not_configured",
        "manual_analysis",
        "llm_toolnode_fallback",
    }


def build_replay_tooling(
    tool_calls: list[Any],
    timeline: list[dict[str, Any]],
) -> dict[str, Any]:
    """Summarize tool calls for the replay response."""
    report_records = [redact_sensitive_data(item) for item in tool_calls if isinstance(item, dict)]
    trace_records = [
        {
            "event_id": item.get("event_id"),
            "step_id": item.get("step_id"),
            "tool_name": item.get("tool_name"),
            "status": item.get("status"),
            "data_source": item.get("data_source"),
            "latency_ms": item.get("latency_ms"),
            "output_summary": item.get("output_summary"),
            "invocation_kind": _invocation_kind(item),
            "actual_tool_invoked": _actual_tool_invoked(item),
        }
        for item in timeline
        if item.get("event_type") == "tool_call"
    ]
    normalized_calls = trace_records or report_records
    actual_calls = [item for item in normalized_calls if _actual_tool_invoked(item)]

    by_tool = Counter(str(item.get("tool_name") or "unknown") for item in actual_calls)
    by_status = Counter(str(item.get("status") or "unknown") for item in actual_calls)
    by_source = Counter(str(item.get("data_source") or "unknown") for item in actual_calls)
    by_invocation_kind = Counter(_invocation_kind(item) for item in normalized_calls)
    duplicate_tools = sorted([tool for tool, count in by_tool.items() if count > 1])
    report_actual_count = sum(1 for item in report_records if _actual_tool_invoked(item))
    trace_actual_count = sum(1 for item in trace_records if _actual_tool_invoked(item))
    return {
        "total": len(actual_calls),
        "audit_record_total": len(normalized_calls),
        "non_tool_record_count": len(normalized_calls) - len(actual_calls),
        "by_invocation_kind": dict(by_invocation_kind),
        "by_tool": dict(by_tool),
        "by_status": dict(by_status),
        "by_source": dict(by_source),
        "failure_count": sum(
            1
            for item in actual_calls
            if str(item.get("status") or "") in {"failed", "error", "blocked"}
        ),
        "duplicate_tool_candidates": duplicate_tools,
        "items": normalized_calls,
        "actual_items": actual_calls,
        "report_record_count": len(report_records),
        "trace_record_count": len(trace_records),
        "report_actual_tool_count": report_actual_count,
        "trace_actual_tool_count": trace_actual_count,
        "trace_report_count_mismatch": bool(
            report_records and trace_records and report_actual_count != trace_actual_count
        ),
    }


def _invocation_kind(item: dict[str, Any]) -> str:
    metadata = item.get("metadata")
    if isinstance(metadata, dict) and metadata.get("invocation_kind"):
        return str(metadata["invocation_kind"])
    return str(item.get("invocation_kind") or "tool")


def _actual_tool_invoked(item: dict[str, Any]) -> bool:
    metadata = item.get("metadata")
    if isinstance(metadata, dict) and "actual_tool_invoked" in metadata:
        return bool(metadata["actual_tool_invoked"])
    if "actual_tool_invoked" in item:
        return bool(item["actual_tool_invoked"])
    return _invocation_kind(item) == "tool"


def build_replay_report_summary(report_payload: dict[str, Any]) -> dict[str, Any]:
    """Return a compact report summary for the replay header/sidebar."""
    if not report_payload:
        return {"available": False}
    return {
        "available": True,
        "report_id": str(report_payload.get("report_id") or ""),
        "status": str(report_payload.get("status") or ""),
        "root_cause": str(report_payload.get("root_cause") or ""),
        "summary": str(report_payload.get("summary") or ""),
        "impact": str(report_payload.get("impact") or ""),
        "confidence": _safe_float(report_payload.get("confidence")) or 0.0,
        "confidence_reason": str(report_payload.get("confidence_reason") or ""),
        "key_findings": _as_list(report_payload.get("key_findings")),
        "next_steps": _as_list(report_payload.get("next_steps")),
        "markdown_available": bool(report_payload.get("markdown")),
    }


def replay_percentile(values: list[float], percentile: float) -> float:
    """Return a simple nearest-rank percentile for replay latency."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * percentile))))
    return round(ordered[index], 2)
