"""AIOps 智能运维接口."""

from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from loguru import logger
from sse_starlette.sse import EventSourceResponse

from app.api.aiops_route_helpers import (
    build_aiops_run_status_payload,
    build_aiops_runs_payload,
    build_change_execution_payload,
    build_demo_incident_payload,
    build_incident_changes_payload,
    build_manual_change_result_payload,
    diagnosis_event_stream,
    resolve_demo_incident,
    resolve_resume_approval,
    resume_diagnosis_event_stream,
    safe_change_event_stream,
)
from app.core.auth import (
    CHANGE_SCOPE,
    DIAGNOSE_SCOPE,
    READ_SCOPE,
    AuthPrincipal,
    audit_actor,
    require_scope,
)
from app.models.aiops import AIOPS_SESSION_ID_MAX_LENGTH, AIOpsRequest, AIOpsResumeRequest
from app.models.approval import ApprovalRequest
from app.models.change_execution import ChangeResumeRequest, ManualExecutionResultRequest
from app.services.aiops_service import aiops_service
from app.services.approval_service import ApprovalService, approval_service
from app.services.change_execution_service import ChangeExecutionService, change_execution_service
from app.services.demo_incidents import (
    canonical_demo_case_id,
    list_demo_incident_items,
)
from app.services.incident_lifecycle import AIOPS_RUN_FILTER_STATUSES, status_catalog
from app.services.report_generator import ReportGenerator, report_generator
from app.services.trace_service import TraceService, trace_service
from app.tools.registry import create_default_tool_registry
from app.utils.log_safety import sanitize_log_value

router = APIRouter()
RESOURCE_ID_MAX_LENGTH = 128
ResourceId = Annotated[str, Path(..., min_length=1, max_length=RESOURCE_ID_MAX_LENGTH)]


def get_approval_service() -> ApprovalService:
    """Return the approval service singleton."""
    return approval_service


def get_trace_service() -> TraceService:
    """Return the trace service singleton."""
    return trace_service


def get_report_generator() -> ReportGenerator:
    """Return the diagnosis report repository singleton."""
    return report_generator


def get_change_execution_service() -> ChangeExecutionService:
    """Return the safe change execution service singleton."""
    return change_execution_service


@router.get("/aiops/tools/contracts", dependencies=[Depends(require_scope(READ_SCOPE))])
async def list_aiops_tool_contracts() -> dict:
    """Return read-only AIOps tool contracts without invoking external systems."""
    registry = create_default_tool_registry([])
    contracts = [contract.model_dump(mode="json") for contract in registry.list_contracts()]
    return {
        "count": len(contracts),
        "items": contracts,
    }


@router.get("/aiops/demo/incidents", dependencies=[Depends(require_scope(READ_SCOPE))])
async def list_demo_incidents() -> dict:
    """Return the central demo incident catalog used by the frontend workbench."""
    items = list_demo_incident_items()
    return {"count": len(items), "items": items}


@router.get("/aiops/status-catalog", dependencies=[Depends(require_scope(READ_SCOPE))])
async def get_aiops_status_catalog() -> dict:
    """Return lifecycle statuses used by AIOps history filters and badges."""
    items = status_catalog(AIOPS_RUN_FILTER_STATUSES)
    return {"count": len(items), "items": items}


