"""
AIOps 智能运维接口
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from sse_starlette.sse import EventSourceResponse

from app.api.sse import is_terminal_event, sse_message
from app.config import config
from app.core.auth import CHANGE_SCOPE, DIAGNOSE_SCOPE, READ_SCOPE, require_scope
from app.models.aiops import AIOpsRequest, AIOpsResumeRequest
from app.models.approval import ApprovalRequest
from app.models.change_execution import ChangeResumeRequest, ManualExecutionResultRequest
from app.services.aiops_service import aiops_service
from app.services.approval_service import ApprovalNotFoundError, ApprovalService, approval_service
from app.services.change_execution_read_models import build_change_execution_read_model
from app.services.change_execution_service import (
    ChangeExecutionNotFoundError,
    ChangeExecutionService,
    ChangeExecutionStateError,
    change_execution_service,
)
from app.services.demo_incidents import (
    DemoIncidentNotFoundError,
    available_demo_case_ids,
    build_demo_incident,
    canonical_demo_case_id,
    demo_incident_aliases,
    list_demo_incident_items,
)
from app.services.incident_lifecycle import AIOPS_RUN_FILTER_STATUSES, status_catalog
from app.services.read_models import (
    build_aiops_run_status,
    build_aiops_run_summary,
    filter_aiops_run_summaries,
    is_known_incident_id,
    list_run_trace_events,
)
from app.services.report_generator import ReportGenerator, report_generator
from app.services.trace_service import TraceService, trace_service
from app.tools.registry import create_default_tool_registry

router = APIRouter()


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
    incident_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    service_name: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    """Return recent durable diagnosis runs for history views."""
    fetch_limit = 100 if status or service_name else limit
    snapshots = aiops_service.list_session_snapshots(
        incident_id=incident_id,
        limit=fetch_limit,
    )
    items = []
    for snapshot in snapshots:
        known_incident = is_known_incident_id(snapshot.incident_id)
        approvals = (
            get_approval_service().list_requests(incident_id=snapshot.incident_id)
            if known_incident
            else []
        )
        report = (
            get_report_generator().get_report(snapshot.incident_id) if known_incident else None
        )
        items.append(
            build_aiops_run_summary(
                snapshot,
                approvals=approvals,
                report=report,
            )
        )
    items = filter_aiops_run_summaries(
        items,
        status=status,
        service_name=service_name,
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


@router.get("/aiops/runs/{session_id}", dependencies=[Depends(require_scope(READ_SCOPE))])
async def get_aiops_run_status(session_id: str) -> dict:
    """Return the latest durable state for one diagnosis run."""
    snapshot = aiops_service.get_session_snapshot(session_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="AIOps diagnosis run not found")
    known_incident = is_known_incident_id(snapshot.incident_id)
    events = list_run_trace_events(snapshot, get_trace_service()) if known_incident else []
    approvals = (
        get_approval_service().list_requests(incident_id=snapshot.incident_id)
        if known_incident
        else []
    )
    report = get_report_generator().get_report(snapshot.incident_id) if known_incident else None
    return build_aiops_run_status(
        snapshot,
        events=events,
        approvals=approvals,
        report=report,
    )


@router.post("/aiops", dependencies=[Depends(require_scope(DIAGNOSE_SCOPE))])
async def diagnose_stream(request: AIOpsRequest):
    """
    AIOps 故障诊断接口（流式 SSE）

    **功能说明：**
    - 接收结构化 Incident，未传入时构造默认诊断事件
    - 使用 Plan-Execute-Replan 模式进行智能诊断
    - 流式返回诊断过程和结果

    **SSE 事件类型：**

    1. `status` - 状态更新
       ```json
       {
         "type": "status",
         "stage": "fetching_alerts",
         "message": "正在获取系统告警信息..."
       }
       ```

    2. `plan` - 诊断计划制定完成
       ```json
       {
         "type": "plan",
         "stage": "plan_created",
         "message": "诊断计划已制定，共 6 个步骤",
         "target_alert": {...},
         "plan": ["步骤1: ...", "步骤2: ..."]
       }
       ```

    3. `step_complete` - 步骤执行完成
       ```json
       {
         "type": "step_complete",
         "stage": "step_executed",
         "message": "步骤执行完成 (2/6)",
         "current_step": "查询系统日志",
         "result_preview": "...",
         "remaining_steps": 4
       }
       ```

    4. `report` - 最终诊断报告
       ```json
       {
         "type": "report",
         "stage": "final_report",
         "message": "最终诊断报告已生成",
         "report": "# 故障诊断报告\\n...",
         "evidence": {...}
       }
       ```

    5. `complete` - 诊断完成
       ```json
       {
         "type": "complete",
         "stage": "diagnosis_complete",
         "message": "诊断流程完成",
         "diagnosis": {...}
       }
       ```

    6. `error` - 错误信息
       ```json
       {
         "type": "error",
         "stage": "error",
         "message": "诊断过程发生错误: ..."
       }
       ```

    **使用示例：**
    ```bash
    export AUTOONCALL_API_BASE_URL="<your-autooncall-api-base-url>"
    curl -X POST "$AUTOONCALL_API_BASE_URL/api/aiops" \\
      -H "Content-Type: application/json" \\
      -d '{"session_id": "session-123"}' \\
      --no-buffer
    ```

    **前端使用示例：**
    ```javascript
    const response = await fetch('/api/aiops', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session_id: 'session-123', incident})
    });
    const reader = response.body.getReader();
    // 逐行解析 SSE data: {...}，直到 type=complete 或 type=error。
    ```

    Args:
        request: AIOps 诊断请求

    Returns:
        SSE 事件流
    """
    session_id = request.session_id or "default"

    logger.info(f"[会话 {session_id}] 收到 AIOps 诊断请求（流式）")

    async def event_generator():
        try:
            async for event in aiops_service.diagnose(
                session_id=session_id,
                incident=request.incident,
            ):
                yield sse_message(event)

                if is_terminal_event(event):
                    break

            logger.info(f"[会话 {session_id}] AIOps 诊断流式响应完成")

        except Exception as e:
            logger.error(f"[会话 {session_id}] AIOps 诊断流式响应异常: {e}", exc_info=True)
            yield sse_message(
                {"type": "error", "stage": "exception", "message": f"诊断异常: {str(e)}"}
            )

    return EventSourceResponse(event_generator())


@router.get("/aiops/demo/incidents/{case_id}", dependencies=[Depends(require_scope(READ_SCOPE))])
async def get_demo_incident(case_id: str):
    """Return a ready-to-run demo incident payload for interviews and local demos."""
    canonical_id = canonical_demo_case_id(case_id)
    incident = _resolve_demo_incident(case_id)
    payload = {
        "session_id": f"demo-{canonical_id}",
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


@router.post(
    "/aiops/demo/incidents/{case_id}/run",
    dependencies=[Depends(require_scope(DIAGNOSE_SCOPE))],
)
async def run_demo_incident(case_id: str, request: AIOpsRequest | None = None):
    """Run a fixed demo incident through the normal AIOps SSE workflow."""
    canonical_id = canonical_demo_case_id(case_id)
    incident = _resolve_demo_incident(case_id)
    request_session_id = request.session_id if request and request.session_id != "default" else None
    session_id = request_session_id or f"demo-{canonical_id}"
    if request and request.incident:
        incident = request.incident
    return await diagnose_stream(AIOpsRequest(session_id=session_id, incident=incident))


@router.post(
    "/incidents/{incident_id}/diagnosis/resume",
    dependencies=[Depends(require_scope(DIAGNOSE_SCOPE))],
)
async def resume_diagnosis_stream(incident_id: str, request: AIOpsResumeRequest):
    """Record an approved human decision and close the paused diagnosis loop."""
    approval = _resolve_resume_approval(incident_id, request.approval_id)
    session_id = request.session_id or str(approval.metadata.get("session_id") or "")
    if not session_id:
        session_id = f"resume-{incident_id}"

    logger.info(
        f"[会话 {session_id}] 收到 AIOps resume 请求: "
        f"incident={incident_id}, approval={approval.approval_id}"
    )

    async def event_generator():
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
                    "message": str(exc),
                    "incident_id": incident_id,
                }
            )
        except ValueError as exc:
            yield sse_message(
                {
                    "type": "error",
                    "stage": "resume_rejected",
                    "status": "failed",
                    "message": str(exc),
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
                    "message": f"恢复诊断异常: {exc}",
                    "incident_id": incident_id,
                }
            )

    return EventSourceResponse(event_generator())


@router.post(
    "/incidents/{incident_id}/changes/{change_plan_id}/resume",
    dependencies=[Depends(require_scope(CHANGE_SCOPE))],
)
async def resume_safe_change_stream(
    incident_id: str,
    change_plan_id: str,
    request: ChangeResumeRequest,
):
    """Start the safe change workflow after an approval decision."""

    async def event_generator():
        try:
            async for event in get_change_execution_service().start_after_approval(
                incident_id=incident_id,
                change_plan_id=change_plan_id,
                approval_id=request.approval_id,
                mode=request.mode,
                operator=request.operator,
                observe_window_seconds=request.observe_window_seconds,
            ):
                yield sse_message(event)
                if is_terminal_event(event):
                    break
        except ApprovalNotFoundError as exc:
            yield sse_message(
                {
                    "type": "error",
                    "stage": "change_approval_not_found",
                    "status": "failed",
                    "message": str(exc),
                    "incident_id": incident_id,
                    "change_plan_id": change_plan_id,
                }
            )
        except ChangeExecutionStateError as exc:
            yield sse_message(
                {
                    "type": "error",
                    "stage": "change_resume_rejected",
                    "status": "failed",
                    "message": str(exc),
                    "incident_id": incident_id,
                    "change_plan_id": change_plan_id,
                }
            )
        except Exception as exc:
            logger.error(f"安全变更恢复异常: {exc}", exc_info=True)
            yield sse_message(
                {
                    "type": "error",
                    "stage": "change_resume_exception",
                    "status": "failed",
                    "message": f"安全变更流程异常: {exc}",
                    "incident_id": incident_id,
                    "change_plan_id": change_plan_id,
                }
            )

    return EventSourceResponse(event_generator())


@router.get(
    "/incidents/{incident_id}/changes",
    dependencies=[Depends(require_scope(READ_SCOPE))],
)
async def list_incident_changes(incident_id: str) -> dict:
    """List safe change executions for one incident."""
    executions = get_change_execution_service().list_executions(incident_id=incident_id)
    return {
        "incident_id": incident_id,
        "items": [build_change_execution_read_model(execution) for execution in executions],
    }


@router.get("/changes/{change_execution_id}", dependencies=[Depends(require_scope(READ_SCOPE))])
async def get_change_execution(change_execution_id: str) -> dict:
    """Return one safe change execution."""
    try:
        execution = get_change_execution_service().get_execution(change_execution_id)
    except ChangeExecutionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"change_execution": build_change_execution_read_model(execution)}


@router.post(
    "/changes/{change_execution_id}/manual-result",
    dependencies=[Depends(require_scope(CHANGE_SCOPE))],
)
async def submit_manual_change_result(
    change_execution_id: str,
    request: ManualExecutionResultRequest,
) -> dict:
    """Record a manual execution result for a waiting safe change workflow."""
    try:
        execution = get_change_execution_service().record_manual_result(
            change_execution_id=change_execution_id,
            request=request,
        )
    except ChangeExecutionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ChangeExecutionStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"change_execution": build_change_execution_read_model(execution)}


def _resolve_resume_approval(
    incident_id: str,
    approval_id: str | None,
) -> ApprovalRequest:
    """Return the approval decision that authorizes diagnosis resume."""
    service = get_approval_service()
    try:
        if approval_id:
            approval = service.get_request(approval_id)
            if approval.incident_id != incident_id:
                raise HTTPException(
                    status_code=400,
                    detail="approval_id does not belong to the requested incident",
                )
        else:
            approved = service.list_requests(incident_id=incident_id, status="approved")
            if not approved:
                pending = service.list_requests(incident_id=incident_id, status="pending")
                if pending:
                    raise HTTPException(
                        status_code=409,
                        detail="approval is still pending",
                    )
                raise HTTPException(
                    status_code=404,
                    detail=f"No approved approval for incident {incident_id}",
                )
            approval = approved[-1]
    except ApprovalNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if approval.status != "approved":
        raise HTTPException(
            status_code=409,
            detail=f"approval is {approval.status}, expected approved",
        )
    return approval


def _resolve_demo_incident(case_id: str):
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
