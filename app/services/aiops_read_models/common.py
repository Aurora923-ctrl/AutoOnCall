"""Shared helpers for AIOps read-model builders."""

from __future__ import annotations

from collections import Counter
from typing import Any, cast

from app.models.aiops_session import AIOpsSessionSnapshot
from app.models.approval import ApprovalRequest
from app.models.incident_state import IncidentState
from app.models.report import DiagnosisReport
from app.models.trace import TraceEvent


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


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "passed", "success", "yes", "1"}:
            return True
        if normalized in {"false", "failed", "error", "no", "0"}:
            return False
    return None
