"""Incident-oriented read APIs."""

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.auth import READ_SCOPE, require_scope
from app.models.api_contracts import (
    IncidentListResponse,
    IncidentOverviewResponse,
    IncidentReportResponse,
    IncidentTraceResponse,
)
from app.models.approval import ApprovalRequest
from app.models.trace import TraceEvent
from app.services.aiops_store import AIOpsStateStore, create_aiops_store
from app.services.approval_service import ApprovalService, approval_service
from app.services.read_models import build_incident_overview
from app.services.report_generator import ReportGenerator, report_generator
from app.services.trace_service import TraceService, trace_service

router = APIRouter()


def get_trace_service() -> TraceService:
    """Return the trace service singleton."""
    return trace_service


def get_report_generator() -> ReportGenerator:
    """Return the report generator singleton."""
    return report_generator


def get_approval_service() -> ApprovalService:
    """Return the approval service singleton."""
    return approval_service


def get_incident_state_store() -> AIOpsStateStore:
    """Return the incident lifecycle state store."""
    return create_aiops_store()


@router.get(
    "/incidents",
    response_model=IncidentListResponse,
    dependencies=[Depends(require_scope(READ_SCOPE))],
)
async def list_incidents(
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    """List incident summaries known by report, trace, or approval storage."""
    limit = limit if isinstance(limit, int) else 20
    report_by_incident = {
        report.incident_id: report for report in get_report_generator().list_reports()
    }
    trace_by_incident: dict[str, list[TraceEvent]] = {}
    for event in get_trace_service().list_events():
        trace_by_incident.setdefault(event.incident_id, []).append(event)
    approvals_by_incident: dict[str, list[ApprovalRequest]] = {}
    for approval in get_approval_service().list_requests():
        approvals_by_incident.setdefault(approval.incident_id, []).append(approval)
    state_by_incident = {
        state.incident_id: state for state in get_incident_state_store().list_incident_states()
    }

    incident_ids = (
        set(report_by_incident)
        | set(trace_by_incident)
        | set(approvals_by_incident)
        | set(state_by_incident)
    )
    summaries = [
        build_incident_overview(
            incident_id,
            report_by_incident.get(incident_id),
            trace_by_incident.get(incident_id, []),
            approvals_by_incident.get(incident_id, []),
            state_by_incident.get(incident_id),
        )
        for incident_id in incident_ids
    ]
    summaries.sort(key=lambda item: item["updated_at"] or "", reverse=True)
    return {"items": summaries[:limit]}


@router.get(
    "/incidents/{incident_id}",
    response_model=IncidentOverviewResponse,
    dependencies=[Depends(require_scope(READ_SCOPE))],
)
async def get_incident_overview(incident_id: str) -> dict:
    """Return one incident overview assembled from report, trace, and approval state."""
    report = get_report_generator().get_report(incident_id)
    events = get_trace_service().list_events(incident_id=incident_id)
    approvals = get_approval_service().list_requests(incident_id=incident_id)
    state = get_incident_state_store().get_incident_state(incident_id)
    if report is None and not events and not approvals and state is None:
        raise HTTPException(status_code=404, detail="incident not found")
    return build_incident_overview(incident_id, report, events, approvals, state)


@router.get(
    "/incidents/{incident_id}/trace",
    response_model=IncidentTraceResponse,
    dependencies=[Depends(require_scope(READ_SCOPE))],
)
async def get_incident_trace(
    incident_id: str,
    event_type: str | None = Query(default=None),
) -> dict:
    """Return trace events for one incident."""
    event_type = event_type if isinstance(event_type, str) else None
    events = get_trace_service().list_events(incident_id=incident_id, event_type=event_type)
    trace_id = events[0].trace_id if events else ""
    return {
        "incident_id": incident_id,
        "trace_id": trace_id,
        "items": [event.model_dump(mode="json") for event in events],
    }


@router.get(
    "/incidents/{incident_id}/report",
    response_model=IncidentReportResponse,
    dependencies=[Depends(require_scope(READ_SCOPE))],
)
async def get_incident_report(
    incident_id: str,
    response_format: str | None = Query(default=None, alias="format"),
) -> dict:
    """Return the latest structured diagnosis report for one incident."""
    response_format = response_format if isinstance(response_format, str) else None
    report = get_report_generator().get_report(incident_id)
    if report is None:
        raise HTTPException(status_code=404, detail="incident report not found")

    payload = {
        "incident_id": incident_id,
        "trace_id": report.trace_id,
        "report_id": report.report_id,
        "report": report.model_dump(mode="json"),
        "markdown": report.markdown,
    }
    if response_format == "markdown":
        return {
            "incident_id": incident_id,
            "trace_id": report.trace_id,
            "report_id": report.report_id,
            "markdown": report.markdown,
        }
    return payload
