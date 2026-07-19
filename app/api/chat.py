"""对话接口

提供基于 RAG Agent 的普通对话和流式对话接口
"""

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi.responses import JSONResponse
from loguru import logger
from sse_starlette.sse import EventSourceResponse

from app.api.sse import sse_message
from app.core.auth import (
    CHAT_WRITE_SCOPE,
    READ_SCOPE,
    AuthPrincipal,
    require_scope,
    scoped_session_id,
)
from app.models.request import SESSION_ID_MAX_LENGTH, ChatRequest, ClearRequest
from app.models.response import ApiResponse, ChatApiResponse, SessionInfoResponse
from app.services.rag_agent_service import rag_agent_service
from app.services.trace_service import trace_service
from app.utils.log_safety import sanitize_log_value, summarize_text_for_log

router = APIRouter()
PUBLIC_CHAT_ERROR_MESSAGE = "对话服务暂时不可用，请稍后重试"
PUBLIC_CHAT_STREAM_ERROR_MESSAGE = "流式对话服务暂时不可用，请稍后重试"
PUBLIC_SESSION_ERROR_MESSAGE = "会话服务暂时不可用，请稍后重试"


@router.post(
    "/chat",
    response_model=ChatApiResponse,
)
async def chat(
    request: ChatRequest,
    principal: AuthPrincipal = Depends(require_scope(CHAT_WRITE_SCOPE)),
):
    """快速对话接口
    {
        "code": 200,
        "message": "success",
        "data": {
            "success": true,
            "answer": "回答内容",
            "errorMessage": null
        }
    }

    Args:
        request: 对话请求

    Returns:
        统一格式的对话响应
    """
    started = time.perf_counter()
    request_metadata = {
        "request_id": request.id,
        "request_kind": "rag",
        "evidence_level": request.evidence_level or "unclassified",
        "is_request_summary": True,
        "path": "/api/chat",
    }
    if request.acceptance_run_id:
        request_metadata["acceptance_run_id"] = request.acceptance_run_id
    try:
        session_id = sanitize_log_value(request.id)
        logger.info(
            f"[会话 {session_id}] 收到快速对话请求: "
            f"{summarize_text_for_log(request.question, label='question')}"
        )

        chat_payload = await rag_agent_service.query_with_retrieval(
            request.question,
            session_id=scoped_session_id(principal, request.id),
            metadata_filter=request.metadata_filter,
        )
        observability = chat_payload.get("observability", {})
        observability = observability if isinstance(observability, dict) else {}
        runtime = observability.get("runtime", {})
        runtime = runtime if isinstance(runtime, dict) else {}
        token_usage = observability.get("token_usage")
        trace_metadata: dict[str, Any] = {
            **request_metadata,
            "model": str(runtime.get("llm_model") or ""),
        }
        if isinstance(token_usage, dict):
            trace_metadata["token_usage"] = token_usage
        try:
            trace_service.create_event(
                trace_id=f"rag-{request.id}",
                incident_id=f"RAG-{request.id}",
                node_name="rag",
                event_type="request_complete",
                output_summary="RAG request completed",
                latency_ms=(time.perf_counter() - started) * 1000,
                status="success",
                metadata=trace_metadata,
            )
        except Exception as trace_exc:
            logger.warning("Record RAG request trace failed: {}", type(trace_exc).__name__)

        logger.info(f"[会话 {session_id}] 快速对话完成")

        return {
            "code": 200,
            "message": "success",
            "data": {
                "success": True,
                "answer": chat_payload.get("answer", ""),
                "citations": chat_payload.get("citations", []),
                "retrieval": chat_payload.get("retrieval", {}),
                "observability": chat_payload.get("observability", {}),
                "noAnswer": chat_payload.get("no_answer", False),
                "answerPolicy": chat_payload.get("answer_policy", ""),
                "errorMessage": None,
            },
        }

    except Exception as exc:
        logger.error("对话接口错误: error_type={}", type(exc).__name__)
        try:
            trace_service.create_event(
                trace_id=f"rag-{request.id}",
                incident_id=f"RAG-{request.id}",
                node_name="rag",
                event_type="request_complete",
                output_summary=PUBLIC_CHAT_ERROR_MESSAGE,
                latency_ms=(time.perf_counter() - started) * 1000,
                status="failed",
                error_message=PUBLIC_CHAT_ERROR_MESSAGE,
                metadata=request_metadata,
            )
        except Exception as trace_exc:
            logger.warning("Record failed RAG request trace failed: {}", type(trace_exc).__name__)
        return JSONResponse(
            status_code=500,
            content={
                "code": 500,
                "message": "error",
                "data": {
                    "success": False,
                    "answer": None,
                    "errorMessage": PUBLIC_CHAT_ERROR_MESSAGE,
                },
            },
        )


