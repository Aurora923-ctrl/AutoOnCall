"""Incident-oriented read APIs."""

import json
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path as ApiPath, Query

from app.config import config
from app.core.auth import ADMIN_SCOPE, DIAGNOSE_SCOPE, READ_SCOPE, AuthPrincipal, require_scope
from app.models.api_contracts import (
    IncidentFeedbackListResponse,
    IncidentFeedbackResponse,
    IncidentListResponse,
    IncidentOverviewResponse,
    IncidentReplayResponse,
    IncidentReportResponse,
    IncidentTraceResponse,
)
from app.models.approval import ApprovalRequest
from app.models.feedback import DiagnosisFeedbackCreate
from app.models.trace import TraceEvent
from app.services.aiops_read_models import build_incident_overview, build_incident_replay
from app.services.aiops_read_models.common import (
    public_trace_event,
    select_incident_artifacts,
)
from app.services.aiops_store import AIOpsStateStore, create_aiops_store
from app.services.approval_service import ApprovalService, approval_service
from app.services.change_execution_read_models import build_change_execution_read_model
from app.services.change_execution_service import (
    ChangeExecutionService,
    change_execution_service,
)
from app.services.feedback_service import FeedbackService, feedback_service
from app.services.report_generator import ReportGenerator, report_generator
from app.services.trace_service import TraceService, trace_service
from app.utils.redaction import redact_sensitive_data

router = APIRouter()
INCIDENT_ID_MAX_LENGTH = 128
IncidentId = Annotated[str, ApiPath(..., min_length=1, max_length=INCIDENT_ID_MAX_LENGTH)]
_incident_state_store: AIOpsStateStore | None = None


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
    global _incident_state_store
    if _incident_state_store is None:
        _incident_state_store = create_aiops_store()
    return _incident_state_store


def get_change_execution_service() -> ChangeExecutionService:
    """Return the safe change execution service singleton."""
    return change_execution_service


def get_feedback_service() -> FeedbackService:
    """Return the feedback service singleton."""
    return feedback_service


def get_eval_summary_for_replay() -> dict[str, Any] | None:
    """Load the latest offline evaluation summary for incident replay, if present."""
    summary_path = Path(config.eval_summary_path)
    if not summary_path.exists():
        return None
    try:
        raw_payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw_payload, dict):
        return None
    from app.services.evaluation_read_models import build_eval_summary_payload

    return build_eval_summary_payload(raw_payload, summary_path=summary_path)


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
    summaries = []
    for incident_id in incident_ids:
        report = report_by_incident.get(incident_id)
        state = state_by_incident.get(incident_id)
        _, events, approvals = select_incident_artifacts(
            report,
            state,
            trace_by_incident.get(incident_id, []),
            approvals_by_incident.get(incident_id, []),
        )
        summaries.append(build_incident_overview(incident_id, report, events, approvals, state))
    summaries.sort(key=lambda item: item["updated_at"] or "", reverse=True)
    return {"items": summaries[:limit]}


@router.get(
    "/incidents/{incident_id}",
    response_model=IncidentOverviewResponse,
    dependencies=[Depends(require_scope(READ_SCOPE))],
)
async def get_incident_overview(incident_id: IncidentId) -> dict:
    """Return one incident overview assembled from report, trace, and approval state."""
    report = get_report_generator().get_report(incident_id)
    state = get_incident_state_store().get_incident_state(incident_id)
    _, events, approvals = select_incident_artifacts(
        report,
        state,
        get_trace_service().list_events(incident_id=incident_id),
        get_approval_service().list_requests(incident_id=incident_id),
    )
    if report is None and not events and not approvals and state is None:
        raise HTTPException(status_code=404, detail="incident not found")
    return build_incident_overview(incident_id, report, events, approvals, state)


@router.get(
    "/incidents/{incident_id}/replay",
    response_model=IncidentReplayResponse,
    dependencies=[Depends(require_scope(READ_SCOPE))],
)
async def get_incident_replay(incident_id: IncidentId) -> dict:
    """Return a replay-ready incident view assembled from all diagnosis artifacts."""
    report = get_report_generator().get_report(incident_id)
    state = get_incident_state_store().get_incident_state(incident_id)
    trace_id, events, approvals = select_incident_artifacts(
        report,
        state,
        get_trace_service().list_events(incident_id=incident_id),
        get_approval_service().list_requests(incident_id=incident_id),
    )
    change_executions = [
        build_change_execution_read_model(execution)
        for execution in get_change_execution_service().list_executions(incident_id=incident_id)
        if execution.trace_id == trace_id
    ]
    if report is None and not events and not approvals and state is None and not change_executions:
        raise HTTPException(status_code=404, detail="incident not found")
    return build_incident_replay(
        incident_id,
        report,
        events,
        approvals,
        state,
        change_executions,
        evaluation_summary=get_eval_summary_for_replay(),
    )


