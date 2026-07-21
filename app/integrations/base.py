"""Shared helpers for external system adapters."""

from __future__ import annotations

import asyncio
import re
from typing import Any, Literal, overload

import httpx

_KUBERNETES_LABEL_VALUE_RE = re.compile(r"^(?:[A-Za-z0-9](?:[-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?)$")
_KUBERNETES_PATH_SEGMENT_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")


class ExternalAdapterError(RuntimeError):
    """Raised when a configured external adapter cannot return usable data."""


class ExternalAdapterResponseError(ExternalAdapterError):
    """Raised when an external system returns a syntactically valid business failure."""

    def __init__(self, message: str = "External dependency returned a failed response"):
        super().__init__(message)


class ExternalAdapterNotFoundError(ExternalAdapterError):
    """Raised when a requested external dependency instance is not configured."""


def require_config(value: str, name: str) -> str:
    """Return a required config value or raise a clear adapter error."""
    if not value:
        raise ExternalAdapterError(f"{name} is not configured")
    return value.rstrip("/")


def bearer_headers(token: str) -> dict[str, str]:
    """Build bearer auth headers when a token is configured."""
    return {"Authorization": f"Bearer {token}"} if token else {}


def first_float(value: Any, default: float = 0.0) -> float:
    """Best-effort conversion for nested API values."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def escape_prometheus_label_value(value: Any) -> str:
    """Escape a value before interpolating it into a quoted PromQL label matcher."""
    return (
        str(value or "")
        .replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace('"', '\\"')
    )


def require_kubernetes_label_value(value: Any, *, field_name: str = "label value") -> str:
    """Return a Kubernetes label value or raise before issuing a broad selector query."""
    text = str(value or "").strip()
    if not text or len(text) > 63 or not _KUBERNETES_LABEL_VALUE_RE.fullmatch(text):
        raise ExternalAdapterError(f"{field_name} must be a valid Kubernetes label value")
    return text


def require_kubernetes_path_segment(value: Any, *, field_name: str) -> str:
    """Validate a DNS-label path segment before interpolating it into a Kubernetes URL."""
    text = str(value or "").strip()
    if not text or not _KUBERNETES_PATH_SEGMENT_RE.fullmatch(text):
        raise ExternalAdapterError(f"{field_name} must be a valid Kubernetes DNS label")
    return text


def parse_duration_seconds(value: str, *, default_seconds: int = 600) -> int:
    """Parse compact duration strings such as 10m, 1h, or 30s."""
    text = (value or "").strip().lower()
    match = re.fullmatch(r"(\d+)([smhd]?)", text)
    if not match:
        return default_seconds
    amount = int(match.group(1))
    unit = match.group(2) or "s"
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return amount * multipliers[unit]


def adapter_success(
    *,
    source: str,
    summary: str,
    signals: dict[str, Any] | None = None,
    raw: Any | None = None,
    **payload: Any,
) -> dict[str, Any]:
    """Return the common success envelope used by production adapters."""
    return {
        **payload,
        "status": "success",
        "source": source,
        "source_quality": payload.get("source_quality", "live"),
        "evidence_origin": payload.get("evidence_origin", source),
        "signals": signals or {},
        "raw": raw if raw is not None else {},
        "summary": summary,
    }


@overload
def require_success_payload(
    payload: Any,
    *,
    system_name: str,
    allow_list: Literal[False] = False,
) -> dict[str, Any]: ...


@overload
def require_success_payload(
    payload: Any,
    *,
    system_name: str,
    allow_list: Literal[True],
) -> dict[str, Any] | list[Any]: ...


def require_success_payload(
    payload: Any,
    *,
    system_name: str,
    allow_list: bool = False,
) -> dict[str, Any] | list[Any]:
    """Validate a JSON response and reject 2xx business-failure envelopes."""
    if not isinstance(payload, (dict, list)):
        raise ExternalAdapterResponseError(
            f"{system_name} response must be a JSON object" + (" or array" if allow_list else "")
        )
    if isinstance(payload, list):
        if allow_list:
            return payload
        raise ExternalAdapterResponseError(f"{system_name} response must be a JSON object")

    status = str(payload.get("status") or "").strip().lower()
    if status in {"failed", "failure", "error"}:
        raise ExternalAdapterResponseError(f"{system_name} returned a failed status")
    if payload.get("success") is False or payload.get("ok") is False:
        raise ExternalAdapterResponseError(f"{system_name} returned success=false")
    if payload.get("error") not in (None, "", False) or payload.get("error_message") not in (
        None,
        "",
        False,
    ):
        raise ExternalAdapterResponseError(f"{system_name} returned an error payload")
    return payload


def adapter_failure(
    source: str,
    exc: Exception,
    *,
    summary_prefix: str = "外部系统查询失败",
    **payload: Any,
) -> dict[str, Any]:
    """Return the common failure envelope used by AIOps tools and reports."""
    error_type = classify_adapter_error(exc)
    message = public_adapter_failure_message(error_type)
    return {
        **payload,
        "status": "failed",
        "source": source,
        "error_type": error_type,
        "message": message,
        "error_message": message,
        "retryable": error_type
        in {"timeout", "connection_error", "server_error", "not_configured"},
        "signals": {},
        "raw": {},
        "summary": f"{summary_prefix}: {message}",
    }


def adapter_not_configured(
    source: str,
    *,
    required_config: str,
    summary_prefix: str = "外部系统未配置",
    **payload: Any,
) -> dict[str, Any]:
    """Return a stable failure payload when mock fallback is disabled."""
    return adapter_failure(
        source,
        ExternalAdapterError(f"{required_config} is not configured and mock fallback is disabled"),
        summary_prefix=summary_prefix,
        **payload,
    )


def classify_adapter_error(exc: Exception) -> str:
    """Classify common adapter failures into stable eval/report categories."""
    if isinstance(exc, ExternalAdapterNotFoundError):
        return "not_found"
    if isinstance(exc, PermissionError):
        return "permission_denied"
    if isinstance(exc, TimeoutError | asyncio.TimeoutError | httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.ConnectError | ConnectionError | OSError):
        return "connection_error"
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        if status_code in {401, 403}:
            return "permission_denied"
        if status_code == 404:
            return "not_found"
        if status_code >= 500:
            return "server_error"
        return "http_error"
    if isinstance(exc, ExternalAdapterResponseError):
        return "server_error"
    text = str(exc).lower()
    if "not configured" in text or "未配置" in text:
        return "not_configured"
    if "permission" in text or "forbidden" in text or "unauthorized" in text or "rbac" in text:
        return "permission_denied"
    if "timeout" in text or "timed out" in text or "超时" in text:
        return "timeout"
    return "adapter_error"


def public_adapter_failure_message(error_type: str) -> str:
    """Return a stable adapter error message safe for API, evidence, and reports."""
    messages = {
        "timeout": "外部依赖查询超时",
        "connection_error": "外部依赖连接失败",
        "permission_denied": "外部依赖权限校验失败",
        "not_found": "外部依赖端点不存在",
        "server_error": "外部依赖返回服务端错误",
        "http_error": "外部依赖返回非预期状态",
        "not_configured": "外部依赖未配置",
    }
    return messages.get(error_type, "外部依赖暂时不可用")
