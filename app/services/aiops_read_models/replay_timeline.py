"""Timeline and Replanner decision builders for incident replay."""

from __future__ import annotations

from typing import Any

from app.models.trace import TraceEvent
from app.services.aiops_read_models.common import (
    _as_list,
    _as_mapping,
    _safe_float,
    public_trace_event,
)
from app.services.aiops_read_models.incident import compact_plan_step


def build_replay_timeline(events: list[TraceEvent]) -> list[dict[str, Any]]:
    """Normalize trace events into a frontend-friendly replay timeline."""
    timeline = []
    for event in sorted(
        events,
        key=lambda item: (
            item.created_at,
            _replay_event_priority(item),
            item.event_id,
        ),
    ):
        public_event = public_trace_event(event)
        stage = replay_stage_for_event(event)
        timeline.append(
            {
                "event_id": event.event_id,
                "trace_id": event.trace_id,
                "stage": stage,
                "stage_label": replay_stage_label(stage),
                "event_type": event.event_type,
                "node_name": event.node_name,
                "step_id": event.step_id or "",
                "tool_name": event.tool_name or "",
                "status": event.status,
                "summary": public_event["output_summary"]
                or public_event.get("error_message")
                or public_event["input_summary"],
                "input_summary": public_event["input_summary"],
                "output_summary": public_event["output_summary"],
                "error_message": public_event.get("error_message") or "",
                "data_source": str(public_event["metadata"].get("data_source") or ""),
                "decision_source": str(public_event["metadata"].get("decision_source") or ""),
                "latency_ms": event.latency_ms,
                "created_at": event.created_at.isoformat(),
                "metadata": public_event["metadata"],
                "tool_args": public_event["tool_args"],
                "tool_result": public_event.get("tool_result"),
            }
        )
    return timeline


def _replay_event_priority(event: TraceEvent) -> int:
    """Make same-timestamp replay ordering match causal workflow stages."""
    stage_order = {
        "alert": 0,
        "planner": 1,
        "executor": 2,
        "replanner": 3,
        "approval": 4,
        "change": 5,
        "report": 6,
        "trace": 7,
    }
    return stage_order.get(replay_stage_for_event(event), 99)


def replay_stage_for_event(event: TraceEvent) -> str:
    """Classify a trace event into one diagnosis replay stage."""
    event_type = str(event.event_type or "")
    node_name = str(event.node_name or "")

    if "report" in event_type or "report" in node_name:
        return "report"
    if event_type.startswith("change") or node_name == "safe_change_workflow":
        return "change"
    if event_type.startswith("approval") or node_name == "approval_service":
        return "approval"
    if event_type == "risk_decision" or node_name == "risk_controller":
        return "approval"
    if event_type == "tool_call" or event.tool_name or node_name == "executor":
        return "executor"
    if "replan" in event_type or "replanner" in node_name:
        return "replanner"
    if node_name == "planner":
        return "planner"
    if event_type in {"workflow_started", "alert_received"}:
        return "alert"
    return "trace"


def replay_stage_label(stage: str) -> str:
    """Return the display label for one replay stage."""
    labels = {
        "alert": "告警进入",
        "planner": "诊断计划",
        "executor": "工具取证",
        "replanner": "重规划",
        "approval": "审批与风险",
        "change": "安全变更",
        "report": "最终报告",
        "trace": "诊断事件",
        "evaluation": "评测结果",
    }
    return labels.get(stage, stage)


def build_replay_replanner_decisions(timeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract structured Replanner decision cards from replay timeline events."""
    decisions = []
    for item in timeline:
        if item.get("stage") != "replanner":
            continue
        metadata = _as_mapping(item.get("metadata"))
        decision = str(metadata.get("decision") or _parse_decision_from_summary(item) or "")
        decision_source = str(metadata.get("decision_source") or "")
        analysis_decision = str(metadata.get("analysis_decision") or "")
        reason = str(metadata.get("reason") or item.get("summary") or "")
        evidence_profile = _as_mapping(metadata.get("evidence_profile"))
        decisions.append(
            {
                "event_id": item.get("event_id", ""),
                "created_at": item.get("created_at", ""),
                "status": item.get("status", "unknown"),
                "decision": decision or "unknown",
                "decision_label": replanner_decision_label(decision),
                "decision_source": decision_source or "unknown",
                "decision_source_label": replanner_decision_source_label(decision_source),
                "analysis_decision": analysis_decision or "unknown",
                "analysis_decision_label": replanner_decision_label(analysis_decision),
                "reason": reason,
                "evidence_sufficient": bool(metadata.get("evidence_sufficient", False)),
                "missing_evidence": _as_list(metadata.get("missing_evidence")),
                "new_steps": [
                    compact_plan_step(step) for step in _as_list(metadata.get("new_steps"))
                ],
                "conflicts": _as_list(metadata.get("conflicts")),
                "confidence_reasons": _as_list(metadata.get("confidence_reasons")),
                "evidence_profile": evidence_profile,
                "average_evidence_confidence": _safe_float(
                    evidence_profile.get("average_evidence_confidence")
                )
                or 0.0,
                "source_quality": str(evidence_profile.get("source_quality") or "unknown"),
                "summary": item.get("summary", ""),
            }
        )
    return decisions


def replanner_decision_label(decision: str) -> str:
    """Return a short display label for a Replanner decision."""
    labels = {
        "continue_investigation": "继续诊断",
        "add_steps": "追加证据",
        "retry_failed_tool": "重试工具",
        "generate_report": "生成报告",
        "request_approval": "请求审批",
        "escalate_to_human": "升级人工",
    }
    return labels.get(decision, decision or "unknown")


def replanner_decision_source_label(source: str) -> str:
    """Return a short display label for a Replanner decision source."""
    labels = {
        "llm_structured": "LLM 结构化决策",
        "evidence_analyzer": "Evidence Analyzer",
        "evidence_analyzer_fallback": "Evidence Analyzer 兜底",
        "evidence_analyzer_safety_priority": "安全优先规则",
        "max_steps_guard": "步数上限保护",
    }
    return labels.get(source, source or "unknown")


def _parse_decision_from_summary(item: dict[str, Any]) -> str:
    summary = str(item.get("summary") or item.get("output_summary") or "")
    marker = "decision="
    if marker not in summary:
        return ""
    suffix = summary.split(marker, 1)[1]
    return suffix.split(",", 1)[0].strip()


def latest_timeline_by_stage(timeline: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return the latest timeline item for every stage."""
    latest: dict[str, dict[str, Any]] = {}
    for item in timeline:
        latest[str(item.get("stage") or "")] = item
    return latest
