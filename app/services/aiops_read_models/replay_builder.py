"""Top-level incident replay response builder."""

from __future__ import annotations

from typing import Any

from app.models.approval import ApprovalRequest
from app.models.incident_state import IncidentState
from app.models.report import DiagnosisReport
from app.models.trace import TraceEvent
from app.services.aiops_read_models.common import (
    _as_list,
    _as_mapping,
    report_for_trace,
    select_incident_artifacts,
)
from app.services.aiops_read_models.incident import build_incident_overview
from app.services.aiops_read_models.replay_evaluation import build_replay_evaluation
from app.services.aiops_read_models.replay_flow import (
    build_replay_approval_flow,
    build_replay_change_flow,
    sort_replay_change_executions,
)
from app.services.aiops_read_models.replay_metrics import (
    build_replay_evidence_quality,
    build_replay_metrics,
    build_replay_report_summary,
    build_replay_tooling,
)
from app.services.aiops_read_models.replay_stages import build_replay_stages
from app.services.aiops_read_models.replay_timeline import (
    build_replay_replanner_decisions,
    build_replay_timeline,
)
from app.utils.redaction import redact_sensitive_data


def build_incident_replay(
    incident_id: str,
    report: DiagnosisReport | None,
    events: list[TraceEvent],
    approvals: list[ApprovalRequest],
    state: IncidentState | None = None,
    change_executions: list[dict[str, Any]] | None = None,
    *,
    evaluation_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a replay-ready incident view across trace, evidence, approval, and report."""
    selected_trace_id, selected_events, selected_approvals = select_incident_artifacts(
        report,
        state,
        events,
        approvals,
    )
    selected_report = report_for_trace(report, selected_trace_id)
    sorted_events = sorted(selected_events, key=lambda item: (item.created_at, item.event_id))
    sorted_approvals = sorted(
        selected_approvals,
        key=lambda item: (item.created_at, item.approval_id),
    )
    changes = sort_replay_change_executions(
        [
            item
            for item in change_executions or []
            if str(item.get("trace_id") or "") == selected_trace_id
        ]
    )
    overview = build_incident_overview(
        incident_id,
        selected_report,
        sorted_events,
        sorted_approvals,
        state,
    )
    report_payload = (
        redact_sensitive_data(selected_report.model_dump(mode="json")) if selected_report else {}
    )
    timeline = build_replay_timeline(sorted_events)
    replanner_decisions = build_replay_replanner_decisions(timeline)
    diagnosis_chain = _as_mapping(overview.get("diagnosis_chain"))
    evidence = _as_list(report_payload.get("evidence"))
    tool_calls = _as_list(report_payload.get("tool_calls"))
    tooling = build_replay_tooling(tool_calls, timeline)
    metrics = build_replay_metrics(
        timeline=timeline,
        replanner_decisions=replanner_decisions,
        report_payload=report_payload,
        diagnosis_chain=diagnosis_chain,
        approvals=sorted_approvals,
        change_executions=changes,
        tooling=tooling,
    )
    evidence_quality = build_replay_evidence_quality(evidence)
    approval_flow = build_replay_approval_flow(sorted_approvals)
    change_flow = build_replay_change_flow(changes)
    report_summary = build_replay_report_summary(report_payload)
    evaluation = build_replay_evaluation(
        incident_id=incident_id,
        overview=overview,
        report_payload=report_payload,
        metrics=metrics,
        evidence_quality=evidence_quality,
        tooling=tooling,
        replanner_decisions=replanner_decisions,
        evaluation_summary=evaluation_summary,
    )

    links = dict(overview.get("links") or {})
    links.update(
        {
            "replay": f"/api/incidents/{incident_id}/replay",
            "changes": f"/api/incidents/{incident_id}/changes",
        }
    )

    return {
        "incident_id": incident_id,
        "trace_id": overview.get("trace_id", ""),
        "status": overview.get("status", "investigating"),
        "status_metadata": overview.get("status_metadata") or {},
        "title": overview.get("title", ""),
        "service_name": overview.get("service_name", ""),
        "severity": overview.get("severity", ""),
        "environment": overview.get("environment", ""),
        "summary": overview.get("summary", ""),
        "root_cause": overview.get("root_cause", ""),
        "overview": overview,
        "stages": build_replay_stages(
            overview=overview,
            timeline=timeline,
            report_payload=report_payload,
            approvals=sorted_approvals,
            change_executions=changes,
            evaluation=evaluation,
        ),
        "timeline": timeline,
        "replanner_decisions": replanner_decisions,
        "metrics": metrics,
        "evidence_quality": evidence_quality,
        "tooling": tooling,
        "approval_flow": approval_flow,
        "change_flow": change_flow,
        "report_summary": report_summary,
        "evaluation": evaluation,
        "links": links,
        "updated_at": overview.get("updated_at", ""),
    }
