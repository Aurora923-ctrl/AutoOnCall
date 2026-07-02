"""Top-level incident replay response builder."""

from __future__ import annotations

from typing import Any

from app.models.approval import ApprovalRequest
from app.models.incident_state import IncidentState
from app.models.report import DiagnosisReport
from app.models.trace import TraceEvent
from app.services.aiops_read_models.common import _as_list, _as_mapping
from app.services.aiops_read_models.incident import build_incident_overview
from app.services.aiops_read_models.replay_evaluation import build_replay_evaluation
from app.services.aiops_read_models.replay_flow import (
    build_replay_approval_flow,
    build_replay_change_flow,
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
    sorted_events = sorted(events, key=lambda item: item.created_at)
    sorted_approvals = sorted(approvals, key=lambda item: item.created_at)
    changes = list(change_executions or [])
    overview = build_incident_overview(
        incident_id,
        report,
        sorted_events,
        sorted_approvals,
        state,
    )
    report_payload = report.model_dump(mode="json") if report else {}
    timeline = build_replay_timeline(sorted_events)
    replanner_decisions = build_replay_replanner_decisions(timeline)
    diagnosis_chain = _as_mapping(overview.get("diagnosis_chain"))
    evidence = _as_list(report_payload.get("evidence"))
    tool_calls = _as_list(report_payload.get("tool_calls"))
    metrics = build_replay_metrics(
        timeline=timeline,
        replanner_decisions=replanner_decisions,
        report_payload=report_payload,
        diagnosis_chain=diagnosis_chain,
        approvals=sorted_approvals,
        change_executions=changes,
    )
    evidence_quality = build_replay_evidence_quality(evidence)
    tooling = build_replay_tooling(tool_calls, timeline)
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
