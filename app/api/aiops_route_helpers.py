"""Route helper functions for AIOps API endpoints.

These helpers keep ``app.api.aiops`` focused on HTTP routing while preserving
the existing URL contracts and response payloads.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, cast
from uuid import uuid4

from fastapi import HTTPException
from loguru import logger

from app.api.sse import is_terminal_event, sse_message
from app.config import config
from app.models.approval import ApprovalRequest
from app.services.aiops_read_models import (
    build_aiops_run_status,
    build_aiops_run_summary,
    filter_aiops_run_summaries,
    is_known_incident_id,
    list_run_trace_events,
    select_run_approvals,
    select_run_report,
)
from app.services.aiops_service import AIOpsResumeConflictError, AIOpsRunConflictError
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
from app.utils.log_safety import sanitize_log_value
from app.utils.public_errors import (
    GENERIC_CHANGE_ERROR,
    GENERIC_DIAGNOSIS_ERROR,
    public_exception_message,
)

_BACKGROUND_STREAM_TASKS: set[asyncio.Task[Any]] = set()


def _track_background_stream_task(task: asyncio.Task[Any]) -> None:
    """Keep detached diagnosis pumps observable until they finish."""
    _BACKGROUND_STREAM_TASKS.add(task)

    def _done(completed: asyncio.Task[Any]) -> None:
        _BACKGROUND_STREAM_TASKS.discard(completed)
        if completed.cancelled():
            return
        try:
            completed.result()
        except Exception:
            logger.exception("Detached AIOps stream task failed")

    task.add_done_callback(_done)


async def _pump_stream(
    source: Any,
    queue: asyncio.Queue[Any],
    detached: asyncio.Event,
) -> None:
    """Run diagnosis after an SSE client disconnects, without retaining its queue."""
    try:
        async for event in source:
            if not detached.is_set():
                await queue.put(("event", event))
    except asyncio.CancelledError as exc:
        if not detached.is_set():
            await asyncio.shield(queue.put(("error", exc)))
        current_task = asyncio.current_task()
        if current_task is not None and current_task.cancelling():
            raise
        return
    except BaseException as exc:
        if not detached.is_set():
            await queue.put(("error", exc))
        return
    if not detached.is_set():
        await queue.put(("done", None))


async def _stream_with_disconnect_survival(source: Any) -> AsyncIterator[Any]:
    """Bridge a durable diagnosis generator to SSE while surviving client disconnects."""
    queue: asyncio.Queue[Any] = asyncio.Queue()
    detached = asyncio.Event()
    task = asyncio.create_task(_pump_stream(source, queue, detached))
    _track_background_stream_task(task)
    try:
        while True:
            kind, value = await queue.get()
            if kind == "done":
                return
            if kind == "error":
                raise value
            yield value
    except asyncio.CancelledError:
        detached.set()
        raise
    finally:
        if not task.done():
            detached.set()


def build_aiops_runs_payload(
    *,
    aiops_service: Any,
    approval_service: Any,
    report_generator: Any,
    incident_id: str | None,
    status: str | None,
    service_name: str | None,
    limit: int,
    session_id_prefix: str = "",
) -> dict:
    """Return recent durable diagnosis run summaries."""
    filtered = bool(status or service_name or session_id_prefix)
    fetch_limit = 100 if filtered else limit
    fetched_snapshots = aiops_service.list_session_snapshots(
        incident_id=incident_id,
        limit=fetch_limit,
    )
    fetched_count = len(fetched_snapshots)
    snapshots = _filter_session_owner(fetched_snapshots, session_id_prefix)
    items = _build_filtered_aiops_run_summaries(
        snapshots,
        approval_service=approval_service,
        report_generator=report_generator,
        status=status,
        service_name=service_name,
    )
    offset = fetched_count
    while filtered and len(items) < limit and fetched_count == fetch_limit:
        fetched_snapshots = aiops_service.list_session_snapshots(
            incident_id=incident_id,
            limit=fetch_limit,
            offset=offset,
        )
        fetched_count = len(fetched_snapshots)
        offset += fetched_count
        snapshots = _filter_session_owner(fetched_snapshots, session_id_prefix)
        items.extend(
            _build_filtered_aiops_run_summaries(
                snapshots,
                approval_service=approval_service,
                report_generator=report_generator,
                status=status,
                service_name=service_name,
            )
        )
        if fetched_count < fetch_limit:
            break
    return {
        "count": len(items[:limit]),
        "items": items[:limit],
        "filters": {
            "incident_id": incident_id or "",
            "status": status or "",
            "service_name": service_name or "",
        },
    }


def _filter_session_owner(snapshots: list[Any], session_id_prefix: str) -> list[Any]:
    if not session_id_prefix:
        return snapshots
    return [
        snapshot
        for snapshot in snapshots
        if str(getattr(snapshot, "session_id", "")).startswith(session_id_prefix)
    ]


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
        approvals = _run_approvals(snapshot, approval_service) if known_incident else []
        report = _run_report(snapshot, report_generator) if known_incident else None
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
    approvals = _run_approvals(snapshot, approval_service) if known_incident else []
    report = _run_report(snapshot, report_generator) if known_incident else None
    return build_aiops_run_status(
        snapshot,
        events=events,
        approvals=approvals,
        report=report,
    )


def _run_approvals(snapshot: Any, approval_service: Any) -> list[ApprovalRequest]:
    """Return approvals explicitly associated with one diagnosis session."""
    approvals = approval_service.list_requests(incident_id=snapshot.incident_id)
    return select_run_approvals(snapshot, approvals)


def _run_report(snapshot: Any, report_generator: Any) -> Any:
    """Return the incident report only when it belongs to this run."""
    return select_run_report(snapshot, report_generator.get_report(snapshot.incident_id))


async def diagnosis_event_stream(
    *,
    aiops_service: Any,
    session_id: str,
    incident: Any,
) -> AsyncIterator[dict[str, str]]:
    """Yield SSE messages for the normal AIOps diagnosis workflow."""
    safe_session_id = sanitize_log_value(session_id)
    terminal_emitted = False
    try:
        source = aiops_service.diagnose(
            session_id=session_id,
            incident=incident,
        )
        async for event in _stream_with_disconnect_survival(source):
            yield sse_message(event)

            if is_terminal_event(event):
                terminal_emitted = True
                break

        if not terminal_emitted:
            logger.error(f"[会话 {safe_session_id}] AIOps 诊断流在 terminal event 前结束")
            yield sse_message(
                {
                    "type": "error",
                    "stage": "stream_ended_without_terminal",
                    "status": "failed",
                    "message": GENERIC_DIAGNOSIS_ERROR,
                    "session_id": session_id,
                }
            )
            return
        logger.info(f"[会话 {safe_session_id}] AIOps 诊断流式响应完成")

    except asyncio.CancelledError:
        raise
    except AIOpsRunConflictError as exc:
        yield sse_message(
            {
                "type": "error",
                "stage": "run_conflict",
                "status": "failed",
                "message": str(exc),
                "session_id": session_id,
            }
        )
    except Exception as exc:
        logger.error(
            "[会话 {}] AIOps 诊断流式响应异常: error_type={}",
            safe_session_id,
            type(exc).__name__,
            exc_info=True,
        )
        yield sse_message(
            {
                "type": "error",
                "stage": "exception",
                "status": "failed",
                "message": public_exception_message(exc, fallback=GENERIC_DIAGNOSIS_ERROR),
                "session_id": session_id,
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
    approval_id: str,
) -> ApprovalRequest:
    """Return the approval decision that authorizes diagnosis resume."""
    try:
        approval = approval_service.get_request(approval_id)
        if approval.incident_id != incident_id:
            raise HTTPException(
                status_code=400,
                detail="approval_id does not belong to the requested incident",
            )
    except HTTPException:
        raise
    except ApprovalNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if approval.status != "approved":
        raise HTTPException(
            status_code=409,
            detail=f"approval is {approval.status}, expected approved",
        )
    if hasattr(approval_service, "is_expired") and approval_service.is_expired(approval):
        raise HTTPException(
            status_code=409,
            detail="approval authorization has expired",
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
    safe_session_id = sanitize_log_value(session_id)
    terminal_emitted = False
    try:
        source = aiops_service.resume_after_approval(
            session_id=session_id,
            incident_id=incident_id,
            approval=approval,
        )
        async for event in _stream_with_disconnect_survival(source):
            yield sse_message(event)
            if is_terminal_event(event):
                terminal_emitted = True
                break
        if not terminal_emitted:
            logger.error(f"[会话 {safe_session_id}] AIOps resume 流在 terminal event 前结束")
            yield sse_message(
                {
                    "type": "error",
                    "stage": "resume_ended_without_terminal",
                    "status": "failed",
                    "message": GENERIC_DIAGNOSIS_ERROR,
                    "session_id": session_id,
                    "incident_id": incident_id,
                }
            )
    except asyncio.CancelledError:
        raise
    except LookupError as exc:
        yield sse_message(
            {
                "type": "error",
                "stage": "resume_not_found",
                "status": "failed",
                "message": public_exception_message(exc),
                "session_id": session_id,
                "incident_id": incident_id,
            }
        )
    except ValueError as exc:
        yield sse_message(
            {
                "type": "error",
                "stage": (
                    "resume_conflict"
                    if isinstance(exc, AIOpsResumeConflictError)
                    else "resume_rejected"
                ),
                "status": "failed",
                "message": public_exception_message(exc),
                "session_id": session_id,
                "incident_id": incident_id,
            }
        )
    except Exception as exc:
        logger.error(
            "[会话 {}] AIOps resume 异常: error_type={}",
            safe_session_id,
            type(exc).__name__,
            exc_info=True,
        )
        yield sse_message(
            {
                "type": "error",
                "stage": "resume_exception",
                "status": "failed",
                "message": public_exception_message(exc, fallback=GENERIC_DIAGNOSIS_ERROR),
                "session_id": session_id,
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
    operator_principal_id: str,
    observe_window_seconds: int,
) -> AsyncIterator[dict[str, str]]:
    """Yield SSE messages for safe change workflow resume."""
    terminal_emitted = False
    try:
        source = change_service.start_after_approval(
            incident_id=incident_id,
            change_plan_id=change_plan_id,
            approval_id=approval_id,
            mode=mode,
            operator=operator,
            operator_principal_id=operator_principal_id,
            observe_window_seconds=observe_window_seconds,
        )
        async for event in _stream_with_disconnect_survival(source):
            yield sse_message(event)
            if is_terminal_event(event):
                terminal_emitted = True
                break
        if not terminal_emitted:
            logger.error(
                "Safe change stream ended before a terminal event: "
                "incident_id={}, change_plan_id={}",
                incident_id,
                change_plan_id,
            )
            yield sse_message(
                {
                    "type": "error",
                    "stage": "change_stream_ended_without_terminal",
                    "status": "failed",
                    "message": GENERIC_CHANGE_ERROR,
                    "incident_id": incident_id,
                    "change_plan_id": change_plan_id,
                }
            )
    except asyncio.CancelledError:
        raise
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
    logger.error(
        "安全变更恢复异常: error_type={}, incident_id={}, change_plan_id={}",
        type(exc).__name__,
        sanitize_log_value(incident_id),
        sanitize_log_value(change_plan_id),
        exc_info=True,
    )
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
    operator_principal_id: str = "",
) -> dict:
    """Record a manual execution result for a waiting safe change workflow."""
    try:
        execution = change_service.record_manual_result(
            change_execution_id=change_execution_id,
            request=request,
            operator_principal_id=operator_principal_id,
        )
    except ChangeExecutionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ChangeExecutionStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"change_execution": build_change_execution_read_model(execution)}