@router.post("/chat_stream")
async def chat_stream(
    request: ChatRequest,
    principal: AuthPrincipal = Depends(require_scope(CHAT_WRITE_SCOPE)),
):
    """流式对话接口（基于 RAG Agent，SSE）

    返回 SSE 格式，data 字段为 JSON：

    工具调用事件:
    event: message
    data: {"type":"tool_call","data":{"tool":"工具名","status":"start|end","input":{...}}}

    内容流式事件:
    event: message
    data: {"type":"content","data":"内容块"}

    完成事件:
    event: message
    data: {"type":"done","data":{"answer":"完整答案","tool_calls":[...]}}

    Args:
        request: 对话请求

    Returns:
        SSE 事件流
    """
    session_id = sanitize_log_value(request.id)
    logger.info(
        f"[会话 {session_id}] 收到流式对话请求: "
        f"{summarize_text_for_log(request.question, label='question')}"
    )

    async def event_generator():
        terminal_sent = False
        try:
            async for chunk in rag_agent_service.query_stream_with_retrieval(
                request.question,
                session_id=scoped_session_id(principal, request.id),
                metadata_filter=request.metadata_filter,
            ):
                chunk_type = chunk.get("type", "unknown")
                chunk_data = chunk.get("data", None)

                # 处理调试类型消息（新增）
                if chunk_type == "debug":
                    yield sse_message(
                        {
                            "type": "debug",
                            "node": chunk.get("node", "unknown"),
                            "message_type": chunk.get("message_type", "unknown"),
                        }
                    )
                elif chunk_type == "tool_call":
                    yield sse_message({"type": "tool_call", "data": chunk_data})
                elif chunk_type == "search_results":
                    yield sse_message({"type": "search_results", "data": chunk_data})
                elif chunk_type == "content":
                    yield sse_message({"type": "content", "data": chunk_data})
                elif chunk_type == "replace_content":
                    yield sse_message({"type": "replace_content", "data": chunk_data})
                elif chunk_type == "complete":
                    yield sse_message({"type": "done", "data": chunk_data})
                    terminal_sent = True
                    return
                elif chunk_type == "error":
                    yield sse_message({"type": "error", "data": PUBLIC_CHAT_STREAM_ERROR_MESSAGE})
                    terminal_sent = True
                    return

            if not terminal_sent:
                yield sse_message({"type": "error", "data": PUBLIC_CHAT_STREAM_ERROR_MESSAGE})
                terminal_sent = True

            logger.info(f"[会话 {session_id}] 流式对话完成")
        except asyncio.CancelledError:
            logger.info(f"[会话 {session_id}] 流式对话客户端已断开")
            raise
        except Exception as exc:
            logger.error("流式对话接口错误: error_type={}", type(exc).__name__)
            if not terminal_sent:
                yield sse_message({"type": "error", "data": PUBLIC_CHAT_STREAM_ERROR_MESSAGE})

    return EventSourceResponse(event_generator())


@router.post(
    "/chat/clear",
    response_model=ApiResponse,
)
async def clear_session(
    request: ClearRequest,
    principal: AuthPrincipal = Depends(require_scope(CHAT_WRITE_SCOPE)),
):
    """清空会话历史

    Args:
        request: 清空请求

    Returns:
        操作结果
    """
    try:
        success = await rag_agent_service.clear_session(
            scoped_session_id(principal, request.session_id)
        )
        logger.info(f"清空会话: {sanitize_log_value(request.session_id)}, 结果: {success}")

        if not success:
            raise HTTPException(status_code=500, detail=PUBLIC_SESSION_ERROR_MESSAGE)
        return ApiResponse(
            status="success",
            message="会话已清空",
            data=None,
        )

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("清空会话错误: error_type={}", type(exc).__name__)
        raise HTTPException(status_code=500, detail=PUBLIC_SESSION_ERROR_MESSAGE) from exc


@router.get(
    "/chat/session/{session_id}",
    response_model=SessionInfoResponse,
)
async def get_session_info(
    session_id: str = Path(..., min_length=1, max_length=SESSION_ID_MAX_LENGTH),
    principal: AuthPrincipal = Depends(require_scope(READ_SCOPE)),
) -> SessionInfoResponse:
    """查询会话历史

    Args:
        session_id: 会话 ID

    Returns:
        会话信息
    """
    try:
        history = await rag_agent_service.get_session_history(
            scoped_session_id(principal, session_id)
        )

        return SessionInfoResponse(
            session_id=session_id, message_count=len(history), history=history
        )

    except Exception as exc:
        logger.error(
            "获取会话信息错误: session_id={}, error_type={}",
            sanitize_log_value(session_id),
            type(exc).__name__,
        )
        raise HTTPException(status_code=500, detail=PUBLIC_SESSION_ERROR_MESSAGE) from exc
