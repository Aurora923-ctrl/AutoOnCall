"""Route helper functions for AIOps API endpoints.

These helpers keep ``app.api.aiops`` focused on HTTP routing while preserving
the existing URL contracts and response payloads.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, cast
from uuid import uuid4

from fastapi import HTTPException
from loguru import logger

from app.api.sse import is_terminal_event, sse_message
from app.config import config
from app.models.approval import ApprovalRequest
from app.services.approval_service import ApprovalNotFoundError
from app.services.change_execution_read_models import build_change_execution_read_model
from app.services.change_execution_service import (
    ChangeExecutionNotFoundError,
    ChangeExecutionStateError,
)
from app.services.demo_incidents import (
    DemoIncidentNotFoundError,
    available_demo_case_ids,
    build_demo_incident,
    canonical_demo_case_id,
    demo_incident_aliases,
)
from app.services.read_models import (
    build_aiops_run_status,
    build_aiops_run_summary,
    filter_aiops_run_summaries,
    is_known_incident_id,
    list_run_trace_events,
)
from app.utils.public_errors import (
    GENERIC_CHANGE_ERROR,
    GENERIC_DIAGNOSIS_ERROR,
    public_exception_message,
)


def build_aiops_runs_payload(
    *,
    aiops_service: Any,
    approval_service: Any,
    report_generator: Any,
    incident_id: str | None,
    status: str | None,
    service_name: str | None,
    limit: int,
) -> dict:
    """Return recent durable diagnosis run summaries."""
    filtered = bool(status or service_name)
    fetch_limit = 100 if filtered else limit
    snapshots = aiops_service.list_session_snapshots(
        incident_id=incident_id,
        limit=fetch_limit,
    )
    items = _build_filtered_aiops_run_summaries(
        snapshots,
        approval_service=approval_service,
        report_generator=report_generator,
        status=status,
        service_name=service_name,
    )
    offset = len(snapshots)
    while filtered and len(items) < limit and len(snapshots) == fetch_limit:
        snapshots = aiops_service.list_session_snapshots(
            incident_id=incident_id,
            limit=fetch_limit,
            offset=offset,
        )
        offset += len(snapshots)
        items.extend(
            _build_filtered_aiops_run_summaries(
                snapshots,
                approval_service=approval_service,
                report_generator=report_generator,
                status=status,
                service_name=service_name,
            )
        )
    return {
        "count": len(items[:limit]),
        "items": items[:limit],
        "filters": {
            "incident_id": incident_id or "",
            "status": status or "",
            "service_name": service_name or "",
        },
    }


def _build_filtered_aiops_run_summaries(
    snapshots: list[Any],
    *,
    approval_service: Any,
    report_generator: Any,
    status: str | None,
    service_name: str | None,
) -> list[dict[str, Any]]:
    items = []
    for snapshot in snapshots:
        known_incident = is_known_incident_id(snapshot.incident_id)
        approvals = (
            approval_service.list_requests(incident_id=snapshot.incident_id)
            if known_incident
            else []
        )
        report = report_generator.get_report(snapshot.incident_id) if known_incident else None
        items.append(
            build_aiops_run_summary(
                snapshot,
                approvals=approvals,
                report=report,
            )
        )
    return filter_aiops_run_summaries(
        items,
        status=status,
        service_name=service_name,
    )


def build_aiops_run_status_payload(
    *,
    aiops_service: Any,
    trace_service: Any,
    approval_service: Any,
    report_generator: Any,
    session_id: str,
) -> dict:
    """Return the latest durable state for one diagnosis run."""
    snapshot = aiops_service.get_session_snapshot(session_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="AIOps diagnosis run not found")
    known_incident = is_known_incident_id(snapshot.incident_id)
    events = list_run_trace_events(snapshot, trace_service) if known_incident else []
    approvals = (
        approval_service.list_requests(incident_id=snapshot.incident_id) if known_incident else []
    )
    report = report_generator.get_report(snapshot.incident_id) if known_incident else None
    return build_aiops_run_status(
        snapshot,
        events=events,
        approvals=approvals,
        report=report,
    )


async def diagnosis_event_stream(
    *,
    aiops_service: Any,
    session_id: str,
    incident: Any,
) -> AsyncIterator[dict[str, str]]:
    """Yield SSE messages for the normal AIOps diagnosis workflow."""
    try:
        async for event in aiops_service.diagnose(
            session_id=session_id,
            incident=incident,
        ):
            yield sse_message(event)

            if is_terminal_event(event):
                break

        logger.info(f"[会话 {session_id}] AIOps 诊断流式响应完成")

    except Exception as exc:
        logger.error(f"[会话 {session_id}] AIOps 诊断流式响应异常: {exc}", exc_info=True)
        yield sse_message(
            {
                "type": "error",
                "stage": "exception",
                "status": "failed",
                "message": public_exception_message(exc, fallback=GENERIC_DIAGNOSIS_ERROR),
            }
        )


def build_demo_incident_payload(case_id: str) -> dict:
    """Return a ready-to-run demo incident payload."""
    canonical_id = canonical_demo_case_id(case_id)
    incident = resolve_demo_incident(case_id)
    payload = {
        "session_id": f"demo-{canonical_id}-{uuid4().hex}",
        "incident": incident.model_dump(mode="json"),
    }
    return {
        "case_id": canonical_id,
        "aliases": demo_incident_aliases(canonical_id),
        "payload": payload,
        "stream_endpoint": f"/api/aiops/demo/incidents/{canonical_id}/run",
        "curl": (
            f"curl -N -X POST {config.normalized_api_base_url}/api/aiops/demo/"
            f"incidents/{canonical_id}/run "
            '-H "Content-Type: application/json" -d "{}"'
        ),
    }


def resolve_demo_incident(case_id: str) -> Any:
    """Return a demo incident or raise a 404 HTTP error."""
    try:
        return build_demo_incident(case_id)
    except DemoIncidentNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Unknown demo case {case_id}. "
                f"Available cases: {', '.join(available_demo_case_ids())}"
            ),
        ) from exc


def resolve_resume_approval(
    approval_service: Any,
    incident_id: str,
    approval_id: str | None,
) -> ApprovalRequest:
    """Return the approval decision that authorizes diagnosis resume."""
    try:
        if approval_id:
            approval = approval_service.get_request(approval_id)
            if approval.incident_id != incident_id:
                raise HTTPException(
                    status_code=400,
                    detail="approval_id does not belong to the requested incident",
                )
        else:
            pending = approval_service.list_requests(incident_id=incident_id, status="pending")
            if pending:
                raise HTTPException(
                    status_code=409,
                    detail="approval is still pending",
                )
            approved = approval_service.list_requests(incident_id=incident_id, status="approved")
            if not approved:
                raise HTTPException(
                    status_code=404,
                    detail=f"No approved approval for incident {incident_id}",
                )
            approval = approved[-1]
    except HTTPException:
        raise
    except ApprovalNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if approval.status != "approved":
        raise HTTPException(
            status_code=409,
            detail=f"approval is {approval.status}, expected approved",
        )
    return cast(ApprovalRequest, approval)


async def resume_diagnosis_event_stream(
    *,
    aiops_service: Any,
    session_id: str,
    incident_id: str,
    approval: Any,
) -> AsyncIterator[dict[str, str]]:
    """Yield SSE messages for post-approval diagnosis resume."""
    try:
        async for event in aiops_service.resume_after_approval(
            session_id=session_id,
            incident_id=incident_id,
            approval=approval,
        ):
            yield sse_message(event)
            if is_terminal_event(event):
                break
    except LookupError as exc:
        yield sse_message(
            {
                "type": "error",
                "stage": "resume_not_found",
                "status": "failed",
                "message": public_exception_message(exc),
                "incident_id": incident_id,
            }
        )
    except ValueError as exc:
        yield sse_message(
            {
                "type": "error",
                "stage": "resume_rejected",
                "status": "failed",
                "message": public_exception_message(exc),
                "incident_id": incident_id,
            }
        )
    except Exception as exc:
        logger.error(f"[会话 {session_id}] AIOps resume 异常: {exc}", exc_info=True)
        yield sse_message(
            {
                "type": "error",
                "stage": "resume_exception",
                "status": "failed",
                "message": public_exception_message(exc, fallback=GENERIC_DIAGNOSIS_ERROR),
                "incident_id": incident_id,
            }
        )


async def safe_change_event_stream(
    *,
    change_service: Any,
    incident_id: str,
    change_plan_id: str,
    approval_id: str,
    mode: str,
    operator: str,
    observe_window_seconds: int,
) -> AsyncIterator[dict[str, str]]:
    """Yield SSE messages for safe change workflow resume."""
    try:
        async for event in change_service.start_after_approval(
            incident_id=incident_id,
            change_plan_id=change_plan_id,
            approval_id=approval_id,
            mode=mode,
            operator=operator,
            observe_window_seconds=observe_window_seconds,
        ):
            yield sse_message(event)
            if is_terminal_event(event):
                break
    except Exception as exc:
        yield sse_message(_change_resume_error_payload(exc, incident_id, change_plan_id))


def _change_resume_error_payload(
    exc: Exception,
    incident_id: str,
    change_plan_id: str,
) -> dict:
    if isinstance(exc, ApprovalNotFoundError):
        return {
            "type": "error",
            "stage": "change_approval_not_found",
            "status": "failed",
            "message": public_exception_message(exc),
            "incident_id": incident_id,
            "change_plan_id": change_plan_id,
        }
    if isinstance(exc, ChangeExecutionStateError):
        return {
            "type": "error",
            "stage": "change_resume_rejected",
            "status": "failed",
            "message": public_exception_message(exc),
            "incident_id": incident_id,
            "change_plan_id": change_plan_id,
        }
    logger.error(f"安全变更恢复异常: {exc}", exc_info=True)
    return {
        "type": "error",
        "stage": "change_resume_exception",
        "status": "failed",
        "message": public_exception_message(exc, fallback=GENERIC_CHANGE_ERROR),
        "incident_id": incident_id,
        "change_plan_id": change_plan_id,
    }


def build_incident_changes_payload(change_service: Any, incident_id: str) -> dict:
    """List safe change executions for one incident."""
    executions = change_service.list_executions(incident_id=incident_id)
    return {
        "incident_id": incident_id,
        "items": [build_change_execution_read_model(execution) for execution in executions],
    }


def build_change_execution_payload(change_service: Any, change_execution_id: str) -> dict:
    """Return one safe change execution."""
    try:
        execution = change_service.get_execution(change_execution_id)
    except ChangeExecutionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"change_execution": build_change_execution_read_model(execution)}


def build_manual_change_result_payload(
    *,
    change_service: Any,
    change_execution_id: str,
    request: Any,
) -> dict:
    """Record a manual execution result for a waiting safe change workflow."""
    try:
        execution = change_service.record_manual_result(
            change_execution_id=change_execution_id,
            request=request,
        )
    except ChangeExecutionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ChangeExecutionStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"change_execution": build_change_execution_read_model(execution)}
