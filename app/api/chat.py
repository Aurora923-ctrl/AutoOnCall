"""对话接口

提供基于 RAG Agent 的普通对话和流式对话接口
"""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi.responses import JSONResponse
from loguru import logger
from sse_starlette.sse import EventSourceResponse

from app.api.sse import sse_message
from app.core.auth import CHAT_WRITE_SCOPE, READ_SCOPE, require_scope
from app.models.request import SESSION_ID_MAX_LENGTH, ChatRequest, ClearRequest
from app.models.response import ApiResponse, ChatApiResponse, SessionInfoResponse
from app.services.rag_agent_service import rag_agent_service
from app.utils.log_safety import sanitize_log_value, summarize_text_for_log

router = APIRouter()
PUBLIC_CHAT_ERROR_MESSAGE = "对话服务暂时不可用，请稍后重试"
PUBLIC_CHAT_STREAM_ERROR_MESSAGE = "流式对话服务暂时不可用，请稍后重试"
PUBLIC_SESSION_ERROR_MESSAGE = "会话服务暂时不可用，请稍后重试"


@router.post(
    "/chat",
    response_model=ChatApiResponse,
    dependencies=[Depends(require_scope(CHAT_WRITE_SCOPE))],
)
async def chat(request: ChatRequest):
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
    try:
        session_id = sanitize_log_value(request.id)
        logger.info(
            f"[会话 {session_id}] 收到快速对话请求: "
            f"{summarize_text_for_log(request.question, label='question')}"
        )

        chat_payload = await rag_agent_service.query_with_retrieval(
            request.question,
            session_id=request.id,
            metadata_filter=request.metadata_filter,
        )

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


@router.post("/chat_stream", dependencies=[Depends(require_scope(CHAT_WRITE_SCOPE))])
async def chat_stream(request: ChatRequest):
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
                session_id=request.id,
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
    dependencies=[Depends(require_scope(CHAT_WRITE_SCOPE))],
)
async def clear_session(request: ClearRequest):
    """清空会话历史

    Args:
        request: 清空请求

    Returns:
        操作结果
    """
    try:
        success = await rag_agent_service.clear_session(request.session_id)
        logger.info(f"清空会话: {sanitize_log_value(request.session_id)}, 结果: {success}")

        return ApiResponse(
            status="success" if success else "error",
            message="会话已清空" if success else "清空会话失败",
            data=None,
        )

    except Exception as exc:
        logger.error("清空会话错误: error_type={}", type(exc).__name__)
        raise HTTPException(status_code=500, detail=PUBLIC_SESSION_ERROR_MESSAGE) from exc


@router.get(
    "/chat/session/{session_id}",
    response_model=SessionInfoResponse,
    dependencies=[Depends(require_scope(READ_SCOPE))],
)
async def get_session_info(
    session_id: str = Path(..., min_length=1, max_length=SESSION_ID_MAX_LENGTH),
) -> SessionInfoResponse:
    """查询会话历史

    Args:
        session_id: 会话 ID

    Returns:
        会话信息
    """
    try:
        history = await rag_agent_service.get_session_history(session_id)

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
