"""Shared fallback policy helpers for AIOps tools."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from app.config import config
from app.integrations.base import (
    adapter_failure,
    adapter_not_configured,
    classify_adapter_error,
)
from app.utils.public_errors import public_adapter_error_message


async def run_adapter_or_mock(
    *,
    configured: bool,
    adapter_call: Callable[[], Awaitable[dict[str, Any]]],
    mock_call: Callable[[], dict[str, Any]],
    source: str,
    required_config: str,
    failure_summary_prefix: str,
    not_configured_summary_prefix: str,
    payload: dict[str, Any] | None = None,
    unavailable_defaults: dict[str, Any] | None = None,
    allow_failure_fallback: bool = False,
) -> dict[str, Any]:
    """Run a configured adapter or apply the repository's mock fallback policy."""
    base_payload = dict(payload or {})
    defaults = dict(unavailable_defaults or {})

    if configured:
        try:
            return await adapter_call()
        except Exception as exc:
            if config.aiops_mock_fallback_enabled and allow_failure_fallback:
                return _mock_with_adapter_failure(
                    mock_call(),
                    source=source,
                    exc=exc,
                    summary_prefix=failure_summary_prefix,
                )
            failure = adapter_failure(
                source,
                exc,
                summary_prefix=failure_summary_prefix,
                **base_payload,
            )
            failure.update(defaults)
            return failure

    if not config.aiops_mock_fallback_enabled:
        unavailable = adapter_not_configured(
            source,
            required_config=required_config,
            summary_prefix=not_configured_summary_prefix,
            **base_payload,
        )
        unavailable.update(defaults)
        return unavailable

    return mock_call()


def _mock_with_adapter_failure(
    payload: dict[str, Any],
    *,
    source: str,
    exc: Exception,
    summary_prefix: str,
) -> dict[str, Any]:
    """Attach adapter failure provenance when falling back to deterministic mock data."""
    result = dict(payload)
    partial_errors = list(result.get("partial_errors") or [])
    public_message = public_adapter_error_message(exc)
    partial_errors.append(
        {
            "source": source,
            "status": "failed",
            "error_type": classify_adapter_error(exc),
            "message": public_message,
            "summary": f"{summary_prefix}: {public_message}",
        }
    )
    result["partial_errors"] = partial_errors
    result["fallback_reason"] = "adapter_failure"
    result["source_quality"] = "mixed_with_fallback"
    if "source_detail" not in result:
        result["source_detail"] = {source: "mock_fallback_after_adapter_failure"}
    return result
