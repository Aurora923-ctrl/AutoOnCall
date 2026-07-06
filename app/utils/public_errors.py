"""Frontend-safe error summaries for API, SSE, and tool fallback responses."""

from __future__ import annotations

from app.integrations.base import classify_adapter_error, public_adapter_failure_message

GENERIC_OPERATION_ERROR = "操作暂时不可用，请稍后重试或查看服务端日志"
GENERIC_DIAGNOSIS_ERROR = "诊断服务暂时不可用，请稍后重试或查看服务端日志"
GENERIC_CHANGE_ERROR = "安全变更流程暂时不可用，请稍后重试或查看服务端日志"


def public_exception_message(exc: Exception, *, fallback: str = GENERIC_OPERATION_ERROR) -> str:
    """Return a stable message without leaking internal exception details."""
    if isinstance(exc, LookupError):
        return "请求的资源不存在或已过期"
    if isinstance(exc, ValueError):
        return "请求状态不满足当前操作，请刷新后重试"
    return fallback


def public_adapter_error_message(exc: Exception) -> str:
    """Return a safe dependency-readiness message."""
    return public_adapter_failure_message(classify_adapter_error(exc))