@router.get("/aiops/runs", dependencies=[Depends(require_scope(READ_SCOPE))])
async def list_aiops_runs(
    incident_id: str | None = Query(default=None, min_length=1, max_length=RESOURCE_ID_MAX_LENGTH),
    status: str | None = Query(default=None),
    service_name: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    """Return recent durable diagnosis runs for history views."""
    return build_aiops_runs_payload(
        aiops_service=aiops_service,
        approval_service=get_approval_service(),
        report_generator=get_report_generator(),
        incident_id=incident_id,
        status=status,
        service_name=service_name,
        limit=limit,
    )


@router.get("/aiops/runs/{session_id}", dependencies=[Depends(require_scope(READ_SCOPE))])
async def get_aiops_run_status(
    session_id: str = Path(..., min_length=1, max_length=AIOPS_SESSION_ID_MAX_LENGTH),
) -> dict:
    """Return the latest durable state for one diagnosis run."""
    return build_aiops_run_status_payload(
        aiops_service=aiops_service,
        trace_service=get_trace_service(),
        approval_service=get_approval_service(),
        report_generator=get_report_generator(),
        session_id=session_id,
    )


@router.post("/aiops", dependencies=[Depends(require_scope(DIAGNOSE_SCOPE))])
async def diagnose_stream(request: AIOpsRequest):
    """Run the Plan-Execute-Replan AIOps diagnosis workflow as an SSE stream."""
    session_id = request.session_id or f"session-{uuid4().hex}"

    logger.info(f"[会话 {sanitize_log_value(session_id)}] 收到 AIOps 诊断请求（流式）")
    return EventSourceResponse(
        diagnosis_event_stream(
            aiops_service=aiops_service,
            session_id=session_id,
            incident=request.incident,
        )
    )


@router.get("/aiops/demo/incidents/{case_id}", dependencies=[Depends(require_scope(READ_SCOPE))])
async def get_demo_incident(
    case_id: ResourceId,
):
    """Return a ready-to-run demo incident payload for interviews and local demos."""
    return build_demo_incident_payload(case_id)


@router.post(
    "/aiops/demo/incidents/{case_id}/run",
    dependencies=[Depends(require_scope(DIAGNOSE_SCOPE))],
)
async def run_demo_incident(case_id: ResourceId, request: AIOpsRequest | None = None):
    """Run a fixed demo incident through the normal AIOps SSE workflow."""
    canonical_id = canonical_demo_case_id(case_id)
    incident = _resolve_demo_incident(case_id)
    request_session_id = request.session_id if request and request.session_id else None
    session_id = request_session_id or f"demo-{canonical_id}-{uuid4().hex}"
    if request and request.incident:
        incident = request.incident
    return await diagnose_stream(AIOpsRequest(session_id=session_id, incident=incident))


@router.post(
    "/incidents/{incident_id}/diagnosis/resume",
    dependencies=[Depends(require_scope(DIAGNOSE_SCOPE))],
)
async def resume_diagnosis_stream(
    incident_id: ResourceId,
    request: AIOpsResumeRequest,
):
    """Record an approved human decision and close the paused diagnosis loop."""
    approval = _resolve_resume_approval(incident_id, request.approval_id)
    try:
        session_id = aiops_service.resolve_resume_session_id(
            incident_id=incident_id,
            approval=approval,
            requested_session_id=request.session_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    logger.info(
        f"[会话 {sanitize_log_value(session_id)}] 收到 AIOps resume 请求: "
        f"incident={sanitize_log_value(incident_id)}, "
        f"approval={sanitize_log_value(approval.approval_id)}"
    )
    return EventSourceResponse(
        resume_diagnosis_event_stream(
            aiops_service=aiops_service,
            session_id=session_id,
            incident_id=incident_id,
            approval=approval,
        )
    )


@router.post(
    "/incidents/{incident_id}/changes/{change_plan_id}/resume",
)
async def resume_safe_change_stream(
    incident_id: ResourceId,
    change_plan_id: ResourceId,
    request: ChangeResumeRequest,
    principal: AuthPrincipal = Depends(require_scope(CHANGE_SCOPE)),
):
    """Start the safe change workflow after an approval decision."""
    operator = audit_actor(principal, request.operator)
    return EventSourceResponse(
        safe_change_event_stream(
            change_service=get_change_execution_service(),
            incident_id=incident_id,
            change_plan_id=change_plan_id,
            approval_id=request.approval_id,
            mode=request.mode,
            operator=operator,
            observe_window_seconds=request.observe_window_seconds,
        )
    )


@router.get(
    "/incidents/{incident_id}/changes",
    dependencies=[Depends(require_scope(READ_SCOPE))],
)
async def list_incident_changes(
    incident_id: ResourceId,
) -> dict:
    """List safe change executions for one incident."""
    return build_incident_changes_payload(get_change_execution_service(), incident_id)


@router.get("/changes/{change_execution_id}", dependencies=[Depends(require_scope(READ_SCOPE))])
async def get_change_execution(
    change_execution_id: ResourceId,
) -> dict:
    """Return one safe change execution."""
    return build_change_execution_payload(get_change_execution_service(), change_execution_id)


@router.post(
    "/changes/{change_execution_id}/manual-result",
)
async def submit_manual_change_result(
    change_execution_id: ResourceId,
    request: ManualExecutionResultRequest,
    principal: AuthPrincipal = Depends(require_scope(CHANGE_SCOPE)),
) -> dict:
    """Record a manual execution result for a waiting safe change workflow."""
    request = request.model_copy(update={"operator": audit_actor(principal, request.operator)})
    return build_manual_change_result_payload(
        change_service=get_change_execution_service(),
        change_execution_id=change_execution_id,
        request=request,
    )


def _resolve_resume_approval(
    incident_id: str,
    approval_id: str,
) -> ApprovalRequest:
    """Return the approval decision that authorizes diagnosis resume."""
    return resolve_resume_approval(get_approval_service(), incident_id, approval_id)


def _resolve_demo_incident(case_id: str):
    """Return a demo incident payload or raise a route-level HTTP error."""
    return resolve_demo_incident(case_id)
