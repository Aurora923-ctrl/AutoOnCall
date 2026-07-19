"""Shared helpers for AIOps read-model builders."""

from __future__ import annotations

from collections import Counter
from typing import Any, cast

from app.models.aiops_session import AIOpsSessionSnapshot
from app.models.approval import ApprovalRequest
from app.models.incident_state import IncidentState
from app.models.report import DiagnosisReport
from app.models.trace import TraceEvent
from app.utils.redaction import redact_sensitive_data


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
    if any(approval.status == "cancelled" for approval in approvals):
        return "approval_cancelled"
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
        if report is None or _state_is_current_for_report(state, report):
            return state.status
    approval_status = approval_status_from_approvals(approvals)
    if approval_status == "approved":
        return "approval_approved"
    if approval_status == "rejected":
        return "approval_rejected"
    if approval_status == "pending":
        return "waiting_approval"
    if approval_status == "cancelled":
        return "approval_cancelled"
    return report.status if report else infer_status_from_approvals(approvals)


def _state_is_current_for_report(
    state: IncidentState,
    report: DiagnosisReport,
) -> bool:
    if state.report_id and state.report_id == report.report_id:
        return True
    if state.trace_id and report.trace_id and state.trace_id != report.trace_id:
        return state.updated_at >= report.created_at
    return state.updated_at >= report.created_at


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
        "latest": public_trace_event(latest_event) if latest_event else None,
    }


def list_run_trace_events(snapshot: AIOpsSessionSnapshot, trace_service: Any) -> list[TraceEvent]:
    """Load trace events for one run without crossing trace identities."""
    if snapshot.trace_id and snapshot.trace_id != "trace-unknown":
        return cast(list[TraceEvent], trace_service.list_events(trace_id=snapshot.trace_id))
    return []


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
        "latest": public_approval_request(latest) if latest else None,
    }


def latest_trace_event(events: list[TraceEvent]) -> TraceEvent | None:
    """Return the newest trace event by creation time."""
    if not events:
        return None
    return sorted(events, key=lambda event: (event.created_at, event.event_id))[-1]


def latest_approval_request(approvals: list[ApprovalRequest]) -> ApprovalRequest | None:
    """Return the newest approval by decision time or creation time."""
    if not approvals:
        return None
    return sorted(
        approvals,
        key=lambda approval: (
            approval.decided_at or approval.created_at,
            approval.approval_id,
        ),
    )[-1]


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


def filter_events_for_trace(
    events: list[TraceEvent],
    trace_id: str,
) -> list[TraceEvent]:
    """Return only events belonging to one selected diagnosis trace."""
    if not trace_id:
        return []
    return [event for event in events if event.trace_id == trace_id]


def filter_approvals_for_trace(
    approvals: list[ApprovalRequest],
    trace_id: str,
    session_id: str = "",
    linked_approval_ids: set[str] | None = None,
) -> list[ApprovalRequest]:
    """Return approvals explicitly linked to the selected run when identity exists."""
    explicit_ids = linked_approval_ids or set()
    matched: list[ApprovalRequest] = []
    for approval in approvals:
        metadata = approval.metadata or {}
        approval_trace_id = str(metadata.get("trace_id") or "")
        approval_session_id = str(metadata.get("session_id") or "")
        if approval_trace_id and approval_trace_id != trace_id:
            continue
        if session_id and approval_session_id and approval_session_id != session_id:
            continue
        if not approval_trace_id and not approval_session_id and trace_id:
            if approval.approval_id not in explicit_ids:
                continue
        matched.append(approval)
    return matched


def select_run_approvals(
    snapshot: AIOpsSessionSnapshot,
    approvals: list[ApprovalRequest],
) -> list[ApprovalRequest]:
    """Return approvals that are explicitly linked to one durable run."""
    trace_id = snapshot.trace_id if snapshot.trace_id != "trace-unknown" else ""
    pending_approval_id = str((snapshot.pending_approval or {}).get("approval_id") or "")
    return filter_approvals_for_trace(
        approvals,
        trace_id,
        snapshot.session_id,
        {pending_approval_id} if pending_approval_id else set(),
    )


def select_run_report(
    snapshot: AIOpsSessionSnapshot,
    report: DiagnosisReport | None,
) -> DiagnosisReport | None:
    """Return an incident report only when it belongs to the selected run."""
    if report is None:
        return None
    if snapshot.final_report_id and report.report_id == snapshot.final_report_id:
        return report
    if (
        snapshot.trace_id
        and snapshot.trace_id != "trace-unknown"
        and report.trace_id == snapshot.trace_id
    ):
        return report
    return None


def select_incident_artifacts(
    report: DiagnosisReport | None,
    state: IncidentState | None,
    events: list[TraceEvent],
    approvals: list[ApprovalRequest],
) -> tuple[str, list[TraceEvent], list[ApprovalRequest]]:
    """Select one coherent run identity for incident-scoped read models."""
    trace_id = ""
    if state and state.trace_id and (report is None or _state_is_current_for_report(state, report)):
        trace_id = state.trace_id
    elif report and report.trace_id:
        trace_id = report.trace_id
    if not trace_id and events:
        latest_event = latest_trace_event(events)
        trace_id = latest_event.trace_id if latest_event else ""
    session_id = state.session_id if state and state.trace_id == trace_id else ""
    linked_approval_ids = {
        value
        for value in (
            str(state.latest_approval_id or "") if state and state.trace_id == trace_id else "",
            str((report.approval_decision or {}).get("approval_id") or "")
            if report and report.trace_id == trace_id
            else "",
        )
        if value
    }
    selected_events = filter_events_for_trace(events, trace_id)
    selected_approvals = filter_approvals_for_trace(
        approvals,
        trace_id,
        session_id,
        linked_approval_ids,
    )
    return trace_id, selected_events, selected_approvals


def report_for_trace(
    report: DiagnosisReport | None,
    trace_id: str,
) -> DiagnosisReport | None:
    """Return the report only when it belongs to the selected incident run."""
    if report is None:
        return None
    if not trace_id or report.trace_id == trace_id:
        return report
    return None


def state_for_trace(
    state: IncidentState | None,
    trace_id: str,
) -> IncidentState | None:
    """Return lifecycle state only when it belongs to the selected incident run."""
    if state is None:
        return None
    if not trace_id or not state.trace_id or state.trace_id == trace_id:
        return state
    return None


def public_trace_event(event: TraceEvent) -> dict[str, Any]:
    """Return a redacted trace event payload for public read models."""
    payload = redact_sensitive_data(event.model_dump(mode="json"))
    return dict(payload) if isinstance(payload, dict) else {}


def public_approval_request(approval: ApprovalRequest) -> dict[str, Any]:
    """Return a redacted approval payload for public read models."""
    payload = redact_sensitive_data(approval.model_dump(mode="json"))
    return dict(payload) if isinstance(payload, dict) else {}


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