@router.get(
    "/incidents/{incident_id}/trace",
    response_model=IncidentTraceResponse,
    dependencies=[Depends(require_scope(READ_SCOPE))],
)
async def get_incident_trace(
    incident_id: IncidentId,
    event_type: str | None = Query(default=None),
) -> dict:
    """Return trace events for one incident."""
    event_type = event_type if isinstance(event_type, str) else None
    trace_repository = get_trace_service()
    raw_events = trace_repository.list_events(incident_id=incident_id)
    report = get_report_generator().get_report(incident_id)
    state = get_incident_state_store().get_incident_state(incident_id)
    trace_id, all_events, _ = select_incident_artifacts(
        report,
        state,
        raw_events,
        [],
    )
    events = [event for event in all_events if event_type is None or event.event_type == event_type]
    if not all_events:
        approvals = get_approval_service().list_requests(incident_id=incident_id)
        change_executions = get_change_execution_service().list_executions(incident_id=incident_id)
        if report is None and not approvals and state is None and not change_executions:
            raise HTTPException(status_code=404, detail="incident not found")
    return {
        "incident_id": incident_id,
        "trace_id": trace_id,
        "items": [public_trace_event(event) for event in events],
    }


@router.get(
    "/incidents/{incident_id}/report",
    response_model=IncidentReportResponse,
    dependencies=[Depends(require_scope(READ_SCOPE))],
)
async def get_incident_report(
    incident_id: IncidentId,
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
        "report": redact_sensitive_data(report.model_dump(mode="json")),
        "markdown": str(redact_sensitive_data(report.markdown)),
    }
    if response_format == "markdown":
        return {
            "incident_id": incident_id,
            "trace_id": report.trace_id,
            "report_id": report.report_id,
            "markdown": str(redact_sensitive_data(report.markdown)),
        }
    return payload


@router.post(
    "/incidents/{incident_id}/feedback",
    response_model=IncidentFeedbackResponse,
)
async def submit_incident_feedback(
    incident_id: IncidentId,
    payload: DiagnosisFeedbackCreate,
    principal: AuthPrincipal = Depends(require_scope(DIAGNOSE_SCOPE)),
) -> dict:
    """Submit minimal operator feedback for report-quality improvement."""
    report = get_report_generator().get_report(incident_id)
    if report is None:
        raise HTTPException(status_code=404, detail="incident report not found")
    if payload.report_id != report.report_id:
        raise HTTPException(status_code=400, detail="report_id does not match latest report")
    store = create_aiops_store()
    if payload.session_id:
        snapshot = store.get_aiops_session_snapshot(payload.session_id)
        if snapshot is None or snapshot.incident_id != incident_id:
            raise HTTPException(status_code=400, detail="session_id does not belong to incident")
        if payload.run_id and payload.run_id != payload.session_id:
            raise HTTPException(status_code=400, detail="run_id does not match session_id")
    elif payload.run_id:
        raise HTTPException(status_code=400, detail="run_id requires session_id")
    if payload.trace_id and payload.trace_id != report.trace_id:
        raise HTTPException(status_code=400, detail="trace_id does not match report")
    trace_events = [
        event
        for event in get_trace_service().list_events(incident_id=incident_id)
        if not report.trace_id or event.trace_id == report.trace_id
    ]
    feedback = get_feedback_service().submit_feedback(
        incident_id=incident_id,
        payload=payload,
        report=report,
        trace_events=trace_events,
        owner_id=principal.principal_id if principal.enabled else "anonymous",
    )
    return {"feedback": feedback}


@router.get(
    "/incidents/{incident_id}/feedback",
    response_model=IncidentFeedbackListResponse,
)
async def list_incident_feedback(
    incident_id: IncidentId,
    principal: AuthPrincipal = Depends(require_scope(READ_SCOPE)),
) -> dict:
    """List operator feedback for one incident."""
    return {
        "incident_id": incident_id,
        "items": get_feedback_service().list_feedback(
            incident_id=incident_id,
            owner_id=(
                None
                if principal.has_scope(ADMIN_SCOPE)
                else principal.principal_id
                if principal.enabled
                else "anonymous"
            ),
        ),
    }
