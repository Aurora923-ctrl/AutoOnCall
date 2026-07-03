"""Metrics, evidence, tooling, and report summaries for incident replay."""

from __future__ import annotations

from collections import Counter
from typing import Any

from app.models.approval import ApprovalRequest
from app.services.aiops_read_models.common import _as_list, _safe_float


def build_replay_metrics(
    *,
    timeline: list[dict[str, Any]],
    replanner_decisions: list[dict[str, Any]],
    report_payload: dict[str, Any],
    diagnosis_chain: dict[str, Any],
    approvals: list[ApprovalRequest],
    change_executions: list[dict[str, Any]],
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
        "tool_call_count": len(tool_calls)
        or sum(1 for item in timeline if item.get("event_type") == "tool_call"),
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
    for item in evidence:
        if not isinstance(item, dict):
            continue
        by_type[str(item.get("evidence_type") or "unknown")] += 1
        by_source[str(item.get("data_source") or "unknown")] += 1
        by_stance[str(item.get("stance") or "neutral")] += 1
        confidence = _safe_float(item.get("confidence"))
        if confidence is not None:
            confidences.append(confidence)
    return {
        "total": sum(by_type.values()),
        "by_type": dict(by_type),
        "by_source": dict(by_source),
        "by_stance": dict(by_stance),
        "average_confidence": round(sum(confidences) / len(confidences), 3) if confidences else 0.0,
        "high_confidence_count": sum(1 for value in confidences if value >= 0.8),
        "low_confidence_count": sum(1 for value in confidences if value < 0.5),
        "has_mock": by_source.get("mock", 0) > 0,
        "has_not_configured": by_source.get("not_configured", 0) > 0,
    }


def build_replay_tooling(
    tool_calls: list[Any],
    timeline: list[dict[str, Any]],
) -> dict[str, Any]:
    """Summarize tool calls for the replay response."""
    normalized_calls = [item for item in tool_calls if isinstance(item, dict)]
    if not normalized_calls:
        normalized_calls = [
            {
                "step_id": item.get("step_id"),
                "tool_name": item.get("tool_name"),
                "status": item.get("status"),
                "data_source": item.get("data_source"),
                "latency_ms": item.get("latency_ms"),
                "output_summary": item.get("output_summary"),
            }
            for item in timeline
            if item.get("event_type") == "tool_call"
        ]

    by_tool = Counter(str(item.get("tool_name") or "unknown") for item in normalized_calls)
    by_status = Counter(str(item.get("status") or "unknown") for item in normalized_calls)
    by_source = Counter(str(item.get("data_source") or "unknown") for item in normalized_calls)
    duplicate_tools = sorted([tool for tool, count in by_tool.items() if count > 1])
    return {
        "total": len(normalized_calls),
        "by_tool": dict(by_tool),
        "by_status": dict(by_status),
        "by_source": dict(by_source),
        "failure_count": sum(
            1
            for item in normalized_calls
            if str(item.get("status") or "") in {"failed", "error", "blocked"}
        ),
        "duplicate_tool_candidates": duplicate_tools,
        "items": normalized_calls,
    }


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
